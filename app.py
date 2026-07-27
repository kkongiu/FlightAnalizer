import os
import re
import secrets
from pathlib import Path
from datetime import datetime
from collections import defaultdict
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as xml_escape
from fastapi import FastAPI, Request, UploadFile, File, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from parser import parse_log
from analyzer import analyze
from database import save_flight, get_all_flights, get_flight, delete_flight, update_notes, update_flight_track

USER = os.environ.get("POCKET_USER")
PASS = os.environ.get("POCKET_PASS")
if not USER or not PASS:
    raise RuntimeError("POCKET_USER and POCKET_PASS environment variables must be set")

SECRET_FILE = Path(__file__).parent / "data" / ".session_secret"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB


def _get_session_secret() -> str:
    SECRET_FILE.parent.mkdir(exist_ok=True)
    if SECRET_FILE.exists():
        return SECRET_FILE.read_text().strip()
    secret = secrets.token_hex(32)
    SECRET_FILE.write_text(secret)
    return secret


def _safe_filename(name: str) -> str:
    name = Path(name).name
    name = re.sub(r'[^\w\-.]', '_', name)
    return name or "unknown.csv"


def _xml_escape(s: str) -> str:
    return xml_escape(str(s))


def dict2str(d):
    if not d:
        return "-"
    return ", ".join(f"{k}: {v}" for k, v in sorted(d.items()))


app = FastAPI(title="Pocket Log Analyzer")
app.add_middleware(
    SessionMiddleware,
    secret_key=_get_session_secret(),
    same_site="lax",
    https_only=True,
    session_cookie="session_token",
)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["dict2str"] = dict2str
LOG_DIR = Path(__file__).parent

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


def require_auth(request: Request):
    if not request.session.get("authenticated"):
        return RedirectResponse(url="/flight/login", status_code=303)
    return None


def require_api_auth(request: Request):
    if not request.session.get("authenticated"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return None


def get_stats():
    flights = get_all_flights()
    total_dist = sum(f.get("distance_km", 0) for f in flights)
    total_dur = sum(f.get("duration_s", 0) for f in flights)
    total_flights = len(flights)

    daily = defaultdict(lambda: {"count": 0, "distance": 0, "duration": 0})
    weekly = defaultdict(lambda: {"count": 0, "distance": 0, "duration": 0})
    monthly = defaultdict(lambda: {"count": 0, "distance": 0, "duration": 0})

    for f in flights:
        date_str = f.get("date", "")
        if not date_str:
            continue
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            continue
        dist = f.get("distance_km", 0)
        dur = f.get("duration_s", 0)

        daily[date_str]["count"] += 1
        daily[date_str]["distance"] += dist
        daily[date_str]["duration"] += dur

        week_key = dt.strftime("%Y-W%V")
        weekly[week_key]["count"] += 1
        weekly[week_key]["distance"] += dist
        weekly[week_key]["duration"] += dur

        month_key = dt.strftime("%Y-%m")
        monthly[month_key]["count"] += 1
        monthly[month_key]["distance"] += dist
        monthly[month_key]["duration"] += dur

    return {
        "total_flights": total_flights,
        "total_distance_km": round(total_dist, 2),
        "total_duration_s": total_dur,
        "daily": [{"period": k, **v} for k, v in sorted(daily.items())],
        "weekly": [{"period": k, **v} for k, v in sorted(weekly.items())],
        "monthly": [{"period": k, **v} for k, v in sorted(monthly.items())],
    }


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if request.session.get("authenticated"):
        return RedirectResponse(url="/flight/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {})


@app.post("/login")
async def login(request: Request):
    body = await request.json()
    user = body.get("user", "")
    pwd = body.get("pass", "")
    if secrets.compare_digest(user, USER) and secrets.compare_digest(pwd, PASS):
        request.session["authenticated"] = True
        return {"ok": True}
    return JSONResponse({"error": "Invalid credentials"}, status_code=401)


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/flight/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    flights = get_all_flights()
    stats = get_stats()
    return templates.TemplateResponse(request, "dashboard.html", {"flights": flights, "stats": stats})


@app.get("/flights", response_class=HTMLResponse)
async def flight_list(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    flights = get_all_flights()
    return templates.TemplateResponse(request, "flights.html", {"flights": flights})


@app.get("/flight/{filename:path}", response_class=HTMLResponse)
async def flight_detail(request: Request, filename: str):
    redirect = require_auth(request)
    if redirect:
        return redirect
    flight = get_flight(filename)
    if not flight:
        return HTMLResponse("Flight not found", status_code=404)
    flights = get_all_flights()
    idx = None
    for i, f in enumerate(flights):
        if f["filename"] == filename:
            idx = i
            break
    prev_flight = flights[idx + 1]["filename"] if idx is not None and idx + 1 < len(flights) else None
    next_flight = flights[idx - 1]["filename"] if idx is not None and idx - 1 >= 0 else None
    return templates.TemplateResponse(request, "flight.html", {
        "flight": flight, "prev": prev_flight, "next": next_flight
    })


@app.post("/api/scan")
async def scan_logs(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    imported = []
    for f in sorted(LOG_DIR.glob("*.csv")):
        key = f.name
        if get_flight(key) is not None:
            continue
        try:
            points = parse_log(f)
            if not points:
                continue
            summary = analyze(key, points)
            save_flight(summary)
            imported.append(key)
        except Exception:
            pass
    return {"imported": imported}


@app.post("/api/upload")
async def upload_log(request: Request, file: UploadFile = File(...)):
    denied = require_api_auth(request)
    if denied:
        return denied
    if not file.filename or not file.filename.lower().endswith(".csv"):
        return JSONResponse({"error": "Only CSV files are supported"}, status_code=400)
    safe_name = _safe_filename(file.filename)
    contents = await file.read()
    if len(contents) > MAX_UPLOAD_BYTES:
        return JSONResponse({"error": "File too large (max 10 MB)"}, status_code=400)
    dest = LOG_DIR / safe_name
    with open(dest, "wb") as f:
        f.write(contents)
    try:
        points = parse_log(dest)
        if not points:
            dest.unlink()
            return JSONResponse({"error": "No valid telemetry data found"}, status_code=400)
        summary = analyze(safe_name, points)
        save_flight(summary)
        return {"imported": safe_name}
    except Exception:
        dest.unlink(missing_ok=True)
        return JSONResponse({"error": "Failed to parse file"}, status_code=400)


@app.get("/api/flights")
async def api_flights(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    return get_all_flights()


@app.get("/api/stats")
async def api_stats(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    return get_stats()


@app.get("/api/flights/{filename:path}")
async def api_flight(request: Request, filename: str):
    denied = require_api_auth(request)
    if denied:
        return denied
    flight = get_flight(filename)
    if not flight:
        return JSONResponse({"error": "not found"}, status_code=404)
    return flight


@app.delete("/api/flights/{filename:path}")
async def api_delete(request: Request, filename: str):
    denied = require_api_auth(request)
    if denied:
        return denied
    delete_flight(filename)
    return {"deleted": filename}


@app.get("/api/export/{filename:path}")
async def api_export(request: Request, filename: str, format: str = "gpx"):
    denied = require_api_auth(request)
    if denied:
        return denied
    flight = get_flight(filename)
    if not flight:
        return JSONResponse({"error": "not found"}, status_code=404)
    coords = flight.get("coordinates", [])
    safe_name = _xml_escape(filename)
    if format == "kml":
        lines = ['<?xml version="1.0" encoding="UTF-8"?>',
                 '<kml xmlns="http://www.opengis.net/kml/2.2">',
                 f'  <Document><name>{safe_name}</name>',
                 '    <Placemark><name>Flight Track</name><LineString><coordinates>']
        for c in coords:
            lines.append(f"      {c[1]},{c[0]},{c[2]}")
        lines.append('</coordinates></LineString></Placemark></Document></kml>')
        content_disp = f'attachment; filename="{safe_name}.kml"'
        return HTMLResponse("\n".join(lines), media_type="application/vnd.google-earth.kml+xml",
                            headers={"Content-Disposition": content_disp})
    else:
        lines = ['<?xml version="1.0" encoding="UTF-8"?>',
                 '<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">',
                 f'  <trk><name>{safe_name}</name><trkseg>']
        for c in coords:
            lines.append(f'    <trkpt lat="{c[0]}" lon="{c[1]}"><ele>{c[2]}</ele><time>{c[4]}</time></trkpt>')
        lines.append('  </trkseg></trk></gpx>')
        content_disp = f'attachment; filename="{safe_name}.gpx"'
        return HTMLResponse("\n".join(lines), media_type="application/gpx+xml",
                            headers={"Content-Disposition": content_disp})


@app.put("/api/flights/{filename:path}/notes")
async def api_save_notes(filename: str, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    body = await request.json()
    notes = body.get("notes", "")[:5000]
    flight = get_flight(filename)
    if not flight:
        return JSONResponse({"error": "not found"}, status_code=404)
    update_notes(filename, notes)
    return {"ok": True}


@app.post("/api/flights/{filename:path}/import-gpx")
async def api_import_gpx(filename: str, request: Request, file: UploadFile = File(...)):
    denied = require_api_auth(request)
    if denied:
        return denied
    flight = get_flight(filename)
    if not flight:
        return JSONResponse({"error": "Flight not found"}, status_code=404)
    if not file.filename or not file.filename.lower().endswith(".gpx"):
        return JSONResponse({"error": "Only GPX files are supported"}, status_code=400)
    contents = await file.read()
    if len(contents) > MAX_UPLOAD_BYTES:
        return JSONResponse({"error": "File too large (max 10 MB)"}, status_code=400)

    try:
        root = ET.fromstring(contents)
    except ET.ParseError:
        return JSONResponse({"error": "Invalid GPX file"}, status_code=400)

    ns = {"gpx": "http://www.topografix.com/GPX/1/1"}
    trkpts = root.findall(".//gpx:trkpt", ns)
    if not trkpts:
        trkpts = root.findall(".//trkpt")
    if not trkpts:
        return JSONResponse({"error": "No track points found in GPX"}, status_code=400)

    original_coords = flight.get("coordinates", [])

    def _parse_gpx_time(time_str: str) -> float:
        time_str = time_str.strip()
        if time_str.endswith("Z"):
            time_str = time_str[:-1] + "+00:00"
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(time_str)
            return dt.timestamp()
        except ValueError:
            return 0.0

    def _find_closest_orig(orig_list: list, target_ts: float):
        if not orig_list:
            return None
        best = min(orig_list, key=lambda c: abs(c[4] - target_ts))
        if abs(best[4] - target_ts) > 300:
            return None
        return best

    gpx_coords = []
    for pt in trkpts:
        lat = float(pt.attrib.get("lat", 0))
        lon = float(pt.attrib.get("lon", 0))
        if lat < -90 or lat > 90 or lon < -180 or lon > 180:
            continue
        ele_el = pt.find("gpx:ele", ns) or pt.find("ele")
        ele = float(ele_el.text) if ele_el is not None and ele_el.text else 0.0
        time_el = pt.find("gpx:time", ns) or pt.find("time")
        ts = _parse_gpx_time(time_el.text) if time_el is not None and time_el.text else 0.0
        speed = 0.0
        speed_el = pt.find("gpx:speed", ns) or pt.find("speed")
        if speed_el is not None and speed_el.text:
            try:
                speed = float(speed_el.text) * 3.6
            except ValueError:
                pass
        gpx_coords.append([lat, lon, ele, speed, ts, 0, 0])

    for gc in gpx_coords:
        orig = _find_closest_orig(original_coords, gc[4])
        if orig:
            gc[5] = orig[5]
            gc[6] = orig[6]

    if len(gpx_coords) >= 2:
        from analyzer import haversine_km
        total_dist = 0.0
        speeds = []
        for i in range(1, len(gpx_coords)):
            total_dist += haversine_km(
                gpx_coords[i-1][0], gpx_coords[i-1][1],
                gpx_coords[i][0], gpx_coords[i][1]
            )
        if gpx_coords[0][4] > 0 and gpx_coords[-1][4] > 0:
            duration_s = gpx_coords[-1][4] - gpx_coords[0][4]
        else:
            duration_s = flight.get("duration_s", 0)
        if duration_s <= 0:
            duration_s = flight.get("duration_s", 0)
        alts = [c[2] for c in gpx_coords]
        if any(c[3] > 0 for c in gpx_coords):
            speeds = [c[3] for c in gpx_coords if c[3] > 0]
        elif duration_s > 0:
            speeds = [(total_dist * 1000) / duration_s * 3.6]
        else:
            speeds = [0]
        stats = {
            "distance_km": round(total_dist, 3),
            "duration_s": round(duration_s, 1),
            "max_alt_m": round(max(alts), 1),
            "min_alt_m": round(min(alts), 1),
            "avg_alt_m": round(sum(alts) / len(alts), 1),
            "max_speed_kmh": round(max(speeds), 1) if speeds else 0,
            "avg_speed_kmh": round(sum(speeds) / len(speeds), 1) if speeds else 0,
        }
    else:
        stats = {
            "distance_km": flight.get("distance_km", 0),
            "duration_s": flight.get("duration_s", 0),
            "max_alt_m": flight.get("max_alt_m", 0),
            "min_alt_m": flight.get("min_alt_m", 0),
            "avg_alt_m": flight.get("avg_alt_m", 0),
            "max_speed_kmh": flight.get("max_speed_kmh", 0),
            "avg_speed_kmh": flight.get("avg_speed_kmh", 0),
        }

    update_flight_track(filename, gpx_coords, stats)
    return {"ok": True, "points": len(gpx_coords), "stats": stats}


@app.get("/api/battery-health")
async def api_battery_health(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    flights = get_all_flights()
    data = []
    for f in flights:
        d = f.get("date", "")
        v_start = f.get("battery_start_v", 0)
        v_end = f.get("battery_end_v", 0)
        v_min = f.get("battery_min_v", 0)
        if d and v_start:
            data.append({"date": d, "start_v": v_start, "end_v": v_end, "min_v": v_min,
                         "consumed_mah": f.get("battery_consumed_mah", 0)})
    return data
