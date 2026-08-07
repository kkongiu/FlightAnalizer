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
from urllib.parse import quote
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
                      verify_password_for_user,
                      recalculate_home_distances, set_flight_track_source,
                      count_user_data, delete_user_cascade, backup_database,
                      create_reset_token, get_reset_token_user,
                      clear_reset_token, set_user_preferences,
                      get_user_by_email, create_confirm_token,
                      get_confirm_token_user, clear_confirm_token, activate_user,
                      log_audit, get_audit_log,
                      send_message, get_thread, get_conversations_for,
                      mark_thread_read, unread_message_count,
                      delete_conversation_for, get_all_conversations,
                      delete_message_admin, get_conversation_by_pair,
                      add_flight_photo, get_flight_photos, get_cover_photo, delete_flight_photo,
                      set_flight_cover, delete_photos_for_flights, cover_map,
                      create_share, get_share, get_share_by_token,
                      get_shares_for_flight, set_share_enabled, delete_share,
                      delete_shares_for_flights, add_comment, get_comments,
                      add_like, remove_like, get_likes,
                      user_likes,
                      get_vehicle_flight_hours, get_maintenance_items,
                      get_maintenance_item,
                      add_maintenance_item, update_maintenance_item,
                      reset_maintenance_item_service, delete_maintenance_item,
                      get_maintenance_alerts,
                      create_api_token, get_api_tokens, api_token_user,
                      revoke_api_token,
                      get_flight_weather, set_flight_weather,
                      get_friends_with_names, get_friend_requests_received,
                      get_friend_requests_sent, send_friend_request,
                      friend_request_by_id, accept_friend_request,
                      reject_friend_request, remove_friends, are_friends,
                      get_feed_flights, set_flight_visibility,
                       set_flight_shared_group, create_group,
                       get_group, update_group_name, delete_group,
                      add_group_member, remove_group_member, get_group_members,
                      groups_of_user)
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
templates.env.filters["slugify"] = lambda s: re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
templates.env.globals["now"] = datetime.now
LOG_DIR = Path(os.environ.get("POCKET_LOG_DIR", Path(__file__).parent))
PHOTO_DIR = database.DATA_DIR / "flight_photos"

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


def _token_auth_user(request: Request) -> dict | None:
    """Resolve a user from an API token (X-API-Token header), else None."""
    raw = request.headers.get("X-API-Token") or request.query_params.get("api_token")
    if not raw:
        return None
    user_id = api_token_user(raw)
    if not user_id:
        return None
    user = get_user_by_id(user_id)
    if not user or user.get("status") != "active":
        return None
    return {
        "user_id": user_id,
        "username": user.get("username"),
        "role": user.get("role"),
        "is_admin": user.get("role") == "admin",
    }


def _current(request: Request) -> dict:
    """Return the current user context for data scoping."""
    is_admin = request.session.get("role") == "admin"
    return {
        "user_id": request.session.get("user_id"),
        "username": request.session.get("username"),
        "role": request.session.get("role"),
        "is_admin": is_admin,
    }


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
    allowed = {"theme", "notify_new_message"}
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
        "messages": _export_messages(me["user_id"]),
        "photos": _export_photos(me["user_id"], me["is_admin"]),
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
    password = str(body.get("password", ""))
    if not verify_password_for_user(me["user_id"], password):
        return JSONResponse({"error": "password is incorrect"}, status_code=403)
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
    for stored in result.get("photos", []):
        (PHOTO_DIR / stored).unlink(missing_ok=True)
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
    covers = cover_map([f["filename"] for f in flights])
    return templates.TemplateResponse(request, "flights.html",
                                      {"flights": flights, "is_admin": me["is_admin"],
                                       "covers": covers})


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
    groups = groups_of_user(me["user_id"])
    return templates.TemplateResponse(request, "flight.html", {
        "flight": flight, "prev": prev_flight, "next": next_flight,
        "vehicles": vehicles, "groups": groups
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
    me = _token_auth_user(request)
    if not me:
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


@app.get("/api/flights/{filename:path}/photos")
async def api_flight_photos(request: Request, filename: str):
    """List photos for a flight (isolated to owner/admin)."""
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    flight = get_flight(filename, me["user_id"], me["is_admin"])
    if not flight:
        return JSONResponse({"error": "not found"}, status_code=404)
    photos = get_flight_photos(filename)
    for p in photos:
        p["url"] = f"/flight/api/flights/{quote(filename)}/photos/{p['id']}/img"
    return photos


@app.post("/api/flights/{filename:path}/photos")
async def api_flight_photo_upload(filename: str, request: Request,
                                  file: UploadFile = File(...)):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    flight = get_flight(filename, me["user_id"], me["is_admin"])
    if not flight:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not file.filename or not file.filename.lower().endswith(
            (".jpg", ".jpeg", ".png", ".webp")):
        return JSONResponse({"error": "Only image files (jpg, png, webp) are supported"},
                            status_code=400)
    contents = await file.read()
    if len(contents) > 10 * 1024 * 1024:
        return JSONResponse({"error": "File too large (max 10 MB)"}, status_code=400)
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    ext = Path(file.filename).suffix.lower() or ".jpg"
    photo = add_flight_photo(filename, me["user_id"], f"tmp{ext}",
                             file.filename)
    stored_name = f"p{photo['id']}{ext}"
    photo_path = PHOTO_DIR / stored_name
    photo_path.write_bytes(contents)
    with database._get_conn() as conn:
        conn.execute("UPDATE flight_photos SET stored_name = ? WHERE id = ?",
                     (stored_name, photo["id"]))
    if len(get_flight_photos(filename)) == 1:
        set_flight_cover(photo["id"], filename, me["user_id"], me["is_admin"])
    _audit(request, "photo_upload", f"flight={filename}")
    return {"id": photo["id"]}


@app.get("/api/flights/{filename:path}/photos/{photo_id}/img")
async def api_flight_photo_img(filename: str, photo_id: int, request: Request):
    if not request.session.get("user_id"):
        return HTMLResponse("", status_code=404)
    me = _current(request)
    flight = get_flight(filename, me["user_id"], me["is_admin"])
    if not flight:
        return HTMLResponse("", status_code=404)
    photos = get_flight_photos(filename)
    photo = next((p for p in photos if p["id"] == photo_id), None)
    if not photo:
        return HTMLResponse("", status_code=404)
    p = PHOTO_DIR / photo["stored_name"]
    if not p.exists():
        return HTMLResponse("", status_code=404)
    from fastapi.responses import FileResponse
    resp = FileResponse(str(p))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/api/flights/{filename:path}/photos/{photo_id}/cover")
async def api_photo_cover(filename: str, photo_id: int, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    if not get_flight(filename, me["user_id"], me["is_admin"]):
        return JSONResponse({"error": "not found"}, status_code=404)
    if not set_flight_cover(photo_id, filename, me["user_id"], me["is_admin"]):
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"ok": True}


@app.delete("/api/flights/{filename:path}/photos/{photo_id}")
async def api_flight_photo_delete(filename: str, photo_id: int, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    photo = delete_flight_photo(photo_id, me["user_id"], me["is_admin"])
    if not photo:
        return JSONResponse({"error": "not found"}, status_code=404)
    (PHOTO_DIR / photo["stored_name"]).unlink(missing_ok=True)
    _audit(request, "photo_delete", f"{filename}")
    return {"deleted": photo_id}


# --- Sharing (F7): owner management ---


@app.post("/api/flights/{filename:path}/share")
async def api_flight_share_create(filename: str, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    flight = get_flight(filename, me["user_id"], me["is_admin"])
    if not flight:
        return JSONResponse({"error": "not found"}, status_code=404)
    share = create_share(filename, me["user_id"])
    if not share:
        return JSONResponse({"error": "could not create share"}, status_code=400)
    _audit(request, "share_create", filename)
    return _shares_with_urls(get_shares_for_flight(filename))


@app.get("/api/flights/{filename:path}/shares")
async def api_share_list(filename: str, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    flight = get_flight(filename, me["user_id"], me["is_admin"])
    if not flight:
        return JSONResponse({"error": "not found"}, status_code=404)
    return _shares_with_urls(get_shares_for_flight(filename))


@app.post("/api/shares/{share_id}/toggle")
async def api_share_toggle(share_id: int, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    share = get_share(share_id)
    if not share or not _share_owner_or_admin(share["owner_id"], me):
        return JSONResponse({"error": "not found"}, status_code=404)
    set_share_enabled(share_id, not share["enabled"])
    _audit(request, "share_toggle", f"{share['flight_filename']}")
    return _shares_with_urls(get_shares_for_flight(share["flight_filename"]))


@app.delete("/api/shares/{share_id}")
async def api_share_revoke(share_id: int, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    share = get_share(share_id)
    if not share or not _share_owner_or_admin(share["owner_id"], me):
        return JSONResponse({"error": "not found"}, status_code=404)
    delete_share(share_id)
    _audit(request, "share_revoke", f"{share['flight_filename']}")
    return {"revoked": True}


def _shares_with_urls(rows: list[dict]) -> list[dict]:
    for s in rows:
        s["url"] = share_public_url(s["token"])
    return rows


def _share_owner_or_admin(owner_id: int | None, me: dict) -> bool:
    return bool(owner_id and owner_id == me.get("user_id"))


def share_public_url(token: str) -> str:
    base = PUBLIC_URL
    if not base:
        return f"/r/{token}"
    return f"{base}/r/{token}"


@app.get("/api/flights/{filename:path}/weather")
async def api_flight_weather(filename: str, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    flight = get_flight(filename, me["user_id"], me["is_admin"])
    if not flight:
        return JSONResponse({"error": "not found"}, status_code=404)
    cached = get_flight_weather(filename)
    if cached:
        return {"weather": cached}
    w = await _fetch_historical_weather(flight)
    if w is None:
        return {"weather": None}
    set_flight_weather(filename, w)
    return {"weather": w}


async def _fetch_historical_weather(flight: dict) -> dict | None:
    """Best-effort historical weather from Open-Meteo (no key) for the flight
    start coordinates/date. Returns None on any failure (offline, bad coords)."""
    coords = flight.get("coordinates") or []
    if not coords:
        return None
    lat, lon = coords[0][0], coords[0][1]
    date = (flight.get("date") or "")[:10]
    if not date or not lat or not lon:
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as ac:
            r = await ac.get("https://archive-api.open-meteo.com/v1/archive",
                             params={
                                 "latitude": lat, "longitude": lon,
                                 "start_date": date, "end_date": date,
                                 "hourly": "temperature_2m,wind_speed_10m",
                                 "timezone": "UTC"})
            r.raise_for_status()
            data = r.json()
            hourly = (data.get("hourly") or {})
            temps = hourly.get("temperature_2m") or []
            winds = hourly.get("wind_speed_10m") or []
            if not temps:
                return None
            return {
                "avg_temp_c": round(sum(temps) / len(temps), 1),
                "min_temp_c": round(min(temps), 1),
                "max_temp_c": round(max(temps), 1),
                "avg_wind_kmh": round(sum(winds) / len(winds), 1) if winds else None,
                "max_wind_kmh": round(max(winds), 1) if winds else None,
                "date": date,
            }
    except Exception:
        return None


@app.put("/api/flights/{filename:path}/visibility")
async def api_flight_visibility(filename: str, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    body = await request.json()
    visibility = str(body.get("visibility", "")).strip()
    if visibility not in ("public", "contacts", "private"):
        return JSONResponse({"error": "visibility must be public/contacts/private"},
                            status_code=400)
    flight = get_flight(filename, me["user_id"], me["is_admin"])
    if not flight:
        return JSONResponse({"error": "not found"}, status_code=404)
    set_flight_visibility(filename, visibility, me["user_id"], me["is_admin"])
    _audit(request, "flight_visibility", f"{filename}={visibility}")
    return {"visibility": visibility}


@app.put("/api/flights/{filename:path}/group-share")
async def api_flight_group_share(filename: str, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    flight = get_flight(filename, me["user_id"], me["is_admin"])
    if not flight:
        return JSONResponse({"error": "not found"}, status_code=404)
    body = await request.json()
    group_id = body.get("group_id")
    if group_id is not None:
        gid = int(group_id)
        if not get_group(gid):
            return JSONResponse({"error": "group not found"}, status_code=404)
    set_flight_shared_group(filename, group_id, me["user_id"], me["is_admin"])
    _audit(request, "flight_group_share", f"{filename}={group_id}")
    return {"shared_with_group": group_id}


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
    for stored in delete_photos_for_flights([filename]):
        (PHOTO_DIR / stored).unlink(missing_ok=True)
    delete_shares_for_flights([filename])
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


@app.get("/api/export/flights.csv")
async def api_export_flights_csv(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    import csv as _csv
    import io
    flights = get_all_flights(me["user_id"], me["is_admin"])
    cols = ["filename", "date", "start_time", "duration_s", "distance_km",
            "max_alt_m", "min_alt_m", "avg_alt_m", "max_speed_kmh",
            "avg_speed_kmh", "max_vspd_ms", "max_rssi_db", "min_rssi_db",
            "avg_rssi_db", "min_rqly", "avg_rqly", "battery_start_v",
            "battery_end_v", "battery_min_v", "battery_start_pct",
            "battery_end_pct", "battery_consumed_mah", "max_current_a",
            "txbat_v", "sats_max", "max_g", "avg_g", "home_distance_km",
            "glide_ratio"]
    buf = io.StringIO()
    w = _csv.writer(buf)
    w.writerow(cols)
    for f in flights:
        w.writerow([f.get(c, "") if not isinstance(f.get(c), (dict, list)) else json.dumps(f.get(c))
                    for c in cols])
    _audit(request, "export_csv", f"rows={len(flights)}")
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=flights.csv"})


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


_QUICKSTART = [
    {"title": "Import your first log",
     "text": "Upload a CSV/GPX/blackbox log from the Dashboard or the Flights page. The analyzer parses telemetry automatically and computes stats, tracks and events. The supported source is the RadioMaster Pocket CSV produced by the EdgeTX SD Logs special function."},
    {"title": "Assign a vehicle",
     "text": "Create a vehicle under Vehicles, then assign each flight to it (editing the flight). Useful for per-vehicle stats, flight hours and battery health."},
    {"title": "Review flight stats & events",
     "text": "Open any flight for the map (playable timeline), metrics, detected events, tags/notes, weather and photo gallery."},
    {"title": "Add contacts and share",
     "text": "Add other users as contacts, then set a flight's visibility to Contacts or Public to push it into their Feed. Or press Share to create a public link with social buttons, GPX download and comments/likes."},
    {"title": "Keep vehicles maintained",
     "text": "Set part intervals (propellers, motors, battery) on each vehicle; the dashboard alerts you when flight hours reach a deadline."},
    {"title": "Use the tools",
     "text": "Compare flights, generate an INAV mission, render a PDF report, or chat with other users from Messages."},
]


def _help_sections() -> list[dict]:
    return [
        {"icon": "📻", "title": "Logging on the RadioMaster (EdgeTX)",
         "cat": "Setup",
         "summary": "How the log CSV is produced on your radio, step by step.",
         "link": "/flight/flights",
         "steps": [
             "Make sure telemetry reaches the radio: RSSI, LQ, GPS, altitude, speed and attitude (Pitch/Roll/Yaw) must appear on the model page.",
             "ExpressLRS/CRSF: in Betaflight enable Telemetry under the Receiver tab; GPS and attitude come from the flight controller through the receiver.",
             "On the radio open MDL → Telemetry → Discover new sensors (restart the radio if sensors are missing).",
             "Open MDL → Special Functions, press + and add: Trigger = Arm switch (recommended), ON (always), or TELE (when a receiver is connected).",
             "Function = SD Logs, Value = 0.5 s (~2 Hz, matches the attitude sensors), Enable = ON.",
             "Logs are written to the SD card under LOGS/ with a name like <model>-<date>-<time>.csv.",
         ],
         "note": "EdgeTX stops logging when the SD card has less than 50 MB free. Unwanted sensors can be excluded in the Telemetry page (Edit sensor → uncheck Logs). With ExpressLRS a higher telemetry ratio produces denser logs; changes apply after a power cycle.",
         "glossary": [
             {"term": "Trigger", "meaning": "What starts logging: the Arm switch (only when armed), ON (always), or TELE (when a receiver is connected)."},
             {"term": "SD Logs", "meaning": "The EdgeTX special function that writes telemetry to the SD card."},
             {"term": "Value 0.5s", "meaning": "Logging interval: a sample every 0.5 s (~2 Hz)."},
         ]},
        {"icon": "✈️", "title": "Flights & logs",
         "cat": "Flights & analysis",
         "summary": "Upload, parse and inspect FPV telemetry logs.",
         "link": "/flight/flights",
         "steps": [
             "Copy the CSV from the radio's SD card (via USB mass storage) and import it with Import CSV on the Dashboard or Flights page, or use Scan Folder to import everything at once.",
             "GPX files are also supported (no battery/link telemetry, but track, altitude and speed).",
             "Open a flight for the interactive map with a playable timeline and the telemetry charts.",
             "Rescan Nav re-parses the log and refines the track and detected events.",
             "Tags group flights, notes add context, and vehicle assignment links a flight to a drone/model.",
             "Photos: upload a gallery per flight, pick a cover, and see thumbnails in the lists.",
             "Weather: the ☀️ button fetches historical temperature and wind for the flight day.",
         ]},
        {"icon": "📈", "title": "Flight page: charts & stats",
         "cat": "Flights & analysis",
         "summary": "Every chart on the flight page, what it plots and why it matters.",
         "link": "/flight/flights",
         "steps": [
             "All line charts share a cursor that follows the map playback, so you can watch time evolve together.",
             "Hover a chart to read exact values; drag or use the buttons to play back the flight on the map.",
         ],
         "glossary": [
             {"term": "Altitude Profile", "meaning": "Altitude in metres over time. Compare with your planned profile to check smoothness."},
             {"term": "Ground Speed", "meaning": "Speed over ground in km/h. Sudden drops can indicate wind, stall or flight-mode changes."},
             {"term": "RSSI Signal Strength", "meaning": "1RSS and 2RSS in dB (the two receivers). Lower (more negative) is weaker; below about -80 dB you may lose the link."},
             {"term": "Battery Voltage", "meaning": "RxBt voltage in volts over time. Watch the sag under throttle and the recovery in low-throttle segments."},
             {"term": "Vertical Speed", "meaning": "Vertical velocity in m/s. Positive = climbing, negative = descending."},
             {"term": "Heading / Compass", "meaning": "Aircraft heading in degrees."},
             {"term": "Load Factor G", "meaning": "Estimated G-force from the bank angle (1/cos roll). Useful to check how hard a manoeuvre was."},
             {"term": "Link Quality", "meaning": "SNR (RSNR/TSNR in dB) and link quality (RQly/TQly in %). Drops here precede signal loss."},
             {"term": "Current Draw", "meaning": "Battery current in amps. Spikes show high-throttle moments."},
             {"term": "Battery Capacity", "meaning": "Capa in mAh and battery percentage over time; the curve shows consumption and remaining charge."},
             {"term": "Transmitter Battery", "meaning": "Voltage of the transmitter's own battery."},
             {"term": "Throttle vs Current", "meaning": "Scatter plot of throttle position against battery current — engine/ESC efficiency."},
             {"term": "Stick Response", "meaning": "Scatter of aileron→roll and elevator→pitch — how the model follows your inputs."},
             {"term": "Navigation Controls & Attitude", "meaning": "RC inputs (Rud/Ele/Thr/Ail) on one axis and angle (Pitch/Roll/Yaw) on the other."},
         ]},
        {"icon": "🚨", "title": "Events & warnings",
         "cat": "Flights & analysis",
         "summary": "What the colored badges and markers on the track mean.",
         "link": "/flight/flights",
         "steps": [
             "Click a colored badge below the map to jump the timeline to that event; the detail panel shows time, position, telemetry and more.",
             "The flight-mode strip along the timeline is colored by flight mode (acro, angle, RTH, land, failsafe...).",
             "Critical-events checkboxes highlight track points that cross your thresholds (low signal, low SNR, low LQ, high speed) and count the 'peaks'.",
         ],
         "glossary": [
             {"term": "✈ green · takeoff", "meaning": "Start of flight, first takeoff detection."},
             {"term": "✈ red · landing", "meaning": "Landing detection near the ground."},
             {"term": "⚠ red · signal_loss", "meaning": "Radio link lost or critically weak."},
             {"term": "⇄ blue · mode_change", "meaning": "The flight mode changed (e.g. acro → RTH)."},
             {"term": "🔄 pink · acro", "meaning": "Acrobatic manoeuvre detected: a loop or flip/roll, with duration and peak rotation."},
             {"term": "💥 dark red · incident", "meaning": "Possible crash: a fast descent ending near the ground, or a GPS stop / failsafe."},
             {"term": "● public badge", "meaning": "In the feed, marks a flight shared publicly (visible to all contacts)."},
         ]},
        {"icon": "🏷️", "title": "Flags & indicators",
         "cat": "Flights & analysis",
         "summary": "The colored dots and labels you'll meet across the app and what they tell you.",
         "link": "/flight/vehicles",
         "glossary": [
             {"term": "Vehicle maintenance dot", "meaning": "Green = OK, amber = DUE (within 2 flight-hours of the interval), red = OVERDUE, no label = no interval set."},
             {"term": "Share link dot", "meaning": "Green next to a share link = enabled; red = revoked or disabled."},
             {"term": "Public indicator", "meaning": "Green ● on a feed card = the flight is visible to all contacts."},
             {"term": "Unread badge", "meaning": "Blue number on Messages in the top bar = unread conversations; 99+ when large."},
             {"term": "DEFAULT badge (vehicle)", "meaning": "Green label: this vehicle is assigned automatically to new flights without one."},
             {"term": "Role badges (Users)", "meaning": "Purple Admin / gray Indipendente; status badges show active, pending or disabled."},
             {"term": "Cover tag", "meaning": "Marks the photo used as the flight's cover in lists and previews."},
             {"term": "Owner username", "meaning": "Shown on feed cards; each user only sees their own flights in Flights, Dashboard and exports."},
             {"term": "Flight mode colors", "meaning": "The strip color per mode: e.g. acro pink, RTH blue, land red, failsafe brown."},
         ]},
        {"icon": "📊", "title": "Dashboard & stats",
         "cat": "Overview",
         "summary": "What every dashboard card and chart measures.",
         "link": "/flight/",
         "steps": [
             "Total Flights, Total Distance and Total Duration are the headline totals for your account.",
             "Records are your personal bests (distance, altitude, speed, duration, max home distance, glide ratio); click one to open that flight.",
             "Flights per Day and Distance per Week show your activity trend.",
             "Battery Health plots Start/End/Min voltage of every flight; Battery Degradation filters the same data by vehicle.",
             "Recent Flights and the daily/weekly/monthly summary give a quick tabular overview.",
         ],
         "glossary": [
             {"term": "Max Home Dist", "meaning": "Furthest distance from the home point reached during the flight."},
             {"term": "Glide Ratio", "meaning": "Horizontal distance travelled per metre of altitude lost (horizontal / altitude drop)."},
             {"term": "Efficiency", "meaning": "Kilometres flown per 1000 mAh of battery consumed (km/k)."},
             {"term": "Vibration Score", "meaning": "RMS of pitch+roll variance — an estimate of airframe vibration level."},
             {"term": "Max G / Avg G", "meaning": "Load factor estimated from bank angle, capped at 10."},
         ]},
        {"icon": "📅", "title": "Calendar & timeline",
         "cat": "Overview",
         "summary": "A month grid of your activity.",
         "link": "/flight/calendar",
         "steps": [
             "Each day cell shows an ✈️ icon per flight; the number in the day header is the flight count.",
             "Click an icon to open that flight; use ‹ › and Today to navigate months.",
             "Export CSV downloads your whole flight list as a spreadsheet.",
         ]},
        {"icon": "🛠️", "title": "Vehicles & maintenance",
         "cat": "Fleet & maintenance",
         "summary": "Keep a per-drone history, flight hours and maintenance schedule.",
         "link": "/flight/vehicles",
         "steps": [
             "Create vehicles with a type (drone, fixed-wing, heli, glider, other), photo and an optional DEFAULT role.",
             "Each vehicle tracks flight count, total distance, flight hours and best stats from its assigned flights.",
             "Add maintenance items per part: name, interval in flight-hours, last service hour and notes.",
             "The alerts box lists parts that are DUE (≤ 2 h left) or OVERDUE; press Service now to record a service at the current flight hours.",
             "Use Apply default vehicle to assign the default model to every flight without a vehicle.",
         ],
         "note": "Maintenance alerts are also useful before a flight session: check the Vehicles page for anything OVERDUE before you arm.",
         "glossary": [
             {"term": "Flight hours", "meaning": "Total duration of all flights assigned to the vehicle, in hours."},
             {"term": "Interval", "meaning": "How many flight-hours a part may run before service (e.g. 50 h for motors)."},
             {"term": "Last service", "meaning": "The flight-hour value at which the part was last serviced."},
             {"term": "DUE / OVERDUE", "meaning": "DUE = within 2 h of the interval; OVERDUE = past the interval."},
         ]},
        {"icon": "⚖️", "title": "Compare & mission",
         "cat": "Tools",
         "summary": "Analysis helpers: side-by-side tracks and INAV mission building.",
         "link": "/flight/compare",
         "steps": [
             "Compare: select two or more flights and press Compare — the map overlays their tracks in different colors with a legend and a stats table.",
             "Mission: build an INAV waypoint mission from a flight's GPS track or from scratch by clicking the map.",
             "Mission options: altitude mode (fixed/track/offset), cruise speed, final action (RTH/LAND/none), per-waypoint altitude/speed, undo and XML preview.",
             "Export the mission as a .mission file to open in INAV Configurator or mwp.",
         ]},
        {"icon": "📄", "title": "Report & export",
         "cat": "Tools",
         "summary": "Printable summaries and downloadable data.",
         "link": "/flight/report",
         "steps": [
             "Report renders a printable page (totals, fleet, records, monthly summary, full flight table); use Print/PDF in the browser.",
             "Export GPX or KML for a single flight from its page; the aggregated CSV of all flights is on the calendar page.",
         ]},
        {"icon": "👥", "title": "Contacts, feed & groups",
         "cat": "Community",
         "summary": "Follow other pilots and share within your team.",
         "link": "/flight/contacts",
         "steps": [
             "Contacts: send a friend request by username; the other pilot accepts or declines on their Contacts page.",
             "Feed lists flights shared by your contacts (visibility Contacts or Public) plus flights shared to your groups.",
             "Set a flight's visibility on the flight page: Private (only you), Contacts (your friends), Public (all your contacts + feed marker).",
             "Groups (admin): create a team, add members, then share a flight to the whole group from its page.",
         ]},
        {"icon": "💬", "title": "Messages & notifications",
         "cat": "Community",
         "summary": "Private messaging between users.",
         "link": "/flight/messages",
         "steps": [
             "Open a conversation from Messages; the unread badge in the top bar shows how many are waiting.",
             "You can attach a flight to a message so the recipient jumps straight to it (respecting per-user isolation).",
             "Toggle new-message email notifications in Account preferences.",
         ]},
        {"icon": "🔗", "title": "Sharing & social",
         "cat": "Community",
         "summary": "Public pages and community feedback.",
         "link": "/flight/flights",
         "steps": [
             "On a flight press Share to create a public link; the owner can enable/disable or revoke it anytime.",
             "The public page shows the map, stats, GPX download and social share buttons (WhatsApp, Telegram, X, Facebook).",
             "Anyone with the link can like and comment on the shared flight without logging in.",
         ]},
        {"icon": "🔐", "title": "Privacy & account",
         "cat": "Account & admin",
         "summary": "Control your data.",
         "link": "/flight/account",
         "steps": [
             "Account: change password, email and preferences; manage your upload API tokens.",
             "Export a copy of all your data (GDPR art. 20) — flights, photos and messages.",
             "Delete your account self-service; you'll see a summary of what will be removed first.",
         ]},
        {"icon": "🛡️", "title": "Admin & API",
         "cat": "Account & admin",
         "summary": "Operations, users and external integrations.",
         "link": "/flight/users",
         "steps": [
             "Users: approve registrations, assign roles (admin/indipendente), set status and disable accounts.",
             "Audit log records who did what and who viewed what (admin only).",
             "API tokens allow external scripts to upload logs automatically; create a token under Account and use it with curl.",
         ],
         "glossary": [
             {"term": "X-API-Token header", "meaning": "Send your token in this HTTP header when calling the upload endpoint."},
             {"term": "Auto-upload", "meaning": "A script on your computer or phone pushes new logs to the server without opening the app."},
             {"term": "Revoked token", "meaning": "A token set to revoked no longer works; the raw value is shown only once at creation."},
         ]},
    ]


@app.get("/help", response_class=HTMLResponse)
async def help_page(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    sections = _help_sections()
    categories: list[dict] = []
    seen: set[str] = set()
    for sec in sections:
        cat = sec.get("cat", "Other")
        if cat not in seen:
            seen.add(cat)
            categories.append({"name": cat, "sections": []})
        categories[-1]["sections"].append(sec)
    return templates.TemplateResponse(request, "help.html", {
        "quickstart": _QUICKSTART,
        "sections": sections,
        "categories": categories,
    })


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


@app.get("/api/users/search")
async def api_search_users(request: Request, q: str = ""):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    q = q.strip().lower()
    users = [u for u in get_all_users()
             if u["id"] != me["user_id"] and (not q or q in u["username"].lower())]
    return {"users": users[:20]}


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
    me = _current(request)
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
    password = str(body.get("password", ""))
    if not verify_password_for_user(me["user_id"], password):
        return JSONResponse({"error": "password is incorrect"}, status_code=403)
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
    for stored in result.get("photos", []):
        (PHOTO_DIR / stored).unlink(missing_ok=True)
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


# --- Messaging (F5) ---


@app.get("/messages", response_class=HTMLResponse)
async def messages_page(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(request, "messages.html", {})


def _other_participants(request: Request) -> list[dict]:
    """Usernames currently holding a conversation with the current user."""
    me = _current(request)
    return [
        {"other_id": c["other_id"], "other_username": c["other_username"]}
        for c in get_conversations_for(me["user_id"])
    ]


@app.get("/api/messages/unread-count")
async def api_messages_unread(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    return {"unread": unread_message_count(me["user_id"])}


@app.get("/api/messages/conversations")
async def api_messages_conversations(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    return get_conversations_for(me["user_id"])


@app.get("/api/messages/conversations/{other_id}")
async def api_messages_thread(other_id: int, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    if other_id == me["user_id"]:
        return JSONResponse({"error": "invalid recipient"}, status_code=400)
    conv = get_conversation_by_pair(me["user_id"], other_id)
    if not conv:
        return {"messages": [], "other": None}
    thread = get_thread(me["user_id"], other_id)
    mark_thread_read(me["user_id"], other_id)
    other = get_user_by_id(other_id)
    return {
        "messages": thread,
        "other": {"id": other["id"], "username": other["username"]} if other else None,
    }


@app.post("/api/messages")
async def api_messages_send(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    body = await request.json()
    recipient_spec = str(body.get("to", "")).strip()
    if not recipient_spec:
        return JSONResponse({"error": "recipient is required"}, status_code=400)
    recipient = get_user(recipient_spec) or get_user_by_id(recipient_spec)
    if not recipient:
        return JSONResponse({"error": "recipient not found"}, status_code=404)
    if recipient["status"] != "active":
        return JSONResponse({"error": "recipient account is not active"}, status_code=400)
    recipient_id = recipient["id"]
    if recipient_id == me["user_id"]:
        return JSONResponse({"error": "cannot message yourself"}, status_code=400)
    text = str(body.get("body", "")).strip()
    flight_file = (str(body.get("flight_file", "")) or "").strip() or None
    if not text and not flight_file:
        return JSONResponse({"error": "message is empty"}, status_code=400)
    if flight_file:
        # #29: only attach a flight the sender can access (isolation).
        if not get_flight(flight_file, me["user_id"], me["is_admin"]):
            return JSONResponse({"error": "flight not accessible"}, status_code=404)
    msg = send_message(me["user_id"], recipient_id, text, flight_file)
    if not msg:
        return JSONResponse({"error": "could not send message"}, status_code=400)
    _audit(request, "message_send", f"to={recipient['username']}")
    notify_new_message_notification(recipient, {"username": me["username"]},
                                    text, flight_file)
    return msg


@app.delete("/api/messages/conversations/{other_id}")
async def api_messages_delete_conversation(other_id: int, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    ok = delete_conversation_for(me["user_id"], other_id)
    if not ok:
        return JSONResponse({"error": "conversation not found"}, status_code=404)
    _audit(request, "conversation_delete", f"with={other_id}")
    return {"deleted": True}


# --- Admin messaging management (F5 #27) ---


@app.get("/api/messages/admin/conversations")
async def api_messages_admin_conversations(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    forbidden = require_admin(request)
    if forbidden:
        return forbidden
    return get_all_conversations()


@app.get("/api/messages/admin/conversations/{conversation_id}")
async def api_messages_admin_thread(conversation_id: int, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    forbidden = require_admin(request)
    if forbidden:
        return forbidden
    with database._get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id ASC",
            (conversation_id,)).fetchall()
    return [dict(r) for r in row]


@app.delete("/api/messages/admin/{message_id}")
async def api_messages_admin_delete_message(message_id: int, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    forbidden = require_admin(request)
    if forbidden:
        return forbidden
    if not delete_message_admin(message_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    _audit(request, "message_delete_admin", f"message={message_id}")
    return {"deleted": True}


def _export_messages(user_id: int) -> list[dict]:
    """Every message the user sent or received, across all conversations."""
    out = []
    for c in get_conversations_for(user_id):
        for m in get_thread(user_id, c["other_id"]):
            out.append(m)
    return out


def _export_photos(user_id: int, is_admin: bool) -> list[dict]:
    """Photo metadata for every flight the user can access."""
    out = []
    for f in get_all_flights(user_id, is_admin):
        for p in get_flight_photos(f["filename"]):
            out.append({"flight": f["filename"], "stored_name": p["stored_name"],
                        "original_name": p["original_name"] or "",
                        "captured_at": p["captured_at"] or "",
                        "is_cover": bool(p["is_cover"])})
    return out


def notify_new_message_notification(recipient: dict, sender: dict, text: str,
                                    flight_file: str | None) -> None:
    """Send an email notification for a new message (F5 #28).

    Requires SMTP configuration and an email on the recipient account; the
    recipient can disable it via the notify_new_message preference."""
    email = (recipient.get("email") or "").strip()
    prefs = recipient.get("preferences") or {}
    if not email or not mailer.smtp_configured():
        return
    if not prefs.get("notify_new_message", True):
        return
    sender_name = sender.get("username") or "Someone"
    try:
        base = PUBLIC_URL or ""
        link = f"{base}/flight/messages"
        body_text = (f"Ciao {recipient['username']},\n\n"
                     f"{sender_name} ti ha inviato un messaggio su Pocket Log Analyzer:\n\n"
                     f"{text}\n\n"
                     f"Apri la conversazione: {link}\n")
        body_html = (f"<p>Ciao <strong>{recipient['username']}</strong>,</p>"
                     f"<p><strong>{sender_name}</strong> ti ha inviato un messaggio:</p>"
                     f"<blockquote>{text}</blockquote>"
                     f'<p><a href="{link}">Apri la conversazione</a></p>')
        mailer.send_email(email, "Nuovo messaggio - Pocket Log Analyzer",
                          body_text, body_html)
    except Exception:
        logger = logging.getLogger(__name__)
        logger.warning("failed to send message notification email", exc_info=True)


# --- F8: Maintenance (#41) ---


def _vehicle_owned(vehicle_id: int, me: dict) -> bool:
    v = get_vehicle(vehicle_id, me["user_id"], False)
    return v is not None


@app.get("/api/vehicles/{vehicle_id}/maintenance")
async def api_vehicle_maintenance(vehicle_id: int, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    if not _vehicle_owned(vehicle_id, me):
        return JSONResponse({"error": "not found"}, status_code=404)
    return {
        "flight_hours": get_vehicle_flight_hours(vehicle_id),
        "items": get_maintenance_items(vehicle_id),
    }


@app.post("/api/vehicles/{vehicle_id}/maintenance")
async def api_maintenance_add(vehicle_id: int, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    if not _vehicle_owned(vehicle_id, me):
        return JSONResponse({"error": "not found"}, status_code=404)
    body = await request.json()
    part_name = str(body.get("part_name", "")).strip()
    if not part_name:
        return JSONResponse({"error": "part_name is required"}, status_code=400)
    interval_hours = float(body.get("interval_hours", 0) or 0)
    item = add_maintenance_item(vehicle_id, part_name, interval_hours,
                                str(body.get("notes", "")))
    _audit(request, "maintenance_add", f"vehicle={vehicle_id} part={part_name}")
    return {"maintenance": item}


@app.put("/api/maintenance/{item_id}")
async def api_maintenance_update(item_id: int, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    item = get_maintenance_item(item_id)
    if not item or not _vehicle_owned(item["vehicle_id"], me):
        return JSONResponse({"error": "not found"}, status_code=404)
    body = await request.json()
    ok = update_maintenance_item(
        item_id, str(body.get("part_name", item["part_name"])).strip(),
        float(body.get("interval_hours", item["interval_hours"]) or 0),
        str(body.get("notes", item["notes"])),
        float(body.get("last_service_hours", item["last_service_hours"]) or 0))
    if not ok:
        return JSONResponse({"error": "not found"}, status_code=404)
    _audit(request, "maintenance_update", f"item={item_id}")
    return {"updated": True}


@app.post("/api/maintenance/{item_id}/service")
async def api_maintenance_service(item_id: int, request: Request):
    """Mark a maintenance item as serviced now (records current flight hours)."""
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    item = get_maintenance_item(item_id)
    if not item or not _vehicle_owned(item["vehicle_id"], me):
        return JSONResponse({"error": "not found"}, status_code=404)
    hours = get_vehicle_flight_hours(item["vehicle_id"])
    reset_maintenance_item_service(item_id, hours)
    _audit(request, "maintenance_service", f"item={item_id} hours={hours}")
    return {"serviced": True, "flight_hours": hours}


@app.delete("/api/maintenance/{item_id}")
async def api_maintenance_delete(item_id: int, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    item = get_maintenance_item(item_id)
    if not item or not _vehicle_owned(item["vehicle_id"], me):
        return JSONResponse({"error": "not found"}, status_code=404)
    if not delete_maintenance_item(item_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    _audit(request, "maintenance_delete", f"item={item_id}")
    return {"deleted": True}


@app.get("/api/maintenance/alerts")
async def api_maintenance_alerts(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    return {"alerts": get_maintenance_alerts(me["user_id"], me["is_admin"])}


# --- F8: CSV export & calendar (#42) ---


@app.get("/calendar", response_class=HTMLResponse)
async def calendar_page(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    me = _current(request)
    flights = get_all_flights(me["user_id"], me["is_admin"])
    return templates.TemplateResponse(request, "calendar.html", {
        "flights": flights,
    })


# --- F8: API tokens (#43) ---


@app.get("/api/tokens")
async def api_tokens_list(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    return {"tokens": get_api_tokens(me["user_id"])}


@app.post("/api/tokens")
async def api_tokens_create(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    body = await request.json()
    name = str(body.get("name", "")).strip() or "auto-upload"
    raw = create_api_token(me["user_id"], name)
    if not raw:
        return JSONResponse({"error": "failed to create token"}, status_code=500)
    _audit(request, "api_token_create", f"name={name}")
    return {"token": raw, "name": name}


@app.delete("/api/tokens/{token_id}")
async def api_tokens_revoke(token_id: int, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    if not revoke_api_token(me["user_id"], token_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    _audit(request, "api_token_revoke", f"token={token_id}")
    return {"revoked": True}


# --- F8: Weather (#44) ---

_WEATHER_CACHE: dict[str, dict] = {}


@app.get("/api/flights/{filename:path}/weather")
async def api_flight_weather(filename: str, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    flight = get_flight(filename, me["user_id"], me["is_admin"])
    if not flight:
        return JSONResponse({"error": "not found"}, status_code=404)
    cached = get_flight_weather(filename)
    if cached:
        return {"weather": cached}
    w = await _fetch_historical_weather(flight)
    if w is None:
        return {"weather": None}
    set_flight_weather(filename, w)
    return {"weather": w}


async def _fetch_historical_weather(flight: dict) -> dict | None:
    """Best-effort historical weather from Open-Meteo (no key) for the flight
    start coordinates/date. Returns None on any failure (offline, bad coords)."""
    import asyncio
    coords = flight.get("coordinates") or []
    if not coords:
        return None
    lat, lon = coords[0][0], coords[0][1]
    date = (flight.get("date") or "")[:10]
    if not date or not lat or not lon:
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as ac:
            r = await ac.get("https://archive-api.open-meteo.com/v1/archive",
                             params={
                                 "latitude": lat, "longitude": lon,
                                 "start_date": date, "end_date": date,
                                 "hourly": "temperature_2m,wind_speed_10m",
                                 "timezone": "UTC"})
            r.raise_for_status()
            data = r.json()
            hourly = (data.get("hourly") or {})
            temps = hourly.get("temperature_2m") or []
            winds = hourly.get("wind_speed_10m") or []
            if not temps:
                return None
            return {
                "avg_temp_c": round(sum(temps) / len(temps), 1),
                "min_temp_c": round(min(temps), 1),
                "max_temp_c": round(max(temps), 1),
                "avg_wind_kmh": round(sum(winds) / len(winds), 1) if winds else None,
                "max_wind_kmh": round(max(winds), 1) if winds else None,
                "date": date,
            }
    except Exception:
        return None


# --- F7 extras: contacts, feed, groups ---


@app.get("/api/contacts")
async def api_contacts(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    return {
        "received": get_friend_requests_received(me["user_id"]),
        "sent": get_friend_requests_sent(me["user_id"]),
        "friends": get_friends_with_names(me["user_id"]),
    }


@app.post("/api/contacts")
async def api_contact_request(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    body = await request.json()
    peer = int(body.get("user_id"))
    if peer == me["user_id"]:
        return JSONResponse({"error": "cannot add yourself"}, status_code=400)
    if not get_user_by_id(peer):
        return JSONResponse({"error": "user not found"}, status_code=404)
    if not send_friend_request(me["user_id"], peer):
        return JSONResponse({"error": "request already sent or already friends"},
                            status_code=409)
    _audit(request, "contact_request", f"to={peer}")
    return {"requested": True}


@app.post("/api/contacts/{request_id}/accept")
async def api_contact_accept(request_id: int, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    if not accept_friend_request(request_id, me["user_id"]):
        return JSONResponse({"error": "not found"}, status_code=404)
    _audit(request, "contact_accept", f"request={request_id}")
    return {"accepted": True}


@app.post("/api/contacts/{request_id}/decline")
async def api_contact_decline(request_id: int, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    if not reject_friend_request(request_id, me["user_id"]):
        return JSONResponse({"error": "not found"}, status_code=404)
    _audit(request, "contact_decline", f"request={request_id}")
    return {"declined": True}


@app.delete("/api/contacts/{peer_id}")
async def api_contact_remove(peer_id: int, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    remove_friends(me["user_id"], peer_id)
    _audit(request, "contact_remove", f"peer={peer_id}")
    return {"removed": True}


@app.get("/api/feed")
async def api_feed(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    return {"flights": get_feed_flights(me["user_id"])}


@app.get("/contacts", response_class=HTMLResponse)
async def contacts_page(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(request, "contacts.html", {})


@app.get("/feed", response_class=HTMLResponse)
async def feed_page(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    me = _current(request)
    flights = get_feed_flights(me["user_id"])
    return templates.TemplateResponse(request, "feed.html", {"flights": flights})


# --- Groups / teams ---


@app.get("/api/groups")
async def api_groups(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    groups = groups_of_user(me["user_id"])
    out = []
    for g in groups:
        out.append({**g, "members": get_group_members(g["id"])})
    return {"groups": out}


@app.post("/api/groups")
async def api_group_create(request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    if not me["is_admin"]:
        return JSONResponse({"error": "admin only"}, status_code=403)
    body = await request.json()
    name = str(body.get("name", "")).strip()
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    g = create_group(name, me["user_id"])
    if g:
        add_group_member(g["id"], me["user_id"])
    _audit(request, "group_create", name)
    return {"group": g}


@app.put("/api/groups/{group_id}")
async def api_group_update(group_id: int, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    if not me["is_admin"]:
        return JSONResponse({"error": "admin only"}, status_code=403)
    body = await request.json()
    name = str(body.get("name", "")).strip()
    if not update_group_name(group_id, name):
        return JSONResponse({"error": "not found"}, status_code=404)
    _audit(request, "group_update", f"id={group_id}")
    return {"updated": True}


@app.delete("/api/groups/{group_id}")
async def api_group_delete(group_id: int, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    if not me["is_admin"]:
        return JSONResponse({"error": "admin only"}, status_code=403)
    if not delete_group(group_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    _audit(request, "group_delete", f"id={group_id}")
    return {"deleted": True}


@app.post("/api/groups/{group_id}/members")
async def api_group_member_add(group_id: int, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    if not me["is_admin"]:
        return JSONResponse({"error": "admin only"}, status_code=403)
    body = await request.json()
    user_id = int(body.get("user_id"))
    add_group_member(group_id, user_id)
    _audit(request, "group_member_add", f"group={group_id} user={user_id}")
    return {"added": True}


@app.delete("/api/groups/{group_id}/members/{user_id}")
async def api_group_member_remove(group_id: int, user_id: int, request: Request):
    denied = require_api_auth(request)
    if denied:
        return denied
    me = _current(request)
    if not me["is_admin"]:
        return JSONResponse({"error": "admin only"}, status_code=403)
    remove_group_member(group_id, user_id)
    _audit(request, "group_member_remove", f"group={group_id} user={user_id}")
    return {"removed": True}


@app.get("/groups", response_class=HTMLResponse)
async def groups_page(request: Request):
    redirect = require_auth(request)
    if redirect:
        return redirect
    me = _current(request)
    groups = groups_of_user(me["user_id"])
    users = get_all_users() if me["is_admin"] else []
    out = [{**g, "members": get_group_members(g["id"])} for g in groups]
    return templates.TemplateResponse(request, "groups.html", {
        "groups": out, "is_admin": me["is_admin"], "users": users,
    })


# --- Public sharing (F7): view, social, og, gpx, comments/likes ---


def _resolve_public_share(token: str) -> tuple[dict | None, dict | None]:
    """Return (share, flight) for a valid enabled share, else (None, None)."""
    share = get_share_by_token(token)
    if not share:
        return None, None
    flight = get_flight(share["flight_filename"], None, True)
    if not flight:
        return None, None
    return share, flight


_PUBLIC_FLIGHT_KEYS = ["filename", "date", "start_time", "duration_s",
                       "distance_km", "max_alt_m", "min_alt_m", "avg_alt_m",
                       "max_speed_kmh", "avg_speed_kmh", "max_vspd_ms",
                       "max_rssi_db", "min_rssi_db", "avg_rssi_db",
                       "battery_start_v", "battery_end_v", "battery_start_pct",
                       "battery_end_pct", "flight_modes", "home_distance_km",
                       "glide_ratio", "max_g", "avg_g", "coordinates"]


def _public_flight_payload(flight: dict) -> dict:
    return {k: flight.get(k) for k in _PUBLIC_FLIGHT_KEYS}


@app.get("/r/{token}", response_class=HTMLResponse)
async def public_share_page(token: str, request: Request):
    share, flight = _resolve_public_share(token)
    if not share:
        return HTMLResponse("Shared flight not found or revoked", status_code=404)
    cover = get_cover_photo(flight["filename"])
    og_image = f"/r/{token}/cover" if cover else ""
    return templates.TemplateResponse(request, "share.html", {
        "flight": _public_flight_payload(flight),
        "token": token,
        "owner": get_user_by_id(share["owner_id"]),
        "og_image": og_image,
        "og_url": share_public_url(token),
        "page_title": f"{flight.get('date')} {flight.get('start_time')} - Shared Flight",
    })


@app.get("/r/{token}/gpx")
async def public_share_gpx(token: str):
    share, flight = _resolve_public_share(token)
    if not share:
        return JSONResponse({"error": "not found"}, status_code=404)
    coords = flight.get("coordinates", [])
    safe_name = _xml_escape(flight["filename"])
    from datetime import timezone as _tz
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1/1">',
             f'  <trk><name>{safe_name}</name><trkseg>']
    for c in coords:
        t = datetime.fromtimestamp(c[4], tz=_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if c[4] else ""
        lines.append(f'    <trkpt lat="{c[0]}" lon="{c[1]}"><ele>{c[2]}</ele><time>{t}</time></trkpt>')
    lines.append('  </trkseg></trk></gpx>')
    return HTMLResponse("\n".join(lines), media_type="application/gpx+xml",
                        headers={"Content-Disposition": f'attachment; filename="{safe_name}.gpx"'})


@app.get("/r/{token}/cover")
async def public_share_cover(token: str, request: Request):
    share, flight = _resolve_public_share(token)
    if not share:
        return HTMLResponse("", status_code=404)
    cover = get_cover_photo(flight["filename"])
    if not cover:
        return HTMLResponse("", status_code=404)
    p = PHOTO_DIR / cover["stored_name"]
    if not p.exists():
        return HTMLResponse("", status_code=404)
    from fastapi.responses import FileResponse
    return FileResponse(str(p))


# --- public board API (no login) ---


@app.get("/public/api/board/{token}")
async def api_public_board(token: str):
    share, flight = _resolve_public_share(token)
    if not share:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {
        "share_id": share["id"],
        "flight": _public_flight_payload(flight),
        "comments": get_comments(share["id"]),
        "likes": len(get_likes(share["id"])),
    }


@app.post("/public/api/board/{token}/comments")
async def api_public_comment_add(token: str, request: Request):
    share, _ = _resolve_public_share(token)
    if not share:
        return JSONResponse({"error": "not found"}, status_code=404)
    body = await request.json()
    username = (str(body.get("username", "")).strip()[:80] or "Guest")
    comment = add_comment(share["id"], username, str(body.get("body", "")))
    if not comment:
        return JSONResponse({"error": "comment is empty"}, status_code=400)
    return comment


@app.post("/public/api/board/{token}/like")
async def api_public_like_toggle(token: str, request: Request):
    share, _ = _resolve_public_share(token)
    if not share:
        return JSONResponse({"error": "not found"}, status_code=404)
    body = await request.json()
    username = (str(body.get("username", "")).strip() or "Guest")
    if user_likes(share["id"], username):
        remove_like(share["id"], username)
        liked = False
    else:
        add_like(share["id"], username)
        liked = True
    return {"liked": liked, "count": len(get_likes(share["id"]))}


# Auto-sync: merge nav telemetry for all flights and recompute derived metrics
# on startup, so every statistic is available from the first load without
# needing a manual rescan-nav on each flight.
if not os.environ.get("FLIGHT_ANALYZER_SKIP_STARTUP_SYNC"):
    sync_all_flights_from_csv()
