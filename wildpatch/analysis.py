"""The survey dataset handed to the code sandbox, and a local re-computation that checks its answer.

The analyst model writes and runs the analysis in Otari's sandbox; the same formulas run here so the
UI can mark the sandbox's numbers as verified rather than taking a model's word for them.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

ZONES = {"meadow": 0.55, "shrub": 0.30, "tree": 0.15}
RESULT_RE = re.compile(r"WILDPATCH_RESULT=(\{.*\})")


def build_dataset(mcp_results: dict[str, Any], *, lat: float, lon: float, radius_km: float,
                  area_m2: float) -> dict:
    """Compact, analysis-ready view of what the MCP tools returned (only data that came through MCP)."""
    snap = mcp_results.get("biodiversity_snapshot") or {}
    native = mcp_results.get("native_species") or {}
    intro = mcp_results.get("introduced_species") or {}
    poll = mcp_results.get("pollinators") or {}
    climate = mcp_results.get("climate_profile") or {}
    monthly = climate.get("monthly") or []
    return {
        "site": {"lat": lat, "lon": lon, "radius_km": radius_km, "planted_area_m2": area_m2},
        "richness_by_group": snap.get("species_by_group") or {},
        "records_by_year": snap.get("gbif_records_by_year") or {},
        "native_plants": {s["scientific_name"]: s["observations"] for s in native.get("species") or []},
        "native_total": native.get("native_species_total") or 0,
        "introduced_total": intro.get("introduced_species_total") or 0,
        "pollinators": {s["scientific_name"]: s["observations"] for s in poll.get("species") or []},
        "climate_monthly": {
            "precip_mm": [m["precip_mm"] for m in monthly],
            "et0_mm": [m["et0_mm"] for m in monthly],
            "tmin_c": [m["tmin_c"] for m in monthly],
            "tmax_c": [m["tmax_c"] for m in monthly],
        },
    }


def _diversity(counts: dict[str, int]) -> dict:
    values = [c for c in counts.values() if c and c > 0]
    total, s = sum(values), len(values)
    if not total:
        return {"richness": 0, "shannon": 0.0, "pielou": 0.0, "simpson": 0.0}
    ps = [c / total for c in values]
    h = -sum(p * math.log(p) for p in ps)
    return {"richness": s, "shannon": round(h, 3), "pielou": round(h / math.log(s), 3) if s > 1 else 0.0,
            "simpson": round(1 - sum(p * p for p in ps), 3)}


def compute(dataset: dict) -> dict:
    """The analysis the sandbox is asked to run, computed locally with the same formulas."""
    native_total, intro_total = dataset["native_total"], dataset["introduced_total"]
    years = [(int(y), n) for y, n in dataset["records_by_year"].items() if n > 0]
    growth = None
    if len(years) >= 2:
        xs, ys = [y for y, _ in years], [math.log(n) for _, n in years]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        den = sum((x - mx) ** 2 for x in xs)
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den else 0.0
        growth = round(100 * (math.exp(slope) - 1), 3)
    area = dataset["site"]["planted_area_m2"]
    cm = dataset["climate_monthly"]
    deficit = [round(max(0.0, 0.5 * e - 0.75 * p), 3) for e, p in zip(cm["et0_mm"], cm["precip_mm"])]
    litres = [round(d * area, 3) for d in deficit]
    meadow, shrub, tree = (round(area * ZONES[z], 3) for z in ("meadow", "shrub", "tree"))
    return {
        "diversity": {"plants": _diversity(dataset["native_plants"]),
                      "pollinators": _diversity(dataset["pollinators"])},
        "native_share_pct": round(100 * native_total / (native_total + intro_total), 3)
        if native_total + intro_total else None,
        "records_growth_pct_per_year": growth,
        "water": {"monthly_deficit_mm": deficit, "litres_by_month": litres,
                  "dry_months": [i + 1 for i, d in enumerate(deficit) if d > 0],
                  "establishment_year_litres": round(sum(litres), 3)},
        "planting": {"meadow_m2": meadow, "meadow_plugs": round(meadow * 6), "shrub_m2": shrub,
                     "shrubs": round(shrub / 1.5), "tree_m2": tree, "trees": max(1, round(tree / 25))},
    }


def parse_sandbox_result(stdout: str) -> dict | None:
    for line in reversed(stdout.splitlines()):
        m = RESULT_RE.search(line)
        if m:
            try:
                return json.loads(m.group(1))
            except ValueError:
                return None
    return None


def verify(sandbox: dict, local: dict, rel_tol: float = 0.02) -> dict:
    """Compare the sandbox's numbers with the local ones; returns {checked, mismatches}."""
    checks = {
        "plants.shannon": (sandbox.get("diversity", {}).get("plants", {}).get("shannon"),
                           local["diversity"]["plants"]["shannon"]),
        "pollinators.shannon": (sandbox.get("diversity", {}).get("pollinators", {}).get("shannon"),
                                local["diversity"]["pollinators"]["shannon"]),
        "native_share_pct": (sandbox.get("native_share_pct"), local["native_share_pct"]),
        "establishment_year_litres": (sandbox.get("water", {}).get("establishment_year_litres"),
                                      local["water"]["establishment_year_litres"]),
        "meadow_plugs": (sandbox.get("planting", {}).get("meadow_plugs"), local["planting"]["meadow_plugs"]),
        "shrubs": (sandbox.get("planting", {}).get("shrubs"), local["planting"]["shrubs"]),
    }
    mismatches = []
    for name, (got, want) in checks.items():
        if want is None:
            continue
        try:
            ok = math.isclose(float(got), float(want), rel_tol=rel_tol, abs_tol=0.01)
        except (TypeError, ValueError):
            ok = False
        if not ok:
            mismatches.append({"metric": name, "sandbox": got, "local": want})
    return {"checked": len(checks), "mismatches": mismatches}
