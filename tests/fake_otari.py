"""A stand-in for the hosted Otari gateway, for tests and offline UI work only.

It speaks the wire formats Otari documents (Messages SSE with server_tool_use / *_tool_result and
mcp_tool_use / mcp_tool_result blocks, Chat Completions SSE, Otari-* headers, 403 budget and guardrail
refusals, /end-users) so the pipeline can be exercised end to end without a key. Model "answers" are
canned; MCP calls run Wildpatch's real tool functions, as hosted Otari would by calling our server.

Run it:  uv run uvicorn tests.fake_otari:app --port 8788
then:    OTARI_BASE_URL=http://127.0.0.1:8788/api/v1 OTARI_API_KEY=fake uv run uvicorn wildpatch.app:app
"""
from __future__ import annotations

import asyncio
import json
import re
from collections import defaultdict
from urllib.parse import unquote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from wildpatch import analysis, mcp_server

app = FastAPI(title="fake otari")
SPEND: dict[str, float] = defaultdict(float)
BUDGET = {"wildpatch-guest": 0.0005}
DEFAULT_BUDGET = 0.5
COST = {"intake": 0.0002, "vision": 0.0011, "survey": 0.0031, "scout": 0.0024, "analysis": 0.0019,
        "plan": 0.0047, "structure": 0.0006, "guard": 0.0001}
BLOCK_WORDS = re.compile(r"dig up|nest location|where (exactly )?(can|do) i find|poison|ignore (all|your)", re.I)
DELAY = 0.02


def _tags(request: Request) -> dict:
    raw = request.headers.get("otari-tags", "")
    return dict(unquote(p).split("=", 1) for p in raw.split(",") if "=" in p)


def _text_of(body: dict) -> str:
    parts = [body.get("system") or ""]
    for m in body.get("messages", []):
        c = m.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            parts.extend(x.get("text", "") for x in c if isinstance(x, dict))
    return "\n".join(parts)


def _headers(model: str, served: str, *, fallback: bool = False, attempts: int = 1, guard: list | None = None):
    h = {"Otari-Request-ID": "fake-" + str(abs(hash((model, served))) % 10**8), "Otari-Provider": "fakeinfra",
         "Otari-Model": served, "Otari-Attempt-Count": str(attempts), "Otari-Fallback": str(fallback).lower(),
         "Otari-Response-Duration-Ms": "420"}
    if guard:
        h["Otari-Guardrails"] = json.dumps(guard)
    return h


def _refusal(body: dict, stage: str, end_user: str | None, budget_id: str | None):
    if body.get("guardrails") and BLOCK_WORDS.search(_text_of(body).split("What the group wants:")[-1]):
        profile = body["guardrails"][0]["profile"]
        return JSONResponse({"detail": f"Guardrail '{profile}' flagged the request", "code": "guardrail_violation"},
                            status_code=403, headers={"Otari-Error-Code": "guardrail_violation",
                                                      "Otari-Guardrails": json.dumps([{"profile": profile,
                                                                                       "mode": "block",
                                                                                       "valid": False,
                                                                                       "score": 1.0}])})
    if end_user:
        # Only the guest names a budget (Otari-End-User-Budget), and it is always the tiny one.
        cap = BUDGET["wildpatch-guest"] if budget_id else DEFAULT_BUDGET
        if SPEND[end_user] + COST.get(stage, 0.001) > cap:
            return JSONResponse({"detail": f"User '{end_user}' has ${max(0, cap - SPEND[end_user]):.4f} of budget "
                                           f"remaining, less than this request's estimated cost",
                                 "code": "budget_exceeded"}, status_code=403,
                                headers={"Otari-Error-Code": "budget_exceeded", "Otari-Budget-Scope": "user",
                                         "Retry-After": "18000"})
    return None


def _guard_header(body: dict) -> list | None:
    if not body.get("guardrails"):
        return None
    return [{"profile": g["profile"], "mode": g.get("mode", "monitor"), "valid": True, "score": 0.0}
            for g in body["guardrails"]]


def _sse(obj: dict) -> str:
    return f"event: {obj.get('type', 'message')}\ndata: {json.dumps(obj)}\n\n"


# ---------------------------------------------------------------- chat completions

@app.post("/api/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    stage = _tags(request).get("stage", "?")
    end_user = body.get("user")
    refusal = _refusal(body, stage, end_user, request.headers.get("otari-end-user-budget"))
    if refusal:
        return refusal
    if stage == "intake":
        area = re.search(r"Stated area: (\d+)", _text_of(body))
        text = json.dumps({"site_type": "schoolyard", "area_m2": float(area.group(1)) if area else 300,
                           "goals": ["pollinator habitat", "outdoor learning"], "constraints": ["children play here"],
                           "audience": "teachers and pupils", "summary": "A school group wants a wildlife patch."})
    elif stage == "vision":
        text = json.dumps({"surface": "compacted lawn and paving", "light": "full sun", "soil_hints": "compacted",
                           "existing_plants": [{"name": "Trifolium repens", "confidence": "medium"}],
                           "wildlife_signs": [], "opportunities": ["mini meadow", "log pile"],
                           "risks": ["trampling"]})
    elif stage == "structure":
        names = re.findall(r"^\| \*?([A-Z][a-z]+ [a-z]+)\*? \| meadow", _text_of(body), re.M)
        text = json.dumps({"palette": [{"scientific_name": n, "common_name": None, "layer": "meadow",
                                        "quantity": 12, "reason": "observed natively nearby"} for n in names[:6]],
                           "quick_wins": ["Stop mowing one corner", "Build a log pile", "Run a bioblitz"]})
    elif stage == "guard":
        text = "safe" if "llama-guard" in body["model"] else '{"violation": 0, "rule": "none", "rationale": "ok"}'
    else:
        text = "ok"
    if end_user:
        SPEND[end_user] += COST.get(stage, 0.001)

    async def gen():
        for i in range(0, len(text), 40):
            yield "data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": text[i:i + 40]}}]}) + "\n\n"
            await asyncio.sleep(DELAY)
        usage = {"prompt_tokens": 300, "completion_tokens": 120, "cost_usd": f"{COST.get(stage, 0.001):.6f}"}
        yield "data: " + json.dumps({"choices": [], "usage": usage}) + "\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers=_headers(body["model"], body["model"], guard=_guard_header(body)))


# ---------------------------------------------------------------- messages

async def _mcp_blocks(body: dict, start_index: int):
    site = re.search(r"lat=([-\d.]+), lon=([-\d.]+), radius_km=([\d.]+)", _text_of(body))
    lat, lon, radius = (float(x.rstrip(".")) for x in site.groups())
    only = re.search(r"Call ONLY these tools now: ([\w, ]+)", _text_of(body))
    calls = [("biodiversity_snapshot", {"lat": lat, "lon": lon, "radius_km": radius}),
             ("native_species", {"lat": lat, "lon": lon, "radius_km": radius, "group": "plants", "limit": 30}),
             ("pollinators", {"lat": lat, "lon": lon, "radius_km": radius, "limit": 20}),
             ("introduced_species", {"lat": lat, "lon": lon, "radius_km": radius, "group": "plants", "limit": 15}),
             ("sensitive_species", {"lat": lat, "lon": lon, "radius_km": 5}),
             ("climate_profile", {"lat": lat, "lon": lon})]
    if only:
        wanted = {t.strip() for t in only.group(1).split(",")}
        calls = [c for c in calls if c[0] in wanted]
    idx = start_index
    for n, (name, args) in enumerate(calls):
        call_id = f"mcptoolu_fake{n}"
        yield idx, {"type": "mcp_tool_use", "id": call_id, "name": name, "server_name": "wildpatch-biodiversity",
                    "input": args}
        result = await getattr(mcp_server, name)(**args)
        yield idx + 1, {"type": "mcp_tool_result", "tool_use_id": call_id, "is_error": False,
                        "content": [{"type": "text", "text": result}]}
        idx += 2


@app.post("/api/v1/messages")
async def messages(request: Request):
    body = await request.json()
    stage = _tags(request).get("stage", "?")
    end_user = (body.get("metadata") or {}).get("user_id")
    refusal = _refusal(body, stage, end_user, request.headers.get("otari-end-user-budget"))
    if refusal:
        return refusal
    model = body["model"]
    drill = model == "wildpatch-drill"
    served = {"wildpatch-planner": "qwen3-235b-a22b-instruct-2507", "wildpatch-drill": "deepseek-v4-pro"}.get(
        model, model.split("/")[-1])
    if end_user:
        SPEND[end_user] += COST.get(stage, 0.001)

    async def gen():
        yield _sse({"type": "message_start", "message": {"id": "msg_fake", "model": model,
                                                         "usage": {"input_tokens": 900, "output_tokens": 1}}})
        idx = 0
        text = ""
        if stage == "survey":
            async for i, block in _mcp_blocks(body, 0):
                yield _sse({"type": "content_block_start", "index": i, "content_block": block})
                yield _sse({"type": "content_block_stop", "index": i})
                idx = i + 1
                await asyncio.sleep(DELAY)
            text = "- Rich urban flora with many native herbs.\n- Bees and butterflies recorded.\n- Dry summers."
        elif stage == "scout":
            yield _sse({"type": "content_block_start", "index": 0, "content_block": {
                "type": "server_tool_use", "id": "otari_srvtoolu_s1", "name": "web_search", "input": {}}})
            yield _sse({"type": "content_block_delta", "index": 0, "delta": {
                "type": "input_json_delta", "partial_json": '{"query": "native plant nursery near site"}'}})
            yield _sse({"type": "content_block_stop", "index": 0})
            results = [{"type": "web_search_result", "title": "Native Plant Nursery Co-op",
                        "url": "https://example.org/native-nursery"},
                       {"type": "web_search_result", "title": "City Biodiversity Grants 2026",
                        "url": "https://example.org/biodiversity-grants"}]
            yield _sse({"type": "content_block_start", "index": 1, "content_block": {
                "type": "web_search_tool_result", "tool_use_id": "otari_srvtoolu_s1", "content": results}})
            yield _sse({"type": "content_block_stop", "index": 1})
            idx = 2
            text = json.dumps({"resources": [
                {"name": "Native Plant Nursery Co-op", "kind": "nursery", "url": "https://example.org/native-nursery",
                 "why": "Sells local-provenance plugs."},
                {"name": "City Biodiversity Grants 2026", "kind": "grant",
                 "url": "https://example.org/biodiversity-grants", "why": "Funds school habitat projects."}]})
        elif stage == "analysis":
            dataset = json.loads(_text_of(body).split("Dataset (JSON):\n", 1)[1].split("\nRun the script")[0])
            result = analysis.compute(dataset)
            code = "import json, math\ndata = json.loads('''...''')\n# diversity, water, planting\n"
            yield _sse({"type": "content_block_start", "index": 0, "content_block": {
                "type": "server_tool_use", "id": "otari_srvtoolu_c1", "name": "code_execution", "input": {"code": code}}})
            yield _sse({"type": "content_block_stop", "index": 0})
            yield _sse({"type": "content_block_start", "index": 1, "content_block": {
                "type": "code_execution_tool_result", "tool_use_id": "otari_srvtoolu_c1", "content": {
                    "type": "code_execution_result", "stdout": "WILDPATCH_RESULT=" + json.dumps(result) + "\n",
                    "stderr": "", "return_code": 0, "content": []}}})
            yield _sse({"type": "content_block_stop", "index": 1})
            idx = 2
            text = "1. Native plants dominate.\n2. Summer needs watering.\n3. Recording effort is rising fast."
        elif stage == "plan":
            natives = re.findall(r'"scientific_name": "([^"]+)"', _text_of(body))[:5]
            rows = "\n".join(f"| {n} | meadow | observed natively nearby | 12 |" for n in natives)
            text = ("## The patch at a glance\n- A sunny schoolyard corner.\n\n## Planting palette\n"
                    "| Species | Layer | Why here | How many |\n|---|---|---|---|\n" + rows +
                    "\n\n## Layout\nMeadow in the sun, shrubs on the edge.\n\n## The first 12 months\n"
                    "- **Oct–Nov:** plant plugs.\n\n## Helping local wildlife\nLog piles.\n\n## Avoid and manage\n"
                    "Pull seedlings of introduced species.\n\n## Local help\n- [Native Plant Nursery Co-op]"
                    "(https://example.org/native-nursery)\n\n## Measuring success\nSeasonal bioblitz.")
        for i in range(0, len(text), 60):
            if i == 0:
                yield _sse({"type": "content_block_start", "index": idx, "content_block": {"type": "text", "text": ""}})
            yield _sse({"type": "content_block_delta", "index": idx, "delta": {"type": "text_delta",
                                                                               "text": text[i:i + 60]}})
            await asyncio.sleep(DELAY)
        if text:
            yield _sse({"type": "content_block_stop", "index": idx})
        yield _sse({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 400, "cost_usd": f"{COST.get(stage, 0.001):.6f}"}})
        yield _sse({"type": "message_stop"})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers=_headers(model, served, fallback=drill, attempts=2 if drill else 1,
                                              guard=_guard_header(body)))


@app.get("/api/v1/end-users")
async def end_users(external_id: str):
    budget = "wildpatch-guest" if external_id.endswith("guest") else "wildpatch-volunteer"
    return [{"external_id": external_id, "user_id": "u-" + external_id, "budget_id": budget, "blocked": False,
             "spend": round(SPEND[external_id], 6), "reserved": 0.0, "current_requests": 0, "current_tokens": 0,
             "next_budget_reset_at": "2026-11-02T00:00:00Z"}] if SPEND.get(external_id) is not None else []
