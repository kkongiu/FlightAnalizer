from models import FlightSummary
from database import (create_user, save_flight, create_vehicle, set_vehicle_photo,
                      get_user)


def _summary(filename):
    s = FlightSummary(
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
    return s


def _login(client, username, password):
    r = client.post("/login", json={"user": username, "pass": password})
    return r.status_code == 200


def _seed(client):
    alice = create_user("alice", "alicepass", role="viewer")
    bob = create_user("bob", "bobpass", role="viewer")
    save_flight(_summary("alice1.csv"), alice["id"])
    save_flight(_summary("alice2.csv"), alice["id"])
    save_flight(_summary("bob1.csv"), bob["id"])
    v_alice = create_vehicle("Alice Drone", "drone", owner_id=alice["id"])
    v_bob = create_vehicle("Bob Quad", "drone", owner_id=bob["id"])
    return {"alice": alice, "bob": bob, "v_alice": v_alice, "v_bob": v_bob}


def test_api_requires_auth(client):
    assert client.get("/api/flights").status_code == 401
    assert client.get("/api/vehicles").status_code == 401


def test_flights_are_scoped_by_owner(client):
    _seed(client)
    assert _login(client, "alice", "alicepass")
    data = client.get("/api/flights").json()
    names = {f["filename"] for f in data}
    assert names == {"alice1.csv", "alice2.csv"}


def test_admin_sees_all_flights(client):
    _seed(client)
    assert _login(client, "admin", "admin")
    data = client.get("/api/flights").json()
    names = {f["filename"] for f in data}
    assert names == {"alice1.csv", "alice2.csv", "bob1.csv"}


def test_user_cannot_read_other_owner_flight(client):
    _seed(client)
    assert _login(client, "alice", "alicepass")
    assert client.get("/api/flights/bob1.csv").status_code == 404
    assert client.delete("/api/flights/bob1.csv").status_code == 404
    assert client.put("/api/flights/bob1.csv/notes",
                      json={"notes": "hax"}).status_code == 404
    assert client.post("/api/flights/bob1.csv/rescan-nav").status_code == 404


def test_user_cannot_touch_other_owner_vehicles(client):
    s = _seed(client)
    assert _login(client, "alice", "alicepass")
    assert client.get("/api/vehicles").json()[0]["id"] == s["v_alice"].id
    assert client.delete(f"/api/vehicles/{s['v_bob'].id}").status_code == 404
    assert client.put(f"/api/vehicles/{s['v_bob'].id}",
                      json={"name": "stolen"}).status_code == 404
    assert client.get(f"/api/vehicles/{s['v_bob'].id}/photo/img").status_code == 404


def test_viewer_cannot_see_users(client):
    _seed(client)
    assert _login(client, "alice", "alicepass")
    assert client.get("/api/users").status_code == 403


def test_delete_user_requires_confirmation(client):
    s = _seed(client)
    assert _login(client, "admin", "admin")
    r = client.delete(f"/api/users/{s['alice']['id']}")
    assert r.status_code == 409
    body = r.json()
    assert body["confirm"] is True
    assert body["counts"]["flights"] == 2
    assert body["counts"]["vehicles"] == 1


def test_delete_user_cascade_removes_data(client, monkeypatch):
    s = _seed(client)
    (client.app_mod.LOG_DIR / "alice1.csv").write_text("a")
    photo_dir = client.app_mod.database.DATA_DIR / "vehicle_photos"
    photo_dir.mkdir(parents=True, exist_ok=True)
    (photo_dir / f"v{s['v_alice'].id}.webp").write_bytes(b"img")
    assert _login(client, "admin", "admin")
    r = client.request("DELETE", f"/api/users/{s['alice']['id']}",
                       json={"confirm": True, "backup": False})
    assert r.status_code == 200
    body = r.json()
    assert body["flights_deleted"] == 2
    assert body["vehicles_deleted"] == 1
    assert "alice1.csv" in body["files_removed"]
    assert get_user("alice") is None
    names = {f["filename"] for f in client.get("/api/flights").json()}
    assert names == {"bob1.csv"}
    # CSV removed from disk
    assert not (client.app_mod.LOG_DIR / "alice1.csv").exists()
    # vehicle photo removed (from the patched dir, never production data)
    assert not (photo_dir / f"v{s['v_alice'].id}.webp").exists()


def test_delete_user_with_backup_creates_snapshot(client):
    s = _seed(client)
    assert _login(client, "admin", "admin")
    r = client.request("DELETE", f"/api/users/{s['alice']['id']}",
                        json={"confirm": True, "backup": True})
    assert r.status_code == 200
    assert r.json()["backup"]
    backup_dir = client.app_mod.database.DATA_DIR / "backups"
    assert any(backup_dir.glob("flights-*.db"))


def test_cannot_delete_self(client):
    _seed(client)
    admin = get_user("admin")
    assert _login(client, "admin", "admin")
    r = client.request("DELETE", f"/api/users/{admin['id']}", json={"confirm": True})
    assert r.status_code == 400
    assert get_user("admin") is not None


def _set_session_cookie(client, data):
    import base64
    import json as _json
    from itsdangerous import TimestampSigner
    payload = base64.b64encode(_json.dumps(data).encode()).decode()
    secret = client.app_mod._get_session_secret()
    client.cookies.set("session_token", TimestampSigner(secret).sign(payload).decode())


def _photo_url(client, vehicle):
    photo_dir = client.app_mod.database.DATA_DIR / "vehicle_photos"
    photo_dir.mkdir(parents=True, exist_ok=True)
    (photo_dir / f"v{vehicle.id}.webp").write_bytes(b"img")
    set_vehicle_photo(vehicle.id, f"/flight/api/vehicles/{vehicle.id}/photo/img")
    return f"/api/vehicles/{vehicle.id}/photo/img"


def test_legacy_session_without_user_id_is_rejected(client):
    """Sessions created before user_id was stored must be forced to re-login:
    they are treated as unauthenticated (no data leak)."""
    s = _seed(client)
    url = _photo_url(client, s["v_alice"])
    _set_session_cookie(client, {"authenticated": True, "role": "admin"})
    assert client.get(url).status_code == 404
    assert client.get("/api/flights").status_code == 401
    r = client.get("/vehicles", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/flight/login"


def test_photo_img_anonymous_is_404(client):
    s = _seed(client)
    url = _photo_url(client, s["v_alice"])
    assert client.get(url).status_code == 404


def test_photo_img_admin_session_ok(client):
    s = _seed(client)
    url = _photo_url(client, s["v_alice"])
    assert _login(client, "admin", "admin")
    assert client.get(url).status_code == 200
