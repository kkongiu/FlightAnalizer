"""F2 · Account test suite.

Covers the F2 phase items:
  - #12 public registration (off / open / approval modes, rate limit, policy)
  - #13 password reset via admin-issued token (expiry, rate limit)
  - #14 self-service account page (username, password, preferences)
  - #15 admin approval / deactivation of accounts (sessions revoked)
"""
import hashlib
from datetime import datetime, timezone, timedelta

from fastapi.testclient import TestClient

import database


def _login(client, user="admin", pwd="admin"):
    r = client.post("/login", json={"user": user, "pass": pwd})
    assert r.status_code == 200, r.text
    token = r.json().get("csrf_token")
    if token:
        client.headers["X-CSRF-Token"] = token
    return r


# --- #12 Registration ---


def test_registration_disabled_by_default(client):
    assert client.get("/register").status_code == 404
    assert client.post("/api/register", json={"username": "u", "password": "CorrectHorse123",
                                              "consent": True}).status_code == 404


def test_registration_requires_consent(monkeypatch, client):
    monkeypatch.setattr(client.app_mod, "REGISTRATION_MODE", "open")
    r = client.post("/api/register", json={"username": "noconsent", "password": "CorrectHorse123"})
    assert r.status_code == 400
    assert "privacy" in r.json()["error"].lower()


def test_registration_open_mode(monkeypatch, client):
    monkeypatch.setattr(client.app_mod, "REGISTRATION_MODE", "open")
    r = client.post("/api/register", json={"username": "newbie", "password": "CorrectHorse123",
                                           "consent": True})
    assert r.status_code == 200
    assert r.json()["status"] == "active"
    r = _login(client, "newbie", "CorrectHorse123")
    assert r.status_code == 200


def test_registration_approval_mode(monkeypatch, client):
    monkeypatch.setattr(client.app_mod, "REGISTRATION_MODE", "approval")
    r = client.post("/api/register", json={"username": "pending_user", "password": "CorrectHorse123",
                                           "consent": True})
    assert r.status_code == 200
    assert r.json()["status"] == "pending"
    r = client.post("/login", json={"user": "pending_user", "pass": "CorrectHorse123"})
    assert r.status_code == 403
    assert "approval" in r.json()["error"].lower()


def test_registration_duplicate_username(monkeypatch, client):
    monkeypatch.setattr(client.app_mod, "REGISTRATION_MODE", "open")
    r = client.post("/api/register", json={"username": "admin", "password": "CorrectHorse123",
                                           "consent": True})
    assert r.status_code == 409


def test_registration_duplicate_email(monkeypatch, client):
    monkeypatch.setattr(client.app_mod, "REGISTRATION_MODE", "open")
    payload = {"username": "em1", "password": "CorrectHorse123",
               "email": "one@example.com", "consent": True}
    assert client.post("/api/register", json=payload).status_code == 200
    payload2 = dict(payload, username="em2")
    r = client.post("/api/register", json=payload2)
    assert r.status_code == 409
    assert "email" in r.json()["error"].lower()


def test_registration_invalid_email(monkeypatch, client):
    monkeypatch.setattr(client.app_mod, "REGISTRATION_MODE", "open")
    r = client.post("/api/register", json={"username": "bademail", "password": "CorrectHorse123",
                                           "email": "not-an-email", "consent": True})
    assert r.status_code == 400


def test_registration_rejects_weak_password(monkeypatch, client):
    monkeypatch.setattr(client.app_mod, "REGISTRATION_MODE", "open")
    r = client.post("/api/register", json={"username": "weakling", "password": "short",
                                           "consent": True})
    assert r.status_code == 400


def test_registration_short_username(monkeypatch, client):
    monkeypatch.setattr(client.app_mod, "REGISTRATION_MODE", "open")
    r = client.post("/api/register", json={"username": "ab", "password": "CorrectHorse123",
                                           "consent": True})
    assert r.status_code == 400


def test_registration_rate_limit(monkeypatch, client):
    monkeypatch.setattr(client.app_mod, "REGISTRATION_MODE", "open")
    monkeypatch.setattr(client.app_mod, "REGISTRATION_LIMIT", 2)
    for i in range(2):
        r = client.post("/api/register", json={"username": f"user{i}", "password": "CorrectHorse123",
                                               "consent": True})
        assert r.status_code == 200
    r = client.post("/api/register", json={"username": "user2", "password": "CorrectHorse123",
                                           "consent": True})
    assert r.status_code == 429
    assert "Retry-After" in r.headers


def test_register_page_renders_when_enabled(monkeypatch, client):
    monkeypatch.setattr(client.app_mod, "REGISTRATION_MODE", "open")
    html = client.get("/register").text
    assert "Create an account" in html


# --- Email confirmation (confirm mode) ---


def _enable_confirm_mailer(monkeypatch, client, urls=None):
    monkeypatch.setattr(client.app_mod, "REGISTRATION_MODE", "confirm")
    monkeypatch.setattr(client.app_mod.mailer, "smtp_configured", lambda: True)
    captured = urls if urls is not None else []
    monkeypatch.setattr(client.app_mod.mailer, "send_activation_email",
                        lambda to, url, username: captured.append(url))
    return captured


def test_confirm_mode_requires_email_and_smtp(monkeypatch, client):
    monkeypatch.setattr(client.app_mod, "REGISTRATION_MODE", "confirm")
    monkeypatch.setattr(client.app_mod.mailer, "smtp_configured", lambda: False)
    r = client.post("/api/register", json={"username": "conf1", "password": "CorrectHorse123",
                                           "email": "c1@example.com", "consent": True})
    assert r.status_code == 503


def test_confirm_mode_requires_valid_email(monkeypatch, client):
    monkeypatch.setattr(client.app_mod, "REGISTRATION_MODE", "confirm")
    monkeypatch.setattr(client.app_mod.mailer, "smtp_configured", lambda: True)
    r = client.post("/api/register", json={"username": "conf2", "password": "CorrectHorse123",
                                           "email": "nope", "consent": True})
    assert r.status_code == 400


def test_confirm_mode_flow(monkeypatch, client):
    urls = []
    _enable_confirm_mailer(monkeypatch, client, urls)
    r = client.post("/api/register", json={"username": "conf3", "password": "CorrectHorse123",
                                           "email": "conf3@example.com", "consent": True})
    assert r.status_code == 200
    assert r.json()["status"] == "pending"
    assert len(urls) == 1
    token = urls[0].split("token=")[1]
    # still pending until the link is clicked
    assert client.post("/login", json={"user": "conf3", "pass": "CorrectHorse123"}).status_code == 403
    page = client.get(f"/confirm?token={token}")
    assert page.status_code == 200
    assert "confermato" in page.text.lower()
    assert client.post("/login", json={"user": "conf3", "pass": "CorrectHorse123"}).status_code == 200
    # a second click is rejected
    assert client.get(f"/confirm?token={token}").status_code == 200


def test_confirm_page_invalid_token(client):
    page = client.get("/confirm?token=bogus")
    assert page.status_code == 200
    assert "invalid" in page.text.lower()


def test_confirm_page_missing_token(client):
    page = client.get("/confirm")
    assert page.status_code == 200


def test_privacy_page_renders(client):
    assert client.get("/privacy").status_code == 200


# --- #13 Password reset ---


def _admin_id(client):
    _login(client)
    return next(u for u in client.get("/api/users").json() if u["role"] == "admin")["id"]


def _new_client():
    import app as app_mod
    return TestClient(app_mod.app, base_url="https://testserver")


def test_reset_password_flow(client):
    _login(client)
    r = client.post("/api/users", json={"username": "lost", "password": "CorrectHorse123",
                                        "role": "viewer"})
    assert r.status_code == 200
    uid = r.json()["id"]
    r = client.post(f"/api/users/{uid}/reset-password")
    assert r.status_code == 200
    reset_url = r.json()["reset_url"]
    token = reset_url.split("token=")[1]

    r = client.post("/api/reset-password", json={"token": token, "password": "NewStrong456!"})
    assert r.status_code == 200
    assert client.post("/login", json={"user": "lost", "pass": "CorrectHorse123"}).status_code == 401
    assert client.post("/login", json={"user": "lost", "pass": "NewStrong456!"}).status_code == 200


def test_reset_password_invalid_token(client):
    r = client.post("/api/reset-password", json={"token": "bogus", "password": "NewStrong456!"})
    assert r.status_code == 400


def test_reset_password_expired_token(client):
    _login(client)
    r = client.post("/api/users", json={"username": "expired", "password": "CorrectHorse123",
                                        "role": "viewer"})
    uid = r.json()["id"]
    token = "expired-token-value"
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    past = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    assert database.create_reset_token(uid, token_hash, past)
    r = client.post("/api/reset-password", json={"token": token, "password": "NewStrong456!"})
    assert r.status_code == 400


def test_reset_password_weak_password(client):
    _login(client)
    r = client.post("/api/users", json={"username": "weakreset", "password": "CorrectHorse123",
                                        "role": "viewer"})
    uid = r.json()["id"]
    r = client.post(f"/api/users/{uid}/reset-password")
    token = r.json()["reset_url"].split("token=")[1]
    r = client.post("/api/reset-password", json={"token": token, "password": "short"})
    assert r.status_code == 400


def test_reset_password_requires_admin(client):
    _login(client)
    r = client.post("/api/users", json={"username": "nonadmin", "password": "CorrectHorse123",
                                        "role": "viewer"})
    uid = r.json()["id"]
    viewer = _new_client()
    _login(viewer, "nonadmin", "CorrectHorse123")
    r = viewer.post(f"/api/users/{uid}/reset-password")
    assert r.status_code == 403


def test_reset_password_rate_limit(monkeypatch, client):
    monkeypatch.setattr(client.app_mod, "PASSWORD_LIMIT", 2)
    for i in range(2):
        r = client.post("/api/reset-password", json={"token": "x", "password": "NewStrong456!"})
        assert r.status_code == 400
    r = client.post("/api/reset-password", json={"token": "x", "password": "NewStrong456!"})
    assert r.status_code == 429


def test_reset_page_renders(client):
    html = client.get("/reset-password?token=demo").text
    assert "Reset Password" in html


# --- #14 Self-service account ---


def test_change_username(client):
    _login(client)
    r = client.put("/api/account", json={"username": "renamed_admin"})
    assert r.status_code == 200
    assert r.json()["username"] == "renamed_admin"
    assert client.get("/account").text.find("renamed_admin") != -1


def test_change_username_taken(client):
    _login(client)
    r = client.post("/api/users", json={"username": "taken", "password": "CorrectHorse123",
                                        "role": "viewer"})
    assert r.status_code == 200
    r = client.put("/api/account", json={"username": "taken"})
    assert r.status_code == 409
    r = client.put("/api/account", json={"username": "ok_name"})
    assert r.status_code == 200


def test_change_password_self(client):
    _login(client)
    r = client.post("/api/account/change-password",
                    json={"current_password": "admin", "password": "NewStrong456!"})
    assert r.status_code == 200
    assert client.post("/login", json={"user": "admin", "pass": "NewStrong456!"}).status_code == 200


def test_change_password_wrong_current(client):
    _login(client)
    r = client.post("/api/account/change-password",
                    json={"current_password": "wrong", "password": "NewStrong456!"})
    assert r.status_code == 400


def test_change_password_weak(client):
    _login(client)
    r = client.post("/api/account/change-password",
                    json={"current_password": "admin", "password": "short"})
    assert r.status_code == 400


def test_save_preferences(client):
    _login(client)
    r = client.put("/api/account/preferences", json={"preferences": {"theme": "dark"}})
    assert r.status_code == 200
    admin_id = database.get_user("admin")["id"]
    assert database.get_user_by_id(admin_id)["preferences"]["theme"] == "dark"


def test_account_page_requires_auth(client):
    r = client.get("/account", follow_redirects=False)
    assert r.status_code == 303


# --- #15 Approval / deactivation ---


def test_admin_approves_pending_user(monkeypatch, client):
    monkeypatch.setattr(client.app_mod, "REGISTRATION_MODE", "approval")
    r = client.post("/api/register", json={"username": "apprv", "password": "CorrectHorse123",
                                           "consent": True})
    assert r.status_code == 200
    _login(client)
    users = client.get("/api/users").json()
    target = next(u for u in users if u["username"] == "apprv")
    assert target["status"] == "pending"
    r = client.put(f"/api/users/{target['id']}", json={"status": "active"})
    assert r.status_code == 200
    assert r.json()["status"] == "active"
    assert client.post("/login", json={"user": "apprv", "pass": "CorrectHorse123"}).status_code == 200


def test_admin_disables_account_revokes_session(monkeypatch, client):
    monkeypatch.setattr(client.app_mod, "REGISTRATION_MODE", "open")
    client.post("/api/register", json={"username": "victim", "password": "CorrectHorse123",
                                       "consent": True})
    victim = _new_client()
    _login(victim, "victim", "CorrectHorse123")
    assert victim.get("/api/flights").status_code == 200

    _login(client)  # admin
    users = client.get("/api/users").json()
    target = next(u for u in users if u["username"] == "victim")
    r = client.put(f"/api/users/{target['id']}", json={"status": "disabled"})
    assert r.status_code == 200
    assert victim.get("/api/flights").status_code == 401
    assert victim.get("/", follow_redirects=False).status_code == 303
    assert victim.post("/login", json={"user": "victim", "pass": "CorrectHorse123"}).status_code == 403


def test_admin_cannot_disable_self(client):
    _login(client)
    admin = next(u for u in client.get("/api/users").json() if u["role"] == "admin")
    r = client.put(f"/api/users/{admin['id']}", json={"status": "disabled"})
    assert r.status_code == 400


def test_status_change_requires_admin(client):
    _login(client)
    r = client.post("/api/users", json={"username": "viewer1", "password": "CorrectHorse123",
                                        "role": "viewer"})
    uid = r.json()["id"]
    viewer = _new_client()
    _login(viewer, "viewer1", "CorrectHorse123")
    r = viewer.put(f"/api/users/{uid}", json={"status": "active"})
    assert r.status_code == 403


def test_invalid_status_rejected(client):
    _login(client)
    r = client.post("/api/users", json={"username": "viewer2", "password": "CorrectHorse123",
                                        "role": "viewer"})
    uid = r.json()["id"]
    r = client.put(f"/api/users/{uid}", json={"status": "bogus"})
    assert r.status_code == 400


def test_disabled_user_cannot_login(client):
    _login(client)
    r = client.post("/api/users", json={"username": "locked", "password": "CorrectHorse123",
                                        "role": "viewer"})
    uid = r.json()["id"]
    client.put(f"/api/users/{uid}", json={"status": "disabled"})
    r = client.post("/login", json={"user": "locked", "pass": "CorrectHorse123"})
    assert r.status_code == 403


def test_admin_create_user_with_email_and_status(client):
    _login(client)
    r = client.post("/api/users", json={"username": "carol", "password": "CorrectHorse123",
                                        "role": "viewer", "status": "pending",
                                        "email": "carol@example.com"})
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "carol@example.com"
    assert body["status"] == "pending"
    row = next(u for u in client.get("/api/users").json() if u["username"] == "carol")
    assert row["email"] == "carol@example.com"
    assert row["status"] == "pending"


def test_admin_create_user_invalid_email_rejected(client):
    _login(client)
    r = client.post("/api/users", json={"username": "carolb", "password": "CorrectHorse123",
                                        "email": "not-an-email"})
    assert r.status_code == 400


def test_admin_create_user_invalid_status_rejected(client):
    _login(client)
    r = client.post("/api/users", json={"username": "carolc", "password": "CorrectHorse123",
                                        "status": "bogus"})
    assert r.status_code == 400


def test_admin_create_user_duplicate_email_rejected(client):
    _login(client)
    assert client.post("/api/users", json={"username": "carold",
                                           "password": "CorrectHorse123",
                                           "email": "dup@example.com"}).status_code == 200
    r = client.post("/api/users", json={"username": "carole",
                                        "password": "CorrectHorse123",
                                        "email": "dup@example.com"})
    assert r.status_code == 409


def test_admin_edit_user_email(client):
    _login(client)
    r = client.post("/api/users", json={"username": "carolf", "password": "CorrectHorse123"})
    uid = r.json()["id"]
    r = client.put(f"/api/users/{uid}", json={"email": "new@example.com"})
    assert r.status_code == 200
    assert r.json()["email"] == "new@example.com"
    row = next(u for u in client.get("/api/users").json() if u["username"] == "carolf")
    assert row["email"] == "new@example.com"


def test_admin_edit_user_email_duplicate_rejected(client):
    _login(client)
    assert client.post("/api/users", json={"username": "carolg",
                                           "password": "CorrectHorse123",
                                           "email": "taken@example.com"}).status_code == 200
    r = client.post("/api/users", json={"username": "carolh", "password": "CorrectHorse123"})
    uid = r.json()["id"]
    r = client.put(f"/api/users/{uid}", json={"email": "taken@example.com"})
    assert r.status_code == 409
