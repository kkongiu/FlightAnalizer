"""End-to-end / API test suite for the Pocket Log Analyzer.

Runs against the real FastAPI app via TestClient with a throwaway SQLite
database and a throwaway log directory (see conftest.py). Covers auth,
flight CRUD (notes, tags, rename, GPX import, export), scan, mission
(track/preview/export), users and backups.
"""
import pytest

FIXTURE_CSV = """Date,Time,1RSS(dB),2RSS(dB),RQly(%),RSNR(dB),ANT,RFMD,TPWR(mW),TRSS(dB),TQly(%),TSNR(dB),FM,VSpd(m/s),Alt(m),GPS,GSpd(kmh),Hdg(°),Sats,RxBt(V),Curr(A),Capa(mAh),Bat%(%),Ptch(rad),Roll(rad),Yaw(rad),Rud,Ele,Thr,Ail,P1,SA,SB,SC,SD,SE,LSW,CH1(us),CH2(us),CH3(us),CH4(us),TxBat(V)
2020-01-01,12:00:00.000,-70,-75,100,12,0,1,250,-70,100,11,OK,0.0,2.0,12.000000 -45.000000,0.0,0,12,16.8,0.5,0,100,0.00,0.00,0.00,0,0,0,0,0,1,0,0,0,0,0,988,988,988,988,8.4
2020-01-01,12:00:00.500,-70,-75,100,12,0,1,250,-70,100,11,OK,0.0,2.1,12.000001 -44.999999,5.0,10,12,16.8,1.0,2,99,0.01,0.01,0.01,0,0,10,0,0,1,0,0,0,0,0,988,988,988,988,8.4
2020-01-01,12:00:01.000,-70,-75,100,12,0,1,250,-70,100,11,OK,0.1,2.2,12.000002 -44.999998,8.0,20,12,16.8,1.5,3,99,0.02,0.02,0.02,0,0,15,0,0,1,0,0,0,0,0,988,988,988,988,8.4
2020-01-01,12:00:01.500,-70,-75,100,12,0,1,250,-70,100,11,OK,0.1,2.3,12.000003 -44.999997,9.0,30,12,16.8,2.0,4,99,0.03,0.03,0.03,0,0,20,0,0,1,0,0,0,0,0,988,988,988,988,8.4
2020-01-01,12:00:02.000,-70,-75,100,12,0,1,250,-70,100,11,OK,0.0,2.4,12.000004 -44.999996,10.0,40,12,16.8,2.5,5,98,0.04,0.04,0.04,0,0,25,0,0,1,0,0,0,0,0,988,988,988,988,8.4
2020-01-01,12:00:02.500,-70,-75,100,12,0,1,250,-70,100,11,OK,0.0,2.5,12.000005 -44.999995,10.0,45,12,16.8,3.0,6,98,0.05,0.05,0.05,0,0,30,0,0,1,0,0,0,0,0,988,988,988,988,8.4
"""

GPX = """<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">
  <trk><name>track</name><trkseg>
    <trkpt lat="12.000000" lon="-45.000000"><ele>2.0</ele><time>2020-01-01T12:00:00Z</time></trkpt>
    <trkpt lat="12.000001" lon="-44.999990"><ele>3.0</ele><time>2020-01-01T12:00:01Z</time></trkpt>
  </trkseg></trk>
</gpx>"""


@pytest.fixture
def no_geocode(client, monkeypatch):
    """Disable the reverse-geocoding network call so uploads keep their names."""
    monkeypatch.setattr(client.app_mod, "reverse_geocode", lambda lat, lon: None)
    return client


def _login(client, user="admin", pwd="admin"):
    r = client.post("/login", json={"user": user, "pass": pwd})
    assert r.status_code == 200
    token = r.json().get("csrf_token")
    if token:
        client.headers["X-CSRF-Token"] = token
    return r


def _upload(client, name="e2e.csv", content=None):
    return client.post("/api/upload", files={"file": (name, content or FIXTURE_CSV, "text/csv")})


# --- Auth ---

def test_health_endpoint(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"


def test_unauthenticated_pages_redirect_to_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert "/flight/login" in r.headers["location"]
    assert client.get("/flights", follow_redirects=False).status_code == 303


def test_api_requires_auth(client):
    assert client.get("/api/flights").status_code == 401
    assert client.get("/api/stats").status_code == 401
    assert client.get("/api/vehicles").status_code == 401
    assert client.post("/api/upload", files={"file": ("x.csv", FIXTURE_CSV, "text/csv")}).status_code == 401


def test_login_wrong_password(client):
    assert client.post("/login", json={"user": "admin", "pass": "wrong"}).status_code == 401


def test_login_logout_session(client):
    _login(client)
    assert client.get("/api/flights").status_code == 200
    client.post("/logout", follow_redirects=False)
    assert client.get("/api/flights").status_code == 401
    assert client.get("/", follow_redirects=False).status_code == 303


# --- Flights CRUD ---

def test_flight_upload_list_detail(no_geocode):
    _login(no_geocode)
    r = _upload(no_geocode)
    assert r.status_code == 200
    fname = r.json()["imported"]
    assert fname == "e2e.csv"

    flights = no_geocode.get("/api/flights").json()
    assert any(f["filename"] == fname for f in flights)

    detail = no_geocode.get(f"/api/flights/{fname}")
    assert detail.status_code == 200
    d = detail.json()
    assert d["date"] == "2020-01-01"
    assert d["duration_s"] == 2.5
    assert d["distance_km"] > 0
    assert len(d["coordinates"]) == 6


def test_flight_notes_tags(no_geocode):
    _login(no_geocode)
    fname = _upload(no_geocode).json()["imported"]

    r = no_geocode.put(f"/api/flights/{fname}/notes", json={"notes": "test note"})
    assert r.status_code == 200
    assert no_geocode.get(f"/api/flights/{fname}").json()["notes"] == "test note"

    r = no_geocode.post(f"/api/flights/{fname}/tags", json={"tags": ["demo", "test"]})
    assert r.status_code == 200
    assert no_geocode.get("/api/tags").json()["tags"] == ["demo", "test"]
    assert no_geocode.get(f"/api/flights/{fname}").json()["tags"] == ["demo", "test"]


def test_flight_rename_export_delete(no_geocode):
    _login(no_geocode)
    fname = _upload(no_geocode).json()["imported"]

    r = no_geocode.put(f"/api/flights/{fname}", json={"new_name": "renamed.csv"})
    assert r.status_code == 200
    assert no_geocode.get("/api/flights/renamed.csv").status_code == 200
    assert no_geocode.get(f"/api/flights/{fname}").status_code == 404
    fname = "renamed.csv"

    gpx = no_geocode.get(f"/api/export/{fname}?format=gpx")
    assert gpx.status_code == 200
    assert "gpx" in gpx.headers["content-type"]
    assert "<trkpt" in gpx.text

    kml = no_geocode.get(f"/api/export/{fname}?format=kml")
    assert kml.status_code == 200
    assert "kml" in kml.headers["content-type"]

    r = no_geocode.post(f"/api/flights/{fname}/rescan-nav")
    assert r.status_code == 200
    assert r.json()["total"] == 6

    r = no_geocode.delete(f"/api/flights/{fname}")
    assert r.status_code == 200
    assert r.json()["deleted"] == fname
    assert no_geocode.get(f"/api/flights/{fname}").status_code == 404


def test_scan_imports_new_csv(no_geocode):
    _login(no_geocode)
    logs = no_geocode.app_mod.LOG_DIR
    (logs / "manual.csv").write_text(FIXTURE_CSV)
    r = no_geocode.post("/api/scan")
    assert r.status_code == 200
    assert "manual.csv" in r.json()["imported"]
    assert no_geocode.get("/api/flights/manual.csv").status_code == 200


def test_upload_rejects_non_csv(client):
    _login(client)
    r = client.post("/api/upload", files={"file": ("notes.txt", "hello", "text/plain")})
    assert r.status_code == 400


def test_gpx_import(no_geocode):
    _login(no_geocode)
    fname = _upload(no_geocode, name="gpxflight.csv").json()["imported"]
    r = no_geocode.post(
        f"/api/flights/{fname}/import-gpx",
        files={"file": ("track.gpx", GPX, "application/gpx+xml")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["points"] == 2
    assert body["stats"]["distance_km"] > 0
    flight = no_geocode.get(f"/api/flights/{fname}").json()
    assert flight["track_source"] == "gpx"


def test_flight_not_found(no_geocode):
    _login(no_geocode)
    assert no_geocode.get("/api/flights/does-not-exist.csv").status_code == 404
    assert no_geocode.delete("/api/flights/does-not-exist.csv").status_code == 404


# --- Mission ---

def test_mission_track(no_geocode):
    _login(no_geocode)
    fname = _upload(no_geocode, name="mission.csv").json()["imported"]
    r = no_geocode.post("/api/mission/track", json={"filename": fname})
    assert r.status_code == 200
    coords = r.json()["coords"]
    assert len(coords) == 6
    assert all(len(c) == 3 for c in coords)


def test_mission_preview_and_export(no_geocode):
    _login(no_geocode)
    body = {
        "waypoints": [
            {"action": "WAYPOINT", "lat": 12.0, "lon": -45.0, "alt": 50, "p1": 0, "p2": 0, "p3": 0},
            {"action": "WAYPOINT", "lat": 12.01, "lon": -44.99, "alt": 60, "p1": 0, "p2": 0, "p3": 0},
        ],
        "final_action": "RTH",
    }
    r = no_geocode.post("/api/mission/preview", json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["xml"].startswith("<?xml")
    assert data["waypoints"][-1]["action"] == "RTH"
    assert data["waypoints"][-1]["no"] == 3

    r = no_geocode.post("/api/mission/export", json={**body, "name": "test-mission"})
    assert r.status_code == 200
    assert "test-mission.mission" in r.headers["content-disposition"]
    assert r.text.startswith("<?xml")


def test_mission_preview_validation(no_geocode):
    _login(no_geocode)
    r = no_geocode.post("/api/mission/preview", json={"waypoints": [], "final_action": "NONE"})
    assert r.status_code == 400
    r = no_geocode.post("/api/mission/preview", json={"waypoints": [], "params": {"filename": "nope.csv"}})
    assert r.status_code == 400


# --- Users ---

def _create_viewer(client, username="viewer1", password="Viewer1234!"):
    return client.post("/api/users", json={"username": username, "password": password, "role": "viewer"})


def test_user_crud_and_permissions(client):
    _login(client)
    r = _create_viewer(client)
    assert r.status_code == 200
    uid = r.json()["id"]

    # viewer cannot manage users
    client.post("/logout", follow_redirects=False)
    _login(client, "viewer1", "Viewer1234!")
    assert client.get("/api/users").status_code == 403
    assert _create_viewer(client, "hacker", "Hackpass123!").status_code == 403
    assert client.get("/api/backups").status_code == 403
    assert client.post("/api/backup").status_code == 403

    # self password change
    r = client.post(f"/api/users/{uid}/change-password", json={"password": "NewPass123!"})
    assert r.status_code == 200
    client.post("/logout", follow_redirects=False)
    _login(client, "viewer1", "NewPass123!")
    client.post("/logout", follow_redirects=False)

    # admin deletes the viewer
    _login(client)
    r = client.request("DELETE", f"/api/users/{uid}",
                       json={"confirm": True, "password": "admin"})
    assert r.status_code == 200
    assert r.json()["deleted"] == uid
    assert all(u["username"] != "viewer1" for u in client.get("/api/users").json())


def test_admin_cannot_delete_self(client):
    _login(client)
    me = next(u for u in client.get("/api/users").json() if u["role"] == "admin")
    r = client.request("DELETE", f"/api/users/{me['id']}", json={"confirm": True})
    assert r.status_code == 400


# --- Backups ---

def test_backup_endpoints_admin_only(no_geocode):
    _login(no_geocode)
    _upload(no_geocode)

    r = no_geocode.post("/api/backup")
    assert r.status_code == 200
    assert r.json()["archive"].endswith(".tar.gz")

    r = no_geocode.get("/api/backups")
    assert r.status_code == 200
    backups = r.json()["backups"]
    assert len(backups) == 1
    assert backups[0]["name"].startswith("backup-")

    import tarfile
    archive = no_geocode.app_mod.LOG_DIR.parent / "backups" / backups[0]["name"]
    assert archive.exists()
    with tarfile.open(archive, "r:gz") as tar:
        assert "flights.db" in tar.getnames()
        assert "logs/e2e.csv" in tar.getnames()


def test_health_detects_db_failure(client, monkeypatch):
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(client.app_mod.database, "_get_conn", boom)
    body = client.get("/api/health").json()
    assert body["status"] == "degraded"
    assert body["database"] == "error"
