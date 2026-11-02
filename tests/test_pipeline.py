"""End-to-end runs of the pipeline against the fake Otari, with canned open-data sources."""
from __future__ import annotations

import asyncio
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from wildpatch import analysis, config, pipeline, sources
from wildpatch.otari import OtariError
from tests import fake_otari

NATIVES = [("Trifolium repens", 40), ("Olea europaea", 30), ("Viburnum tinus", 25), ("Celtis australis", 20),
           ("Rosmarinus officinalis", 12)]


async def _species_counts(lat, lon, radius_km, *, iconic=None, taxon_ids=None, place_id=None, native=None,
                          introduced=None, threatened=None, limit=25):
    if native:
        rows = NATIVES
    elif introduced:
        rows = [("Ipomoea indica", 49), ("Pittosporum tobira", 30)]
    elif threatened:
        rows = [("Oryctolagus cuniculus", 52)]
    else:
        rows = [("Apis mellifera", 96), ("Rhodometra sacraria", 81), ("Anthidium florentinum", 20)]
    out = [{"scientific_name": n, "common_name": None, "group": "Plantae", "observations": c, "inat_taxon_id": i,
            "photo": f"https://example.org/{i}.jpg"} for i, (n, c) in enumerate(rows, 1)]
    return len(out) * 10, out


async def _climate(lat, lon, years=10):
    monthly = [{"month": m, "tmax_c": 20.0, "tmin_c": 10.0, "precip_mm": 40.0 if m not in (6, 7, 8) else 10.0,
                "et0_mm": 60.0 if m in (5, 6, 7, 8, 9) else 30.0, "frost_days": 0.0} for m in range(1, 13)]
    return {"period": "2016-2025", "monthly": monthly, "annual_precip_mm": 390, "annual_et0_mm": 510,
            "frost_months": [], "dry_months": [6, 7, 8]}


@pytest.fixture(autouse=True)
def canned_sources(monkeypatch):
    async def richness(*a, **k):
        return {"Plantae": 398, "Insecta": 527, "Aves": 97}

    async def by_year(*a, **k):
        return {str(y): 1000 + 400 * (y - 2014) for y in range(2014, 2026)}

    async def places(*a, **k):
        return {"country": {"id": 6774, "name": "Spain"}, "region": None}

    async def reverse(*a, **k):
        return {"name": "Gràcia, Barcelona", "city": "Barcelona", "country": "Spain", "country_code": "ES"}

    monkeypatch.setattr(sources, "species_counts", _species_counts)
    monkeypatch.setattr(sources, "climate_profile", _climate)
    monkeypatch.setattr(sources, "richness_by_group", richness)
    monkeypatch.setattr(sources, "gbif_records_by_year", by_year)
    monkeypatch.setattr(sources, "inat_places", places)
    monkeypatch.setattr(sources, "reverse_geocode", reverse)
    fake_otari.SPEND.clear()


@pytest.fixture(scope="session")
def fake_base_url():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    fake_otari.DELAY = 0
    server = uvicorn.Server(uvicorn.Config(fake_otari.app, port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        try:
            httpx.get(f"http://127.0.0.1:{port}/docs", timeout=0.5)
            break
        except httpx.HTTPError:
            time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/api/v1"
    server.should_exit = True


@pytest.fixture(autouse=True)
def point_at_fake(monkeypatch, fake_base_url):
    monkeypatch.setattr(config, "OTARI_BASE_URL", fake_base_url)
    monkeypatch.setattr(config, "OTARI_API_KEY", "fake")
    monkeypatch.setattr(config, "MCP_URL", "https://wildpatch.example/mcp")
    monkeypatch.setattr(config, "GUARD_URL", "https://wildpatch.example/guard/t")


def run(goals: str = "A pollinator meadow the children can study.", **kw) -> list[dict]:
    req = pipeline.RunRequest(lat=41.4036, lon=2.1744, radius_km=2, site_name="Schoolyard", area_m2=300,
                              goals=goals, **kw)

    async def collect():
        return [ev async for ev in pipeline.Run(req).events()]

    return asyncio.run(collect())


def by_type(events, kind):
    return [e for e in events if e["type"] == kind]


def test_full_run_uses_every_stage_and_feature():
    events = run()
    assert not by_type(events, "stage_error"), by_type(events, "stage_error")
    done = {e["stage"] for e in by_type(events, "stage_done")}
    assert done == {"intake", "survey", "scout", "analysis", "plan", "structure"}
    mcp_calls = [e for e in by_type(events, "tool_call") if e["kind"] == "mcp"]
    assert {e["name"] for e in mcp_calls} == set(pipeline.SURVEY_TOOLS)
    analysis_data = next(e["value"] for e in by_type(events, "data") if e["key"] == "analysis")
    assert analysis_data["source"] == "sandbox"
    assert analysis_data["verification"]["mismatches"] == []
    result = by_type(events, "result")[0]
    assert result["palette"]["palette"] and all(p["verified_native"] for p in result["palette"]["palette"])
    assert result["scout"]["resources"][0]["verified"] is True
    card = by_type(events, "scorecard")[0]["features"]
    assert all(card[k]["lit"] for k in ("mcp", "web", "code", "routing", "guardrails", "multi_model", "per_user"))
    assert by_type(events, "allowance")[0]["status"]["spend"] > 0
    assert events[-1]["type"] == "run_done" and events[-1]["total_cost_usd"] > 0


def test_guardrail_refusal_stops_the_run():
    events = run("Where exactly can I find the wild orchids nearby so we can dig up a few for the garden?")
    blocked = by_type(events, "blocked")
    assert blocked and blocked[0]["reason"] == "guardrail" and blocked[0]["stage"] == "intake"
    assert not by_type(events, "result")
    assert events[-1]["type"] == "run_done"


def test_guest_budget_runs_out():
    events = run(volunteer="guest")
    blocked = by_type(events, "blocked")
    assert blocked and blocked[0]["reason"] == "budget"
    assert blocked[0]["error"]["retry_after"] == 18000 and blocked[0]["error"]["budget_scope"] == "user"


def test_outage_drill_reports_fallback():
    events = run(drill=True)
    routing = by_type(events, "routing")[0]
    assert routing["fallback"] is True and routing["attempts"] == 2 and routing["policy"] == "wildpatch-drill"


def test_waf_safe_unwraps_inline_calls_only():
    assert pipeline.waf_safe("call `total([1, 2])` in `app/calc.py`") == "call total([1, 2]) in `app/calc.py`"


def test_otari_error_from_html_page():
    r = httpx.Response(403, headers={"server": "awselb/2.0"}, text="<html><body>403 Forbidden</body></html>")
    err = OtariError.from_response(r, r.content)
    assert "HTML 403" in err.detail and "awselb" in err.detail


def test_otari_error_budget_fields():
    r = httpx.Response(403, headers={"Otari-Error-Code": "budget_exceeded", "Otari-Budget-Scope": "user",
                                     "Retry-After": "60"}, json={"detail": "User 'x' has exceeded budget limit"})
    err = OtariError.from_response(r, r.content)
    assert (err.code, err.budget_scope, err.retry_after) == ("budget_exceeded", "user", 60)


def test_local_analysis_matches_itself():
    ds = {"site": {"planted_area_m2": 100}, "records_by_year": {"2020": 10, "2021": 20}, "native_total": 3,
          "introduced_total": 1, "native_plants": {"a": 5, "b": 5}, "pollinators": {"x": 1},
          "climate_monthly": {"et0_mm": [100] * 12, "precip_mm": [10] * 12, "tmin_c": [0] * 12, "tmax_c": [0] * 12}}
    local = analysis.compute(ds)
    assert local["diversity"]["plants"]["shannon"] == round(__import__("math").log(2), 3)
    assert local["water"]["establishment_year_litres"] == round(12 * (50 - 7.5) * 100, 3)
    assert local["records_growth_pct_per_year"] == 100.0
    assert analysis.verify(local, local)["mismatches"] == []
