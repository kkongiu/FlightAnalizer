"""INAV waypoint mission builder from a GPS track.

Converts a recorded track into a waypoint mission compatible with INAV
Configurator / mwp XML mission files.

Coordinate format used throughout is the flight "coordinates" row:
    [lat, lon, alt, ...]   (only the first 3 fields are used)
"""
import math
from datetime import datetime, timezone

ACTIONS = {
    "WAYPOINT": 1,
    "POSHOLD_TIME": 3,
    "RTH": 4,
    "SET_POI": 5,
    "JUMP": 6,
    "SET_HEAD": 7,
    "LAND": 8,
}
MAX_WAYPOINTS = 120

EARTH_R = 6371000.0
M_PER_DEG_LAT = 111320.0


def haversine_m(lat1, lon1, lat2, lon2):
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return 2 * EARTH_R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def clean_coords(coords):
    """Return [[lat, lon, alt], ...] dropping invalid GPS points and
    consecutive duplicates. This is the array the editor and the exporter both
    operate on."""
    out = []
    for c in coords:
        lat, lon = c[0], c[1]
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        if abs(lat) < 0.001 and abs(lon) < 0.001:
            continue
        if out and lat == out[-1][0] and lon == out[-1][1]:
            continue
        out.append([lat, lon, c[2]])
    return out


def _perpendicular_m(lat1, lon1, lat2, lon2, plat, plon):
    lat_m = M_PER_DEG_LAT
    lon_m = M_PER_DEG_LAT * math.cos(math.radians((lat1 + lat2 + plat) / 3.0))
    x1, y1 = lon1 * lon_m, lat1 * lat_m
    x2, y2 = lon2 * lon_m, lat2 * lat_m
    xp, yp = plon * lon_m, plat * lat_m
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(xp - x1, yp - y1)
    t = ((xp - x1) * dx + (yp - y1) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(xp - (x1 + t * dx), yp - (y1 + t * dy))


def _dp_indices(coords, tolerance_m):
    """Ramer-Douglas-Peucker over lat/lon, returning kept indices."""
    n = len(coords)
    if n < 2:
        return list(range(n))
    keep = [False] * n
    keep[0] = keep[n - 1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        max_d, max_i = -1.0, -1
        lat1, lon1 = coords[a][0], coords[a][1]
        lat2, lon2 = coords[b][0], coords[b][1]
        for i in range(a + 1, b):
            d = _perpendicular_m(lat1, lon1, lat2, lon2, coords[i][0], coords[i][1])
            if d > max_d:
                max_d, max_i = d, i
        if max_d > tolerance_m:
            keep[max_i] = True
            stack.append((a, max_i))
            stack.append((max_i, b))
    return [i for i, k in enumerate(keep) if k]


def _track_length_m(coords):
    return sum(haversine_m(coords[i - 1][0], coords[i - 1][1], coords[i][0], coords[i][1])
               for i in range(1, len(coords)))


def simplify_indices(coords, max_points):
    """Return indices of up to max_points representative points.

    Uses Douglas-Peucker with a tolerance found by binary search so the result
    fits within max_points, with a uniform decimation fallback."""
    n = len(coords)
    if max_points < 2:
        max_points = 2
    if n <= max_points:
        return list(range(n))
    total = _track_length_m(coords)
    lo, hi = 0.0, max(total, 1.0)
    idxs = list(range(n))
    for _ in range(50):
        mid = (lo + hi) / 2.0
        cand = _dp_indices(coords, mid)
        if len(cand) <= max_points:
            idxs = cand
            hi = mid
        else:
            lo = mid
    if len(idxs) > max_points:
        step = (n - 1) / (max_points - 1)
        idxs = sorted({round(i * step) for i in range(max_points)})
    return idxs


def cut_coords(coords, start_frac=0.0, end_frac=1.0,
               mid_start_frac=None, mid_end_frac=None):
    """Return a sliced copy of coords. Fractions are in [0, 1].

    First keeps the range [start, end], then optionally removes the segment
    [mid_start, mid_end] from the resulting track."""
    if not coords:
        return []
    n = len(coords)
    s = round(max(0.0, min(1.0, float(start_frac))) * (n - 1))
    e = round(max(0.0, min(1.0, float(end_frac))) * (n - 1))
    if e < s:
        s, e = e, s
    out = list(coords[s:e + 1])
    if mid_start_frac is not None and mid_end_frac is not None:
        m1 = max(0.0, min(1.0, float(mid_start_frac)))
        m2 = max(0.0, min(1.0, float(mid_end_frac)))
        if m1 != m2:
            if m1 > m2:
                m1, m2 = m2, m1
            a = round(m1 * (len(out) - 1))
            b = round(m2 * (len(out) - 1))
            if b > a:
                out = out[:a + 1] + out[b:]
    return out


def build_waypoints(coords, alt_mode="fixed", alt_value=50.0, relative=True,
                    cruise_speed=1000, final_action="RTH"):
    """Convert track points into INAV waypoint dicts.

    alt_mode: 'fixed' (use alt_value) | 'track' (recorded alt) | 'offset'
    relative: altitude relative to home (first point of the track) for
        track/offset modes; fixed values are always "above home" when relative.
    final_action: 'RTH' | 'LAND' | 'NONE' appended after the last waypoint.
    """
    if not coords:
        return []
    home_alt = coords[0][2]
    wps = []
    for c in coords:
        lat, lon, alt = float(c[0]), float(c[1]), float(c[2])
        if alt_mode == "fixed":
            a = float(alt_value)
        elif alt_mode == "offset":
            a = alt + float(alt_value)
        else:
            a = alt
        if relative and alt_mode in ("track", "offset"):
            a = a - home_alt
        wps.append({
            "action": "WAYPOINT",
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "alt": round(a, 1),
            "p1": int(cruise_speed),
            "p2": 0,
            "p3": 0 if relative else 1,
        })
    if final_action and final_action != "NONE":
        fa = final_action.upper()
        if fa == "RTH":
            wps.append({"action": "RTH", "lat": 0.0, "lon": 0.0, "alt": 0.0,
                        "p1": 1, "p2": 0, "p3": 0})
        elif fa == "LAND":
            wps.append({"action": "LAND", "lat": 0.0, "lon": 0.0, "alt": 0.0,
                        "p1": 0, "p2": 0, "p3": 0})
    for i, w in enumerate(wps):
        w["no"] = i + 1
        w["flag"] = 165 if i == len(wps) - 1 else 0
    return wps


def build_mission_from_params(coords, params):
    """Full pipeline: clean -> cut -> simplify -> waypoints."""
    coords = clean_coords(coords)
    cut = params.get("cut") or {}
    mid = params.get("mid") or {}
    coords = cut_coords(
        coords,
        cut.get("start", 0.0),
        cut.get("end", 1.0),
        mid.get("start") if mid.get("start") is not None else None,
        mid.get("end") if mid.get("end") is not None else None,
    )
    max_points = int(params.get("max_points", 60))
    idxs = simplify_indices(coords, max_points)
    chosen = [coords[i] for i in idxs]
    return build_waypoints(
        chosen,
        alt_mode=params.get("alt_mode", "fixed"),
        alt_value=float(params.get("alt_value", 50)),
        relative=bool(params.get("relative", True)),
        cruise_speed=int(params.get("cruise_speed", 1000)),
        final_action=params.get("final_action", "RTH"),
    )


def validate_waypoints(waypoints):
    """Return (ok, error)."""
    if not waypoints:
        return False, "Nessun waypoint nella missione."
    if len(waypoints) > MAX_WAYPOINTS:
        return False, f"Troppi waypoint ({len(waypoints)}); massimo {MAX_WAYPOINTS}."
    for i, w in enumerate(waypoints, 1):
        action = str(w.get("action", "WAYPOINT")).upper()
        if action not in ACTIONS:
            return False, f"Waypoint {i}: azione non valida '{action}'."
        if action in ("WAYPOINT", "POSHOLD_TIME", "LAND"):
            lat, lon = w.get("lat", 0), w.get("lon", 0)
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                return False, f"Waypoint {i}: coordinate non valide."
        alt = w.get("alt", 0)
        if not (-2000 <= alt <= 10000):
            return False, f"Waypoint {i}: quota non valida ({alt} m)."
    return True, None


def track_meta(coords):
    """Compute mwp metadata (centre + home) from a track of [lat, lon, ...]."""
    valid = [[c[0], c[1]] for c in coords
             if -90 <= c[0] <= 90 and -180 <= c[1] <= 180
             and (abs(c[0]) > 0.001 or abs(c[1]) > 0.001)]
    if not valid:
        return {}
    lats = [c[0] for c in valid]
    lons = [c[1] for c in valid]
    return {
        "cx": round((min(lons) + max(lons)) / 2, 6),
        "cy": round((min(lats) + max(lats)) / 2, 6),
        "home_x": round(valid[0][1], 6),
        "home_y": round(valid[0][0], 6),
        "zoom": 14,
    }


def _coord_str(v):
    return "0" if v == 0 else "%.6f" % v


def render_mission_xml(waypoints, meta=None):
    """Render INAV mission XML (mwp / INAV Configurator format)."""
    meta = meta or {}
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<mission>',
             ' <version value="2.3-pre8"/>']
    if meta.get("cx") is not None:
        lines.append(
            ' <mwp cx="%.6f" cy="%.6f" home-x="%.6f" home-y="%.6f" zoom="%s" '
            'save-date="%s" generator="pocket-log-analyzer"/>'
            % (meta["cx"], meta["cy"], meta.get("home_x", 0), meta.get("home_y", 0),
               meta.get("zoom", 15), datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
    for w in waypoints:
        attrs = [
            f'no="{w["no"]}"',
            f'action="{w["action"]}"',
            f'lat="{_coord_str(w.get("lat", 0))}"',
            f'lon="{_coord_str(w.get("lon", 0))}"',
            f'alt="{int(round(w.get("alt", 0)))}"',
            f'parameter1="{int(w.get("p1", 0))}"',
            f'parameter2="{int(w.get("p2", 0))}"',
            f'parameter3="{int(w.get("p3", 0))}"',
            f'flag="{w.get("flag", 0)}"',
        ]
        lines.append(' <missionitem ' + ' '.join(attrs) + '/>')
    lines.append('</mission>')
    return '\n'.join(lines)
