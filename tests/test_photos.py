"""F6 · Flight photo tests: upload, gallery listing, cover, deletion and
isolation (issues #30, #31, #32, #33)."""
from models import FlightSummary
from database import (create_user, save_flight, get_flight_photos,
                      delete_photos_for_flights, create_vehicle)


def _summary(filename, owner_id=None):
    f = FlightSummary(
        filename=filename, date="2020-01-01", start_time="12:00:00",
        duration_s=10.0, distance_km=0.5, max_alt_m=10.0, min_alt_m=0.0,
        avg_alt_m=5.0, max_speed_kmh=36.0, avg_speed_kmh=30.0, max_vspd_ms=2.0,
        max_rssi_db=-60, min_rssi_db=-90, avg_rssi_db=-75.0, min_rqly=80,
        avg_rqly=95.0, battery_start_v=16.8, battery_end_v=15.0,
        battery_min_v=14.8, battery_start_pct=100.0, battery_end_pct=80.0,
        battery_consumed_mah=500.0, max_current_a=20.0, txbat_v=8.4,
        flight_modes={"OK": 20}, sats_max=12, max_g=2.45, avg_g=1.15,
        events=[], coordinates=[])
    save_flight(f, owner_id)
    return f


def _login(client, user="alice", pwd="pass123", token_set=True):
    r = client.post("/login", json={"user": user, "pass": pwd})
    if r.status_code == 200:
        token = r.json().get("csrf_token")
        if token:
            client.headers["X-CSRF-Token"] = token
    return r.status_code == 200


def _png_bytes():
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _seed():
    alice = create_user("alice", "pass123", role="viewer")
    bob = create_user("bob", "pass123", role="viewer")
    _summary("a.csv", alice["id"])
    _summary("b.csv", bob["id"])
    return alice, bob


# --- #30 upload + listing ---


def test_upload_and_list_photos(client):
    seed = _seed()
    assert _login(client)
    r = client.post("/api/flights/a.csv/photos",
                    files={"file": ("shot1.png", _png_bytes(),
                                    "image/png")})
    assert r.status_code == 200
    pid = r.json()["id"]
    photos = client.get("/api/flights/a.csv/photos").json()
    assert len(photos) == 1
    assert photos[0]["id"] == pid
    # first uploaded photo becomes the cover (#33)
    assert photos[0]["is_cover"] == 1


def test_upload_rejects_non_image_and_too_large(client):
    _seed()
    assert _login(client)
    assert client.post("/api/flights/a.csv/photos",
                       files={"file": ("a.txt", b"hello", "text/plain")}
                       ).status_code == 400
    # overriding MAX image size (10MB) is covered by a big payload
    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (11 * 1024 * 1024)
    assert client.post("/api/flights/a.csv/photos",
                       files={"file": ("big.png", big, "image/png")}
                       ).status_code == 400


def test_photo_requires_auth(client):
    assert client.get("/api/flights/a.csv/photos").status_code == 401


# --- #32 isolation: only owner (or admin) upload/delete/set-cover ---


def test_upload_blocked_for_non_owner(client):
    _seed()
    assert _login(client, "bob")
    assert client.post("/api/flights/a.csv/photos",
                       files={"file": ("x.png", _png_bytes(), "image/png")}
                       ).status_code == 404


def test_delete_blocked_for_non_owner(client):
    _seed()
    assert _login(client)
    pid = client.post("/api/flights/a.csv/photos",
                      files={"file": ("x.png", _png_bytes(), "image/png")}
                      ).json()["id"]
    assert _login(client, "bob")
    assert client.delete(f"/api/flights/a.csv/photos/{pid}").status_code == 404
    # admin (not the owner) cannot delete another user's photos either
    assert _login(client, "admin", "admin")
    assert client.delete(f"/api/flights/a.csv/photos/{pid}").status_code == 404


def test_delete_removes_cover_and_db_row(client):
    _seed()
    assert _login(client)
    pid = client.post("/api/flights/a.csv/photos",
                      files={"file": ("x.png", _png_bytes(), "image/png")}
                      ).json()["id"]
    assert len(get_flight_photos("a.csv")) == 1
    assert client.delete(f"/api/flights/a.csv/photos/{pid}").status_code == 200
    assert get_flight_photos("a.csv") == []
    # files cleaned up: no leftover stored_name
    assert delete_photos_for_flights(["a.csv"]) == []


def test_cover_can_be_set_by_owner_only(client):
    _seed()
    assert _login(client)
    pid1 = client.post("/api/flights/a.csv/photos",
                       files={"file": ("1.png", _png_bytes(), "image/png")}).json()["id"]
    pid2 = client.post("/api/flights/a.csv/photos",
                       files={"file": ("2.png", _png_bytes(), "image/png")}).json()["id"]
    assert _login(client, "bob")
    assert client.post(f"/api/flights/a.csv/photos/{pid2}/cover").status_code == 404
    assert _login(client)
    assert client.post(f"/api/flights/a.csv/photos/{pid2}/cover").status_code == 200
    photos = client.get("/api/flights/a.csv/photos").json()
    cover = [p for p in photos if p["is_cover"] == 1]
    assert len(cover) == 1 and cover[0]["id"] == pid2


def test_user_data_counts_include_photos(client):
    _seed()
    assert _login(client)
    client.post("/api/flights/a.csv/photos",
                files={"file": ("x.png", _png_bytes(), "image/png")})
    r = client.request("DELETE", "/api/account", json={})
    assert r.status_code == 409
    assert r.json()["counts"]["photos"] == 1


def test_flight_detail_and_list_render_with_photos(client):
    _seed()
    assert _login(client)
    client.post("/api/flights/a.csv/photos",
                files={"file": ("x.png", _png_bytes(), "image/png")})
    r = client.get("/flight/a.csv")
    assert r.status_code == 200
    assert "photoGallery" in r.text
    assert "photoFileInput" in r.text
    r = client.get("/flights")
    assert r.status_code == 200
    assert "Cover" in r.text


def test_delete_flight_removes_photos(client):
    _seed()
    assert _login(client)
    pid = client.post("/api/flights/a.csv/photos",
                      files={"file": ("x.png", _png_bytes(), "image/png")}).json()["id"]
    stored = (client.app_mod.PHOTO_DIR / f"p{pid}.png")
    assert stored.exists()
    assert client.delete("/api/flights/a.csv").status_code == 200
    assert not stored.exists()
    assert get_flight_photos("a.csv") == []