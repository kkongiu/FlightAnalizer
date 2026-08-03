from pathlib import Path

from parser import parse_log, parse_timestamp, safe_float, safe_int

FIXTURE = Path(__file__).parent / "fixtures" / "sample.csv"


def test_parse_timestamp_epoch():
    ts = parse_timestamp("2020-01-01", "12:00:00.000")
    assert isinstance(ts, float)
    assert ts > 0


def test_parse_timestamp_monotonic():
    t0 = parse_timestamp("2020-01-01", "12:00:00.000")
    t1 = parse_timestamp("2020-01-01", "12:00:00.500")
    assert t1 - t0 == 0.5


def test_safe_float_handles_bad_input():
    assert safe_float("3.5") == 3.5
    assert safe_float("") == 0.0
    assert safe_float("abc") == 0.0
    assert safe_float(None) == 0.0


def test_safe_int_handles_bad_input():
    assert safe_int("7") == 7
    assert safe_int("7.9") == 7
    assert safe_int("") == 0
    assert safe_int("zzz") == 0


def test_parse_log_reads_fixture():
    points = parse_log(FIXTURE)
    assert len(points) == 6
    p = points[0]
    assert p.lat == 12.0
    assert p.lon == -45.0
    assert p.alt == 2.0
    assert p.gspd == 0.0
    assert p.vspd == 0.0
    assert p.heading == 0.0
    assert p.rssi_1 == -70
    assert p.rssi_2 == -75
    assert p.rqly == 100
    assert p.flight_mode == "OK"
    assert p.sats == 12
    assert p.rxbt == 16.8
    assert p.txbat == 8.4


def test_parse_log_timestamps_are_sequential():
    points = parse_log(FIXTURE)
    dts = [points[i].timestamp - points[i - 1].timestamp for i in range(1, len(points))]
    assert all(abs(d - 0.5) < 1e-6 for d in dts)


def test_parse_log_field_mapping_tail():
    points = parse_log(FIXTURE)
    last = points[-1]
    assert last.alt == 2.5
    assert last.gspd == 10.0
    assert last.curr == 3.0
    assert last.capa == 6.0
    assert last.bat_pct == 98.0
