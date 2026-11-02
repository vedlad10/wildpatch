"""Open biodiversity and climate data behind the Wildpatch MCP server.

Every source is public and keyless: iNaturalist (observations, native status, threat status),
GBIF (occurrence records over time), Open-Meteo (ERA5 climate) and OpenStreetMap Nominatim (places).
Results are cached in memory because a planning run asks for the same site several times.
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Any

import httpx

from .net import tls

USER_AGENT = "Wildpatch/0.1 (rewilding planner; https://github.com/wildpatch)"
INAT = "https://api.inaturalist.org/v1"
GBIF = "https://api.gbif.org/v1"
NOMINATIM = "https://nominatim.openstreetmap.org"
OPEN_METEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"

CACHE_TTL_S = 6 * 3600

# iNaturalist "iconic taxa", with the everyday words people (and models) use for them.
GROUPS = {
    "plants": "Plantae", "insects": "Insecta", "birds": "Aves", "mammals": "Mammalia",
    "reptiles": "Reptilia", "amphibians": "Amphibia", "fungi": "Fungi", "spiders": "Arachnida",
    "molluscs": "Mollusca",
}
# Bees, butterflies & moths, hoverflies (iNaturalist taxon ids, verified against /v1/taxa).
POLLINATOR_TAXA = {"630955": "bees", "47157": "butterflies & moths", "49995": "hoverflies"}

_cache: dict[tuple, tuple[float, Any]] = {}
_nominatim_lock = asyncio.Lock()  # Nominatim's policy: at most one request per second


class SourceError(RuntimeError):
    """An upstream open-data service failed; the message is safe to show a model."""


def group_name(group: str) -> str:
    """Accept 'plants', 'Plantae', 'Plants'... and return the iNaturalist iconic taxon name."""
    g = group.strip()
    if g in GROUPS.values():
        return g
    key = g.lower().rstrip("s") + "s"
    if key in GROUPS:
        return GROUPS[key]
    if g.lower() in GROUPS:
        return GROUPS[g.lower()]
    raise SourceError(f"unknown group '{group}'; use one of {sorted(GROUPS)}")


async def _get_json(url: str, params: dict[str, Any] | None = None, *, timeout: float = 20.0) -> Any:
    key = (url, tuple(sorted((params or {}).items())))
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL_S:
        return hit[1]
    async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": USER_AGENT}, verify=tls()) as client:
        for attempt in range(3):
            try:
                r = await client.get(url, params=params)
            except httpx.HTTPError as e:
                if attempt == 2:
                    raise SourceError(f"{url.split('/')[2]} unreachable: {type(e).__name__}") from e
            else:
                if r.status_code in (429, 502, 503, 504) and attempt < 2:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                if r.status_code >= 400:
                    raise SourceError(f"{url.split('/')[2]} answered HTTP {r.status_code}")
                data = r.json()
                _cache[key] = (time.monotonic(), data)
                return data
            await asyncio.sleep(1.0 * (attempt + 1))
    raise SourceError(f"{url.split('/')[2]} unavailable")


# ---------------------------------------------------------------- places

async def geocode(query: str) -> dict:
    async with _nominatim_lock:
        data = await _get_json(f"{NOMINATIM}/search", {"q": query, "format": "jsonv2", "limit": 1,
                                                         "addressdetails": 1, "accept-language": "en"})
        await asyncio.sleep(1.0)
    if not data:
        raise SourceError(f"no place found for '{query}'")
    hit = data[0]
    addr = hit.get("address") or {}
    return {"name": hit.get("display_name"), "lat": round(float(hit["lat"]), 5),
            "lon": round(float(hit["lon"]), 5), "country": addr.get("country"),
            "country_code": (addr.get("country_code") or "").upper(),
            "city": addr.get("city") or addr.get("town") or addr.get("village") or addr.get("county")}


async def reverse_geocode(lat: float, lon: float) -> dict:
    async with _nominatim_lock:
        data = await _get_json(f"{NOMINATIM}/reverse", {"lat": round(lat, 5), "lon": round(lon, 5),
                                                          "format": "jsonv2", "zoom": 14,
                                                          "accept-language": "en"})
        await asyncio.sleep(1.0)
    addr = data.get("address") or {}
    return {"name": data.get("display_name"), "country": addr.get("country"),
            "country_code": (addr.get("country_code") or "").upper(),
            "city": addr.get("city") or addr.get("town") or addr.get("village") or addr.get("county")}


async def inat_places(lat: float, lon: float) -> dict:
    """The iNaturalist country and region places containing a point (native status is defined per place)."""
    d = 0.01
    data = await _get_json(f"{INAT}/places/nearby", {"nelat": lat + d, "nelng": lon + d, "swlat": lat - d,
                                                      "swlng": lon - d, "per_page": 20})
    out: dict[str, Any] = {"country": None, "region": None}
    for p in (data.get("results") or {}).get("standard") or []:
        if p.get("admin_level") == 0 and not out["country"]:
            out["country"] = {"id": p["id"], "name": p["name"]}
        elif p.get("admin_level") == 10 and not out["region"]:
            out["region"] = {"id": p["id"], "name": p["name"]}
    return out


# ---------------------------------------------------------------- species

def _species_row(r: dict) -> dict:
    t = r["taxon"]
    cons = t.get("conservation_status") or {}
    row = {"scientific_name": t["name"], "common_name": t.get("preferred_common_name"),
           "group": t.get("iconic_taxon_name"), "observations": r["count"], "inat_taxon_id": t["id"]}
    photo = (t.get("default_photo") or {}).get("square_url")
    if photo:
        row["photo"] = photo
    if cons.get("status_name") or cons.get("status"):
        row["conservation_status"] = cons.get("status_name") or cons.get("status")
    return row


async def species_counts(lat: float, lon: float, radius_km: float, *, iconic: str | None = None,
                         taxon_ids: str | None = None, place_id: int | None = None,
                         native: bool | None = None, introduced: bool | None = None,
                         threatened: bool | None = None, limit: int = 25) -> tuple[int, list[dict]]:
    params: dict[str, Any] = {"lat": lat, "lng": lon, "radius": radius_km, "quality_grade": "research",
                              "per_page": max(1, min(limit, 100))}
    if iconic:
        params["iconic_taxa"] = iconic
    if taxon_ids:
        params["taxon_id"] = taxon_ids
    if place_id:
        params["place_id"] = place_id
    for flag, value in (("native", native), ("introduced", introduced), ("threatened", threatened)):
        if value is not None:
            params[flag] = str(value).lower()
    data = await _get_json(f"{INAT}/observations/species_counts", params)
    return data.get("total_results", 0), [_species_row(r) for r in data.get("results", [])]


async def richness_by_group(lat: float, lon: float, radius_km: float) -> dict[str, int]:
    data = await _get_json(f"{INAT}/observations/iconic_taxa_species_counts",
                           {"lat": lat, "lng": lon, "radius": radius_km, "quality_grade": "research"})
    return {r["taxon"]["name"]: r["count"] for r in data.get("results", [])}


async def gbif_records_by_year(lat: float, lon: float, radius_km: float, years: int = 12) -> dict[str, int]:
    data = await _get_json(f"{GBIF}/occurrence/search", {"geoDistance": f"{lat},{lon},{radius_km}km",
                                                         "limit": 0, "facet": "year", "facetLimit": 60})
    this_year = time.gmtime().tm_year
    counts = {int(c["name"]): c["count"] for f in data.get("facets", []) for c in f.get("counts", [])}
    # The current year is incomplete, so the series stops at last year.
    return {str(y): counts.get(y, 0) for y in range(this_year - years, this_year)}


# ---------------------------------------------------------------- climate

async def climate_profile(lat: float, lon: float, years: int = 10) -> dict:
    """Monthly climate normals from ERA5 reanalysis for the last `years` complete years."""
    end = time.gmtime().tm_year - 1
    data = await _get_json(OPEN_METEO_ARCHIVE, {
        "latitude": round(lat, 2), "longitude": round(lon, 2),
        "start_date": f"{end - years + 1}-01-01", "end_date": f"{end}-12-31", "timezone": "auto",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,et0_fao_evapotranspiration",
    }, timeout=40.0)
    daily = data["daily"]
    sums: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    days: dict[int, int] = defaultdict(int)
    for i, day in enumerate(daily["time"]):
        m = int(day[5:7])
        tmax, tmin = daily["temperature_2m_max"][i], daily["temperature_2m_min"][i]
        if tmax is None or tmin is None:
            continue
        days[m] += 1
        sums[m]["tmax"] += tmax
        sums[m]["tmin"] += tmin
        sums[m]["precip"] += daily["precipitation_sum"][i] or 0.0
        sums[m]["et0"] += daily["et0_fao_evapotranspiration"][i] or 0.0
        sums[m]["frost_days"] += 1 if tmin < 0 else 0
    months = []
    for m in range(1, 13):
        n = days[m] or 1
        months.append({"month": m, "tmax_c": round(sums[m]["tmax"] / n, 1), "tmin_c": round(sums[m]["tmin"] / n, 1),
                       "precip_mm": round(sums[m]["precip"] / years, 1), "et0_mm": round(sums[m]["et0"] / years, 1),
                       "frost_days": round(sums[m]["frost_days"] / years, 1)})
    return {"period": f"{end - years + 1}-{end}", "source": "ERA5 via Open-Meteo",
            "elevation_m": data.get("elevation"), "monthly": months,
            "annual_precip_mm": round(sum(x["precip_mm"] for x in months)),
            "annual_et0_mm": round(sum(x["et0_mm"] for x in months)),
            "frost_months": [x["month"] for x in months if x["frost_days"] >= 1],
            "dry_months": [x["month"] for x in months if x["et0_mm"] > 2 * x["precip_mm"]]}
