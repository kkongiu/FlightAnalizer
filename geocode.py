import json
import re
from pathlib import Path

import httpx

GEO_CACHE_FILE = Path(__file__).parent / "data" / "geocache.json"
_cache = None


def _load_cache() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(GEO_CACHE_FILE.read_text())
        except Exception:
            _cache = {}
    return _cache


def _save_cache(cache: dict):
    try:
        GEO_CACHE_FILE.write_text(json.dumps(cache))
    except Exception:
        pass


def home_coords(points):
    """Return the first valid GPS fix (lat, lon) used as the flight location."""
    for p in points:
        if abs(p.lat) > 0.001 and abs(p.lon) > 0.001 and p.sats >= 5:
            return p.lat, p.lon
    for p in points:
        if abs(p.lat) > 0.001 and abs(p.lon) > 0.001:
            return p.lat, p.lon
    return None, None


def reverse_geocode(lat: float, lon: float) -> str | None:
    """Look up a readable place name for coordinates (cached on disk)."""
    if not lat or not lon:
        return None
    cache = _load_cache()
    key = f"{round(lat, 4)},{round(lon, 4)}"
    if key in cache:
        return cache[key] or None
    place = None
    try:
        r = httpx.get(
            "https://photon.komoot.io/reverse",
            params={"lat": lat, "lon": lon},
            headers={"User-Agent": "PocketLogAnalyzer/1.0 (self-hosted)"},
            timeout=5.0,
        )
        if r.status_code == 200:
            features = (r.json().get("features") or [{}])
            props = (features[0] if features else {}).get("properties") or {}
            place = (props.get("city") or props.get("town") or props.get("village")
                     or props.get("hamlet") or props.get("district")
                     or props.get("suburb") or props.get("locality")
                     or props.get("name") or props.get("county") or props.get("state"))
            if place:
                place = re.sub(r"[^\w\-. ]+", "", str(place)).strip()
                place = re.sub(r"\s+", "_", place)[:40]
    except Exception:
        place = None
    if place:
        cache[key] = place
        _save_cache(cache)
    return place
