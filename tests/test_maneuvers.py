from maneuvers import detect_acros, detect_incidents
from tests.conftest import make_point


def test_detect_acros_finds_flip():
    n = 70
    # a sharp roll to ~pi (180°) that is then sustained, on a level airborne
    # cruise — like a real flip where the roll stays pinned during the move
    rolls = [0.0] * 15 + [3.14] * (n - 15)
    pts = [make_point(timestamp=i * 0.5, alt=10.0, gspd=36.0, roll=rolls[i]) for i in range(n)]
    events = detect_acros(pts)
    flips = [e for e in events if e["kind"] == "flip_roll"]
    assert len(flips) >= 1
    e = flips[0]
    assert e["peak_roll"] >= 1.2
    assert e["peak_rotation"] > 0
    assert e["i"] >= 0 and e["end_i"] < n


def test_detect_acros_nothing_on_cruise():
    pts = [make_point(timestamp=i * 0.5, alt=10.0, gspd=36.0) for i in range(30)]
    assert detect_acros(pts) == []


def test_detect_incidents_finds_crash():
    n = 40
    pts = []
    for i in range(n):
        if i <= 5:
            alt, vspd, gspd = 0.0, 0.0, 0.0
        elif i <= 10:
            alt, vspd, gspd = 50.0, 0.0, 36.0
        elif i <= 16:
            alts = [50, 40, 30, 20, 10, 2]
            alt, vspd, gspd = alts[i - 11], -8.0, 30.0
        else:
            alt, vspd, gspd = 2.0, 0.0, 0.0
        pts.append(make_point(timestamp=i * 0.5, alt=alt, vspd=vspd, gspd=gspd))
    events = detect_incidents(pts)
    assert len(events) >= 1
    e = events[0]
    assert e["type"] == "incident"
    assert e["drop"] >= 20
    assert e["vspd"] <= -5


def test_detect_incidents_nothing_on_gentle_descent():
    # slow descent with small drop must not trigger an incident
    pts = [make_point(timestamp=i * 0.5, alt=20.0 - i * 0.5, vspd=-1.0, gspd=36.0) for i in range(30)]
    assert detect_incidents(pts) == []
