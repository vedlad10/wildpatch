"""Check that the hosted Otari account is set up the way Wildpatch expects.

    uv run python scripts/check_otari.py

Reads .env. Spends a fraction of a cent (one tiny completion per model role).
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from wildpatch import config  # noqa: E402
from wildpatch.net import tls  # noqa: E402

OK, BAD, WARN = "  ok  ", " FAIL ", " warn "


def line(status: str, what: str, detail: str = "") -> None:
    print(f"[{status}] {what}" + (f"  ({detail})" if detail else ""))


async def main() -> int:
    if not config.OTARI_API_KEY:
        line(BAD, "OTARI_API_KEY is not set in .env")
        return 1
    failures = 0
    headers = {"Authorization": f"Bearer {config.OTARI_API_KEY}"}
    async with httpx.AsyncClient(base_url=config.OTARI_BASE_URL, headers=headers, timeout=60, verify=tls()) as c:
        r = await c.get("/models")
        if r.status_code != 200:
            line(BAD, f"GET /models -> {r.status_code}", r.text[:200])
            return 1
        data = r.json()
        ids = {m.get("id") for m in (data.get("data") if isinstance(data, dict) else data) or []}
        line(OK, f"key works against {config.OTARI_BASE_URL}", f"{len(ids)} models listed")

        r = await c.get("/tools")
        tools = r.json() if r.status_code == 200 else {}
        rows = tools.get("tools") or tools.get("data") or tools if isinstance(tools, (dict, list)) else []
        print("        tools:", json.dumps(rows)[:400])

        for role, model in config.MODELS.items():
            if role.startswith("guard_safety"):
                messages = [{"role": "user", "content": "How do I plant a hedge?"}]
            else:
                messages = [{"role": "user", "content": "Reply with the word ok."}]
            body = {"model": model, "messages": messages, "max_tokens": 16, "user": f"{config.END_USER_PREFIX}smoke"}
            r = await c.post("/chat/completions", json=body)
            listed = "listed" if model in ids else "not in /models"
            if r.status_code == 200:
                usage = r.json().get("usage") or {}
                line(OK, f"{role:12} {model}", f"{listed}, served by {r.headers.get('otari-provider')}:"
                                               f"{r.headers.get('otari-model')}, ${usage.get('cost_usd')}")
            else:
                failures += 1
                line(BAD, f"{role:12} {model}", f"{listed}, HTTP {r.status_code} {r.text[:160]}")

        r = await c.get("/end-users", params={"external_id": f"{config.END_USER_PREFIX}smoke"})
        if r.status_code == 200:
            line(OK, "service key: per-user end users work", json.dumps(r.json())[:200])
        else:
            failures += 1
            line(BAD, "service key: GET /end-users", f"HTTP {r.status_code} {r.text[:160]} -> make the key a Service key")

    if config.PUBLIC_URL:
        async with httpx.AsyncClient(timeout=20, verify=tls()) as c:
            try:
                r = await c.get(f"{config.PUBLIC_URL}/healthz")
                line(OK if r.status_code == 200 else BAD, f"public URL {config.PUBLIC_URL}", f"HTTP {r.status_code}")
            except httpx.HTTPError as e:
                failures += 1
                line(BAD, f"public URL {config.PUBLIC_URL}", type(e).__name__)
    else:
        line(WARN, "PUBLIC_URL not set: Otari cannot reach the MCP server or the guardrail service yet")
    if not config.GUARD_TOKEN or not config.MCP_TOKEN:
        line(WARN, "set MCP_TOKEN and GUARD_TOKEN in .env")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
