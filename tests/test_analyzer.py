import math

import pytest

from analyzer import analyze, compute_load_factor, haversine_km
from tests.conftest import make_flight, make_point


def test_haversine_known_distance():
    # 1 degree of latitude is ~111.195 km on the equator
    d = haversine_km(0.0, 0.0, 1.0, 0.0)
    assert abs(d - 111.195) < 1.0


def test_haversine_zero():
    assert haversine_km(12.0, -45.0, 12.0, -45.0) == 0.0


def test_load_factor_level_flight():
    pts = [make_point(roll=0.0)]
    assert compute_load_factor(pts) == [1.0]


def test_load_factor_banked_turn():
    pts = [make_point(roll=math.radians(60))]
    assert compute_load_factor(pts) == [2.0]


def test_load_factor_steep_bank():
    pts = [make_point(roll=math.radians(75))]
    assert compute_load_factor(pts) == [round(1.0 / math.cos(math.radians(75)), 2)]


def test_load_factor_near_vertical_is_invalid():
    pts = [make_point(roll=math.radians(85))]
    assert compute_load_factor(pts) == [0.0]


def test_analyze_basic_stats():
    pts = make_flight(40)
    s = analyze("fake.csv", pts)
    assert s.filename == "fake.csv"
    assert s.duration_s == pytest.approx(19.5)
    assert s.max_alt_m == 10.0
    assert s.min_alt_m == 10.0
    assert s.avg_alt_m == 10.0
    assert s.max_speed_kmh == 36.0
    assert s.avg_speed_kmh == 36.0
    assert s.distance_km > 0


def test_analyze_coordinates_include_g():
    pts = make_flight(20)
    pts[5] = make_point(timestamp=5 * 0.5, alt=10.0, gspd=36.0, roll=1.0)
    s = analyze("fake.csv", pts)
    c = s.coordinates[5]
    assert len(c) == 35
    assert c[34] == pytest.approx(round(1.0 / math.cos(1.0), 2))
    assert s.max_g == pytest.approx(round(1.0 / math.cos(1.0), 2))
    assert s.avg_g > 1.0


def test_analyze_empty():
    s = analyze("empty.csv", [])
    assert s.duration_s == 0
    assert s.max_g == 0
    assert s.coordinates == []


def test_analyze_events_no_false_positives_on_cruise():
    pts = make_flight(30)
    s = analyze("cruise.csv", pts)
    acros = [e for e in s.events if e["type"] == "acro"]
    incidents = [e for e in s.events if e["type"] == "incident"]
    assert acros == []
    assert incidents == []
