from mission import (build_mission_from_params, build_waypoints, clean_coords,
                     cut_coords, render_mission_xml, simplify_indices,
                     validate_waypoints)


def make_coords(n, lat0=40.0, lon0=9.0, alt=120.0, step=0.0005):
    """A straight level track of [lat, lon, alt] rows."""
    return [[lat0 + i * step, lon0 + i * step, alt] for i in range(n)]


def test_clean_coords_drops_invalid():
    coords = [[0.0, 0.0, 10.0], [12.0, -45.0, 10.0], [12.0, -45.0, 10.0],
              [12.0001, -45.0, 11.0]]
    out = clean_coords(coords)
    assert len(out) == 2
    assert out[0] == [12.0, -45.0, 10.0]


def test_cut_coords_range():
    coords = make_coords(101)
    out = cut_coords(coords, 0.2, 0.4)
    assert out[0] == coords[20]
    assert out[-1] == coords[40]
    assert len(out) == 21


def test_cut_coords_swaps_when_reversed():
    coords = make_coords(101)
    out = cut_coords(coords, 0.8, 0.2)
    assert out[0] == coords[20]
    assert out[-1] == coords[80]


def test_cut_coords_removes_middle_segment():
    coords = make_coords(101)
    out = cut_coords(coords, 0.0, 1.0, 0.3, 0.7)
    assert out[:31] == coords[:31]
    assert out[31] == coords[70]
    assert len(out) == 62


def test_simplify_indices_bounds():
    coords = make_coords(500)
    for target in (5, 20, 120):
        idx = simplify_indices(coords, target)
        assert idx[0] == 0
        assert idx[-1] == len(coords) - 1
        assert len(idx) <= target
        assert len(idx) >= 2


def test_simplify_indices_keeps_all_when_small():
    coords = make_coords(10)
    assert simplify_indices(coords, 60) == list(range(10))


def test_build_waypoints_fixed_relative_rth():
    coords = make_coords(5)
    wps = build_waypoints(coords, alt_mode="fixed", alt_value=150.0,
                          relative=True, cruise_speed=1200, final_action="RTH")
    assert len(wps) == 6  # 5 waypoints + RTH
    for w in wps[:5]:
        assert w["action"] == "WAYPOINT"
        assert w["alt"] == 150.0
        assert w["p1"] == 1200
        assert w["p3"] == 0
    assert wps[-1]["action"] == "RTH"
    assert wps[-1]["p1"] == 1
    assert wps[-1]["lat"] == 0.0
    assert wps[-1]["flag"] == 165
    assert all(w["flag"] == 0 for w in wps[:-1])


def test_build_waypoints_track_relative_subtracts_home():
    coords = make_coords(3, alt=100.0)
    coords[2][2] = 80.0
    wps = build_waypoints(coords, alt_mode="track", relative=True,
                          final_action="NONE")
    assert wps[0]["alt"] == 0.0
    assert wps[2]["alt"] == -20.0


def test_build_waypoints_absolute_flag():
    coords = make_coords(3)
    wps = build_waypoints(coords, alt_mode="fixed", alt_value=200.0,
                          relative=False, final_action="NONE")
    assert wps[0]["p3"] == 1
    assert wps[0]["alt"] == 200.0


def test_build_mission_from_params_pipeline():
    coords = make_coords(300)
    wps = build_mission_from_params(coords, {
        "cut": {"start": 0.1, "end": 0.9},
        "max_points": 10,
        "alt_mode": "fixed",
        "alt_value": 100.0,
        "relative": True,
        "cruise_speed": 1000,
        "final_action": "RTH",
    })
    assert 2 <= len(wps) <= 11
    assert wps[-1]["action"] == "RTH"


def test_validate_waypoints():
    ok, err = validate_waypoints([{"action": "WAYPOINT", "lat": 40.0, "lon": 9.0, "alt": 100}])
    assert ok and err is None
    ok, err = validate_waypoints([{"action": "WAYPOINT", "lat": 91.0, "lon": 9.0, "alt": 100}])
    assert not ok
    ok, err = validate_waypoints([{"action": "RTH", "lat": 0, "lon": 0, "alt": 0}])
    assert ok


def test_render_mission_xml_structure():
    wps = build_waypoints(make_coords(3), alt_value=100.0, final_action="RTH")
    xml = render_mission_xml(wps)
    assert xml.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert "<mission>" in xml
    assert 'action="WAYPOINT"' in xml
    assert 'action="RTH"' in xml
    assert 'flag="165"' in xml
    assert 'no="1"' in xml and 'no="4"' in xml


def _wp(lat=40.0, lon=9.0, alt=100.0, p1=1000):
    return {"action": "WAYPOINT", "lat": lat, "lon": lon, "alt": alt,
            "p1": p1, "p2": 0, "p3": 0}


def test_explicit_waypoints_appends_final_action():
    from app import _mission_result
    wps, xml = _mission_result({"waypoints": [_wp(), _wp(40.01, 9.01)],
                                "final_action": "RTH"})
    assert [w["action"] for w in wps] == ["WAYPOINT", "WAYPOINT", "RTH"]
    assert [w["no"] for w in wps] == [1, 2, 3]
    assert wps[-1]["flag"] == 165
    assert all(w["flag"] == 0 for w in wps[:-1])
    assert 'no="3"' in xml and 'flag="165"' in xml


def test_explicit_waypoints_none_keeps_no_final():
    from app import _mission_result
    wps, _ = _mission_result({"waypoints": [_wp(), _wp(40.01, 9.01)],
                              "final_action": "NONE"})
    assert [w["action"] for w in wps] == ["WAYPOINT", "WAYPOINT"]


def test_explicit_waypoints_strips_old_final_then_appends():
    from app import _mission_result
    wps, _ = _mission_result({"waypoints": [_wp(), _wp(40.01, 9.01),
                                            {"action": "RTH", "lat": 0, "lon": 0,
                                             "alt": 0, "p1": 1, "p2": 0, "p3": 0}],
                              "final_action": "LAND"})
    assert [w["action"] for w in wps] == ["WAYPOINT", "WAYPOINT", "LAND"]


def test_explicit_waypoints_empty_is_rejected():
    from app import _mission_result
    import pytest
    with pytest.raises(ValueError):
        _mission_result({"waypoints": [], "final_action": "RTH"})
