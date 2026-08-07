"""Help & guide tests: interactive guide page renders with quick-start and
feature sections, and the help FAB/nav are present in the base layout."""
from database import create_user


def _login(client, user="alice", pwd="pass123"):
    r = client.post("/login", json={"user": user, "pass": pwd})
    if r.status_code == 200:
        token = r.json().get("csrf_token")
        if token:
            client.headers["X-CSRF-Token"] = token
    return r.status_code == 200


def test_help_page_renders_full_guide(client):
    create_user("alice", "pass123", role="viewer")
    assert _login(client)
    r = client.get("/help")
    assert r.status_code == 200
    t = r.text
    assert "Help &amp; Guide" in t or "Help & Guide" in t
    assert "Import your first log" in t
    assert "Sharing" in t
    assert "helpSearch" in t
    assert "Getting started" in t


def test_help_nav_and_fab_present(client):
    create_user("alice", "pass123", role="viewer")
    assert _login(client)
    r = client.get("/flights")
    assert r.status_code == 200
    assert "/flight/help" in r.text
    assert 'id="helpFab"' in r.text
    assert 'id="helpFabMenu"' in r.text


def test_nav_grouped_dropdowns_for_viewer(client):
    """The nav is grouped by area (Flights/Community/Account); admin-only items
    (Admin/Users/Audit) are hidden for non-admin users."""
    create_user("alice", "pass123", role="viewer")
    assert _login(client)
    r = client.get("/flights")
    assert r.status_code == 200
    t = r.text
    assert "nav-group" in t
    assert "mnav-group" in t
    assert 'id="menuToggle"' in t
    assert 'id="mobileNav"' in t
    # grouped labels present
    assert ">Flights <" in t
    assert ">Community <" in t
    assert ">Account <" in t
    # admin-only section hidden for viewers
    assert ">Admin <" not in t
    assert "/flight/users" not in t
    assert "/flight/audit" not in t


def test_nav_grouped_dropdowns_for_admin(client):
    """Admin sees the Admin group with Users and Audit."""
    assert _login(client, user="admin", pwd="admin")
    r = client.get("/flights")
    assert r.status_code == 200
    t = r.text
    assert ">Admin <" in t
    assert "/flight/users" in t
    assert "/flight/audit" in t