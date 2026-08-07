"""F5 · Messaging tests: private messaging, unread badge, isolation,
conversation deletion and admin management (issues #25, #26, #27, #29)."""
from models import FlightSummary
from database import (create_user, save_flight,
                      get_thread, get_conversations_for)


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


def _all_users():
    users = {}
    for name in ("alice", "bob", "carol"):
        users[name] = create_user(name, "pass123", role="viewer")
    return users


def _login(client, user="alice", pwd="pass123"):
    r = client.post("/login", json={"user": user, "pass": pwd})
    if r.status_code == 200:
        token = r.json().get("csrf_token")
        if token:
            client.headers["X-CSRF-Token"] = token
    return r.status_code == 200


def _login_admin(client):
    return _login(client, "admin", "admin")


# --- #25 Sending and reading own messages ---


def test_send_message_between_users(client):
    users = _all_users()
    assert _login(client)
    r = client.post("/api/messages", json={"to": "bob", "body": "Ciao Bob!"})
    assert r.status_code == 200
    msg = r.json()
    assert msg["sender_id"] == users["alice"]["id"]
    assert msg["recipient_id"] == users["bob"]["id"]
    assert msg["body"] == "Ciao Bob!"


def test_message_requires_recipient(client):
    _all_users()
    assert _login(client)
    assert client.post("/api/messages",
                       json={"to": "", "body": "x"}).status_code == 400


def test_cannot_message_self(client):
    _all_users()
    assert _login(client)
    assert client.post("/api/messages",
                       json={"to": "alice", "body": "x"}).status_code == 400


def test_cannot_message_missing_or_disabled_user(client):
    from database import get_user, update_user
    _all_users()
    update_user(get_user("bob")["id"], status="disabled")
    assert _login(client)
    assert client.post("/api/messages",
                       json={"to": "ghost", "body": "x"}).status_code == 404
    assert client.post("/api/messages",
                       json={"to": "bob", "body": "x"}).status_code == 400


def test_unread_badge_and_mark_read(client):
    users = _all_users()
    assert _login(client)
    client.post("/api/messages", json={"to": "bob", "body": "hi"})
    assert _login(client, "bob")
    assert client.get("/api/messages/unread-count").json()["unread"] == 1
    conv = get_conversations_for(users["bob"]["id"])
    assert conv and conv[0]["unread"] == 1
    client.get(f"/api/messages/conversations/{users['alice']['id']}")
    assert client.get("/api/messages/unread-count").json()["unread"] == 0


def test_messages_requires_auth(client):
    assert client.get("/api/messages/conversations").status_code == 401
    assert client.get("/api/messages/unread-count").status_code == 401
    assert client.post("/api/messages",
                       json={"to": "bob", "body": "x"}).status_code == 401


# --- #25 read-only own messages (isolation) ---


def test_thread_isolation_third_party_cannot_see_other_thread(client):
    _all_users()
    assert _login(client)  # alice -> bob
    client.post("/api/messages", json={"to": "bob", "body": "secret"})
    assert _login(client, "carol")
    convs = client.get("/api/messages/conversations").json()
    assert all(c["other_username"] not in ("alice", "bob") for c in convs)


def test_message_page_renders_for_any_user(client):
    _all_users()
    assert _login(client)
    r = client.get("/messages")
    assert r.status_code == 200
    assert "Conversations" in r.text
    assert "New" in r.text


# --- #29 flight attachment respects isolation ---


def test_flight_attachment_checks_owner(client):
    users = _all_users()
    save_flight(_summary("a.csv"), users["alice"]["id"])
    assert _login(client)
    r = client.post("/api/messages", json={"to": "bob", "body": "see",
                                           "flight_file": "a.csv"})
    assert r.status_code == 200
    assert r.json()["flight_file"] == "a.csv"
    assert _login(client, "bob")
    r = client.post("/api/messages", json={"to": "alice", "body": "reply",
                                           "flight_file": "a.csv"})
    assert r.status_code == 404


# --- #27 deletion and admin management ---


def test_delete_conversation_for_one_user(client):
    users = _all_users()
    assert _login(client)
    client.post("/api/messages", json={"to": "bob", "body": "hi"})
    r = client.delete(f"/api/messages/conversations/{users['bob']['id']}")
    assert r.status_code == 200
    assert get_conversations_for(users["alice"]["id"]) == []
    assert len(get_thread(users["bob"]["id"], users["alice"]["id"])) == 1


def test_conversation_purged_when_both_delete(client):
    from database import get_conversation_by_pair
    users = _all_users()
    assert _login(client)
    client.post("/api/messages", json={"to": "bob", "body": "hi"})
    client.delete(f"/api/messages/conversations/{users['bob']['id']}")
    assert _login(client, "bob")
    client.delete(f"/api/messages/conversations/{users['alice']['id']}")
    assert get_conversation_by_pair(users["alice"]["id"],
                                    users["bob"]["id"]) is None


def test_admin_can_delete_any_message(client):
    _all_users()
    assert _login(client)
    msg = client.post("/api/messages",
                      json={"to": "bob", "body": "admin-test"}).json()
    mid = msg["id"]
    assert client.delete(f"/api/messages/admin/{mid}").status_code == 403
    assert _login_admin(client)
    assert client.get("/api/messages/admin/conversations").status_code == 200
    assert client.delete(f"/api/messages/admin/{mid}").status_code == 200
    assert client.delete(f"/api/messages/admin/{mid}").status_code == 404


def test_user_data_counts_include_messages(client):
    users = _all_users()
    assert _login(client)
    client.post("/api/messages", json={"to": "bob", "body": "msg1"})
    client.post("/api/messages", json={"to": "bob", "body": "msg2"})
    assert _login(client, "bob")
    r = client.request("DELETE", "/api/account", json={})
    assert r.status_code == 409
    assert r.json()["counts"]["messages"] == 2
