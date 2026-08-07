"""F7 · Sharing tests: create/link/toggle/revoke, public page, gpx,
board comments/likes, and isolation (issues #35, #36, #37, #38, #39)."""
from models import FlightSummary
from database import (create_user, save_flight, create_share,
                      get_shares_for_flight)


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
        events=[], coordinates=[[45.0, 9.0, 100.0, 0, 1577836800],
                                [45.01, 9.01, 120.0, 0, 1577836860]])
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
    _summary("a.csv", alice["id"])
    _summary("b.csv", bob["id"])
    return alice, bob, create_share("a.csv", alice["id"])


# --- #35 owner share management ---


def test_create_and_list_shares(client):
    alice, bob, share = _seed()
    assert _login(client)
    r = client.get("/api/flights/a.csv/shares")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]["id"] == share["id"]
    assert data[0]["token"] == share["token"]
    assert data[0]["enabled"] == 1


def test_share_requires_auth_and_ownership(client):
    _seed()
    assert client.get("/api/flights/a.csv/shares").status_code == 401
    # bob cannot manage alice's share
    assert _login(client, "bob")
    assert client.post("/api/shares/1/toggle").status_code == 404
    assert client.delete("/api/shares/1").status_code == 404


def test_toggle_and_revoke(client):
    alice, bob, share = _seed()
    assert _login(client)
    r = client.post(f"/api/shares/{share['id']}/toggle")
    assert r.status_code == 200
    assert r.json()[0]["enabled"] == 0
    assert client.delete(f"/api/shares/{share['id']}").status_code == 200
    assert client.get(f"/r/{share['token']}").status_code == 404
    assert client.get(f"/public/api/board/{share['token']}").status_code == 404


# --- #36 public page ---


def test_public_page_renders(client):
    alice, bob, share = _seed()
    r = client.get(f"/r/{share['token']}")
    assert r.status_code == 200
    assert "Shared Flight" in r.text
    assert "a.csv" in r.text
    assert "fullscreen" not in r.text  # only public view, no owner chrome


def test_public_page_404_for_unknown_token(client):
    assert client.get("/r/does-not-exist").status_code == 404


def test_public_page_works_without_login(client):
    alice, bob, share = _seed()
    assert client.get(f"/r/{share['token']}").status_code == 200


# --- #38 gpx download ---


def test_public_gpx(client):
    alice, bob, share = _seed()
    r = client.get(f"/r/{share['token']}/gpx")
    assert r.status_code == 200
    assert "<trk" in r.text
    assert r.headers.get("Content-Disposition", "").startswith("attachment")


def test_public_gpx_404_after_revoke(client):
    alice, bob, share = _seed()
    assert _login(client)
    client.delete(f"/api/shares/{share['id']}")
    assert client.get(f"/r/{share['token']}/gpx").status_code == 404


# --- #39 board: comments + likes ---


def test_comment_add_and_board(client):
    alice, bob, share = _seed()
    assert client.post(f"/public/api/board/{share['token']}/comments",
                       json={"username": "guest1", "body": "Nice flight!"}
                       ).status_code == 200
    board = client.get(f"/public/api/board/{share['token']}").json()
    assert len(board["comments"]) == 1
    assert board["comments"][0]["body"] == "Nice flight!"


def test_comment_empty_rejected(client):
    alice, bob, share = _seed()
    r = client.post(f"/public/api/board/{share['token']}/comments",
                    json={"username": "g", "body": "   "})
    assert r.status_code == 400


def test_like_toggle(client):
    alice, bob, share = _seed()
    r = client.post(f"/public/api/board/{share['token']}/like",
                    json={"username": "guest"})
    assert r.status_code == 200
    assert r.json() == {"liked": True, "count": 1}
    r2 = client.post(f"/public/api/board/{share['token']}/like",
                     json={"username": "guest"})
    assert r2.json() == {"liked": False, "count": 0}


def test_board_isolated_per_share(client):
    alice, bob, _ = _seed()
    share2 = create_share("b.csv", bob["id"])
    assert client.get(f"/public/api/board/{share2['token']}").json()["likes"] == 0
    client.post(f"/public/api/board/{share2['token']}/like", json={"username": "g"})
    assert client.get(f"/public/api/board/{share2['token']}").json()["likes"] == 1
    # alice's share unaffected
    alice_token = get_shares_for_flight("a.csv")[0]["token"]
    assert client.get(f"/public/api/board/{alice_token}").json()["likes"] == 0


def test_flight_delete_removes_shares(client):
    alice, bob, share = _seed()
    assert _login(client)
    client.request("DELETE", "/api/flights/a.csv")
    assert client.get(f"/r/{share['token']}").status_code == 404