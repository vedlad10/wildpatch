"""One Wildpatch planning run: seven open-weight model roles, every call routed through Otari.

    intake     reads the group's brief            guardrails: wildlife-protection + content-safety
    vision     looks at the site photo            (Gemma 3)
    survey     open biodiversity data             Otari-managed MCP loop over our MCP server
    scout      local nurseries, grants, rules     Otari web search + web fetch
    analysis   diversity, water, quantities       Otari code-execution sandbox, checked locally
    plan       the plan itself                    routing policy: fallback chain + budget tier-down
    structure  palette cards and quick wins       checked against the native-species list

vision, survey and scout run in parallel. Each call bills the volunteer as an end user of the service key,
so per-volunteer budgets and the app's own ceiling are enforced by Otari before any provider is called.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from . import analysis, config, guard, prompts, sources
from .otari import Otari, OtariError, Receipt

STAGES = [
    {"id": "intake", "title": "Read the brief", "role": "intake", "feature": "guardrails"},
    {"id": "vision", "title": "Look at the site photo", "role": "vision", "feature": "multi_model"},
    {"id": "survey", "title": "Survey open biodiversity data", "role": "surveyor", "feature": "mcp"},
    {"id": "scout", "title": "Scout local help on the web", "role": "scout", "feature": "web"},
    {"id": "analysis", "title": "Run the numbers in a sandbox", "role": "analyst", "feature": "code"},
    {"id": "plan", "title": "Write the rewilding plan", "role": "planner", "feature": "routing"},
    {"id": "structure", "title": "Build the planting palette", "role": "structurer", "feature": "multi_model"},
]
SURVEY_TOOLS = ["biodiversity_snapshot", "native_species", "pollinators", "introduced_species",
                "sensitive_species", "climate_profile"]
_WAF_INLINE_CALL = re.compile(r"`([^`\n]*\([^`\n]*)`")
_TOOL_PREVIEW_CHARS = 20000


@dataclass
class RunRequest:
    lat: float
    lon: float
    radius_km: float = 2.0
    site_name: str = ""
    area_m2: float | None = None
    goals: str = ""
    photo: str | None = None  # data URL
    volunteer: str = "ana"
    drill: bool = False


class Stop(Exception):
    """A refusal that ends the run (guardrail or budget)."""


def waf_safe(text: str) -> str:
    """Unwrap inline code that contains a call: hosted Otari's WAF answers 403 to it (mozilla-ai/otari#2704)."""
    return _WAF_INLINE_CALL.sub(r"\1", text)


def parse_json(text: str) -> dict | None:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    for candidate in (text, *re.findall(r"\{.*\}", text, re.S)):
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _TOOL_PREVIEW_CHARS:
        return value[:_TOOL_PREVIEW_CHARS] + "…"
    return value


def _verdicts_from_our_guard(user_text: str, since: float) -> list[dict]:
    """The verdicts our guardrail service gave Otari for this input.

    Otari omits the Otari-Guardrails header on streamed responses, but the service that answered Otari is this
    app, so its log has the verdicts. Otari sends the user message as input_text; match on its tail."""
    tail = user_text.strip()[-120:]
    found: dict[str, dict] = {}
    for g in guard.GUARD_LOG:
        if g["at"] >= since - 1 and tail and g["preview"].strip().endswith(tail[-min(len(tail), 100):]):
            found[g["profile"]] = {"profile": g["profile"], "mode": "block", "valid": g.get("valid"),
                                   "score": g.get("score"), "explanation": g.get("explanation"),
                                   "source": "wildpatch guard service"}
    return list(found.values())


def _is_refusal(e: OtariError) -> bool:
    """Budget and guardrail refusals end the run; other errors may be survivable."""
    return e.code == "budget_exceeded" or bool(e.budget_scope) or bool(e.code and "guardrail" in e.code)


def end_user_id(volunteer: str) -> str:
    return f"{config.END_USER_PREFIX}{volunteer}"


class Run:
    def __init__(self, req: RunRequest):
        self.req = req
        self.id = uuid.uuid4().hex[:10]
        volunteer = config.VOLUNTEERS.get(req.volunteer) or config.VOLUNTEERS["ana"]
        self.otari = Otari(end_user=end_user_id(req.volunteer), end_user_budget=volunteer["budget"],
                           conversation_id=f"wildpatch-{self.id}",
                           tags={"app": "wildpatch", "run": self.id, "volunteer": req.volunteer})
        self.queue: asyncio.Queue[dict | None] = asyncio.Queue()
        self.receipts: list[Receipt] = []
        self.tools_seen: set[str] = set()
        self.guard_verdicts: list[dict] = []
        self.started = time.monotonic()
        self.state: dict[str, Any] = {}
        self.blocked = False
        self.halt_reason: str | None = None

    # ------------------------------------------------------------------ plumbing

    async def emit(self, type_: str, **data: Any) -> None:
        await self.queue.put({"type": type_, **data})

    async def events(self) -> AsyncIterator[dict]:
        task = asyncio.create_task(self._run())
        try:
            while (ev := await self.queue.get()) is not None:
                yield ev
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _guardrails(self, profiles: list[str]) -> list[dict] | None:
        if not config.GUARD_URL:
            return None
        return [{"profile": p, "mode": "block", "url": config.GUARD_URL, "on_unavailable": "block"} for p in profiles]

    def _mcp(self) -> dict:
        if config.OTARI_MCP_SERVER_ID:
            return {"mcp_server_ids": [config.OTARI_MCP_SERVER_ID]}
        if config.MCP_URL:
            server = {"name": "wildpatch-biodiversity", "url": config.MCP_URL,
                      "purpose_hint": "Open biodiversity and climate data for the survey site"}
            if config.MCP_TOKEN:
                server["authorization_token"] = config.MCP_TOKEN
            return {"mcp_servers": [server]}
        raise RuntimeError("the MCP server has no public URL: set PUBLIC_URL (or OTARI_MCP_SERVER_ID)")

    async def _consume(self, stage: str, stream: AsyncIterator[dict], calls: dict[str, dict] | None = None,
                       guarded_input: str | None = None) -> tuple[str, Receipt, dict[str, dict]]:
        """Forward a model stream to the UI; return its text, receipt and tool calls by id.

        Tool calls accumulate in `calls`, which the caller may own: if the stream breaks part-way, what the
        gateway's tools already returned is still there to salvage. `guarded_input` is the user message a
        guardrail checked, used to find the verdicts when Otari's header does not carry them."""
        started = time.time()
        text: list[str] = []
        calls = {} if calls is None else calls
        receipt: Receipt | None = None
        async for ev in stream:
            kind = ev["type"]
            if kind == "text":
                text.append(ev["text"])
                await self.emit("text", stage=stage, text=ev["text"])
            elif kind == "tool_call":
                calls[ev["id"]] = {"name": ev.get("name"), "kind": ev["kind"], "input": ev.get("input")}
                await self.emit("tool_call", stage=stage, id=ev["id"], kind=ev["kind"], name=ev.get("name"),
                                server=ev.get("server"), input=ev.get("input"))
            elif kind == "tool_result":
                calls.setdefault(ev["id"], {"name": None, "kind": ev["kind"], "input": None})
                calls[ev["id"]].update(output=ev["output"], is_error=ev["is_error"])
                self.tools_seen.add(ev["kind"])
                await self.emit("tool_result", stage=stage, id=ev["id"], kind=ev["kind"],
                                name=calls[ev["id"]].get("name"), output=_clip(ev["output"]),
                                is_error=ev["is_error"])
            elif kind == "receipt":
                receipt = ev["receipt"]
        assert receipt is not None
        if guarded_input and not receipt.guardrails:
            receipt.guardrails = _verdicts_from_our_guard(guarded_input, since=started)
        self.receipts.append(receipt)
        self.guard_verdicts.extend(receipt.guardrails)
        await self.emit("receipt", stage=stage, receipt=receipt.to_dict())
        return "".join(text), receipt, calls

    async def _stage(self, stage: str, coro_fn, *, critical: bool = False) -> Any:
        meta = next(s for s in STAGES if s["id"] == stage)
        model = self._model_for(meta["role"])
        await self.emit("stage_start", stage=stage, title=meta["title"], role=meta["role"], model=model)
        t0 = time.monotonic()
        try:
            result = await coro_fn()
        except OtariError as e:
            info = e.to_dict()
            if e.code == "budget_exceeded" or e.budget_scope:
                self.blocked = True
                await self.emit("blocked", stage=stage, reason="budget", error=info)
                raise Stop from e
            if e.code and "guardrail" in e.code:
                self.blocked = True
                await self.emit("blocked", stage=stage, reason="guardrail", error=info)
                raise Stop from e
            await self.emit("stage_error", stage=stage, error=info)
            if critical:
                self.halt_reason = f"{meta['title']} failed, so the run stopped: {e.code or ''} {e.detail}".strip()
                raise Stop from e
            return None
        except Stop:
            raise
        except Exception as e:  # a bug or an upstream data failure: keep the run going where possible
            await self.emit("stage_error", stage=stage, error={"detail": f"{type(e).__name__}: {e}"})
            if critical:
                self.halt_reason = f"{meta['title']} failed, so the run stopped: {type(e).__name__}: {e}"
                raise Stop from e
            return None
        await self.emit("stage_done", stage=stage, ms=int((time.monotonic() - t0) * 1000))
        return result

    def _model_for(self, role: str) -> str:
        if role == "planner" and self.req.drill:
            return config.PLANNER_DRILL_POLICY
        return config.MODELS[role]

    # ------------------------------------------------------------------ the run

    async def _run(self) -> None:
        req = self.req
        await self.emit("run_start", run_id=self.id, volunteer=req.volunteer, end_user=end_user_id(req.volunteer),
                        stages=[s for s in STAGES if s["id"] != "vision" or req.photo],
                        site={"lat": req.lat, "lon": req.lon, "radius_km": req.radius_km, "name": req.site_name},
                        guard=bool(config.GUARD_URL), drill=req.drill)
        try:
            place = await sources.reverse_geocode(req.lat, req.lon)
        except sources.SourceError:
            place = {"name": req.site_name, "city": None, "country": None}
        self.state["place"] = place
        try:
            brief = await self._stage("intake", self._intake, critical=True)
            area = req.area_m2 or float(brief.get("area_m2") or 200)
            self.state["area_m2"] = max(5.0, min(area, 200000.0))
            parallel = [asyncio.create_task(self._stage("survey", self._survey, critical=True)),
                        asyncio.create_task(self._stage("scout", self._scout))]
            if req.photo:
                parallel.append(asyncio.create_task(self._stage("vision", self._vision)))
            try:
                survey, scout, *rest = [await t for t in parallel]
            except BaseException:
                for t in parallel:
                    t.cancel()
                await asyncio.gather(*parallel, return_exceptions=True)
                raise
            vision = rest[0] if rest else None
            self.state["survey"] = survey
            numbers = await self._stage("analysis", self._analysis)
            plan_md = await self._stage("plan", lambda: self._plan(brief, vision, survey, scout, numbers),
                                        critical=True)
            palette = await self._stage("structure", lambda: self._structure(plan_md, survey))
            await self.emit("result", brief=brief, vision=vision, survey=survey, scout=scout, analysis=numbers,
                            plan=plan_md, palette=palette, place=place, area_m2=self.state["area_m2"])
        except Stop:
            if not self.blocked:
                await self.emit("run_error", error={"detail": self.halt_reason or "A required stage failed."})
        except Exception as e:
            await self.emit("run_error", error={"detail": f"{type(e).__name__}: {e}"})
        finally:
            try:
                await self._finish()
            finally:
                await self.otari.aclose()
                await self.queue.put(None)

    async def _finish(self) -> None:
        total = sum(r.cost_usd or 0 for r in self.receipts)
        allowance = None
        try:
            allowance = await self.otari.end_user_status(end_user_id(self.req.volunteer))
        except Exception as e:
            allowance = {"error": str(e)[:200]}
        await self.emit("allowance", volunteer=self.req.volunteer, status=allowance)
        await self.emit("scorecard", features=self._scorecard())
        await self.emit("run_done", total_cost_usd=round(total, 6), wall_s=round(time.monotonic() - self.started, 1),
                        receipts=[r.to_dict() for r in self.receipts])

    # ------------------------------------------------------------------ stages

    async def _intake(self) -> dict:
        req = self.req
        described = (f"Site: {req.site_name or self.state['place'].get('name')}\n"
                     f"Stated area: {req.area_m2 or 'not given'} m2\n"
                     f"What the group wants:\n{req.goals.strip() or '(no description)'}")
        text, _, _ = await self._consume("intake", self.otari.chat_stream(
            stage="intake", role="intake", model=config.MODELS["intake"], max_tokens=700, temperature=0,
            messages=[{"role": "system", "content": prompts.INTAKE}, {"role": "user", "content": waf_safe(described)}],
            json_mode=True, reasoning_effort="low",
            guardrails=self._guardrails(["wildlife-protection", "content-safety"])), guarded_input=waf_safe(described))
        brief = parse_json(text) or {"summary": req.goals[:200]}
        await self.emit("data", stage="intake", key="brief", value=brief)
        return brief

    async def _vision(self) -> dict | None:
        text, _, _ = await self._consume("vision", self.otari.chat_stream(
            stage="vision", role="vision", model=config.MODELS["vision"], max_tokens=700, temperature=0.2,
            messages=[{"role": "system", "content": prompts.VISION},
                      {"role": "user", "content": [
                          {"type": "text", "text": f"Site: {self.req.site_name or 'community patch'}"},
                          {"type": "image_url", "image_url": {"url": self.req.photo}}]}]))
        vision = parse_json(text)
        await self.emit("data", stage="vision", key="vision", value=vision)
        return vision

    async def _survey(self) -> dict:
        req = self.req
        results: dict[str, Any] = {}
        summary = ""
        for attempt in range(2):
            missing = [t for t in SURVEY_TOOLS if t not in results]
            if not missing:
                break
            ask = (f"Site: {req.site_name or 'community patch'}. lat={req.lat}, lon={req.lon}, "
                   f"radius_km={req.radius_km}.")
            if attempt:
                ask += f" Call ONLY these tools now: {', '.join(missing)}."
            calls: dict[str, dict] = {}
            try:
                text, _, _ = await self._consume("survey", self.otari.messages_stream(
                    stage="survey", role="surveyor", model=config.MODELS["surveyor"], max_tokens=900, temperature=0,
                    system=prompts.SURVEYOR_SYSTEM, messages=[{"role": "user", "content": ask}],
                    max_tool_iterations=10, **self._mcp()), calls)
            except OtariError as e:
                # A stream that breaks after some MCP results keeps them; the next attempt asks for the rest.
                if _is_refusal(e) or attempt == 1 or not calls:
                    raise
                text = ""
            summary = text or summary
            for call in calls.values():
                name, out = call.get("name"), call.get("output")
                if not name or not isinstance(out, dict) or "error" in out:
                    continue
                group = (call.get("input") or {}).get("group", "plants")
                if name in ("native_species", "introduced_species") and sources.group_name(str(group)) != "Plantae":
                    results.setdefault(f"{name}:{group}", out)
                    continue
                results.setdefault(name, out)
        survey = {"tools": results, "summary": summary, "missing": [t for t in SURVEY_TOOLS if t not in results]}
        await self.emit("data", stage="survey", key="survey", value={"summary": summary, "missing": survey["missing"],
                                                                     "tools_returned": sorted(results)})
        if "native_species" not in results:
            raise RuntimeError("the survey returned no native-species data")
        return survey

    async def _scout(self) -> dict:
        place = self.state["place"]
        where = ", ".join(x for x in (place.get("city"), place.get("country")) if x) or self.req.site_name
        calls: dict[str, dict] = {}
        salvaged = False
        try:
            text, _, _ = await self._consume("scout", self.otari.messages_stream(
                stage="scout", role="scout", model=config.MODELS["scout"], max_tokens=1500, temperature=0.2,
                system=prompts.SCOUT_SYSTEM.format(place=where),
                messages=[{"role": "user", "content": f"Find local help for rewilding a "
                                                      f"{self.req.site_name or 'patch'} in {where}."}],
                tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 2},
                       {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 1}],
                headers={"Otari-Web-Search": "otari", "Otari-Web-Fetch": "otari"}, max_tool_iterations=4), calls)
        except OtariError as e:
            # Hosted search/fetch can be slow and a long stream can break; keep what the searches found.
            if _is_refusal(e) or not any(isinstance(c.get("output"), list) for c in calls.values()):
                raise
            text, salvaged = "", True
        seen: dict[str, str] = {}
        for call in calls.values():
            out = call.get("output")
            if isinstance(out, list):
                seen.update({i["url"]: i.get("title") or i["url"] for i in out if isinstance(i, dict) and i.get("url")})
            elif isinstance(out, dict) and out.get("url"):
                seen[out["url"]] = out.get("title") or out["url"]
        resources = (parse_json(text) or {}).get("resources") or []
        for r in resources:
            url = (r.get("url") or "").rstrip("/")
            r["verified"] = any(url and s.rstrip("/").startswith(url[:60]) for s in seen)
        if not resources and seen:
            salvaged = True
            resources = [{"name": title, "kind": "link", "url": url, "why": "Found by Otari web search.",
                          "verified": True} for url, title in list(seen.items())[:5]]
        scout = {"where": where, "resources": resources, "searched": len(seen), "salvaged": salvaged}
        await self.emit("data", stage="scout", key="scout", value=scout)
        return scout

    async def _analysis(self) -> dict | None:
        survey = self.state["survey"]
        dataset = analysis.build_dataset(survey["tools"], lat=self.req.lat, lon=self.req.lon,
                                         radius_km=self.req.radius_km, area_m2=self.state["area_m2"])
        local = analysis.compute(dataset)
        sandbox = None
        for attempt in range(2):
            ask = ("Dataset (JSON):\n" + json.dumps(dataset, ensure_ascii=False) +
                   ("\nRun the script with the code execution tool now." if attempt else ""))
            text, _, calls = await self._consume("analysis", self.otari.messages_stream(
                stage="analysis", role="analyst", model=config.MODELS["analyst"], max_tokens=3000, temperature=0,
                system=prompts.ANALYST_SYSTEM, messages=[{"role": "user", "content": ask}],
                tools=[{"type": "code_execution_20250825", "name": "code_execution"}],
                headers={"Otari-Code-Execution": "otari"}, max_tool_iterations=4))
            for call in calls.values():
                out = call.get("output")
                if call.get("kind") == "code" and isinstance(out, dict):
                    sandbox = analysis.parse_sandbox_result(out.get("stdout") or "") or sandbox
            if sandbox:
                break
        numbers = {"dataset": dataset, "local": local, "sandbox": sandbox,
                   "verification": analysis.verify(sandbox, local) if sandbox else None,
                   "source": "sandbox" if sandbox else "local"}
        await self.emit("data", stage="analysis", key="analysis", value=numbers)
        return numbers

    async def _plan(self, brief, vision, survey, scout, numbers) -> str:
        if not survey:
            raise RuntimeError("no survey data to plan from")
        tools = survey["tools"]
        best = (numbers or {}).get("sandbox") or (numbers or {}).get("local")
        evidence = {
            "SITE": {"name": self.req.site_name, "place": self.state["place"].get("name"),
                     "lat": self.req.lat, "lon": self.req.lon, "planted_area_m2": self.state["area_m2"]},
            "BRIEF": brief, "PHOTO (vision model)": vision,
            "NATIVE PLANTS (observed natively within radius, with observation counts)":
                [{"scientific_name": s["scientific_name"], "common_name": s.get("common_name"),
                  "observations": s["observations"]} for s in (tools.get("native_species") or {}).get("species", [])],
            "POLLINATORS": [{"name": s["scientific_name"], "common_name": s.get("common_name")}
                            for s in (tools.get("pollinators") or {}).get("species", [])[:12]],
            "INTRODUCED PLANTS": [{"name": s["scientific_name"], "common_name": s.get("common_name")}
                                  for s in (tools.get("introduced_species") or {}).get("species", [])[:10]],
            "THREATENED SPECIES (names only)": [{"name": s["scientific_name"], "common_name": s.get("common_name"),
                                                 "status": s.get("conservation_status")}
                                                for s in (tools.get("sensitive_species") or {}).get("species", [])[:8]],
            "CLIMATE": {k: v for k, v in (tools.get("climate_profile") or {}).items() if k != "monthly"},
            "ANALYST NUMBERS (from the code sandbox)": best,
            "SURVEYOR SUMMARY": survey.get("summary"),
            "LOCAL RESOURCES": [{"name": r.get("name"), "url": r.get("url"), "kind": r.get("kind"), "why": r.get("why")}
                                for r in (scout or {}).get("resources", [])],
        }
        user_text = waf_safe(json.dumps(evidence, ensure_ascii=False, indent=1))
        text, receipt, _ = await self._consume("plan", self.otari.messages_stream(
            stage="plan", role="planner", model=self._model_for("planner"), max_tokens=2600, temperature=0.4,
            system=prompts.PLANNER_SYSTEM, messages=[{"role": "user", "content": user_text}],
            guardrails=self._guardrails(["wildlife-protection"])), guarded_input=user_text)
        economy = config.PLANNER_ECONOMY_MODEL.split("/")[-1].split(":")[-1]
        tier_down = bool(receipt.served_model and economy and economy in receipt.served_model and not receipt.fallback)
        await self.emit("routing", stage="plan", policy=receipt.requested_model, served=receipt.served_model,
                        provider=receipt.provider, fallback=receipt.fallback, attempts=receipt.attempts,
                        tier_down=tier_down, drill=self.req.drill)
        self.state["routing"] = {"fallback": receipt.fallback, "tier_down": tier_down}
        return text

    async def _structure(self, plan_md: str, survey: dict) -> dict:
        text, _, _ = await self._consume("structure", self.otari.chat_stream(
            stage="structure", role="structurer", model=config.MODELS["structurer"], max_tokens=1500, temperature=0,
            messages=[{"role": "system", "content": prompts.STRUCTURER},
                      {"role": "user", "content": waf_safe(plan_md)}], json_mode=True, reasoning_effort="low"))
        data = parse_json(text) or {"palette": [], "quick_wins": []}
        natives = {s["scientific_name"].lower(): s for s in
                   (survey["tools"].get("native_species") or {}).get("species", [])}
        for item in data.get("palette") or []:
            match = natives.get((item.get("scientific_name") or "").strip().lower())
            item["verified_native"] = bool(match)
            if match:
                item.update(observations=match["observations"], photo=match.get("photo"),
                            inat_url=f"https://www.inaturalist.org/taxa/{match['inat_taxon_id']}")
        return data

    # ------------------------------------------------------------------ scorecard

    def _scorecard(self) -> dict:
        served = sorted({r.served_model or r.requested_model for r in self.receipts if r.served_model})
        roles = sorted({r.role for r in self.receipts})
        routing = self.state.get("routing") or {}
        verdicts = [v for v in self.guard_verdicts if isinstance(v, dict)]
        return {
            "open_weight": {"lit": len(served) >= 2, "detail": served},
            "multi_model": {"lit": len(served) >= 2 and len(roles) >= 3, "detail": roles},
            "routing": {"lit": any(r.role == "planner" for r in self.receipts),
                        "detail": "fallback served the plan" if routing.get("fallback") else
                        "budget tier-down" if routing.get("tier_down") else "policy served by its first choice"},
            "mcp": {"lit": "mcp" in self.tools_seen},
            "web": {"lit": bool({"search", "fetch"} & self.tools_seen)},
            "code": {"lit": "code" in self.tools_seen},
            "guardrails": {"lit": bool(verdicts), "detail": verdicts[:4]},
            "budgets": {"lit": bool(self.receipts), "detail": "app ceiling on the service key"},
            "per_user": {"lit": bool(self.receipts), "detail": end_user_id(self.req.volunteer)},
        }
