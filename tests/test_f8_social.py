"""F7 extras: contacts, group share, per-flight visibility, and the feed."""
from models import FlightSummary
from database import (create_user, save_flight, send_friend_request,
                      accept_friend_request, set_flight_visibility,
                      create_group, add_group_member, get_feed_flights)


def _summary(filename, owner_id=None, date="2020-01-01"):
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


# --- contacts / friend requests ---


def test_friend_request_and_accept(client):
    alice = create_user("alice", "pass123", role="viewer")
    bob = create_user("bob", "pass123", role="viewer")
    assert send_friend_request(alice["id"], bob["id"])
    assert not send_friend_request(alice["id"], bob["id"])  # duplicate blocked
    assert accept_friend_request(1, bob["id"])
    assert _login(client, "alice")
    r = client.get("/api/contacts")
    assert r.status_code == 200
    data = r.json()
    assert [f["id"] for f in data["friends"]] == [bob["id"]]
    assert data["received"] == [] and data["sent"] == []


def test_contact_request_api_conflict_and_self(client):
    alice = create_user("alice", "pass123", role="viewer")
    bob = create_user("bob", "pass123", role="viewer")
    assert _login(client)
    r = client.post("/api/contacts", json={"user_id": alice["id"]})
    assert r.status_code == 400  # can't add yourself
    r = client.post("/api/contacts", json={"user_id": bob["id"]})
    assert r.status_code == 200
    r = client.post("/api/contacts", json={"user_id": bob["id"]})
    assert r.status_code == 409  # duplicate

    # bob (new session) accepts
    assert _login(client, "bob")
    data = client.get("/api/contacts").json()
    req = data["received"][0]
    r = client.post(f"/api/contacts/{req['id']}/accept")
    assert r.status_code == 200
    assert client.get("/api/contacts").json()["friends"][0]["id"] == alice["id"]


def test_contact_remove(client):
    alice = create_user("alice", "pass123", role="viewer")
    bob = create_user("bob", "pass123", role="viewer")
    send_friend_request(alice["id"], bob["id"])
    accept_friend_request(1, bob["id"])
    assert _login(client)
    r = client.delete(f"/api/contacts/{bob['id']}")
    assert r.status_code == 200
    assert [f["id"] for f in client.get("/api/contacts").json()["friends"]] == []


# --- visibility & feed ---


def test_visibility_change_and_feed_for_contacts(client):
    alice = create_user("alice", "pass123", role="viewer")
    bob = create_user("bob", "pass123", role="viewer")
    send_friend_request(alice["id"], bob["id"])
    accept_friend_request(1, bob["id"])
    _summary("a.csv", alice["id"])

    assert _login(client, "alice")
    r = client.put("/api/flights/a.csv/visibility",
                   json={"visibility": "contacts"})
    assert r.status_code == 200
    r = client.put("/api/flights/a.csv/visibility", json={"visibility": "weird"})
    assert r.status_code == 400

    # Bob sees alice's flight in his feed (shared via contacts)
    assert _login(client, "bob")
    feed = client.get("/api/feed").json()["flights"]
    assert [f["filename"] for f in feed] == ["a.csv"]

    # A stranger (carol) does not see it
    carol = create_user("carol", "pass123", role="viewer")
    assert _login(client, "carol")
    assert client.get("/api/feed").json()["flights"] == []


def test_visibility_private_excluded_from_feed(client):
    alice = create_user("alice", "pass123", role="viewer")
    bob = create_user("bob", "pass123", role="viewer")
    send_friend_request(alice["id"], bob["id"])
    accept_friend_request(1, bob["id"])
    _summary("a.csv", alice["id"])
    # default visibility is private
    assert _login(client, "bob")
    assert client.get("/api/feed").json()["flights"] == []


def test_feed_page_renders(client):
    alice = create_user("alice", "pass123", role="viewer")
    bob = create_user("bob", "pass123", role="viewer")
    send_friend_request(alice["id"], bob["id"])
    accept_friend_request(1, bob["id"])
    _summary("a.csv", alice["id"])
    set_flight_visibility("a.csv", "public", alice["id"], False)
    assert _login(client, "bob")
    r = client.get("/feed")
    assert r.status_code == 200
    assert b"a.csv" in r.content


# --- groups ---


def test_group_share_shows_in_feed(client):
    alice = create_user("alice", "pass123", role="admin")
    bob = create_user("bob", "pass123", role="viewer")
    _summary("a.csv", alice["id"])
    g = create_group("Team A", alice["id"])
    add_group_member(g["id"], bob["id"])
    _login(client, "alice")
    from database import set_flight_shared_group
    set_flight_shared_group("a.csv", g["id"], alice["id"], True)
    assert _login(client, "bob")
    feed = client.get("/api/feed").json()["flights"]
    assert [f["filename"] for f in feed] == ["a.csv"]


def test_groups_admin_api(client):
    bob = create_user("bob", "pass123", role="viewer")
    assert _login(client, "admin", pwd="admin")  # seeded default admin
    r = client.post("/api/groups", json={"name": "TEAM"})
    assert r.status_code == 200
    gid = r.json()["group"]["id"]
    r = client.post(f"/api/groups/{gid}/members", json={"user_id": bob["id"]})
    assert r.status_code == 200
    data = client.get("/api/groups").json()["groups"]
    assert data[0]["name"] == "TEAM"
    assert [m["user_id"] for m in data[0]["members"]] == [bob["id"]]
    r = client.delete(f"/api/groups/{gid}/members/{bob['id']}")
    assert r.status_code == 200
    r = client.delete(f"/api/groups/{gid}")
    assert r.status_code == 200


def test_groups_admin_only(client):
    alice = create_user("alice", "pass123", role="viewer")
    assert _login(client)
    r = client.post("/api/groups", json={"name": "NOPE"})
    assert r.status_code == 403


def test_groups_and_contacts_pages_render(client):
    create_user("alice", "pass123", role="viewer")
    assert _login(client)
    assert client.get("/groups").status_code == 200
    assert client.get("/contacts").status_code == 200