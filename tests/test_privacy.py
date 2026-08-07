"""F4 · Privacy tests: audit log, self-service data export and account deletion."""
from models import FlightSummary
from database import (create_user, save_flight, create_vehicle,
                      get_audit_log, get_user)


def _summary(filename):
    return FlightSummary(
        filename=filename,
        date="2020-01-01",
        start_time="12:00:00",
        duration_s=10.0,
        distance_km=0.5,
        max_alt_m=10.0,
        min_alt_m=0.0,
        avg_alt_m=5.0,
        max_speed_kmh=36.0,
        avg_speed_kmh=30.0,
        max_vspd_ms=2.0,
        max_rssi_db=-60,
        min_rssi_db=-90,
        avg_rssi_db=-75.0,
        min_rqly=80,
        avg_rqly=95.0,
        battery_start_v=16.8,
        battery_end_v=15.0,
        battery_min_v=14.8,
        battery_start_pct=100.0,
        battery_end_pct=80.0,
        battery_consumed_mah=500.0,
        max_current_a=20.0,
        txbat_v=8.4,
        flight_modes={"OK": 20},
        sats_max=12,
        max_g=2.45,
        avg_g=1.15,
        events=[],
        coordinates=[],
    )


def _seed(client):
    alice = create_user("alice", "alicepass", role="viewer")
    save_flight(_summary("alice1.csv"), alice["id"])
    create_vehicle("Alice Drone", "drone", owner_id=alice["id"])
    return alice


def _login(client, user="alice", pwd="alicepass"):
    r = client.post("/login", json={"user": user, "pass": pwd})
    if r.status_code == 200:
        token = r.json().get("csrf_token")
        if token:
            client.headers["X-CSRF-Token"] = token
    return r.status_code == 200


def _login_admin(client):
    return _login(client, "admin", "admin")


# --- #23 Audit log ---


def test_audit_logs_login_and_failed_login(client):
    assert client.post("/login", json={"user": "alice", "pass": "wrong"}).status_code == 401
    _seed(client)
    assert _login(client)
    assert _login_admin(client)
    entries = client.get("/api/audit").json()
    actions = [e["action"] for e in entries]
    assert "login_failed" in actions
    assert "login" in actions


def test_audit_logs_actions(client):
    _seed(client)
    assert _login(client)
    client.post("/api/scan")
    assert client.get("/api/flights/alice1.csv").status_code == 200
    assert client.delete("/api/flights/alice1.csv").status_code == 200
    assert _login_admin(client)
    actions = [e["action"] for e in client.get("/api/audit").json()]
    assert "scan" in actions
    assert "flight_view" in actions
    assert "flight_delete" in actions


def test_audit_requires_admin(client):
    _seed(client)
    assert _login(client)
    assert client.get("/api/audit").status_code == 403
    assert client.get("/audit", follow_redirects=False).status_code == 403


def test_audit_filter_by_username(client):
    _seed(client)
    assert _login(client)
    assert _login_admin(client)
    rows = client.get("/api/audit?username=alice").json()
    assert rows and all(e["username"] == "alice" for e in rows)


def test_audit_page_renders_for_admin(client):
    _seed(client)
    assert _login_admin(client)
    r = client.get("/audit")
    assert r.status_code == 200
    assert "Audit Log" in r.text


# --- #22 Self-service export ---


def test_self_export_returns_full_dataset(client):
    _seed(client)
    assert _login(client)
    r = client.get("/api/account/export")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["account"]["username"] == "alice"
    assert {f["filename"] for f in body["flights"]} == {"alice1.csv"}
    assert body["vehicles"] and body["vehicles"][0]["name"] == "Alice Drone"


def test_self_export_requires_auth(client):
    assert client.get("/api/account/export").status_code == 401


def test_self_export_logged_in_audit(client):
    _seed(client)
    assert _login(client)
    client.get("/api/account/export")
    assert _login_admin(client)
    assert any(e["action"] == "data_export" for e in client.get("/api/audit").json())


# --- #22 Self-service deletion ---


def test_self_delete_requires_confirmation(client):
    _seed(client)
    assert _login(client)
    r = client.delete("/api/account")
    assert r.status_code == 409
    body = r.json()
    assert body["confirm"] is True
    assert body["counts"]["flights"] == 1
    assert body["counts"]["vehicles"] == 1
    assert get_user("alice") is not None


def test_self_delete_requires_correct_password(client):
    _seed(client)
    assert _login(client)
    # wrong password is rejected even with confirm=true
    r = client.request("DELETE", "/api/account",
                       json={"confirm": True, "backup": False, "password": "wrong"})
    assert r.status_code == 403
    assert get_user("alice") is not None
    # missing password is rejected
    r = client.request("DELETE", "/api/account", json={"confirm": True, "backup": False})
    assert r.status_code == 403
    assert get_user("alice") is not None


def test_self_delete_cascade_removes_data(client):
    _seed(client)
    (client.app_mod.LOG_DIR / "alice1.csv").write_text("a")
    assert _login(client)
    r = client.request("DELETE", "/api/account",
                       json={"confirm": True, "backup": False, "password": "alicepass"})
    assert r.status_code == 200
    body = r.json()
    assert body["flights_deleted"] == 1
    assert body["vehicles_deleted"] == 1
    assert get_user("alice") is None
    assert not (client.app_mod.LOG_DIR / "alice1.csv").exists()
    # session cleared: subsequent API calls are unauthenticated
    assert client.get("/api/flights").status_code == 401


def test_self_delete_blocked_for_admin(client):
    assert _login_admin(client)
    r = client.request("DELETE", "/api/account",
                       json={"confirm": True, "password": "admin"})
    assert r.status_code == 400
    assert get_user("admin") is not None


def test_self_delete_logged_in_audit(client):
    _seed(client)
    assert _login(client)
    client.request("DELETE", "/api/account",
                   json={"confirm": True, "backup": False, "password": "alicepass"})
    entries = get_audit_log()
    assert any(e["username"] == "alice" and e["action"] == "account_delete" for e in entries)


def test_account_page_has_export_and_delete(client):
    _seed(client)
    assert _login(client)
    r = client.get("/account")
    assert r.status_code == 200
    assert "exportBtn" in r.text
    assert "deleteAccountBtn" in r.text
