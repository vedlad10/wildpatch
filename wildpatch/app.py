"""Wildpatch web app.

One service, three faces:
  /                         the planner UI (static) and its /api/* routes
  /mcp                      the Biodiversity MCP server Otari connects to (streamable HTTP, bearer token)
  /guard/<token>/validate   the guardrail service Otari calls before a guarded request reaches a provider
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import time
from collections import defaultdict, deque

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field
from starlette.routing import Route

from . import config, guard, mcp_server, net, sources
from .otari import Otari, OtariError
from .pipeline import Run, RunRequest, end_user_id

WEB = config.ROOT / "web"
HEARTBEAT_S = 12

mcp_http = mcp_server.mcp.streamable_http_app(
    streamable_http_path="/mcp", json_response=True, stateless_http=True,
    # Otari reaches us through a public hostname, so the localhost-only Host check must be off.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    net.tls()  # build the shared TLS context before the first request needs it
    async with mcp_server.mcp.session_manager.run():
        yield


app = FastAPI(title="Wildpatch", lifespan=lifespan)


class BearerGate:
    """Require `Authorization: Bearer <token>` on an ASGI app when a token is configured."""

    def __init__(self, inner, token: str):
        self.inner, self.token = inner, token

    async def __call__(self, scope, receive, send):
        if self.token and scope["type"] == "http":
            auth = dict(scope.get("headers") or []).get(b"authorization", b"").decode("latin-1")
            if not hmac.compare_digest(auth.encode(), f"Bearer {self.token}".encode()):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.inner(scope, receive, send)


for route in mcp_http.routes:
    if isinstance(route, Route) and route.path == "/mcp":
        app.router.routes.append(Route("/mcp", endpoint=BearerGate(route.app, config.MCP_TOKEN)))


# ---------------------------------------------------------------- guardrail service (called by Otari)

class GuardRequest(BaseModel):
    profile: str
    input_text: str = ""
    validate_kwargs: dict | None = None


def _check_guard_token(token: str) -> None:
    if not config.GUARD_TOKEN or not hmac.compare_digest(token.encode(), config.GUARD_TOKEN.encode()):
        raise HTTPException(status_code=404)


@app.get("/guard/{token}/profiles")
async def guard_profiles(token: str):
    _check_guard_token(token)
    return [{"name": name, "guardrail_name": desc} for name, desc in guard.PROFILES.items()]


@app.post("/guard/{token}/validate")
async def guard_validate(token: str, body: GuardRequest):
    _check_guard_token(token)
    result = await guard.validate(body.profile, body.input_text)
    return {"profile": body.profile, "result": {k: result.get(k) for k in ("valid", "explanation", "score")}}


# ---------------------------------------------------------------- planner API

class RunBody(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    radius_km: float = Field(2.0, ge=0.2, le=10)
    site_name: str = Field("", max_length=120)
    area_m2: float | None = Field(None, ge=5, le=200000)
    goals: str = Field("", max_length=2000)
    photo: str | None = Field(None, max_length=6_000_000)
    volunteer: str = "ana"
    drill: bool = False


_runs_by_ip: dict[str, deque] = defaultdict(deque)
_run_slots = asyncio.Semaphore(config.MAX_CONCURRENT_RUNS)


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


def _rate_limit(ip: str) -> None:
    window = _runs_by_ip[ip]
    now = time.time()
    while window and now - window[0] > 3600:
        window.popleft()
    if len(window) >= config.RUNS_PER_HOUR_PER_IP:
        raise HTTPException(status_code=429, detail="Too many plans from this address in the last hour.")
    window.append(now)


@app.get("/api/config")
async def get_config():
    return {
        "otari_configured": bool(config.OTARI_API_KEY),
        "base_url": config.OTARI_BASE_URL,
        "public_url": config.PUBLIC_URL,
        "guardrails": bool(config.GUARD_URL),
        "mcp": "stored" if config.OTARI_MCP_SERVER_ID else ("inline" if config.MCP_URL else None),
        "models": config.MODELS,
        "planner_economy_model": config.PLANNER_ECONOMY_MODEL,
        "drill_policy": config.PLANNER_DRILL_POLICY,
        "volunteers": [{"id": k, "end_user": end_user_id(k), **{f: v[f] for f in ("name", "role", "cap_usd")}}
                       for k, v in config.VOLUNTEERS.items()],
    }


@app.get("/api/geocode")
async def geocode(q: str):
    try:
        return await sources.geocode(q[:200])
    except sources.SourceError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.get("/api/allowance/{volunteer}")
async def allowance(volunteer: str):
    if volunteer not in config.VOLUNTEERS:
        raise HTTPException(status_code=404)
    client = Otari()
    try:
        return {"volunteer": volunteer, "status": await client.end_user_status(end_user_id(volunteer))}
    except OtariError as e:
        return JSONResponse({"volunteer": volunteer, "error": e.to_dict()}, status_code=200)
    finally:
        await client.aclose()


@app.get("/api/inbox")
async def inbox():
    """What our MCP server and guardrail service received recently: proof Otari's cloud is calling them."""
    return {"mcp": list(mcp_server.CALL_LOG)[-20:], "guard": list(guard.GUARD_LOG)[-20:]}


@app.post("/api/run")
async def run(body: RunBody, request: Request):
    if not config.OTARI_API_KEY:
        raise HTTPException(status_code=503, detail="OTARI_API_KEY is not set on the server.")
    if body.volunteer not in config.VOLUNTEERS:
        raise HTTPException(status_code=422, detail="Unknown volunteer.")
    _rate_limit(_client_ip(request))
    plan = Run(RunRequest(**body.model_dump()))

    async def stream():
        async with _run_slots:
            events = plan.events().__aiter__()
            pending = asyncio.ensure_future(events.__anext__())
            try:
                while True:
                    done, _ = await asyncio.wait({pending}, timeout=HEARTBEAT_S)
                    if not done:
                        yield ": keep-alive\n\n"
                        continue
                    try:
                        ev = pending.result()
                    except StopAsyncIteration:
                        break
                    yield f"data: {json.dumps(ev, default=str)}\n\n"
                    pending = asyncio.ensure_future(events.__anext__())
            finally:
                if not pending.done():
                    pending.cancel()
                await events.aclose()

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/healthz")
async def healthz():
    return {"ok": True}


app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
