import json
import sqlite3
from pathlib import Path
from models import FlightSummary

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "flights.db"


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS flights (
                filename TEXT PRIMARY KEY,
                date TEXT,
                start_time TEXT,
                duration_s REAL,
                distance_km REAL,
                max_alt_m REAL,
                min_alt_m REAL,
                avg_alt_m REAL,
                max_speed_kmh REAL,
                avg_speed_kmh REAL,
                max_vspd_ms REAL,
                max_rssi_db INTEGER,
                min_rssi_db INTEGER,
                avg_rssi_db REAL,
                min_rqly INTEGER,
                avg_rqly REAL,
                battery_start_v REAL,
                battery_end_v REAL,
                battery_min_v REAL,
                battery_start_pct REAL,
                battery_end_pct REAL,
                battery_consumed_mah REAL,
                max_current_a REAL,
                txbat_v REAL,
                flight_modes TEXT,
                sats_max INTEGER,
                coordinates TEXT,
                notes TEXT DEFAULT ''
            )
        """)


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    if d.get("flight_modes"):
        d["flight_modes"] = json.loads(d["flight_modes"])
    else:
        d["flight_modes"] = {}
    if d.get("coordinates"):
        d["coordinates"] = json.loads(d["coordinates"])
    else:
        d["coordinates"] = []
    return d


def save_flight(summary: FlightSummary):
    with _get_conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO flights
            (filename, date, start_time, duration_s, distance_km,
             max_alt_m, min_alt_m, avg_alt_m, max_speed_kmh, avg_speed_kmh, max_vspd_ms,
             max_rssi_db, min_rssi_db, avg_rssi_db, min_rqly, avg_rqly,
             battery_start_v, battery_end_v, battery_min_v,
             battery_start_pct, battery_end_pct, battery_consumed_mah,
             max_current_a, txbat_v, flight_modes, sats_max, coordinates)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            summary.filename, summary.date, summary.start_time, summary.duration_s, summary.distance_km,
            summary.max_alt_m, summary.min_alt_m, summary.avg_alt_m, summary.max_speed_kmh, summary.avg_speed_kmh, summary.max_vspd_ms,
            summary.max_rssi_db, summary.min_rssi_db, summary.avg_rssi_db, summary.min_rqly, summary.avg_rqly,
            summary.battery_start_v, summary.battery_end_v, summary.battery_min_v,
            summary.battery_start_pct, summary.battery_end_pct, summary.battery_consumed_mah,
            summary.max_current_a, summary.txbat_v, json.dumps(summary.flight_modes), summary.sats_max,
            json.dumps(summary.coordinates),
        ))


def get_all_flights() -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute("SELECT * FROM flights ORDER BY date DESC, start_time DESC").fetchall()
    return [_row_to_dict(r) for r in rows]


def get_flight(filename: str) -> dict | None:
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM flights WHERE filename = ?", (filename,)).fetchone()
    return _row_to_dict(row) if row else None


def delete_flight(filename: str):
    with _get_conn() as conn:
        conn.execute("DELETE FROM flights WHERE filename = ?", (filename,))


def update_notes(filename: str, notes: str):
    with _get_conn() as conn:
        conn.execute("UPDATE flights SET notes = ? WHERE filename = ?", (notes, filename))


def update_flight_track(filename: str, coordinates: list, stats: dict):
    with _get_conn() as conn:
        conn.execute("""
            UPDATE flights SET
                coordinates = ?,
                distance_km = ?,
                duration_s = ?,
                max_alt_m = ?,
                min_alt_m = ?,
                avg_alt_m = ?,
                max_speed_kmh = ?,
                avg_speed_kmh = ?
            WHERE filename = ?
        """, (
            json.dumps(coordinates),
            stats["distance_km"],
            stats["duration_s"],
            stats["max_alt_m"],
            stats["min_alt_m"],
            stats["avg_alt_m"],
            stats["max_speed_kmh"],
            stats["avg_speed_kmh"],
            filename,
        ))


init_db()
