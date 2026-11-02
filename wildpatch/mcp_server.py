"""Wildpatch Biodiversity MCP server.

Registered in Otari as a workspace MCP server, so the planning models reach open biodiversity data
through Otari's managed MCP loop. Read-only tools over iNaturalist, GBIF and Open-Meteo.

Data ethics: no tool ever returns coordinates of individual observations, and threatened species
are reported by name and status only, so the model has nothing to leak to a would-be poacher.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from . import sources

mcp = MCPServer(
    name="wildpatch-biodiversity",
    instructions=("Open biodiversity data for a site: species observed nearby (native and introduced), "
                  "pollinators, threatened species (names only), records over time and climate normals."),
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)

# Recent calls, newest last, shown in the app's "MCP inbox" so a demo can prove Otari reached this server.
CALL_LOG: deque[dict] = deque(maxlen=50)


def _clamp_radius(radius_km: float, hi: float = 10.0) -> float:
    return round(min(max(float(radius_km), 0.2), hi), 2)


async def _logged(tool: str, args: dict, coro):
    started = time.time()
    entry = {"tool": tool, "args": args, "at": started}
    try:
        result = await coro
        entry.update(ok=True, ms=int((time.time() - started) * 1000))
    except sources.SourceError as e:
        entry.update(ok=False, error=str(e), ms=int((time.time() - started) * 1000))
        result = {"error": str(e)}
    finally:
        CALL_LOG.append(entry)
    # Compact JSON: the SDK would pretty-print a dict, and every byte here is a model input token.
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


async def _country_place_id(lat: float, lon: float) -> tuple[int | None, str | None]:
    places = await sources.inat_places(lat, lon)
    country = places.get("country") or {}
    return country.get("id"), country.get("name")


@mcp.tool(annotations=READ_ONLY)
async def locate_site(place: str) -> str:
    """Find a place by name or address. Returns its name, latitude, longitude and country."""
    return await _logged("locate_site", {"place": place}, sources.geocode(place))


@mcp.tool(annotations=READ_ONLY)
async def biodiversity_snapshot(lat: float, lon: float, radius_km: float = 2.0) -> str:
    """Species richness around a site by group (plants, insects, birds, ...) from research-grade
    iNaturalist observations, plus GBIF occurrence records per year for the last 12 complete years."""
    radius_km = _clamp_radius(radius_km)

    async def run():
        richness, by_year = await asyncio.gather(sources.richness_by_group(lat, lon, radius_km),
                                                 sources.gbif_records_by_year(lat, lon, radius_km),
                                                 return_exceptions=True)
        if isinstance(richness, Exception):
            raise richness
        note = "Counts reflect recording effort as well as biodiversity."
        if isinstance(by_year, Exception):  # GBIF has outages of its own; richness alone is still useful
            by_year, note = {}, f"{note} GBIF records over time are unavailable right now ({by_year})."
        return {"radius_km": radius_km, "species_by_group": richness,
                "total_species": sum(richness.values()), "gbif_records_by_year": by_year, "note": note}

    return await _logged("biodiversity_snapshot", {"lat": lat, "lon": lon, "radius_km": radius_km}, run())


@mcp.tool(annotations=READ_ONLY)
async def native_species(lat: float, lon: float, radius_km: float = 2.0, group: str = "plants",
                         limit: int = 25) -> str:
    """Species native to this country that have been observed near the site, most-recorded first.
    group: plants, insects, birds, mammals, reptiles, amphibians, fungi, spiders or molluscs.
    Use this to choose plants that already thrive locally."""
    radius_km = _clamp_radius(radius_km)

    async def run():
        place_id, country = await _country_place_id(lat, lon)
        if not place_id:
            return {"error": "native status is not available for this location"}
        total, rows = await sources.species_counts(lat, lon, radius_km, iconic=sources.group_name(group),
                                                   place_id=place_id, native=True, limit=min(limit, 50))
        return {"native_to": country, "group": group, "radius_km": radius_km,
                "native_species_total": total, "species": rows}

    return await _logged("native_species", {"lat": lat, "lon": lon, "radius_km": radius_km, "group": group}, run())


@mcp.tool(annotations=READ_ONLY)
async def introduced_species(lat: float, lon: float, radius_km: float = 2.0, group: str = "plants",
                             limit: int = 15) -> str:
    """Introduced (non-native) species observed near the site, most-recorded first: candidates to avoid
    planting, or to manage if they are invasive."""
    radius_km = _clamp_radius(radius_km)

    async def run():
        place_id, country = await _country_place_id(lat, lon)
        if not place_id:
            return {"error": "native status is not available for this location"}
        total, rows = await sources.species_counts(lat, lon, radius_km, iconic=sources.group_name(group),
                                                   place_id=place_id, introduced=True, limit=min(limit, 50))
        return {"introduced_to": country, "group": group, "radius_km": radius_km,
                "introduced_species_total": total, "species": rows}

    return await _logged("introduced_species", {"lat": lat, "lon": lon, "radius_km": radius_km, "group": group},
                         run())


@mcp.tool(annotations=READ_ONLY)
async def pollinators(lat: float, lon: float, radius_km: float = 2.0, limit: int = 20) -> str:
    """Bees, butterflies & moths and hoverflies observed near the site, most-recorded first."""
    radius_km = _clamp_radius(radius_km)

    async def run():
        total, rows = await sources.species_counts(lat, lon, radius_km, taxon_ids=",".join(sources.POLLINATOR_TAXA),
                                                   limit=min(limit, 50))
        return {"groups": list(sources.POLLINATOR_TAXA.values()), "radius_km": radius_km,
                "pollinator_species_total": total, "species": rows}

    return await _logged("pollinators", {"lat": lat, "lon": lon, "radius_km": radius_km}, run())


@mcp.tool(annotations=READ_ONLY)
async def sensitive_species(lat: float, lon: float, radius_km: float = 5.0) -> str:
    """Threatened species recorded in the wider area, by name and status only. Locations are withheld
    on purpose: use this to plan habitat that helps them, never to find them."""
    radius_km = _clamp_radius(radius_km, hi=25.0)

    async def run():
        place_id, _ = await _country_place_id(lat, lon)
        total, rows = await sources.species_counts(lat, lon, radius_km, place_id=place_id, threatened=True, limit=15)
        for r in rows:
            r.pop("photo", None)
        return {"radius_km": radius_km, "threatened_species_total": total, "species": rows,
                "locations": "withheld to protect sensitive species from collection and disturbance"}

    return await _logged("sensitive_species", {"lat": lat, "lon": lon, "radius_km": radius_km}, run())


@mcp.tool(annotations=READ_ONLY)
async def climate_profile(lat: float, lon: float) -> str:
    """Monthly climate normals for the site (last 10 complete years, ERA5): mean max/min temperature,
    precipitation, reference evapotranspiration (ET0) and frost days, plus frost and dry months."""
    return await _logged("climate_profile", {"lat": lat, "lon": lon}, sources.climate_profile(lat, lon))
