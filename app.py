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
from analyzer import analyze, detect_events
from geocode import home_coords, reverse_geocode
from database import (save_flight, get_all_flights, get_flight, delete_flight,
                      rename_flight, update_notes, update_flight_track,
                      get_vehicles, get_vehicle, get_default_vehicle,
                      update_flight_events,
                      create_vehicle, update_vehicle, delete_vehicle,
                      set_vehicle_photo, get_vehicle_stats, assign_vehicle_to_flight,
                      get_all_tags, set_flight_tags, get_flight_tags,
                      get_battery_health_by_vehicle,
                      create_user, get_user, get_user_by_id, get_all_users,
                      update_user, change_password, delete_user, verify_user,
                      recalculate_home_distances, set_flight_track_source)
import httpx

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


def _place_filename(path: Path, points) -> str | None:
    """Return a new filename with the GPS place appended, or None."""
    lat, lon = home_coords(points)
    if not lat or not lon:
        return None
    place = reverse_geocode(lat, lon)
    if not place:
        return None
    stem = path.stem
    if stem.endswith("-" + place):
        return None
    new_name = f"{stem}-{place}{path.suffix}"
    if (LOG_DIR / new_name).exists() or get_flight(new_name) is not None:
        return None
    return new_name


def _xml_escape(s: str) -> str:
    return xml_escape(str(s))


def dict2str(d):
    if not d:
        return "-"
    return ", ".join(f"{k}: {v}" for k, v in sorted(d.items()))


def fmt_duration(seconds):
    if not seconds:
        return "0s"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    elif m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


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
templates.env.filters["fmt_duration"] = fmt_duration
templates.env.globals["now"] = datetime.now
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
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    elif not request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
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

    records = {
        "max_distance": {"value": 0, "flight": ""},
        "max_alt": {"value": 0, "flight": ""},
        "max_speed": {"value": 0, "flight": ""},
        "max_duration": {"value": 0, "flight": ""},
        "max_home_dist": {"value": 0, "flight": ""},
        "best_glide": {"value": 0, "flight": ""},
        "max_vspd": {"value": 0, "flight": ""},
    }

    for f in flights:
        date_str = f.get("date", "")
        fn = f.get("filename", "")
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

        if (f.get("distance_km") or 0) > records["max_distance"]["value"]:
            records["max_distance"] = {"value": f["distance_km"] or 0, "flight": fn}
        if (f.get("max_alt_m") or 0) > records["max_alt"]["value"]:
            records["max_alt"] = {"value": f["max_alt_m"] or 0, "flight": fn}
        if (f.get("max_speed_kmh") or 0) > records["max_speed"]["value"]:
            records["max_speed"] = {"value": f["max_speed_kmh"] or 0, "flight": fn}
        if (f.get("duration_s") or 0) > records["max_duration"]["value"]:
            records["max_duration"] = {"value": f["duration_s"] or 0, "flight": fn}
        if (f.get("home_distance_km") or 0) > records["max_home_dist"]["value"]:
            records["max_home_dist"] = {"value": f["home_distance_km"] or 0, "flight": fn}
        if (f.get("glide_ratio") or 0) > records["best_glide"]["value"]:
            records["best_glide"] = {"value": f["glide_ratio"] or 0, "flight": fn}
        if (f.get("max_vspd_ms") or 0) > records["max_vspd"]["value"]:
            records["max_vspd"] = {"value": f["max_vspd_ms"] or 0, "flight": fn}

    return {
        "total_flights": total_flights,
        "total_distance_km": round(total_dist, 2),
        "total_duration_s": total_dur,
        "records": records,
        "daily": [{"period": k, **v} for k, v in sorted(daily.items(), reverse=True)],
        "weekly": [{"period": k, **v} for k, v in sorted(weekly.items(), reverse=True)],
        "monthly": [{"period": k, **v} for k, v in sorted(monthly.items(), reverse=True)],
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
    db_user = verify_user(user, pwd)
    if db_user:
        request.session["authenticated"] = True
        request.session["username"] = db_user["username"]
        request.session["role"] = db_user["role"]
        return {"ok": True}
    return JSONResponse({"error": "Invalid credentials"}, status_code=401)


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/flight/login", status_code=303)


def require_admin(request: Request):
    if request.session.get("role") != "admin":
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return None


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    flights = get_all_flights()
    stats = get_stats()
    vehicle_stats = get_vehicle_stats()
    vehicles = get_vehicles()
    return templates.TemplateResponse(request, "dashboard.html", {
        "flights": flights, "stats": stats, "vehicle_stats": vehicle_stats, "vehicles": vehicles
    })


@app.get("/flights", response_class=HTMLResponse)
async def flight_list(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    flights = get_all_flights()
    return templates.TemplateResponse(request, "flights.html", {"flights": flights})


@app.get("/report", response_class=HTMLResponse)
async def report_page(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    flights = get_all_flights()
    stats = get_stats()
    vehicle_stats = get_vehicle_stats()
    return templates.TemplateResponse(request, "report.html", {
        "flights": flights, "stats": stats, "vehicle_stats": vehicle_stats
    })


@app.get("/compare", response_class=HTMLResponse)
async def compare_page(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    flights = get_all_flights()
    return templates.TemplateResponse(request, "compare.html", {"flights": flights})


@app.get("/api/compare-coords")
async def api_compare_coords(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    files = request.query_params.get("files", "")
    if not files:
        return {"flights": []}
    names = [f.strip() for f in files.split(",") if f.strip()]
    result = []
    for name in names:
        flight = get_flight(name)
        if flight:
            result.append({
                "filename": flight["filename"],
                "date": flight["date"],
                "distance_km": flight["distance_km"],
                "duration_s": flight["duration_s"],
                "max_alt_m": flight["max_alt_m"],
                "max_speed_kmh": flight["max_speed_kmh"],
                "coordinates": flight["coordinates"]
            })
    return {"flights": result}


@app.get("/api/tags")
async def api_tags(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    return {"tags": get_all_tags()}


@app.post("/api/flights/{filename:path}/tags")
async def api_set_tags(request: Request, filename: str):
    denied = require_api_auth(request)
    if denied:
        return denied
    body = await request.json()
    tags = body.get("tags", [])
    set_flight_tags(filename, tags)
    return {"tags": tags}


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
    vehicles = get_vehicles()
    return templates.TemplateResponse(request, "flight.html", {
        "flight": flight, "prev": prev_flight, "next": next_flight, "vehicles": vehicles
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
            new_name = _place_filename(f, points)
            if new_name:
                f.rename(LOG_DIR / new_name)
                key = new_name
            summary = analyze(key, points)
            default_v = get_default_vehicle()
            if default_v:
                summary.vehicle_id = default_v.id
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
        new_name = _place_filename(dest, points)
        if new_name:
            dest.rename(LOG_DIR / new_name)
            safe_name = new_name
        summary = analyze(safe_name, points)
        default_v = get_default_vehicle()
        if default_v:
            summary.vehicle_id = default_v.id
        save_flight(summary)
        return {"imported": safe_name}
    except Exception:
        dest.unlink(missing_ok=True)
        return JSONResponse({"error": "Failed to parse file"}, status_code=400)


@app.post("/api/reprocess")
async def reprocess_flights(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    results = []
    for f in sorted(LOG_DIR.glob("*.csv")):
        key = f.name
        try:
            points = parse_log(f)
            if not points:
                continue
            old_key = key
            new_name = _place_filename(f, points)
            if new_name:
                f.rename(LOG_DIR / new_name)
                key = new_name
            summary = analyze(key, points)
            existing = get_flight(key)
            if existing:
                summary.vehicle_id = existing.get("vehicle_id")
            elif key != old_key and get_flight(old_key) is not None:
                rename_flight(old_key, key)
                existing = get_flight(key)
                summary.vehicle_id = existing.get("vehicle_id") if existing else None
            save_flight(summary)
            results.append(key)
        except Exception as e:
            results.append(f"{key}: error: {e}")
    return {"reprocessed": results}


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


@app.put("/api/flights/{filename:path}/notes")
async def api_save_notes(filename: str, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    raw = await request.body()
    if not raw:
        return JSONResponse({"error": "empty body"}, status_code=400)
    try:
        body = __import__("json").loads(raw)
    except Exception as e:
        return JSONResponse({"error": f"invalid JSON: {e}"}, status_code=400)
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

    try:
        ns_match = re.match(r'\{([^}]+)\}', root.tag)
        ns_url = ns_match.group(1) if ns_match else ""
        ns = {"gpx": ns_url} if ns_url else {}
        trkpts = root.findall(".//gpx:trkpt", ns) if ns_url else root.findall(".//trkpt")
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
            ele_el = pt.find("gpx:ele", ns) if ns_url else pt.find("ele")
            ele = float(ele_el.text) if ele_el is not None and ele_el.text else 0.0
            time_el = pt.find("gpx:time", ns) if ns_url else pt.find("time")
            ts = _parse_gpx_time(time_el.text) if time_el is not None and time_el.text else 0.0
            speed = 0.0
            speed_el = pt.find("gpx:speed", ns) if ns_url else pt.find("speed")
            if speed_el is not None and speed_el.text:
                try:
                    speed = float(speed_el.text) * 3.6
                except ValueError:
                    pass
            gpx_coords.append([lat, lon, ele, speed, ts, 0, 0])

        for gc in gpx_coords:
            while len(gc) < 34:
                gc.append(0)
            orig = _find_closest_orig(original_coords, gc[4])
            if orig:
                if gc[3] == 0:
                    gc[3] = orig[3]
                for i in range(5, len(orig)):
                    gc[i] = orig[i]

        # Overlay live nav telemetry from the CSV log onto the GPX track so the
        # extended data (attitude, RC input, switches, flight mode, battery, LQ,
        # RSSI2, etc.) is preserved instead of being lost on import.
        try:
            csv_points = parse_log(LOG_DIR / filename)
        except Exception:
            csv_points = []
        if csv_points:
            gpx_coords, _ = merge_nav({"coordinates": gpx_coords}, csv_points)

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
        set_flight_track_source(filename, "gpx")
        return {"ok": True, "points": len(gpx_coords), "stats": stats}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


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


@app.put("/api/flights/{filename:path}/vehicle")
async def api_assign_vehicle(filename: str, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    body = await request.json()
    vehicle_id = body.get("vehicle_id")
    assign_vehicle_to_flight(filename, vehicle_id)
    return {"ok": True}


@app.put("/api/flights/{filename:path}")
async def api_rename(request: Request, filename: str):
    denied = require_api_auth(request)
    if denied:
        return denied
    body = await request.json()
    new_name = body.get("new_name", "").strip()
    if not new_name or "/" in new_name or "\\" in new_name:
        return JSONResponse({"error": "invalid name"}, status_code=400)
    if get_flight(new_name):
        return JSONResponse({"error": "a flight with this name already exists"}, status_code=409)
    old_path = LOG_DIR / filename
    new_path = LOG_DIR / new_name
    if old_path.exists():
        old_path.rename(new_path)
    if not rename_flight(filename, new_name):
        return JSONResponse({"error": "rename failed"}, status_code=500)
    return {"filename": new_name}


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
        from datetime import timezone as _tz
        lines = ['<?xml version="1.0" encoding="UTF-8"?>',
                 '<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">',
                 f'  <trk><name>{safe_name}</name><trkseg>']
        for c in coords:
            t = datetime.fromtimestamp(c[4], tz=_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if c[4] else ""
            lines.append(f'    <trkpt lat="{c[0]}" lon="{c[1]}"><ele>{c[2]}</ele><time>{t}</time></trkpt>')
        lines.append('  </trkseg></trk></gpx>')
        content_disp = f'attachment; filename="{safe_name}.gpx"'
        return HTMLResponse("\n".join(lines), media_type="application/gpx+xml",
                            headers={"Content-Disposition": content_disp})





def merge_nav(flight: dict, points: list) -> tuple[list, int]:
    """Merge nav telemetry (pitch/roll/RC/sticks/etc.) into stored coordinates
    by matching timestamps with the parsed log. Returns (updated_coords, matched)."""
    nav_list = sorted([(p.timestamp, (p.pitch, p.roll, p.yaw, p.rud, p.ele, p.thr, p.ail, p.vspd, p.heading, p.sa, p.sb, p.sc, p.sd, p.se, p.lsw, p.p1, p.flight_mode, p.rssi_2, p.rsnr, p.trss, p.tqly, p.tsnr, p.curr, p.capa, p.bat_pct, p.txbat, p.rqly)) for p in points])
    nav_tss = [t for t, _ in nav_list]

    def find_nearest(ts, max_delta=0.6):
        if not nav_tss or ts == 0:
            return None
        import bisect
        i = bisect.bisect_left(nav_tss, ts)
        best = None
        if i < len(nav_tss):
            best = (nav_tss[i], abs(nav_tss[i] - ts))
        if i > 0:
            cand = (nav_tss[i - 1], abs(nav_tss[i - 1] - ts))
            if best is None or cand[1] < best[1]:
                best = cand
        if best and best[1] <= max_delta:
            return best[0]
        return None

    nav_by_ts = {t: v for t, v in nav_list}
    updated = []
    matched = 0
    for c in flight.get("coordinates", []):
        ts = c[4] if len(c) > 4 else 0
        nearest = find_nearest(ts)
        nav = nav_by_ts.get(nearest)
        if nav:
            matched += 1
            if len(c) >= 34:
                c[7], c[8], c[9], c[10], c[11], c[12], c[13], c[14], c[15], c[16], c[17], c[18], c[19], c[20], c[21], c[22], c[23], c[24], c[25], c[26], c[27], c[28], c[29], c[30], c[31], c[32], c[33] = nav
            else:
                c = list(c) + list(nav)
        elif len(c) < 34:
            c = list(c) + [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, '', 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
        updated.append(c)
    return updated, matched


def _detect_events_for_track(points, coords) -> list[dict]:
    """Detect events from raw points, then remap each event index onto the
    stored coordinate array (which may differ from the CSV points, e.g. for
    GPX-imported tracks) by nearest timestamp."""
    import bisect
    events = detect_events(points)
    if not coords:
        return events
    tss = [c[4] for c in coords]

    def _idx(ts):
        i = bisect.bisect_left(tss, ts)
        if i >= len(tss):
            return len(tss) - 1
        if i == 0:
            return 0
        return i if ts - tss[i - 1] > tss[i] - ts else i - 1

    for e in events:
        e["i"] = _idx(e.get("ts", 0))
    return events


def sync_all_flights_from_csv() -> list:
    """Re-analyze every flight that still has a CSV on disk with full-resolution
    coordinates and all metrics, then recompute derived statistics. Runs at
    startup so every statistic is complete from the first load without needing
    a manual rescan-nav on each flight. GPX-imported tracks are preserved."""
    updated = []
    for f in sorted(LOG_DIR.glob("*.csv")):
        key = f.name
        flight = get_flight(key)
        if not flight:
            continue
        try:
            points = parse_log(f)
            if not points:
                continue
        except Exception:
            continue
        if flight.get("track_source") == "gpx":
            # Keep the GPX track, refresh nav telemetry on top of it and
            # recompute events (acro/incident detection) from the CSV.
            new_coords, matched = merge_nav(flight, points)
            if not matched:
                continue
            events = _detect_events_for_track(points, new_coords)
            update_flight_events(key, events)
            update_flight_track(key, new_coords, {
                "distance_km": flight.get("distance_km", 0),
                "duration_s": flight.get("duration_s", 0),
                "max_alt_m": flight.get("max_alt_m", 0),
                "min_alt_m": flight.get("min_alt_m", 0),
                "avg_alt_m": flight.get("avg_alt_m", 0),
                "max_speed_kmh": flight.get("max_speed_kmh", 0),
                "avg_speed_kmh": flight.get("avg_speed_kmh", 0),
            })
            updated.append(key)
            continue
        try:
            summary = analyze(key, points)
        except Exception:
            continue
        summary.vehicle_id = flight.get("vehicle_id")
        save_flight(summary)
        updated.append(key)
    recalculate_home_distances()
    return updated


@app.post("/api/flights/{filename:path}/rescan-nav")
async def api_rescan_nav(filename: str, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    flight = get_flight(filename)
    if not flight:
        return JSONResponse({"error": "not found"}, status_code=404)
    csv_path = LOG_DIR / filename
    if not csv_path.exists():
        return JSONResponse({"error": "CSV file not found"}, status_code=404)
    try:
        points = parse_log(csv_path)
    except Exception as e:
        return JSONResponse({"error": f"failed to parse CSV: {e}"}, status_code=400)
    updated, matched = merge_nav(flight, points)
    flight["coordinates"] = updated
    events = _detect_events_for_track(points, updated)
    update_flight_events(filename, events)
    update_flight_track(filename, updated, {
        "distance_km": flight.get("distance_km", 0),
        "duration_s": flight.get("duration_s", 0),
        "max_alt_m": flight.get("max_alt_m", 0),
        "min_alt_m": flight.get("min_alt_m", 0),
        "avg_alt_m": flight.get("avg_alt_m", 0),
        "max_speed_kmh": flight.get("max_speed_kmh", 0),
        "avg_speed_kmh": flight.get("avg_speed_kmh", 0),
    })
    return {"ok": True, "matched": matched, "total": len(updated)}


@app.get("/vehicles", response_class=HTMLResponse)
async def vehicles_page(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    vehicles = get_vehicles()
    stats = get_vehicle_stats()
    return templates.TemplateResponse(request, "vehicles.html", {
        "vehicles": vehicles, "stats": stats
    })


@app.get("/api/vehicles")
async def api_vehicles(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    return [{"id": v.id, "name": v.name, "vehicle_type": v.vehicle_type,
             "photo": v.photo, "is_default": v.is_default} for v in get_vehicles()]


@app.post("/api/vehicles")
async def api_create_vehicle(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    body = await request.json()
    name = body.get("name", "").strip()
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    vtype = body.get("vehicle_type", "drone")
    is_default = body.get("is_default", False)
    v = create_vehicle(name, vtype, is_default)
    return {"id": v.id, "name": v.name, "vehicle_type": v.vehicle_type, "is_default": v.is_default}


@app.put("/api/vehicles/{vehicle_id}")
async def api_update_vehicle(vehicle_id: int, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    body = await request.json()
    v = update_vehicle(
        vehicle_id,
        name=body.get("name"),
        vehicle_type=body.get("vehicle_type"),
        is_default=body.get("is_default"),
    )
    if not v:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"id": v.id, "name": v.name, "vehicle_type": v.vehicle_type, "is_default": v.is_default}


@app.delete("/api/vehicles/{vehicle_id}")
async def api_delete_vehicle(vehicle_id: int, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    if delete_vehicle(vehicle_id):
        return {"deleted": vehicle_id}
    return JSONResponse({"error": "not found"}, status_code=404)


@app.post("/api/vehicles/{vehicle_id}/photo")
async def api_vehicle_photo(vehicle_id: int, request: Request, file: UploadFile = File(...)):
    denied = require_api_auth(request)
    if denied:
        return denied
    v = get_vehicle(vehicle_id)
    if not v:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not file.filename or not file.filename.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
        return JSONResponse({"error": "Only image files (jpg, png, webp) are supported"}, status_code=400)
    photo_dir = Path(__file__).parent / "data" / "vehicle_photos"
    photo_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(file.filename).suffix
    photo_name = f"v{vehicle_id}{ext}"
    photo_path = photo_dir / photo_name
    contents = await file.read()
    if len(contents) > 5 * 1024 * 1024:
        return JSONResponse({"error": "File too large (max 5 MB)"}, status_code=400)
    photo_path.write_bytes(contents)
    set_vehicle_photo(vehicle_id, f"/flight/api/vehicles/{vehicle_id}/photo/img")
    return {"photo": f"/flight/api/vehicles/{vehicle_id}/photo/img"}


@app.get("/api/vehicles/{vehicle_id}/photo/img")
async def api_vehicle_photo_img(vehicle_id: int, request: Request):
    v = get_vehicle(vehicle_id)
    if not v or not v.photo:
        return HTMLResponse("", status_code=404)
    photo_dir = Path(__file__).parent / "data" / "vehicle_photos"
    ext_candidates = [".jpg", ".jpeg", ".png", ".webp"]
    for ext in ext_candidates:
        p = photo_dir / f"v{vehicle_id}{ext}"
        if p.exists():
            from fastapi.responses import FileResponse
            return FileResponse(str(p))
    return HTMLResponse("", status_code=404)


@app.post("/api/vehicles/apply-default")
async def api_apply_default_vehicle(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    default_v = get_default_vehicle()
    if not default_v:
        return JSONResponse({"error": "No default vehicle set"}, status_code=400)
    flights = get_all_flights()
    updated = 0
    for f in flights:
        if f.get("vehicle_id") is None:
            assign_vehicle_to_flight(f["filename"], default_v.id)
            updated += 1
    return {"updated": updated, "vehicle": default_v.name}


@app.get("/replay3d/{filename:path}", response_class=HTMLResponse)
async def replay3d_page(request: Request, filename: str):
    redirect = require_auth(request)
    if redirect:
        return redirect
    flight = get_flight(filename)
    if not flight:
        return HTMLResponse("Flight not found", status_code=404)
    vehicle = get_vehicle(flight.get("vehicle_id")) if flight.get("vehicle_id") else None
    resp = templates.TemplateResponse(request, "replay3d.html", {
        "flight": flight, "vehicle": vehicle, "filename": filename
    })
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.get("/api/replay3d/{filename:path}")
async def api_replay3d(request: Request, filename: str):
    denied = require_api_auth(request)
    if denied:
        return denied
    flight = get_flight(filename)
    if not flight:
        return JSONResponse({"error": "not found"}, status_code=404)

    coords = flight.get("coordinates", [])
    if len(coords) < 2:
        return JSONResponse({"error": "not enough coordinates"}, status_code=400)

    # Home point (first valid GPS)
    home_lat, home_lon = 0.0, 0.0
    for c in coords:
        lat, lon = c[0], c[1]
        if abs(lat) > 0.001 and abs(lon) > 0.001:
            home_lat, home_lon = lat, lon
            break

    # Bounding box with 25% padding
    lats = [c[0] for c in coords if abs(c[0]) > 0.001]
    lons = [c[1] for c in coords if abs(c[1]) > 0.001]
    if not lats or not lons:
        return JSONResponse({"error": "no valid GPS"}, status_code=400)
    min_lat, max_lat = min(lats), max(lats)
    min_lon, max_lon = min(lons), max(lons)
    lat_pad = max((max_lat - min_lat) * 0.25, 0.001)
    lon_pad = max((max_lon - min_lon) * 0.25, 0.001)
    min_lat -= lat_pad; max_lat += lat_pad
    min_lon -= lon_pad; max_lon += lon_pad

    # Elevation grid (20x20 for speed vs quality tradeoff)
    GRID = 20
    elevs = [[0.0] * GRID for _ in range(GRID)]
    try:
        locations = []
        for yi in range(GRID):
            lat = min_lat + (max_lat - min_lat) * yi / (GRID - 1)
            for xi in range(GRID):
                lon = min_lon + (max_lon - min_lon) * xi / (GRID - 1)
                locations.append({"latitude": round(lat, 6), "longitude": round(lon, 6)})
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                "https://api.open-elevation.com/api/v1/lookup",
                json={"locations": locations}
            )
            if r.status_code == 200:
                results = r.json().get("results", [])
                for yi in range(GRID):
                    for xi in range(GRID):
                        idx = yi * GRID + xi
                        if idx < len(results):
                            elev = results[idx].get("elevation", 0)
                            elevs[yi][xi] = round(max(elev, 0), 1)
    except Exception:
        pass

    return {
        "coordinates": coords,
        "home": {"lat": home_lat, "lon": home_lon},
        "bbox": {"min_lat": min_lat, "max_lat": max_lat,
                 "min_lon": min_lon, "max_lon": max_lon},
        "center": {"lat": (min_lat + max_lat) / 2, "lon": (min_lon + max_lon) / 2},
        "grid_size": GRID,
        "elevation": elevs,
    }


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


@app.get("/api/battery-per-vehicle")
async def api_battery_per_vehicle(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    vehicle_id = request.query_params.get("vehicle_id")
    return get_battery_health_by_vehicle(int(vehicle_id) if vehicle_id else None)


# --- User management (admin only) ---

@app.get("/users", response_class=HTMLResponse)
async def users_page(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    forbidden = require_admin(request)
    if forbidden:
        return forbidden
    users = get_all_users()
    return templates.TemplateResponse(request, "users.html", {"users": users})


@app.get("/api/users")
async def api_list_users(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    forbidden = require_admin(request)
    if forbidden:
        return forbidden
    return get_all_users()


@app.post("/api/users")
async def api_create_user(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    forbidden = require_admin(request)
    if forbidden:
        return forbidden
    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "")
    role = body.get("role", "viewer")
    if not username or not password:
        return JSONResponse({"error": "username and password required"}, status_code=400)
    if role not in ("admin", "viewer"):
        return JSONResponse({"error": "role must be admin or viewer"}, status_code=400)
    user = create_user(username, password, role)
    if not user:
        return JSONResponse({"error": "username already exists"}, status_code=409)
    return {"id": user["id"], "username": user["username"], "role": user["role"]}


@app.put("/api/users/{user_id}")
async def api_update_user(user_id: int, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    forbidden = require_admin(request)
    if forbidden:
        return forbidden
    body = await request.json()
    user = update_user(
        user_id,
        username=body.get("username"),
        role=body.get("role"),
    )
    if not user:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"id": user["id"], "username": user["username"], "role": user["role"]}


@app.delete("/api/users/{user_id}")
async def api_delete_user(user_id: int, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    forbidden = require_admin(request)
    if forbidden:
        return forbidden
    user = get_user_by_id(user_id)
    if not user:
        return JSONResponse({"error": "not found"}, status_code=404)
    if user["username"] == request.session.get("username"):
        return JSONResponse({"error": "cannot delete yourself"}, status_code=400)
    delete_user(user_id)
    return {"deleted": user_id}


@app.post("/api/users/{user_id}/change-password")
async def api_change_password(user_id: int, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    body = await request.json()
    new_password = body.get("password", "")
    if not new_password:
        return JSONResponse({"error": "password required"}, status_code=400)
    user = get_user_by_id(user_id)
    if not user:
        return JSONResponse({"error": "not found"}, status_code=404)
    is_admin = request.session.get("role") == "admin"
    is_self = user["username"] == request.session.get("username")
    if not is_admin and not is_self:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    change_password(user_id, new_password)
    return {"ok": True}


# Auto-sync: merge nav telemetry for all flights and recompute derived metrics
# on startup, so every statistic is available from the first load without
# needing a manual rescan-nav on each flight.
sync_all_flights_from_csv()
