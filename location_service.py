from typing import Any, Dict, List
import aiohttp

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"

async def search_locations(session: aiohttp.ClientSession, query: str, count: int = 5) -> List[Dict[str, Any]]:
    q = (query or "").strip()
    if len(q) < 2:
        raise ValueError("Enter a city, postal code, or place name.")
    params = {"name": q, "count": max(1, min(count, 10)), "language": "en", "format": "json"}
    async with session.get(GEOCODING_URL, params=params, timeout=aiohttp.ClientTimeout(total=12)) as r:
        if r.status != 200:
            raise RuntimeError("Location search is unavailable right now.")
        data = await r.json()
    out = []
    for item in data.get("results") or []:
        parts = [item.get("name"), item.get("admin1"), item.get("country")]
        display = ", ".join(str(x) for x in parts if x)
        out.append({
            "query": q, "display_name": display, "latitude": float(item["latitude"]),
            "longitude": float(item["longitude"]), "country_code": item.get("country_code"),
            "admin1": item.get("admin1"), "timezone": item.get("timezone") or "UTC",
        })
    if not out:
        raise ValueError("I couldn't find that location. Try adding a region or country.")
    return out
