"""A small async client for the hosted Otari gateway.

Every model call in Wildpatch goes through here. Calls are streamed: hosted Otari sits behind a CDN that
cuts idle non-streamed responses after ~60 s, and a stream also lets the UI show each gateway-run tool
(MCP, code execution, web search/fetch) as it happens. The Messages API reports those runs as
`server_tool_use` / `*_tool_result` and `mcp_tool_use` / `mcp_tool_result` blocks, even for open models.

Each call ends with a Receipt built from Otari's response headers and inline cost: which model and
provider served it, whether a routing fallback happened, guardrail verdicts and what it cost.
"""
from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import quote

import httpx

from . import config
from .net import tls

STREAM_TIMEOUT = httpx.Timeout(connect=15.0, read=180.0, write=60.0, pool=15.0)
MCP_BETA = "mcp-client-2025-11-20"

TOOL_KIND = {  # server_tool_use name -> what the UI calls it
    "web_search": "search", "web_fetch": "fetch", "code_execution": "code",
    "bash_code_execution": "code", "text_editor_code_execution": "code",
}


@dataclass
class Receipt:
    stage: str
    role: str
    requested_model: str
    served_model: str | None = None
    provider: str | None = None
    attempts: int = 1
    fallback: bool = False
    duration_ms: int | None = None
    request_id: str | None = None
    cost_usd: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    guardrails: list = field(default_factory=list)
    end_user: str | None = None
    end_user_budget: str | None = None
    tools: list[str] = field(default_factory=list)
    wall_ms: int = 0

    def read_headers(self, h: httpx.Headers) -> None:
        self.request_id = h.get("otari-request-id")
        self.provider = h.get("otari-provider")
        self.served_model = h.get("otari-model")
        attempts = h.get("otari-attempt-count", "1")
        self.attempts = int(attempts) if attempts.isdigit() else 1
        self.fallback = h.get("otari-fallback", "false").lower() == "true"
        duration = h.get("otari-response-duration-ms", "")
        self.duration_ms = int(duration) if duration.isdigit() else None
        self.end_user_budget = h.get("otari-end-user-budget")
        if h.get("otari-guardrails"):
            try:
                self.guardrails = json.loads(h["otari-guardrails"])
            except ValueError:
                self.guardrails = [{"raw": h["otari-guardrails"]}]

    def read_usage(self, usage: dict | None) -> None:
        if not usage:
            return
        self.input_tokens = usage.get("input_tokens") or usage.get("prompt_tokens") or self.input_tokens
        self.output_tokens = usage.get("output_tokens") or usage.get("completion_tokens") or self.output_tokens
        if usage.get("cost_usd") is not None:
            self.cost_usd = float(usage["cost_usd"])

    def to_dict(self) -> dict:
        return asdict(self)


class OtariError(Exception):
    """A refusal or failure from Otari, with the fields the UI needs to explain it."""

    def __init__(self, status: int, detail: str, code: str | None = None, *, retry_after: int | None = None,
                 budget_scope: str | None = None, request_id: str | None = None):
        super().__init__(f"{status} {code or ''} {detail}".strip())
        self.status, self.detail, self.code = status, detail, code
        self.retry_after, self.budget_scope, self.request_id = retry_after, budget_scope, request_id

    @classmethod
    def from_response(cls, r: httpx.Response, body: bytes) -> OtariError:
        detail: Any = body.decode("utf-8", "replace")[:600]
        code = r.headers.get("otari-error-code")
        try:
            data = json.loads(body)
        except ValueError:
            data = None
        if isinstance(data, dict):
            err = data.get("error") if isinstance(data.get("error"), dict) else None
            detail = data.get("detail") or (err or {}).get("message") or detail
            code = code or data.get("code") or (err or {}).get("code") or (err or {}).get("type")
            if isinstance(detail, dict):
                code = code or detail.get("code")
                # A guardrail refusal names each flagged profile with the explanation our service gave.
                why = "; ".join(f"{g.get('profile')}: {g.get('explanation')}" for g in detail.get("guardrails") or []
                                if isinstance(g, dict) and g.get("explanation"))
                detail = detail.get("message") or detail.get("detail") or json.dumps(detail)
                if why:
                    detail = f"{detail} {why}"
            elif isinstance(detail, list):
                detail = "; ".join(str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in detail)
        elif "<html" in str(detail).lower():
            # A CDN/WAF page rather than Otari's own JSON (see mozilla-ai/otari#2704, #2692).
            detail = f"edge returned an HTML {r.status_code} page (server: {r.headers.get('server', '?')})"
        retry = r.headers.get("retry-after", "")
        return cls(r.status_code, str(detail), code, retry_after=int(retry) if retry.isdigit() else None,
                   budget_scope=r.headers.get("otari-budget-scope"), request_id=r.headers.get("otari-request-id"))

    def to_dict(self) -> dict:
        return {"status": self.status, "code": self.code, "detail": self.detail, "retry_after": self.retry_after,
                "budget_scope": self.budget_scope, "request_id": self.request_id}


async def _sse(response: httpx.Response) -> AsyncIterator[dict]:
    """Yield the JSON payload of each server-sent event."""
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif line == "" and data_lines:
            payload = "\n".join(data_lines)
            data_lines = []
            if payload.strip() == "[DONE]":
                return
            try:
                yield json.loads(payload)
            except ValueError:
                continue
    if data_lines and "\n".join(data_lines).strip() not in ("", "[DONE]"):
        try:
            yield json.loads("\n".join(data_lines))
        except ValueError:
            pass


def _result_output(block: dict) -> tuple[Any, bool]:
    """Normalize a *_tool_result block's content for the UI; returns (output, is_error)."""
    btype = block.get("type", "")
    content = block.get("content")
    if btype == "mcp_tool_result":
        texts = [c.get("text", "") for c in content or [] if isinstance(c, dict) and c.get("type") == "text"]
        text = "\n".join(texts) if texts else (content if isinstance(content, str) else json.dumps(content))
        try:
            return json.loads(text), bool(block.get("is_error"))
        except (TypeError, ValueError):
            return text, bool(block.get("is_error"))
    if btype.endswith("code_execution_tool_result"):
        c = content if isinstance(content, dict) else {}
        files = [{"file_id": f.get("file_id"), "filename": f.get("filename")} for f in c.get("content") or []
                 if isinstance(f, dict)]
        failed = c.get("type", "").endswith("error") or (c.get("return_code") or 0) != 0
        return {"stdout": c.get("stdout", ""), "stderr": c.get("stderr", ""), "return_code": c.get("return_code"),
                "files": files, "error_code": c.get("error_code")}, failed
    if btype == "web_search_tool_result":
        if isinstance(content, list):
            return [{"title": i.get("title"), "url": i.get("url"), "page_age": i.get("page_age")}
                    for i in content if isinstance(i, dict)], False
        return {"error": (content or {}).get("error_code", "search failed")}, True
    if btype == "web_fetch_tool_result":
        c = content if isinstance(content, dict) else {}
        if c.get("type", "").endswith("error"):
            return {"error": c.get("error_code", "fetch failed")}, True
        doc = c.get("content") or {}
        source = (doc.get("source") or {}) if isinstance(doc, dict) else {}
        text = source.get("data") if isinstance(source.get("data"), str) else ""
        return {"url": c.get("url"), "title": doc.get("title") if isinstance(doc, dict) else None,
                "excerpt": text[:400]}, False
    return content, False


class Otari:
    def __init__(self, *, end_user: str | None = None, end_user_budget: str | None = None,
                 conversation_id: str | None = None, tags: dict[str, str] | None = None,
                 api_key: str | None = None, base_url: str | None = None):
        self.api_key = api_key or config.OTARI_API_KEY
        self.base_url = (base_url or config.OTARI_BASE_URL).rstrip("/")
        self.end_user = end_user
        self.end_user_budget = end_user_budget
        self.conversation_id = conversation_id
        self.tags = tags or {}
        self._client = httpx.AsyncClient(timeout=STREAM_TIMEOUT, verify=tls())

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self, stage: str, extra: dict | None = None) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        tags = {**self.tags, "stage": stage}
        h["Otari-Tags"] = ",".join(f"{quote(k, safe='')}={quote(str(v), safe='')}" for k, v in tags.items())
        if self.conversation_id:
            h["Otari-Conversation-Id"] = self.conversation_id
        if self.end_user and self.end_user_budget:
            h["Otari-End-User-Budget"] = self.end_user_budget  # only read when the end user is created
        h.update(extra or {})
        return h

    # ------------------------------------------------------------------ Messages (tool runs)

    async def messages_stream(self, *, stage: str, role: str, model: str, messages: list[dict],
                              system: str | None = None, max_tokens: int = 1500,
                              temperature: float | None = None, tools: list[dict] | None = None,
                              mcp_server_ids: list[str] | None = None, mcp_servers: list[dict] | None = None,
                              guardrails: list[dict] | None = None, max_tool_iterations: int | None = None,
                              headers: dict | None = None) -> AsyncIterator[dict]:
        """Stream one Messages request. Yields text/tool_call/tool_result events, then one receipt event."""
        body: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens, "stream": True}
        if system:
            body["system"] = system
        if temperature is not None:
            body["temperature"] = temperature
        if tools:
            body["tools"] = tools
        if mcp_server_ids:
            body["mcp_server_ids"] = mcp_server_ids
        if mcp_servers:
            body["mcp_servers"] = mcp_servers
        if guardrails:
            body["guardrails"] = guardrails
        if max_tool_iterations:
            body["max_tool_iterations"] = max_tool_iterations
        if self.end_user:
            body["metadata"] = {"user_id": self.end_user}
        extra = dict(headers or {})
        if mcp_server_ids or mcp_servers:
            extra["anthropic-beta"] = MCP_BETA  # ask Otari to stream mcp_tool_use / mcp_tool_result blocks
        extra.setdefault("anthropic-version", "2023-06-01")

        receipt = Receipt(stage=stage, role=role, requested_model=model, end_user=self.end_user)
        started = time.monotonic()
        blocks: dict[int, dict] = {}
        async with self._client.stream("POST", f"{self.base_url}/messages", json=body,
                                       headers=self._headers(stage, extra)) as r:
            if r.status_code >= 400:
                raise OtariError.from_response(r, await r.aread())
            receipt.read_headers(r.headers)
            async for ev in _sse(r):
                etype = ev.get("type")
                if etype == "message_start":
                    msg = ev.get("message") or {}
                    receipt.read_usage(msg.get("usage"))
                    receipt.served_model = receipt.served_model or msg.get("model")
                elif etype == "content_block_start":
                    block = dict(ev.get("content_block") or {})
                    block["_json"] = ""
                    blocks[ev.get("index", len(blocks))] = block
                    btype = block.get("type", "")
                    if btype == "text" and block.get("text"):
                        yield {"type": "text", "text": block["text"]}
                    elif btype.endswith("tool_result"):
                        output, is_error = _result_output(block)
                        kind = "mcp" if btype == "mcp_tool_result" else (
                            "code" if "code_execution" in btype else "search" if "search" in btype else "fetch")
                        receipt.tools.append(kind)
                        yield {"type": "tool_result", "id": block.get("tool_use_id"), "kind": kind,
                               "output": output, "is_error": is_error}
                elif etype == "content_block_delta":
                    delta = ev.get("delta") or {}
                    block = blocks.get(ev.get("index"), {})
                    if delta.get("type") == "text_delta":
                        yield {"type": "text", "text": delta.get("text", "")}
                    elif delta.get("type") == "input_json_delta":
                        block["_json"] = block.get("_json", "") + delta.get("partial_json", "")
                elif etype == "content_block_stop":
                    block = blocks.get(ev.get("index"), {})
                    btype = block.get("type", "")
                    if btype in ("server_tool_use", "mcp_tool_use", "tool_use"):
                        args = block.get("input") or {}
                        if block.get("_json"):
                            try:
                                args = json.loads(block["_json"])
                            except ValueError:
                                args = {"_raw": block["_json"]}
                        kind = "mcp" if btype == "mcp_tool_use" else TOOL_KIND.get(block.get("name", ""), "tool")
                        yield {"type": "tool_call", "id": block.get("id"), "kind": kind,
                               "name": block.get("name"), "server": block.get("server_name"), "input": args}
                elif etype == "message_delta":
                    receipt.read_usage(ev.get("usage"))
                elif etype == "error":
                    err = ev.get("error") or {}
                    raise OtariError(502, err.get("message", "stream error"), err.get("type"),
                                     request_id=receipt.request_id)
        receipt.wall_ms = int((time.monotonic() - started) * 1000)
        yield {"type": "receipt", "receipt": receipt}

    # ------------------------------------------------------------------ Chat Completions

    async def chat_stream(self, *, stage: str, role: str, model: str, messages: list[dict],
                          max_tokens: int = 800, temperature: float | None = None, json_mode: bool = False,
                          guardrails: list[dict] | None = None, reasoning_effort: str | None = None,
                          headers: dict | None = None) -> AsyncIterator[dict]:
        body: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens, "stream": True,
                                "stream_options": {"include_usage": True}}
        if temperature is not None:
            body["temperature"] = temperature
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if guardrails:
            body["guardrails"] = guardrails
        if reasoning_effort:
            body["reasoning_effort"] = reasoning_effort
        if self.end_user:
            body["user"] = self.end_user
        receipt = Receipt(stage=stage, role=role, requested_model=model, end_user=self.end_user)
        started = time.monotonic()
        async with self._client.stream("POST", f"{self.base_url}/chat/completions", json=body,
                                       headers=self._headers(stage, headers)) as r:
            if r.status_code >= 400:
                raise OtariError.from_response(r, await r.aread())
            receipt.read_headers(r.headers)
            async for chunk in _sse(r):
                if chunk.get("error"):
                    err = chunk["error"] if isinstance(chunk["error"], dict) else {"message": str(chunk["error"])}
                    raise OtariError(502, err.get("message", "stream error"), err.get("code"),
                                     request_id=receipt.request_id)
                for choice in chunk.get("choices") or []:
                    text = (choice.get("delta") or {}).get("content")
                    if text:
                        yield {"type": "text", "text": text}
                receipt.read_usage(chunk.get("usage"))
        receipt.wall_ms = int((time.monotonic() - started) * 1000)
        yield {"type": "receipt", "receipt": receipt}

    async def chat(self, *, stage: str, role: str, model: str, messages: list[dict], **kw) -> tuple[str, Receipt]:
        """Collect a streamed chat completion into (text, receipt)."""
        parts: list[str] = []
        receipt: Receipt | None = None
        async for ev in self.chat_stream(stage=stage, role=role, model=model, messages=messages, **kw):
            if ev["type"] == "text":
                parts.append(ev["text"])
            elif ev["type"] == "receipt":
                receipt = ev["receipt"]
        assert receipt is not None
        return "".join(parts), receipt

    # ------------------------------------------------------------------ end users (service key)

    async def end_user_status(self, external_id: str) -> dict | None:
        r = await self._client.get(f"{self.base_url}/end-users", params={"external_id": external_id},
                                   headers={"Authorization": f"Bearer {self.api_key}"}, timeout=15.0)
        if r.status_code >= 400:
            raise OtariError.from_response(r, r.content)
        data = r.json()
        rows = data if isinstance(data, list) else data.get("data") or data.get("items") or data.get("end_users") or []
        return rows[0] if rows else None
