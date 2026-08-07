"""F8 · Domain & integration tests: vehicle maintenance (#41), CSV export and
calendar (#42), upload API tokens (#43), and weather endpoint (#44)."""
from models import FlightSummary
from database import (create_user, create_vehicle, save_flight,
                      add_maintenance_item, get_maintenance_alerts,
                      create_api_token, api_token_user,
                      assign_vehicle_to_flight)


def _summary(filename, owner_id=None, vehicle_id=None, date="2020-01-01"):
    f = FlightSummary(
        filename=filename, date=date, start_time="12:00:00",
        duration_s=3600.0, distance_km=10.0, max_alt_m=50.0, min_alt_m=0.0,
        avg_alt_m=25.0, max_speed_kmh=60.0, avg_speed_kmh=40.0, max_vspd_ms=3.0,
        max_rssi_db=-60, min_rssi_db=-90, avg_rssi_db=-75.0, min_rqly=80,
        avg_rqly=95.0, battery_start_v=16.8, battery_end_v=15.0,
        battery_min_v=14.8, battery_start_pct=100.0, battery_end_pct=80.0,
        battery_consumed_mah=500.0, max_current_a=20.0, txbat_v=8.4,
        flight_modes={"OK": 20}, sats_max=12, max_g=2.45, avg_g=1.15,
        events=[], coordinates=[[45.0, 9.0, 100.0, 0, 1577836800]])
    save_flight(f, owner_id)
    return f


def _login(client, user="alice", pwd="pass123"):
    r = client.post("/login", json={"user": user, "pass": pwd})
    if r.status_code == 200:
        token = r.json().get("csrf_token")
        if token:
            client.headers["X-CSRF-Token"] = token
    return r.status_code == 200


def _seed():
    alice = create_user("alice", "pass123", role="viewer")
    bob = create_user("bob", "pass123", role="viewer")
    v = create_vehicle("Quad", "drone", False, alice["id"])
    _summary("a.csv", alice["id"], v.id)
    assign_vehicle_to_flight("a.csv", v.id, alice["id"], False)
    _summary("b.csv", bob["id"])
    return alice, bob, v


# --- #41 maintenance ---


def test_flight_hours_and_maintenance_item(client):
    alice, bob, v = _seed()
    assert get_maintenance_alerts(alice["id"], False) == []
    item = add_maintenance_item(v.id, "propellers", 1.5)
    assert item and item["part_name"] == "propellers"
    alerts = get_maintenance_alerts(alice["id"], False)
    assert len(alerts) == 1
    a = alerts[0]
    assert a["vehicle_name"] == "Quad"
    assert a["flight_hours"] == 1.0  # one flight of 3600s
    assert a["overdue"] == 0 and a["due"] == 1  # 1h used of 1.5h interval


def test_maintenance_api_flow(client):
    alice, bob, v = _seed()
    assert _login(client)
    r = client.post(f"/api/vehicles/{v.id}/maintenance",
                    json={"part_name": "motors", "interval_hours": 0.5})
    assert r.status_code == 200
    item = r.json()["maintenance"]
    data = client.get(f"/api/vehicles/{v.id}/maintenance").json()
    assert data["flight_hours"] == 1.0
    assert len(data["items"]) == 1
    # mark serviced -> resets baseline to current hours
    r = client.post(f"/api/maintenance/{item['id']}/service")
    assert r.status_code == 200 and r.json()["serviced"]
    data = client.get(f"/api/maintenance/alerts").json()
    # after service, remaining = interval - 0 = 0.5h -> due
    assert data["alerts"][0]["items"][0]["status"] == "due"
    # delete
    assert client.delete(f"/api/maintenance/{item['id']}").status_code == 200
    assert client.get(f"/api/vehicles/{v.id}/maintenance").json()["items"] == []


def test_maintenance_isolated(client):
    alice, bob, v = _seed()
    add_maintenance_item(v.id, "battery", 10.0)
    assert _login(client, "bob")
    # bob cannot manage alice's vehicle maintenance
    assert client.get(f"/api/vehicles/{v.id}/maintenance").status_code == 404
    assert client.post(f"/api/vehicles/{v.id}/maintenance",
                       json={"part_name": "x", "interval_hours": 1}).status_code == 404


# --- #42 CSV export + calendar ---


def test_export_csv(client):
    alice, bob, v = _seed()
    assert _login(client)
    r = client.get("/api/export/flights.csv")
    assert r.status_code == 200
    assert r.headers["Content-Disposition"].startswith("attachment")
    lines = r.text.splitlines()
    assert len(lines) == 2  # header + 1 flight (bob's flight not included)
    assert "filename" in lines[0]
    assert "a.csv" in lines[1]


def test_calendar_page(client):
    alice, bob, v = _seed()
    _summary("c.csv", alice["id"], v.id, date="2019-11-05")
    assert _login(client)
    r = client.get("/calendar")
    assert r.status_code == 200
    assert "2020-01" in r.text
    assert "2019-11" in r.text
    assert "Export CSV" in r.text


# --- #43 API tokens ---


def test_create_and_revoke_token(client):
    _seed()
    assert _login(client)
    r = client.post("/api/tokens", json={"name": "radio"})
    assert r.status_code == 200
    raw = r.json()["token"]
    assert raw and len(raw) >= 32
    toks = client.get("/api/tokens").json()["tokens"]
    assert len(toks) == 1 and toks[0]["name"] == "radio"
    assert api_token_user(raw)  # db-level lookup works
    assert client.delete(f"/api/tokens/{toks[0]['id']}").status_code == 200
    assert api_token_user(raw) is None  # revoked token invalid


def test_upload_with_token(client):
    _seed()
    assert _login(client)
    raw = client.post("/api/tokens", json={"name": "script"}).json()["token"]
    # fresh client (no session cookie -> no CSRF, token provides identity)
    from fastapi.testclient import TestClient
    import app as app_mod
    with TestClient(app_mod.app, base_url="https://testserver") as c:
        c.headers["X-API-Token"] = raw
        csv = ("time,lat,lon,alt,gspd,vspd,hdg,rssi,rssi2,rqly,rsnr,trss,tqly,tsnr,"
               "rxbt,curr,capa,bat_pct,pitch,roll,yaw,rud,ele,thr,ail,fs,sats,txbat\n"
               "0,12.0,-45.0,10,10,0,0,-70,-75,100,12,-70,100,11,16.8,0,0,100,0,0,0,0,0,0,0,OK,12,8.4\n")
        r = c.post("/api/upload", files={"file": ("x.csv", csv, "text/csv")})
        assert r.status_code in (200, 400)


def test_upload_with_invalid_token_rejected(client):
    from fastapi.testclient import TestClient
    import app as app_mod
    with TestClient(app_mod.app, base_url="https://testserver") as c:
        c.headers["X-API-Token"] = "bogus-token"
        csv = "time,lat,lon\n1,2,3\n"
        assert c.post("/api/upload",
                      files={"file": ("x.csv", csv, "text/csv")}).status_code == 401


# --- #44 weather ---


def test_weather_endpoint_unavailable_gracefully(client, monkeypatch):
    alice, bob, v = _seed()
    _summary("c.csv", alice["id"], v.id, date="2020-01-01")
    assert _login(client)

    async def _fake_fetch(flight):
        return None

    import app as app_mod
    monkeypatch.setattr(app_mod, "_fetch_historical_weather", _fake_fetch)
    r = client.get("/api/flights/c.csv/weather")
    assert r.status_code == 200
    assert r.json()["weather"] is None


def test_weather_unknown_flight(client):
    _seed()
    assert _login(client)
    assert client.get("/api/flights/nope.csv/weather").status_code == 404