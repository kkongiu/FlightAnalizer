"""F1 · Security test suite.

Covers the F1 phase items:
  - #6  rate limiting / brute-force protection on login and password changes
  - #7  CSRF protection on state-changing endpoints
  - #8  secure session cookie flags + session secret from the environment
  - #9  require_auth coverage across all routes
  - #10 password minimum-policy on create and change password
"""
import re
import stat

import pytest


def _login(client, user="admin", pwd="admin"):
    r = client.post("/login", json={"user": user, "pass": pwd})
    assert r.status_code == 200
    token = r.json().get("csrf_token")
    if token:
        client.headers["X-CSRF-Token"] = token
    return r


# --- #8 Secure session cookie + secret from env ---


def test_session_cookie_flags(client):
    r = _login(client)
    set_cookie = r.headers.get("set-cookie", "").lower()
    assert "session_token=" in set_cookie
    assert "httponly" in set_cookie
    assert "samesite=lax" in set_cookie
    assert "secure" in set_cookie


def test_session_secret_from_env(monkeypatch):
    import app as app_mod
    monkeypatch.setenv("POCKET_SESSION_SECRET", "s" * 40)
    assert app_mod._get_session_secret() == "s" * 40


def test_session_secret_too_short_rejected(monkeypatch):
    import app as app_mod
    monkeypatch.setenv("POCKET_SESSION_SECRET", "too-short")
    with pytest.raises(RuntimeError):
        app_mod._get_session_secret()


def test_session_secret_file_fallback(monkeypatch, tmp_path):
    import app as app_mod
    monkeypatch.delenv("POCKET_SESSION_SECRET", raising=False)
    monkeypatch.setattr(app_mod, "SECRET_FILE", tmp_path / ".session_secret")
    first = app_mod._get_session_secret()
    assert app_mod._get_session_secret() == first  # persisted across calls
    assert len(first) >= 32
    mode = stat.S_IMODE((tmp_path / ".session_secret").stat().st_mode)
    assert mode == 0o600


# --- #7 CSRF protection ---


def test_csrf_blocks_post_without_token(client):
    _login(client)
    client.headers.pop("X-CSRF-Token", None)
    assert client.post("/api/scan").status_code == 403


def test_csrf_rejects_wrong_token(client):
    _login(client)
    client.headers["X-CSRF-Token"] = "not-the-real-token"
    assert client.post("/api/scan").status_code == 403


def test_csrf_allows_post_with_token(client):
    _login(client)
    assert client.post("/api/scan").status_code == 200


def test_csrf_blocks_put_delete(client):
    _login(client)
    client.headers["X-CSRF-Token"] = "wrong"
    assert client.put("/api/vehicles", json={"name": "x"}).status_code == 403
    assert client.delete("/api/vehicles/1").status_code == 403


def test_csrf_get_reads_need_no_token(client):
    _login(client)
    client.headers.pop("X-CSRF-Token", None)
    assert client.get("/api/flights").status_code == 200
    assert client.get("/").status_code == 200


def test_csrf_login_is_exempt(client):
    r = client.post("/login", json={"user": "admin", "pass": "admin"})
    assert r.status_code == 200


def test_csrf_logout_form_token(client):
    r = client.post("/login", json={"user": "admin", "pass": "admin"})
    token = r.json()["csrf_token"]
    resp = client.post("/logout", data={"csrf_token": token}, follow_redirects=False)
    assert resp.status_code == 303
    assert client.get("/api/flights").status_code == 401


def test_csrf_logout_form_missing_token_blocked(client):
    _login(client)
    client.headers.pop("X-CSRF-Token", None)
    assert client.post("/logout", data={}, follow_redirects=False).status_code == 403


# --- #6 Rate limiting ---


def test_login_rate_limit(monkeypatch, client):
    monkeypatch.setattr(client.app_mod, "LOGIN_LIMIT", 3)
    for _ in range(3):
        assert client.post("/login", json={"user": "admin", "pass": "wrong"}).status_code == 401
    r = client.post("/login", json={"user": "admin", "pass": "wrong"})
    assert r.status_code == 429
    assert "Retry-After" in r.headers


def test_password_change_rate_limit(monkeypatch, client):
    monkeypatch.setattr(client.app_mod, "PASSWORD_LIMIT", 2)
    _login(client)
    admin = next(u for u in client.get("/api/users").json() if u["role"] == "admin")
    for _ in range(2):
        r = client.post(f"/api/users/{admin['id']}/change-password",
                        json={"password": "NewPass123!"})
        assert r.status_code == 200
    r = client.post(f"/api/users/{admin['id']}/change-password",
                    json={"password": "NewPass123!"})
    assert r.status_code == 429


def test_rate_limiter_window():
    from security import RateLimiter
    rl = RateLimiter()
    assert rl.allow("k", 2, 60)
    assert rl.allow("k", 2, 60)
    assert not rl.allow("k", 2, 60)
    assert rl.retry_after("k", 60) > 0
    rl.clear()
    assert rl.allow("k", 2, 60)


def test_rate_limiter_per_key():
    from security import RateLimiter
    rl = RateLimiter()
    assert rl.allow("a", 1, 60)
    assert not rl.allow("a", 1, 60)
    assert rl.allow("b", 1, 60)


# --- #9 require_auth coverage ---

PUBLIC_PATHS = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc",
                "/api/health", "/login", "/logout"}


def _fill_params(path: str) -> str:
    return (path
            .replace("{filename:path}", "x.csv")
            .replace("{filename}", "x.csv")
            .replace("{vehicle_id}", "1")
            .replace("{user_id}", "1"))


def test_all_routes_require_auth(client):
    leaked = []
    for route in client.app_mod.app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or not methods or path in PUBLIC_PATHS:
            continue
        url = _fill_params(path)
        for method in methods:
            if method in ("HEAD", "OPTIONS"):
                continue
            body = {} if method in ("POST", "PUT", "PATCH", "DELETE") else None
            r = client.request(method, url, follow_redirects=False, json=body)
            if r.status_code in (200, 201, 204):
                leaked.append(f"{method} {path} -> {r.status_code}")
    assert not leaked, f"Routes leaked data unauthenticated:\n" + "\n".join(leaked)


# --- #10 Password policy ---


def test_create_user_rejects_weak_passwords(client):
    _login(client)
    for weak in ("short", "alllowercase", "1234567890", "AbcDefGhij"):
        r = client.post("/api/users", json={"username": "u", "password": weak, "role": "viewer"})
        assert r.status_code == 400, f"{weak!r} should be rejected"


def test_create_user_accepts_strong_password(client):
    _login(client)
    r = client.post("/api/users", json={"username": "strong", "password": "CorrectHorse123",
                                        "role": "viewer"})
    assert r.status_code == 200


def test_change_password_enforces_policy(client):
    _login(client)
    admin = next(u for u in client.get("/api/users").json() if u["role"] == "admin")
    r = client.post(f"/api/users/{admin['id']}/change-password", json={"password": "weak"})
    assert r.status_code == 400


def test_login_page_meta_contains_csrf_token(client):
    _login(client)
    html = client.get("/").text
    m = re.search(r'<meta name="csrf-token" content="([^"]+)">', html)
    assert m and m.group(1) == client.headers["X-CSRF-Token"]
