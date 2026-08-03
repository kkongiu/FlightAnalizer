import sqlite3

from models import FlightSummary
import database
from database import delete_flight, get_flight, init_db, save_flight, update_flight_track


def _summary():
    return FlightSummary(
        filename="fake.csv",
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
        events=[{"type": "acro", "kind": "flip_roll", "peak_rotation": 4.0}],
        coordinates=[[12.0, -45.0, 10.0, 36.0, 0.0, -70, 16.8, 0.0, 1.0, 0.0,
                      0, 0, 0, 0, 0.0, 0.0, 0, 0, 0, 0, 0, "", 0, "OK", -75, 12,
                      -70, 100, 11, 1.0, 5, 98, 8.4, 100, 1.85]],
    )


def test_save_and_get_roundtrip(tmp_db):
    save_flight(_summary())
    f = get_flight("fake.csv")
    assert f is not None
    assert f["max_g"] == 2.45
    assert f["avg_g"] == 1.15
    assert f["distance_km"] == 0.5
    assert f["flight_modes"] == {"OK": 20}
    assert f["events"][0]["peak_rotation"] == 4.0
    assert f["coordinates"][0][34] == 1.85
    assert len(f["coordinates"][0]) == 35


def test_save_overwrites_existing(tmp_db):
    save_flight(_summary())
    s = _summary()
    s.max_g = 3.1
    save_flight(s)
    assert get_flight("fake.csv")["max_g"] == 3.1


def test_delete_flight(tmp_db):
    save_flight(_summary())
    delete_flight("fake.csv")
    assert get_flight("fake.csv") is None


def test_update_flight_track_persists_g(tmp_db):
    save_flight(_summary())
    stats = {
        "distance_km": 1.0,
        "duration_s": 12.0,
        "max_alt_m": 20.0,
        "min_alt_m": 0.0,
        "avg_alt_m": 9.0,
        "max_speed_kmh": 50.0,
        "avg_speed_kmh": 35.0,
        "max_g": 3.5,
        "avg_g": 1.2,
    }
    coords = [[12.0, -45.0, 10.0, 36.0, 0.0, -70, 16.8, 0.0, 1.0, 0.0,
               0, 0, 0, 0, 0.0, 0.0, 0, 0, 0, 0, 0, "", 0, "OK", -75, 12,
               -70, 100, 11, 1.0, 5, 98, 8.4, 100, 1.85]]
    update_flight_track("fake.csv", coords, stats)
    f = get_flight("fake.csv")
    assert f["max_g"] == 3.5
    assert f["avg_g"] == 1.2
    assert f["coordinates"] == coords


def test_migration_adds_g_columns(monkeypatch, tmp_path):
    # simulate a pre-existing DB created before the G-force columns existed
    db_file = tmp_path / "flights.db"
    conn = sqlite3.connect(db_file)
    conn.execute("""CREATE TABLE flights (
        filename TEXT PRIMARY KEY, date TEXT, start_time TEXT, duration_s REAL
    )""")
    conn.commit()
    conn.close()

    monkeypatch.setattr(database, "DB_PATH", db_file)
    monkeypatch.setattr(database, "DATA_DIR", tmp_path)
    init_db()

    cols = [r[1] for r in sqlite3.connect(db_file).execute("PRAGMA table_info(flights)")]
    assert "max_g" in cols
    assert "avg_g" in cols
