import asyncio
import hashlib
import logging
import os
import re
import secrets
import math
import json
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime, timezone, timedelta
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
import database
from database import (save_flight, get_all_flights, get_flight, delete_flight,
                      rename_flight, update_notes, update_flight_track,
                      get_vehicles, get_vehicle, get_default_vehicle,
                      update_flight_events,
                      create_vehicle, update_vehicle, delete_vehicle,
                      set_vehicle_photo, get_vehicle_stats, assign_vehicle_to_flight,
                      get_all_tags, set_flight_tags, get_flight_tags,
                      get_battery_health_by_vehicle,
                      create_user, get_user, get_user_by_id, get_all_users,
                      update_user, change_password, verify_user,
                      recalculate_home_distances, set_flight_track_source,
                      count_user_data, delete_user_cascade, backup_database,
                      create_reset_token, get_reset_token_user,
                      clear_reset_token, set_user_preferences,
                      get_user_by_email, create_confirm_token,
                      get_confirm_token_user, clear_confirm_token, activate_user,
                      log_audit, get_audit_log)
import httpx
import backup
import mailer
from mission import (clean_coords, build_mission_from_params, track_meta,
                     validate_waypoints, render_mission_xml)
from fastapi.responses import Response
from security import RateLimiter, client_ip, tokens_equal, validate_password

APP_VERSION = os.environ.get("APP_VERSION", "dev")
SECRET_FILE = database.DATA_DIR / ".session_secret"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB

LOGIN_LIMIT = int(os.environ.get("POCKET_LOGIN_RATE_LIMIT", "10"))
LOGIN_WINDOW = int(os.environ.get("POCKET_LOGIN_RATE_WINDOW", "900"))
PASSWORD_LIMIT = int(os.environ.get("POCKET_PASSWORD_RATE_LIMIT", "5"))
PASSWORD_WINDOW = int(os.environ.get("POCKET_PASSWORD_RATE_WINDOW", "900"))
REGISTRATION_MODE = os.environ.get("POCKET_REGISTRATION", "off").strip().lower()
REGISTRATION_LIMIT = int(os.environ.get("POCKET_REGISTRATION_RATE_LIMIT", "5"))
REGISTRATION_WINDOW = int(os.environ.get("POCKET_REGISTRATION_RATE_WINDOW", "3600"))
RESET_TOKEN_TTL_SECONDS = int(os.environ.get("POCKET_RESET_TOKEN_TTL", str(24 * 3600)))
CONFIRM_TTL_SECONDS = int(os.environ.get("POCKET_CONFIRM_TTL", str(24 * 3600)))
PUBLIC_URL = os.environ.get("POCKET_PUBLIC_URL", "").strip().rstrip("/")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

login_limiter = RateLimiter()
password_limiter = RateLimiter()
register_limiter = RateLimiter()


def _setup_logging():
    log_file = Path(os.environ.get("POCKET_LOG_FILE", str(database.DATA_DIR / "app.log")))
    handlers = [logging.StreamHandler()]
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file))
    except OSError:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=handlers,
    )


_setup_logging()
logger = logging.getLogger("pocket-log-analyzer")


def _get_session_secret() -> str:
    """Session signing secret: POCKET_SESSION_SECRET env var wins, otherwise a
    persistent per-deployment secret stored next to the DB."""
    env_secret = os.environ.get("POCKET_SESSION_SECRET", "").strip()
    if env_secret:
        if len(env_secret) < 32:
            raise RuntimeError("POCKET_SESSION_SECRET must be at least 32 characters")
        return env_secret
    SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    if SECRET_FILE.exists():
        return SECRET_FILE.read_text().strip()
    secret = secrets.token_hex(32)
    SECRET_FILE.write_text(secret)
    try:
        os.chmod(SECRET_FILE, 0o600)
    except OSError:
        pass
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


async def _scheduled_backup_loop():
    """Daily automatic backup. Enabled only when BACKUP_ENABLED is set."""
    while True:
        try:
            await asyncio.to_thread(backup.run_backup, None, LOG_DIR, None)
            logger.info("scheduled backup completed")
        except Exception:
            logger.exception("scheduled backup failed")
        await asyncio.sleep(24 * 3600)


@asynccontextmanager
async def lifespan(app):
    if os.environ.get("BACKUP_ENABLED", "").lower() in ("1", "true", "yes"):
        asyncio.create_task(_scheduled_backup_loop())
    yield


app = FastAPI(title="Pocket Log Analyzer", lifespan=lifespan)


@app.middleware("http")
async def csrf_protect(request: Request, call_next):
    """Validate a CSRF token on state-changing requests for authenticated
    sessions, and expose the token to templates for the frontend.

    Registered before SessionMiddleware so the session is already loaded when
    this middleware runs."""
    session = request.session
    if session.get("user_id"):
        if not session.get("csrf_token"):
            session["csrf_token"] = secrets.token_urlsafe(32)
        request.state.csrf_token = session["csrf_token"]
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            provided = request.headers.get("X-CSRF-Token", "")
            if not provided and request.headers.get("content-type", "").startswith(
                "application/x-www-form-urlencoded"
            ):
                form = await request.form()
                provided = form.get("csrf_token", "")
            if not tokens_equal(provided, session["csrf_token"]):
                return JSONResponse({"error": "CSRF validation failed"}, status_code=403)
    return await call_next(request)


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
LOG_DIR = Path(os.environ.get("POCKET_LOG_DIR", Path(__file__).parent))

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


@app.middleware("http")
async def error_logging(request: Request, call_next):
    try:
        return await call_next(request)
    except Exception:
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        raise


@app.get("/api/health")
async def api_health():
    """Lightweight, unauthenticated health check for uptime monitors."""
    db_ok = True
    try:
        conn = database._get_conn()
        conn.execute("SELECT 1").fetchone()
        conn.close()
    except Exception:
        db_ok = False
    return {
        "status": "ok" if db_ok else "degraded",
        "version": APP_VERSION,
        "time": datetime.now(timezone.utc).isoformat(),
        "database": "ok" if db_ok else "error",
    }


def _session_user_active(request: Request) -> bool:
    """Verify the session user still exists and is active (disabled/pending
    accounts are locked out immediately, including existing sessions)."""
    user_id = request.session.get("user_id")
    if not user_id:
        return False
    user = get_user_by_id(user_id)
    return bool(user and user.get("status", "active") == "active")


def _public_base(request: Request) -> str:
    """External base URL of the app (with the /flight prefix stripped by nginx)."""
    if PUBLIC_URL:
        return PUBLIC_URL
    proto = request.url.scheme
    if os.environ.get("POCKET_TRUSTED_PROXY", "").lower() in ("1", "true", "yes"):
        forwarded = request.headers.get("x-forwarded-proto")
        if forwarded:
            proto = forwarded.split(",")[0].strip()
    host = request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}/flight"


def require_auth(request: Request):
    if not request.session.get("authenticated") or not request.session.get("user_id"):
        return RedirectResponse(url="/flight/login", status_code=303)
    if not _session_user_active(request):
        request.session.clear()
        return RedirectResponse(url="/flight/login", status_code=303)
    return None


def require_api_auth(request: Request):
    if not request.session.get("authenticated") or not request.session.get("user_id"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not _session_user_active(request):
        request.session.clear()
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return None


def _current(request: Request) -> dict:
    """Return the current user context for data scoping."""
    is_admin = request.session.get("role") == "admin"
    return {
        "user_id": request.session.get("user_id"),
        "username": request.session.get("username"),
        "role": request.session.get("role"),
        "is_admin": is_admin,
    }


def _scope(owner_id, is_admin=False):
    """(owner_id, is_admin) tuple kept small at call sites."""
    return owner_id, is_admin


def _audit(request: Request, action: str, detail: str | None = None) -> None:
    """Append a row to the audit log for the current session user."""
    me = _current(request)
    try:
        log_audit(me.get("user_id"), me.get("username"), action, detail,
                  client_ip(request))
    except Exception:
        pass


def get_stats(owner_id=None, is_admin=False):
    flights = get_all_flights(owner_id, is_admin)
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
    if request.session.get("authenticated") and request.session.get("user_id"):
        return RedirectResponse(url="/flight/", status_code=303)
    return templates.TemplateResponse(request, "login.html",
                                      {"registration": REGISTRATION_MODE})


@app.post("/login")
async def login(request: Request):
    key = f"login:{client_ip(request)}"
    if not login_limiter.allow(key, LOGIN_LIMIT, LOGIN_WINDOW):
        retry = login_limiter.retry_after(key, LOGIN_WINDOW)
        return JSONResponse({"error": "Too many login attempts, try again later"},
                            status_code=429, headers={"Retry-After": str(retry)})
    body = await request.json()
    user = body.get("user", "")
    pwd = body.get("pass", "")
    db_user = verify_user(user, pwd)
    if db_user:
        if db_user.get("status", "active") != "active":
            msg = ("Account pending admin approval" if db_user.get("status") == "pending"
                   else "Account disabled")
            return JSONResponse({"error": msg}, status_code=403)
        request.session["authenticated"] = True
        request.session["user_id"] = db_user["id"]
        request.session["username"] = db_user["username"]
        request.session["role"] = db_user["role"]
        csrf_token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = csrf_token
        try:
            log_audit(db_user["id"], db_user["username"], "login", ip=client_ip(request))
        except Exception:
            pass
        return {"ok": True, "csrf_token": csrf_token}
    try:
        log_audit(None, user, "login_failed", ip=client_ip(request))
    except Exception:
        pass
    return JSONResponse({"error": "Invalid credentials"}, status_code=401)


@app.post("/logout")
async def logout(request: Request):
    _audit(request, "logout")
    request.session.clear()
    return RedirectResponse(url="/flight/login", status_code=303)


def require_admin(request: Request):
    if request.session.get("role") != "admin":
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return None


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    if REGISTRATION_MODE == "off":
        return JSONResponse({"error": "registration is disabled"}, status_code=404)
    return templates.TemplateResponse(request, "register.html", {"mode": REGISTRATION_MODE})


@app.post("/api/register")
async def api_register(request: Request):
    if REGISTRATION_MODE == "off":
        return JSONResponse({"error": "registration is disabled"}, status_code=404)
    key = f"register:{client_ip(request)}"
    if not register_limiter.allow(key, REGISTRATION_LIMIT, REGISTRATION_WINDOW):
        retry = register_limiter.retry_after(key, REGISTRATION_WINDOW)
        return JSONResponse({"error": "Too many attempts, try again later"},
                            status_code=429, headers={"Retry-After": str(retry)})
    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "")
    email = (body.get("email") or "").strip().lower()
    consent = body.get("consent") is True

    if not username or not password:
        return JSONResponse({"error": "username and password are required"}, status_code=400)
    if len(username) < 3:
        return JSONResponse({"error": "username must be at least 3 characters"}, status_code=400)
    if not consent:
        return JSONResponse({"error": "you must accept the privacy policy"}, status_code=400)
    if REGISTRATION_MODE == "confirm":
        if not EMAIL_RE.match(email):
            return JSONResponse({"error": "a valid email is required"}, status_code=400)
        if not mailer.smtp_configured():
            return JSONResponse({"error": "email sending is not configured on this server"},
                                status_code=503)
    elif email and not EMAIL_RE.match(email):
        return JSONResponse({"error": "a valid email is required"}, status_code=400)
    if email and get_user_by_email(email):
        return JSONResponse({"error": "email already registered"}, status_code=409)

    policy_error = validate_password(password)
    if policy_error:
        return JSONResponse({"error": policy_error}, status_code=400)

    now = datetime.now(timezone.utc).isoformat()
    if REGISTRATION_MODE == "confirm":
        status = "pending"
    elif REGISTRATION_MODE == "open":
        status = "active"
    else:
        status = "pending"

    user = create_user(username, password, role="viewer", status=status,
                       email=email or None, privacy_accepted_at=now)
    if not user:
        return JSONResponse({"error": "username already exists"}, status_code=409)
    try:
        log_audit(user["id"], username, "register", f"status={status}", client_ip(request))
    except Exception:
        pass

    if status == "pending" and REGISTRATION_MODE == "confirm":
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        expires = (datetime.now(timezone.utc) + timedelta(seconds=CONFIRM_TTL_SECONDS)).isoformat()
        create_confirm_token(user["id"], token_hash, expires)
        activation_url = f"{_public_base(request)}/confirm?token={token}"
        try:
            mailer.send_activation_email(email, activation_url, username)
        except Exception:
            logging.getLogger(__name__).exception("activation email failed for %s", username)
            return JSONResponse({"error": "could not send the confirmation email"},
                                status_code=500)

    return {"ok": True, "status": status, "mode": REGISTRATION_MODE}


@app.get("/confirm", response_class=HTMLResponse)
async def confirm_page(request: Request, token: str = ""):
    success = False
    error = ""
    if not token:
        error = "Missing confirmation link"
    else:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        user = get_confirm_token_user(token_hash)
        if user:
            activate_user(user["id"])
            clear_confirm_token(user["id"])
            try:
                log_audit(user["id"], user["username"], "account_activated",
                          ip=client_ip(request))
            except Exception:
                pass
            success = True
        else:
            error = "Invalid or expired confirmation link"
    return templates.TemplateResponse(request, "confirm.html",
                                      {"success": success, "error": error, "token": token})


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_page(request: Request):
    user = None
    if request.session.get("user_id"):
        user = get_user_by_id(request.session.get("user_id"))
    return templates.TemplateResponse(request, "privacy.html",
                                      {"user": user,
                                       "privacy_accepted_at": (user or {}).get("privacy_accepted_at") or ""})


@app.get("/reset-password", response_class=HTMLResponse)
async def reset_password_page(request: Request, token: str = ""):
    return templates.TemplateResponse(request, "reset_password.html", {"token": token})


@app.post("/api/reset-password")
async def api_reset_password_submit(request: Request):
    key = f"reset:{client_ip(request)}"
    if not password_limiter.allow(key, PASSWORD_LIMIT, PASSWORD_WINDOW):
        retry = password_limiter.retry_after(key, PASSWORD_WINDOW)
        return JSONResponse({"error": "Too many attempts, try again later"},
                            status_code=429, headers={"Retry-After": str(retry)})
    body = await request.json()
    token = body.get("token", "")
    password = body.get("password", "")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    user = get_reset_token_user(token_hash)
    if not user:
        return JSONResponse({"error": "invalid or expired reset token"}, status_code=400)
    policy_error = validate_password(password)
    if policy_error:
        return JSONResponse({"error": policy_error}, status_code=400)
    change_password(user["id"], password)
    clear_reset_token(user["id"])
    try:
        log_audit(user["id"], user["username"], "password_reset", ip=client_ip(request))
    except Exception:
        pass
    return {"ok": True}


@app.post("/api/users/{user_id}/reset-password")
async def api_issue_reset_password(user_id: int, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    forbidden = require_admin(request)
    if forbidden:
        return forbidden
    user = get_user_by_id(user_id)
    if not user:
        return JSONResponse({"error": "not found"}, status_code=404)
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expires = (datetime.now(timezone.utc) + timedelta(seconds=RESET_TOKEN_TTL_SECONDS)).isoformat()
    create_reset_token(user_id, token_hash, expires)
    _audit(request, "password_reset_link", user["username"])
    return {"reset_url": f"/reset-password?token={token}"}


@app.get("/account", response_class=HTMLResponse)
async def account_page(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    me = _current(request)
    user = get_user_by_id(me["user_id"])
    return templates.TemplateResponse(request, "account.html", {"user": user})


@app.put("/api/account")
async def api_account_update(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    body = await request.json()
    username = body.get("username", "").strip()
    email = (body.get("email") or "").strip().lower()
    if not username:
        return JSONResponse({"error": "username required"}, status_code=400)
    if len(username) < 3:
        return JSONResponse({"error": "username must be at least 3 characters"}, status_code=400)
    if email and not EMAIL_RE.match(email):
        return JSONResponse({"error": "a valid email is required"}, status_code=400)
    user = update_user(me["user_id"], username=username, email=email or None)
    if not user:
        return JSONResponse({"error": "username or email already in use"}, status_code=409)
    request.session["username"] = user["username"]
    _audit(request, "account_update", f"username={user['username']} email={user.get('email') or ''}")
    return {"ok": True, "username": user["username"], "email": user.get("email") or ""}


@app.post("/api/account/change-password")
async def api_account_change_password(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    key = f"password:{client_ip(request)}"
    if not password_limiter.allow(key, PASSWORD_LIMIT, PASSWORD_WINDOW):
        retry = password_limiter.retry_after(key, PASSWORD_WINDOW)
        return JSONResponse({"error": "Too many attempts, try again later"},
                            status_code=429, headers={"Retry-After": str(retry)})
    me = _current(request)
    body = await request.json()
    current_password = body.get("current_password", "")
    new_password = body.get("password", "")
    if not verify_user(me["username"], current_password):
        return JSONResponse({"error": "current password is incorrect"}, status_code=400)
    policy_error = validate_password(new_password)
    if policy_error:
        return JSONResponse({"error": policy_error}, status_code=400)
    change_password(me["user_id"], new_password)
    _audit(request, "password_change")
    return {"ok": True}


@app.put("/api/account/preferences")
async def api_account_preferences(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    body = await request.json()
    prefs = body.get("preferences", {})
    if not isinstance(prefs, dict):
        return JSONResponse({"error": "preferences must be an object"}, status_code=400)
    allowed = {"theme"}
    set_user_preferences(me["user_id"], {k: v for k, v in prefs.items() if k in allowed})
    return {"ok": True}


@app.get("/api/account/export")
async def api_account_export(request: Request):
    """Self-service GDPR data export: the current user's whole dataset."""
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    user = get_user_by_id(me["user_id"])
    payload = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "account": {
            "username": user["username"],
            "role": user["role"],
            "status": user.get("status"),
            "email": user.get("email") or "",
            "created_at": user.get("created_at") or "",
            "preferences": user.get("preferences") or {},
        },
        "flights": get_all_flights(me["user_id"], me["is_admin"]),
        "vehicles": [v.__dict__ for v in get_vehicles(me["user_id"], me["is_admin"])],
    }
    _audit(request, "data_export")
    name = re.sub(r"[^\w\-.]", "_", user["username"]) or "user"
    return Response(
        content=json.dumps(payload, indent=2, ensure_ascii=False),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{name}-data-export.json"'},
    )


@app.delete("/api/account")
async def api_account_delete(request: Request):
    """Self-service account deletion with confirmation (GDPR art. 17)."""
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    user = get_user_by_id(me["user_id"])
    if not user:
        return JSONResponse({"error": "not found"}, status_code=404)
    if user["role"] == "admin":
        return JSONResponse(
            {"error": "an admin account cannot delete itself; ask another admin"},
            status_code=400)
    counts = count_user_data(me["user_id"])
    raw = await request.body()
    body = {}
    if raw:
        try:
            body = json.loads(raw)
        except Exception:
            body = {}
    confirm = str(body.get("confirm", "")).strip().lower() == "true"
    if not confirm:
        return JSONResponse({
            "error": "confirmation required",
            "confirm": True,
            "counts": counts,
        }, status_code=409)
    backup_info = None
    if str(body.get("backup", "")).strip().lower() == "true":
        backup_dir = database.DATA_DIR / "backups"
        backup_info = backup_database(backup_dir)
    username = user["username"]
    result = delete_user_cascade(me["user_id"])
    for fname in result.get("flights", []):
        (LOG_DIR / fname).unlink(missing_ok=True)
    photo_dir = database.DATA_DIR / "vehicle_photos"
    for vid in result.get("vehicles", []):
        for ext in (".jpg", ".jpeg", ".png", ".webp"):
            (photo_dir / f"v{vid}{ext}").unlink(missing_ok=True)
    try:
        log_audit(me["user_id"], username, "account_delete", ip=client_ip(request))
    except Exception:
        pass
    request.session.clear()
    return {
        "deleted": username,
        "flights_deleted": len(result.get("flights", [])),
        "vehicles_deleted": len(result.get("vehicles", [])),
        "backup": str(backup_info) if backup_info else None,
    }


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    me = _current(request)
    flights = get_all_flights(me["user_id"], me["is_admin"])
    stats = get_stats(me["user_id"], me["is_admin"])
    vehicle_stats = get_vehicle_stats(me["user_id"], me["is_admin"])
    vehicles = get_vehicles(me["user_id"], me["is_admin"])
    return templates.TemplateResponse(request, "dashboard.html", {
        "flights": flights, "stats": stats, "vehicle_stats": vehicle_stats, "vehicles": vehicles
    })


@app.get("/flights", response_class=HTMLResponse)
async def flight_list(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    me = _current(request)
    flights = get_all_flights(me["user_id"], me["is_admin"])
    return templates.TemplateResponse(request, "flights.html",
                                      {"flights": flights, "is_admin": me["is_admin"]})


@app.get("/report", response_class=HTMLResponse)
async def report_page(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    me = _current(request)
    flights = get_all_flights(me["user_id"], me["is_admin"])
    stats = get_stats(me["user_id"], me["is_admin"])
    vehicle_stats = get_vehicle_stats(me["user_id"], me["is_admin"])
    return templates.TemplateResponse(request, "report.html", {
        "flights": flights, "stats": stats, "vehicle_stats": vehicle_stats
    })


@app.get("/compare", response_class=HTMLResponse)
async def compare_page(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    me = _current(request)
    flights = get_all_flights(me["user_id"], me["is_admin"])
    return templates.TemplateResponse(request, "compare.html", {"flights": flights})


@app.get("/api/compare-coords")
async def api_compare_coords(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    files = request.query_params.get("files", "")
    if not files:
        return {"flights": []}
    names = [f.strip() for f in files.split(",") if f.strip()]
    result = []
    for name in names:
        flight = get_flight(name, me["user_id"], me["is_admin"])
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
    me = _current(request)
    return {"tags": get_all_tags(me["user_id"], me["is_admin"])}


@app.post("/api/flights/{filename:path}/tags")
async def api_set_tags(request: Request, filename: str):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    body = await request.json()
    tags = body.get("tags", [])
    if not set_flight_tags(filename, tags, me["user_id"], me["is_admin"]):
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"tags": tags}


@app.get("/flight/{filename:path}", response_class=HTMLResponse)
async def flight_detail(request: Request, filename: str):
    redirect = require_auth(request)
    if redirect:
        return redirect
    me = _current(request)
    flight = get_flight(filename, me["user_id"], me["is_admin"])
    if not flight:
        return HTMLResponse("Flight not found", status_code=404)
    flights = get_all_flights(me["user_id"], me["is_admin"])
    idx = None
    for i, f in enumerate(flights):
        if f["filename"] == filename:
            idx = i
            break
    prev_flight = flights[idx + 1]["filename"] if idx is not None and idx + 1 < len(flights) else None
    next_flight = flights[idx - 1]["filename"] if idx is not None and idx - 1 >= 0 else None
    vehicles = get_vehicles(me["user_id"], me["is_admin"])
    return templates.TemplateResponse(request, "flight.html", {
        "flight": flight, "prev": prev_flight, "next": next_flight, "vehicles": vehicles
    })


@app.post("/api/scan")
async def scan_logs(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
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
            default_v = get_default_vehicle(me["user_id"], me["is_admin"])
            if default_v:
                summary.vehicle_id = default_v.id
            save_flight(summary, me["user_id"])
            imported.append(key)
        except Exception:
            pass
    _audit(request, "scan", f"imported={len(imported)}")
    return {"imported": imported}


@app.post("/api/upload")
async def upload_log(request: Request, file: UploadFile = File(...)):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
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
        existing = get_flight(safe_name)
        if existing and existing.get("owner_id") not in (None, me["user_id"]):
            (LOG_DIR / safe_name).unlink(missing_ok=True)
            return JSONResponse(
                {"error": "flight already registered to another user"},
                status_code=409)
        summary = analyze(safe_name, points)
        default_v = get_default_vehicle(me["user_id"], me["is_admin"])
        if default_v:
            summary.vehicle_id = default_v.id
        save_flight(summary, me["user_id"])
        _audit(request, "upload", safe_name)
        return {"imported": safe_name}
    except Exception:
        dest.unlink(missing_ok=True)
        return JSONResponse({"error": "Failed to parse file"}, status_code=400)


@app.post("/api/reprocess")
async def reprocess_flights(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    forbidden = require_admin(request)
    if forbidden:
        return forbidden
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
            existing = get_flight(key, is_admin=True)
            if existing:
                summary.vehicle_id = existing.get("vehicle_id")
            elif key != old_key and get_flight(old_key, is_admin=True) is not None:
                rename_flight(old_key, key, is_admin=True)
                existing = get_flight(key, is_admin=True)
                summary.vehicle_id = existing.get("vehicle_id") if existing else None
            save_flight(summary)
            results.append(key)
        except Exception as e:
            results.append(f"{key}: error: {e}")
    _audit(request, "reprocess", f"count={len(results)}")
    return {"reprocessed": results}


@app.get("/api/flights")
async def api_flights(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    flights = get_all_flights(me["user_id"], me["is_admin"])
    if me["is_admin"]:
        owner = (request.query_params.get("owner") or "").strip()
        if owner:
            flights = [f for f in flights if f.get("owner_username") == owner]
    return flights


@app.get("/api/stats")
async def api_stats(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    return get_stats(me["user_id"], me["is_admin"])


@app.put("/api/flights/{filename:path}/notes")
async def api_save_notes(filename: str, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    raw = await request.body()
    if not raw:
        return JSONResponse({"error": "empty body"}, status_code=400)
    try:
        body = json.loads(raw)
    except Exception as e:
        return JSONResponse({"error": f"invalid JSON: {e}"}, status_code=400)
    notes = body.get("notes", "")[:5000]
    flight = get_flight(filename, me["user_id"], me["is_admin"])
    if not flight:
        return JSONResponse({"error": "not found"}, status_code=404)
    update_notes(filename, notes, me["user_id"], me["is_admin"])
    return {"ok": True}


@app.post("/api/flights/{filename:path}/import-gpx")
async def api_import_gpx(filename: str, request: Request, file: UploadFile = File(...)):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    flight = get_flight(filename, me["user_id"], me["is_admin"])
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
                from datetime import datetime, timezone, timedelta
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
                    if i >= len(gc):
                        gc.append(orig[i])
                    else:
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

        update_flight_track(filename, gpx_coords, stats, me["user_id"], me["is_admin"])
        set_flight_track_source(filename, "gpx", me["user_id"], me["is_admin"])
        return {"ok": True, "points": len(gpx_coords), "stats": stats}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/flights/{filename:path}")
async def api_flight(request: Request, filename: str):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    flight = get_flight(filename, me["user_id"], me["is_admin"])
    if not flight:
        return JSONResponse({"error": "not found"}, status_code=404)
    _audit(request, "flight_view", filename)
    return flight


@app.delete("/api/flights/{filename:path}")
async def api_delete(request: Request, filename: str):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    if not delete_flight(filename, me["user_id"], me["is_admin"]):
        return JSONResponse({"error": "not found"}, status_code=404)
    (LOG_DIR / filename).unlink(missing_ok=True)
    _audit(request, "flight_delete", filename)
    return {"deleted": filename}


@app.put("/api/flights/{filename:path}/vehicle")
async def api_assign_vehicle(filename: str, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    body = await request.json()
    vehicle_id = body.get("vehicle_id")
    if not assign_vehicle_to_flight(filename, vehicle_id, me["user_id"], me["is_admin"]):
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"ok": True}


@app.put("/api/flights/{filename:path}")
async def api_rename(request: Request, filename: str):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    body = await request.json()
    new_name = body.get("new_name", "").strip()
    if not new_name or "/" in new_name or "\\" in new_name:
        return JSONResponse({"error": "invalid name"}, status_code=400)
    if get_flight(new_name, is_admin=True):
        return JSONResponse({"error": "a flight with this name already exists"}, status_code=409)
    old_path = LOG_DIR / filename
    new_path = LOG_DIR / new_name
    if old_path.exists():
        old_path.rename(new_path)
    if not rename_flight(filename, new_name, me["user_id"], me["is_admin"]):
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"filename": new_name}


@app.get("/api/export/{filename:path}")
async def api_export(request: Request, filename: str, format: str = "gpx"):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    flight = get_flight(filename, me["user_id"], me["is_admin"])
    if not flight:
        return JSONResponse({"error": "not found"}, status_code=404)
    _audit(request, "flight_export", f"{filename} ({format})")
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





def _mission_result(body: dict, owner_id: int | None = None, is_admin: bool = False):
    """Build mission waypoints + XML from a request body.

    Two modes:
      - explicit: body["waypoints"] is a list of waypoint dicts; the final
        action (RTH/LAND) is driven by body["final_action"] and re-appended.
      - params:   body["params"] drives clean -> cut -> simplify -> build
    Raises ValueError on validation errors."""
    if body.get("waypoints"):
        waypoints = [dict(w) for w in body["waypoints"]]
        final_action = str(body.get("final_action", "NONE")).upper()
        waypoints = [w for w in waypoints
                     if str(w.get("action", "WAYPOINT")).upper() not in ("RTH", "LAND")]
        if not waypoints:
            raise ValueError("Nessun waypoint nella missione.")
        if final_action == "RTH":
            waypoints.append({"action": "RTH", "lat": 0.0, "lon": 0.0, "alt": 0.0,
                              "p1": 1, "p2": 0, "p3": 0})
        elif final_action == "LAND":
            waypoints.append({"action": "LAND", "lat": 0.0, "lon": 0.0, "alt": 0.0,
                              "p1": 0, "p2": 0, "p3": 0})
    else:
        params = body.get("params") or {}
        filename = params.get("filename", "")
        flight = get_flight(filename, owner_id, is_admin)
        if not flight:
            raise ValueError("Volo non trovato")
        waypoints = build_mission_from_params(flight.get("coordinates", []), params)
    ok, err = validate_waypoints(waypoints)
    if not ok:
        raise ValueError(err)
    for i, w in enumerate(waypoints, 1):
        w["no"] = i
        w["flag"] = 165 if i == len(waypoints) else 0
    meta = track_meta([[w.get("lat", 0), w.get("lon", 0)] for w in waypoints])
    return waypoints, render_mission_xml(waypoints, meta)


def _mission_flight_options(owner_id: int | None = None, is_admin: bool = False):
    return [{"filename": f["filename"], "date": f.get("date", ""),
             "start_time": f.get("start_time", ""),
             "distance_km": f.get("distance_km", 0),
             "duration_s": f.get("duration_s", 0)}
            for f in get_all_flights(owner_id, is_admin)]


@app.get("/mission", response_class=HTMLResponse)
async def mission_page(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    me = _current(request)
    flights = _mission_flight_options(me["user_id"], me["is_admin"])
    return templates.TemplateResponse(request, "mission.html", {"flights": flights, "current": None})


@app.get("/mission/{filename:path}", response_class=HTMLResponse)
async def mission_page_flight(request: Request, filename: str):
    redirect = require_auth(request)
    if redirect:
        return redirect
    me = _current(request)
    flight = get_flight(filename, me["user_id"], me["is_admin"])
    if not flight:
        return HTMLResponse("Flight not found", status_code=404)
    flights = _mission_flight_options(me["user_id"], me["is_admin"])
    current = {"filename": flight["filename"], "date": flight.get("date", ""),
               "start_time": flight.get("start_time", "")}
    return templates.TemplateResponse(request, "mission.html", {"flights": flights, "current": current})


@app.post("/api/mission/track")
async def api_mission_track(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    body = await request.json()
    filename = body.get("filename", "")
    flight = get_flight(filename, me["user_id"], me["is_admin"])
    if not flight:
        return JSONResponse({"error": "Volo non trovato"}, status_code=404)
    coords = clean_coords(flight.get("coordinates", []))
    return {"filename": filename, "coords": coords}


@app.post("/api/mission/preview")
async def api_mission_preview(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    body = await request.json()
    try:
        waypoints, xml = _mission_result(body, me["user_id"], me["is_admin"])
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"waypoints": waypoints, "xml": xml}


@app.post("/api/mission/export")
async def api_mission_export(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    body = await request.json()
    try:
        _waypoints, xml = _mission_result(body, me["user_id"], me["is_admin"])
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    name = re.sub(r"[^\w\-.]", "_", str(body.get("name", "mission"))) or "mission"
    return Response(content=xml, media_type="application/xml",
                    headers={"Content-Disposition": f'attachment; filename="{name}.mission"'})


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
            cos_roll = math.cos(c[8])
            g_val = round(min(1.0 / cos_roll, 10.0), 2) if cos_roll > 0.1 else 0.0
            if len(c) > 34:
                c[34] = g_val
            else:
                c = list(c) + [g_val]
        elif len(c) < 34:
            c = list(c) + [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, '', 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
        if len(c) < 35:
            c = list(c) + [0.0]
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
            gs = [c[34] for c in new_coords if len(c) > 34 and c[34] > 0]
            update_flight_track(key, new_coords, {
                "distance_km": flight.get("distance_km", 0),
                "duration_s": flight.get("duration_s", 0),
                "max_alt_m": flight.get("max_alt_m", 0),
                "min_alt_m": flight.get("min_alt_m", 0),
                "avg_alt_m": flight.get("avg_alt_m", 0),
                "max_speed_kmh": flight.get("max_speed_kmh", 0),
                "avg_speed_kmh": flight.get("avg_speed_kmh", 0),
                "max_g": round(max(gs), 2) if gs else 0,
                "avg_g": round(sum(gs) / len(gs), 2) if gs else 0,
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
    me = _current(request)
    flight = get_flight(filename, me["user_id"], me["is_admin"])
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
    update_flight_events(filename, events, me["user_id"], me["is_admin"])
    gs = [c[34] for c in updated if len(c) > 34 and c[34] > 0]
    update_flight_track(filename, updated, {
        "distance_km": flight.get("distance_km", 0),
        "duration_s": flight.get("duration_s", 0),
        "max_alt_m": flight.get("max_alt_m", 0),
        "min_alt_m": flight.get("min_alt_m", 0),
        "avg_alt_m": flight.get("avg_alt_m", 0),
        "max_speed_kmh": flight.get("max_speed_kmh", 0),
        "avg_speed_kmh": flight.get("avg_speed_kmh", 0),
        "max_g": round(max(gs), 2) if gs else 0,
        "avg_g": round(sum(gs) / len(gs), 2) if gs else 0,
    }, me["user_id"], me["is_admin"])
    return {"ok": True, "matched": matched, "total": len(updated)}


@app.get("/vehicles", response_class=HTMLResponse)
async def vehicles_page(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    me = _current(request)
    vehicles = get_vehicles(me["user_id"], me["is_admin"])
    stats = get_vehicle_stats(me["user_id"], me["is_admin"])
    return templates.TemplateResponse(request, "vehicles.html", {
        "vehicles": vehicles, "stats": stats
    })


@app.get("/api/vehicles")
async def api_vehicles(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    return [{"id": v.id, "name": v.name, "vehicle_type": v.vehicle_type,
             "photo": v.photo, "is_default": v.is_default}
            for v in get_vehicles(me["user_id"], me["is_admin"])]


@app.post("/api/vehicles")
async def api_create_vehicle(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    body = await request.json()
    name = body.get("name", "").strip()
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    vtype = body.get("vehicle_type", "drone")
    is_default = body.get("is_default", False)
    v = create_vehicle(name, vtype, is_default, me["user_id"])
    return {"id": v.id, "name": v.name, "vehicle_type": v.vehicle_type, "is_default": v.is_default}


@app.put("/api/vehicles/{vehicle_id}")
async def api_update_vehicle(vehicle_id: int, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    body = await request.json()
    v = update_vehicle(
        vehicle_id,
        name=body.get("name"),
        vehicle_type=body.get("vehicle_type"),
        is_default=body.get("is_default"),
        owner_id=me["user_id"],
        is_admin=me["is_admin"],
    )
    if not v:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"id": v.id, "name": v.name, "vehicle_type": v.vehicle_type, "is_default": v.is_default}


@app.delete("/api/vehicles/{vehicle_id}")
async def api_delete_vehicle(vehicle_id: int, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    if delete_vehicle(vehicle_id, me["user_id"], me["is_admin"]):
        return {"deleted": vehicle_id}
    return JSONResponse({"error": "not found"}, status_code=404)


@app.post("/api/vehicles/{vehicle_id}/photo")
async def api_vehicle_photo(vehicle_id: int, request: Request, file: UploadFile = File(...)):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    v = get_vehicle(vehicle_id, me["user_id"], me["is_admin"])
    if not v:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not file.filename or not file.filename.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
        return JSONResponse({"error": "Only image files (jpg, png, webp) are supported"}, status_code=400)
    photo_dir = database.DATA_DIR / "vehicle_photos"
    photo_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(file.filename).suffix
    photo_name = f"v{vehicle_id}{ext}"
    photo_path = photo_dir / photo_name
    contents = await file.read()
    if len(contents) > 5 * 1024 * 1024:
        return JSONResponse({"error": "File too large (max 5 MB)"}, status_code=400)
    photo_path.write_bytes(contents)
    set_vehicle_photo(vehicle_id, f"/flight/api/vehicles/{vehicle_id}/photo/img", me["user_id"], me["is_admin"])
    return {"photo": f"/flight/api/vehicles/{vehicle_id}/photo/img"}


@app.get("/api/vehicles/{vehicle_id}/photo/img")
async def api_vehicle_photo_img(vehicle_id: int, request: Request):
    if not request.session.get("user_id"):
        return HTMLResponse("", status_code=404)
    me = _current(request)
    v = get_vehicle(vehicle_id, me["user_id"], me["is_admin"])
    if not v or not v.photo:
        return HTMLResponse("", status_code=404)
    photo_dir = database.DATA_DIR / "vehicle_photos"
    ext_candidates = [".jpg", ".jpeg", ".png", ".webp"]
    for ext in ext_candidates:
        p = photo_dir / f"v{vehicle_id}{ext}"
        if p.exists():
            from fastapi.responses import FileResponse
            resp = FileResponse(str(p))
            resp.headers["Cache-Control"] = "no-store"
            return resp
    return HTMLResponse("", status_code=404)


@app.post("/api/vehicles/apply-default")
async def api_apply_default_vehicle(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    default_v = get_default_vehicle(me["user_id"], me["is_admin"])
    if not default_v:
        return JSONResponse({"error": "No default vehicle set"}, status_code=400)
    flights = get_all_flights(me["user_id"], me["is_admin"])
    updated = 0
    for f in flights:
        if f.get("vehicle_id") is None:
            assign_vehicle_to_flight(f["filename"], default_v.id, me["user_id"], me["is_admin"])
            updated += 1
    return {"updated": updated, "vehicle": default_v.name}


@app.get("/replay3d/{filename:path}", response_class=HTMLResponse)
async def replay3d_page(request: Request, filename: str):
    redirect = require_auth(request)
    if redirect:
        return redirect
    me = _current(request)
    flight = get_flight(filename, me["user_id"], me["is_admin"])
    if not flight:
        return HTMLResponse("Flight not found", status_code=404)
    vehicle = get_vehicle(flight.get("vehicle_id"), me["user_id"], me["is_admin"]) if flight.get("vehicle_id") else None
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
    me = _current(request)
    flight = get_flight(filename, me["user_id"], me["is_admin"])
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
    me = _current(request)
    flights = get_all_flights(me["user_id"], me["is_admin"])
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
    me = _current(request)
    vehicle_id = request.query_params.get("vehicle_id")
    return get_battery_health_by_vehicle(int(vehicle_id) if vehicle_id else None,
                                         me["user_id"], me["is_admin"])


# --- Backup (admin only) ---

@app.get("/api/backups")
async def api_list_backups(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    forbidden = require_admin(request)
    if forbidden:
        return forbidden
    return {"backups": backup.list_backups()}


@app.post("/api/backup")
async def api_run_backup(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    forbidden = require_admin(request)
    if forbidden:
        return forbidden
    try:
        archive = await asyncio.to_thread(backup.run_backup, None, LOG_DIR, None)
    except Exception as e:
        logger.exception("manual backup failed")
        return JSONResponse({"error": str(e)}, status_code=500)
    return {"archive": archive.name, "path": str(archive)}


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
    email = (body.get("email") or "").strip().lower()
    status = body.get("status") or "active"
    if not username or not password:
        return JSONResponse({"error": "username and password required"}, status_code=400)
    if len(username) < 3:
        return JSONResponse({"error": "username must be at least 3 characters"}, status_code=400)
    policy_error = validate_password(password)
    if policy_error:
        return JSONResponse({"error": policy_error}, status_code=400)
    if role not in ("admin", "viewer"):
        return JSONResponse({"error": "role must be admin or viewer"}, status_code=400)
    if status not in ("active", "pending", "disabled"):
        return JSONResponse({"error": "status must be active, pending or disabled"},
                            status_code=400)
    if email and not EMAIL_RE.match(email):
        return JSONResponse({"error": "a valid email is required"}, status_code=400)
    if email and get_user_by_email(email):
        return JSONResponse({"error": "email already registered"}, status_code=409)
    user = create_user(username, password, role, status=status, email=email or None)
    if not user:
        return JSONResponse({"error": "username already exists"}, status_code=409)
    _audit(request, "user_create", f"{username} role={role} status={status}")
    return {"id": user["id"], "username": user["username"],
            "role": user["role"], "status": user["status"], "email": user.get("email") or ""}


@app.put("/api/users/{user_id}")
async def api_update_user(user_id: int, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    forbidden = require_admin(request)
    if forbidden:
        return forbidden
    body = await request.json()
    status = body.get("status")
    if status is not None and status not in ("active", "pending", "disabled"):
        return JSONResponse({"error": "status must be active, pending or disabled"},
                            status_code=400)
    if status == "disabled" and user_id == request.session.get("user_id"):
        return JSONResponse({"error": "you cannot disable your own account"}, status_code=400)
    email = None
    if "email" in body:
        email = (body.get("email") or "").strip().lower() or None
        if email and not EMAIL_RE.match(email):
            return JSONResponse({"error": "a valid email is required"}, status_code=400)
        existing = get_user_by_email(email) if email else None
        if existing and existing["id"] != user_id:
            return JSONResponse({"error": "email already registered"}, status_code=409)
    user = update_user(
        user_id,
        username=body.get("username"),
        role=body.get("role"),
        status=status,
        email=email if "email" in body else None,
    )
    if not user:
        return JSONResponse({"error": "not found"}, status_code=404)
    _audit(request, "user_update",
           f"user={user['username']} role={user['role']} status={user['status']}")
    return {"id": user["id"], "username": user["username"],
            "role": user["role"], "status": user["status"], "email": user.get("email") or ""}


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
    body = {}
    raw = await request.body()
    if raw:
        try:
            body = json.loads(raw)
        except Exception:
            body = {}
    confirm = str(body.get("confirm", "")).strip().lower() == "true"
    counts = count_user_data(user_id)
    if not confirm:
        return JSONResponse({
            "error": "confirmation required",
            "confirm": True,
            "counts": counts,
        }, status_code=409)
    deleted_files = []
    backup_info = None
    if str(body.get("backup", "")).strip().lower() == "true":
        backup_dir = database.DATA_DIR / "backups"
        backup_info = backup_database(backup_dir)
    result = delete_user_cascade(user_id)
    _audit(request, "user_delete", user["username"])
    for fname in result.get("flights", []):
        (LOG_DIR / fname).unlink(missing_ok=True)
        deleted_files.append(fname)
    photo_dir = database.DATA_DIR / "vehicle_photos"
    for vid in result.get("vehicles", []):
        for ext in (".jpg", ".jpeg", ".png", ".webp"):
            (photo_dir / f"v{vid}{ext}").unlink(missing_ok=True)
    return {
        "deleted": user_id,
        "flights_deleted": len(result.get("flights", [])),
        "vehicles_deleted": len(result.get("vehicles", [])),
        "files_removed": deleted_files,
        "backup": str(backup_info) if backup_info else None,
    }


@app.post("/api/users/{user_id}/change-password")
async def api_change_password(user_id: int, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    key = f"password:{client_ip(request)}"
    if not password_limiter.allow(key, PASSWORD_LIMIT, PASSWORD_WINDOW):
        retry = password_limiter.retry_after(key, PASSWORD_WINDOW)
        return JSONResponse({"error": "Too many attempts, try again later"},
                            status_code=429, headers={"Retry-After": str(retry)})
    body = await request.json()
    new_password = body.get("password", "")
    if not new_password:
        return JSONResponse({"error": "password required"}, status_code=400)
    policy_error = validate_password(new_password)
    if policy_error:
        return JSONResponse({"error": policy_error}, status_code=400)
    user = get_user_by_id(user_id)
    if not user:
        return JSONResponse({"error": "not found"}, status_code=404)
    is_admin = request.session.get("role") == "admin"
    is_self = user["username"] == request.session.get("username")
    if not is_admin and not is_self:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    change_password(user_id, new_password)
    return {"ok": True}


# --- Audit log (admin, F4) ---


@app.get("/audit", response_class=HTMLResponse)
async def audit_page(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    forbidden = require_admin(request)
    if forbidden:
        return forbidden
    return templates.TemplateResponse(request, "audit.html", {})


@app.get("/api/audit")
async def api_audit(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    forbidden = require_admin(request)
    if forbidden:
        return forbidden
    limit = 200
    try:
        limit = min(500, max(1, int(request.query_params.get("limit", "200"))))
    except ValueError:
        pass
    username = (request.query_params.get("username") or "").strip() or None
    return get_audit_log(limit=limit, username=username)


# Auto-sync: merge nav telemetry for all flights and recompute derived metrics
# on startup, so every statistic is available from the first load without
# needing a manual rescan-nav on each flight.
if not os.environ.get("FLIGHT_ANALYZER_SKIP_STARTUP_SYNC"):
    sync_all_flights_from_csv()
