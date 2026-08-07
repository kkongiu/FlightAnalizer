import json
import hashlib
import os
import secrets
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from models import FlightSummary, Vehicle

DATA_DIR = Path(os.environ.get("POCKET_DATA_DIR", Path(__file__).parent / "data"))
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "flights.db"


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


# --- Versioned schema migrations ---
#
# The schema created by init_db() is the baseline (version 1). Every later
# change is an ordered step applied exactly once, inside a transaction, and
# recorded in `PRAGMA user_version`. A failing migration aborts (rollback) and
# surfaces instead of being silently swallowed.

SCHEMA_BASELINE_VERSION = 1


def _first_admin_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT id FROM users WHERE role = 'admin' ORDER BY id ASC LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def _migrate_002_flights_owner_id(conn: sqlite3.Connection):
    conn.execute("ALTER TABLE flights ADD COLUMN owner_id INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_flights_owner ON flights(owner_id)")
    admin_id = _first_admin_id(conn)
    if admin_id:
        conn.execute("UPDATE flights SET owner_id = ? WHERE owner_id IS NULL", (admin_id,))


def _migrate_003_vehicles_owner_id(conn: sqlite3.Connection):
    conn.execute("ALTER TABLE vehicles ADD COLUMN owner_id INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vehicles_owner ON vehicles(owner_id)")
    admin_id = _first_admin_id(conn)
    if admin_id:
        conn.execute("UPDATE vehicles SET owner_id = ? WHERE owner_id IS NULL", (admin_id,))


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    row = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in row)


def _migrate_004_users_account_fields(conn: sqlite3.Connection):
    if not _column_exists(conn, "users", "status"):
        conn.execute("ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
    if not _column_exists(conn, "users", "reset_token_hash"):
        conn.execute("ALTER TABLE users ADD COLUMN reset_token_hash TEXT")
    if not _column_exists(conn, "users", "reset_expires_at"):
        conn.execute("ALTER TABLE users ADD COLUMN reset_expires_at TEXT")
    if not _column_exists(conn, "users", "preferences"):
        conn.execute("ALTER TABLE users ADD COLUMN preferences TEXT NOT NULL DEFAULT '{}'")


def _migrate_005_users_email_confirmation(conn: sqlite3.Connection):
    if not _column_exists(conn, "users", "email"):
        conn.execute("ALTER TABLE users ADD COLUMN email TEXT")
    if not _column_exists(conn, "users", "confirm_token_hash"):
        conn.execute("ALTER TABLE users ADD COLUMN confirm_token_hash TEXT")
    if not _column_exists(conn, "users", "confirm_expires_at"):
        conn.execute("ALTER TABLE users ADD COLUMN confirm_expires_at TEXT")
    if not _column_exists(conn, "users", "privacy_accepted_at"):
        conn.execute("ALTER TABLE users ADD COLUMN privacy_accepted_at TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email)")


def _migrate_006_audit_log(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL DEFAULT (datetime('now')),
            user_id INTEGER,
            username TEXT,
            action TEXT NOT NULL,
            detail TEXT,
            ip TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_username ON audit_log(username)")


def _migrate_007_messages(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_a INTEGER NOT NULL,
            user_b INTEGER NOT NULL,
            archived_by_a INTEGER NOT NULL DEFAULT 0,
            archived_by_b INTEGER NOT NULL DEFAULT 0,
            UNIQUE (user_a, user_b)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            sender_id INTEGER NOT NULL,
            recipient_id INTEGER NOT NULL,
            body TEXT NOT NULL,
            flight_file TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            read_at TEXT,
            deleted_by_sender INTEGER NOT NULL DEFAULT 0,
            deleted_by_recipient INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_recipient ON messages(recipient_id)")


def _migrate_008_flight_photos(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS flight_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            flight_filename TEXT NOT NULL,
            owner_id INTEGER,
            stored_name TEXT NOT NULL,
            original_name TEXT,
            captured_at TEXT,
            is_cover INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_flight ON flight_photos(flight_filename)")


MIGRATIONS = [
    (2, "flights_owner_id", _migrate_002_flights_owner_id),
    (3, "vehicles_owner_id", _migrate_003_vehicles_owner_id),
    (4, "users_account_fields", _migrate_004_users_account_fields),
    (5, "users_email_confirmation", _migrate_005_users_email_confirmation),
    (6, "audit_log", _migrate_006_audit_log),
    (7, "messages", _migrate_007_messages),
    (8, "flight_photos", _migrate_008_flight_photos),
]


def _apply_migrations():
    with _get_conn() as conn:
        row = conn.execute("PRAGMA user_version").fetchone()
        current = row[0] if row else 0
        if current < SCHEMA_BASELINE_VERSION:
            conn.execute(f"PRAGMA user_version = {SCHEMA_BASELINE_VERSION}")
            current = SCHEMA_BASELINE_VERSION
        for version, name, fn in sorted(MIGRATIONS):
            if version <= current:
                continue
            fn(conn)
            conn.execute(f"PRAGMA user_version = {version}")
            print(f"[database] migration applied: {version} - {name}", flush=True)


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
                home_distance_km REAL DEFAULT 0,
                glide_ratio REAL DEFAULT 0,
                efficiency_km_per_mah REAL DEFAULT 0,
                vibration_score REAL DEFAULT 0,
                events TEXT DEFAULT '[]',
                coordinates TEXT,
                notes TEXT DEFAULT '',
                vehicle_id INTEGER DEFAULT NULL
            )
        """)
        try:
            conn.execute("ALTER TABLE flights ADD COLUMN tags TEXT DEFAULT '[]'")
        except Exception:
            pass
        conn.execute("""
            CREATE TABLE IF NOT EXISTS vehicles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                vehicle_type TEXT NOT NULL DEFAULT 'drone',
                photo TEXT DEFAULT '',
                is_default INTEGER DEFAULT 0
            )
        """)
        # Migrations
        for col, typ in [('home_distance_km', 'REAL DEFAULT 0'), ('glide_ratio', 'REAL DEFAULT 0'), ('efficiency_km_per_mah', 'REAL DEFAULT 0'), ('vibration_score', 'REAL DEFAULT 0'), ('events', "TEXT DEFAULT '[]'")]:
            try:
                conn.execute(f"ALTER TABLE flights ADD COLUMN {col} {typ}")
            except Exception:
                pass
        try:
            conn.execute("ALTER TABLE flights ADD COLUMN vehicle_id INTEGER DEFAULT NULL")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE flights ADD COLUMN track_source TEXT DEFAULT 'csv'")
        except Exception:
            pass
        for col, typ in [('max_g', 'REAL DEFAULT 0'), ('avg_g', 'REAL DEFAULT 0')]:
            try:
                conn.execute(f"ALTER TABLE flights ADD COLUMN {col} {typ}")
            except Exception:
                pass
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'viewer',
                status TEXT NOT NULL DEFAULT 'active',
                reset_token_hash TEXT,
                reset_expires_at TEXT,
                preferences TEXT NOT NULL DEFAULT '{}',
                email TEXT,
                confirm_token_hash TEXT,
                confirm_expires_at TEXT,
                privacy_accepted_at TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        # Seed initial admin from env vars if no users exist
        existing = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if existing == 0:
            admin_user = os.environ.get("POCKET_USER", "admin")
            admin_pass = os.environ.get("POCKET_PASS", "admin")
            create_user(admin_user, admin_pass, role="admin")
    _apply_migrations()


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
    if d.get("events"):
        d["events"] = json.loads(d["events"])
    else:
        d["events"] = []
    if d.get("tags"):
        d["tags"] = json.loads(d["tags"])
    else:
        d["tags"] = []
    return d


def save_flight(summary: FlightSummary, owner_id: int | None = None):
    with _get_conn() as conn:
        conn.execute("""
            INSERT INTO flights
            (filename, date, start_time, duration_s, distance_km,
             max_alt_m, min_alt_m, avg_alt_m, max_speed_kmh, avg_speed_kmh, max_vspd_ms,
             max_g, avg_g,
             max_rssi_db, min_rssi_db, avg_rssi_db, min_rqly, avg_rqly,
             battery_start_v, battery_end_v, battery_min_v,
             battery_start_pct, battery_end_pct, battery_consumed_mah,
             max_current_a, txbat_v, flight_modes, sats_max,
             home_distance_km, glide_ratio, efficiency_km_per_mah, vibration_score, events,
             coordinates, owner_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(filename) DO UPDATE SET
                date = excluded.date,
                start_time = excluded.start_time,
                duration_s = excluded.duration_s,
                distance_km = excluded.distance_km,
                max_alt_m = excluded.max_alt_m,
                min_alt_m = excluded.min_alt_m,
                avg_alt_m = excluded.avg_alt_m,
                max_speed_kmh = excluded.max_speed_kmh,
                avg_speed_kmh = excluded.avg_speed_kmh,
                max_vspd_ms = excluded.max_vspd_ms,
                max_g = excluded.max_g,
                avg_g = excluded.avg_g,
                max_rssi_db = excluded.max_rssi_db,
                min_rssi_db = excluded.min_rssi_db,
                avg_rssi_db = excluded.avg_rssi_db,
                min_rqly = excluded.min_rqly,
                avg_rqly = excluded.avg_rqly,
                battery_start_v = excluded.battery_start_v,
                battery_end_v = excluded.battery_end_v,
                battery_min_v = excluded.battery_min_v,
                battery_start_pct = excluded.battery_start_pct,
                battery_end_pct = excluded.battery_end_pct,
                battery_consumed_mah = excluded.battery_consumed_mah,
                max_current_a = excluded.max_current_a,
                txbat_v = excluded.txbat_v,
                flight_modes = excluded.flight_modes,
                sats_max = excluded.sats_max,
                home_distance_km = excluded.home_distance_km,
                glide_ratio = excluded.glide_ratio,
                efficiency_km_per_mah = excluded.efficiency_km_per_mah,
                vibration_score = excluded.vibration_score,
                events = excluded.events,
                coordinates = excluded.coordinates,
                owner_id = COALESCE(excluded.owner_id, flights.owner_id)
        """, (
            summary.filename, summary.date, summary.start_time, summary.duration_s, summary.distance_km,
            summary.max_alt_m, summary.min_alt_m, summary.avg_alt_m, summary.max_speed_kmh, summary.avg_speed_kmh, summary.max_vspd_ms,
            summary.max_g, summary.avg_g,
            summary.max_rssi_db, summary.min_rssi_db, summary.avg_rssi_db, summary.min_rqly, summary.avg_rqly,
            summary.battery_start_v, summary.battery_end_v, summary.battery_min_v,
            summary.battery_start_pct, summary.battery_end_pct, summary.battery_consumed_mah,
            summary.max_current_a, summary.txbat_v, json.dumps(summary.flight_modes), summary.sats_max,
            summary.home_distance_km, summary.glide_ratio, summary.efficiency_km_per_mah, summary.vibration_score,
            json.dumps(summary.events),
            json.dumps(summary.coordinates),
            owner_id,
        ))


def _username_map() -> dict:
    with _get_conn() as conn:
        rows = conn.execute("SELECT id, username FROM users").fetchall()
    return {r["id"]: r["username"] for r in rows}


def get_all_flights(owner_id: int | None = None, is_admin: bool = False) -> list[dict]:
    with _get_conn() as conn:
        if is_admin or owner_id is None:
            rows = conn.execute("SELECT * FROM flights ORDER BY date DESC, start_time DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM flights WHERE owner_id = ? ORDER BY date DESC, start_time DESC",
                (owner_id,)).fetchall()
        out = [_row_to_dict(r) for r in rows]
    usernames = _username_map()
    for d in out:
        d["owner_username"] = usernames.get(d.get("owner_id"))
    return out


def get_flight(filename: str, owner_id: int | None = None, is_admin: bool = False) -> dict | None:
    with _get_conn() as conn:
        if is_admin or owner_id is None:
            row = conn.execute("SELECT * FROM flights WHERE filename = ?", (filename,)).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM flights WHERE filename = ? AND owner_id = ?",
                (filename, owner_id)).fetchone()
        d = _row_to_dict(row) if row else None
    if d:
        d["owner_username"] = _username_map().get(d.get("owner_id"))
    return d


def delete_flight(filename: str, owner_id: int | None = None, is_admin: bool = False) -> bool:
    with _get_conn() as conn:
        if is_admin or owner_id is None:
            cur = conn.execute("DELETE FROM flights WHERE filename = ?", (filename,))
        else:
            cur = conn.execute("DELETE FROM flights WHERE filename = ? AND owner_id = ?",
                               (filename, owner_id))
        return cur.rowcount > 0


def update_flight_events(filename: str, events: list, owner_id: int | None = None, is_admin: bool = False) -> bool:
    with _get_conn() as conn:
        if is_admin or owner_id is None:
            cur = conn.execute("UPDATE flights SET events = ? WHERE filename = ?",
                               (json.dumps(events), filename))
        else:
            cur = conn.execute("UPDATE flights SET events = ? WHERE filename = ? AND owner_id = ?",
                               (json.dumps(events), filename, owner_id))
        return cur.rowcount > 0


def rename_flight(old_filename: str, new_filename: str, owner_id: int | None = None, is_admin: bool = False) -> bool:
    with _get_conn() as conn:
        if is_admin or owner_id is None:
            cur = conn.execute("UPDATE flights SET filename = ? WHERE filename = ?",
                               (new_filename, old_filename))
        else:
            cur = conn.execute("UPDATE flights SET filename = ? WHERE filename = ? AND owner_id = ?",
                               (new_filename, old_filename, owner_id))
        return cur.rowcount > 0


def update_notes(filename: str, notes: str, owner_id: int | None = None, is_admin: bool = False) -> bool:
    with _get_conn() as conn:
        if is_admin or owner_id is None:
            cur = conn.execute("UPDATE flights SET notes = ? WHERE filename = ?", (notes, filename))
        else:
            cur = conn.execute("UPDATE flights SET notes = ? WHERE filename = ? AND owner_id = ?",
                               (notes, filename, owner_id))
        return cur.rowcount > 0


def update_flight_track(filename: str, coordinates: list, stats: dict, owner_id: int | None = None, is_admin: bool = False) -> bool:
    with _get_conn() as conn:
        if is_admin or owner_id is None:
            cur = conn.execute("""
                UPDATE flights SET
                    coordinates = ?,
                    distance_km = ?,
                    duration_s = ?,
                    max_alt_m = ?,
                    min_alt_m = ?,
                    avg_alt_m = ?,
                    max_speed_kmh = ?,
                    avg_speed_kmh = ?,
                    max_g = ?,
                    avg_g = ?
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
                stats.get("max_g", 0),
                stats.get("avg_g", 0),
                filename,
            ))
        else:
            cur = conn.execute("""
                UPDATE flights SET
                    coordinates = ?,
                    distance_km = ?,
                    duration_s = ?,
                    max_alt_m = ?,
                    min_alt_m = ?,
                    avg_alt_m = ?,
                    max_speed_kmh = ?,
                    avg_speed_kmh = ?,
                    max_g = ?,
                    avg_g = ?
                WHERE filename = ? AND owner_id = ?
            """, (
                json.dumps(coordinates),
                stats["distance_km"],
                stats["duration_s"],
                stats["max_alt_m"],
                stats["min_alt_m"],
                stats["avg_alt_m"],
                stats["max_speed_kmh"],
                stats["avg_speed_kmh"],
                stats.get("max_g", 0),
                stats.get("avg_g", 0),
                filename,
                owner_id,
            ))
        return cur.rowcount > 0


# --- Vehicle CRUD ---

def get_vehicles(owner_id: int | None = None, is_admin: bool = False) -> list[Vehicle]:
    with _get_conn() as conn:
        if is_admin or owner_id is None:
            rows = conn.execute("SELECT * FROM vehicles ORDER BY is_default DESC, name ASC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM vehicles WHERE owner_id = ? ORDER BY is_default DESC, name ASC",
                (owner_id,)).fetchall()
    return [Vehicle(**dict(r)) for r in rows]


def get_vehicle(vehicle_id: int, owner_id: int | None = None, is_admin: bool = False) -> Vehicle | None:
    with _get_conn() as conn:
        if is_admin or owner_id is None:
            row = conn.execute("SELECT * FROM vehicles WHERE id = ?", (vehicle_id,)).fetchone()
        else:
            row = conn.execute("SELECT * FROM vehicles WHERE id = ? AND owner_id = ?",
                               (vehicle_id, owner_id)).fetchone()
    return Vehicle(**dict(row)) if row else None


def get_default_vehicle(owner_id: int | None = None, is_admin: bool = False) -> Vehicle | None:
    with _get_conn() as conn:
        if is_admin or owner_id is None:
            row = conn.execute("SELECT * FROM vehicles WHERE is_default = 1 LIMIT 1").fetchone()
        else:
            row = conn.execute("SELECT * FROM vehicles WHERE is_default = 1 AND owner_id = ? LIMIT 1",
                               (owner_id,)).fetchone()
    return Vehicle(**dict(row)) if row else None


def _clear_defaults(conn: sqlite3.Connection, owner_id: int | None):
    if owner_id is None:
        conn.execute("UPDATE vehicles SET is_default = 0")
    else:
        conn.execute("UPDATE vehicles SET is_default = 0 WHERE owner_id = ?", (owner_id,))


def create_vehicle(name: str, vehicle_type: str = "drone", is_default: bool = False,
                   owner_id: int | None = None) -> Vehicle | None:
    with _get_conn() as conn:
        if is_default:
            _clear_defaults(conn, owner_id)
        cur = conn.execute(
            "INSERT INTO vehicles (name, vehicle_type, is_default, owner_id) VALUES (?, ?, ?, ?)",
            (name, vehicle_type, 1 if is_default else 0, owner_id),
        )
        vehicle_id = cur.lastrowid
    return get_vehicle(vehicle_id, owner_id, owner_id is None)


def update_vehicle(vehicle_id: int, name: str = None, vehicle_type: str = None, is_default: bool = None,
                   owner_id: int | None = None, is_admin: bool = False) -> Vehicle | None:
    with _get_conn() as conn:
        if is_admin or owner_id is None:
            existing = conn.execute("SELECT * FROM vehicles WHERE id = ?", (vehicle_id,)).fetchone()
        else:
            existing = conn.execute("SELECT * FROM vehicles WHERE id = ? AND owner_id = ?",
                                    (vehicle_id, owner_id)).fetchone()
        if not existing:
            return None
        new_name = name if name is not None else existing["name"]
        new_type = vehicle_type if vehicle_type is not None else existing["vehicle_type"]
        new_default = is_default if is_default is not None else bool(existing["is_default"])
        if new_default:
            _clear_defaults(conn, existing["owner_id"] if not (is_admin or owner_id is None) else owner_id)
        conn.execute(
            "UPDATE vehicles SET name = ?, vehicle_type = ?, is_default = ? WHERE id = ?",
            (new_name, new_type, 1 if new_default else 0, vehicle_id),
        )
    return get_vehicle(vehicle_id, owner_id, is_admin)


def delete_vehicle(vehicle_id: int, owner_id: int | None = None, is_admin: bool = False) -> bool:
    with _get_conn() as conn:
        if is_admin or owner_id is None:
            cur = conn.execute("DELETE FROM vehicles WHERE id = ?", (vehicle_id,))
        else:
            cur = conn.execute("DELETE FROM vehicles WHERE id = ? AND owner_id = ?",
                               (vehicle_id, owner_id))
        if cur.rowcount:
            conn.execute("UPDATE flights SET vehicle_id = NULL WHERE vehicle_id = ?", (vehicle_id,))
        return cur.rowcount > 0


def set_vehicle_photo(vehicle_id: int, photo_path: str, owner_id: int | None = None, is_admin: bool = False) -> bool:
    with _get_conn() as conn:
        if is_admin or owner_id is None:
            cur = conn.execute("UPDATE vehicles SET photo = ? WHERE id = ?", (photo_path, vehicle_id))
        else:
            cur = conn.execute("UPDATE vehicles SET photo = ? WHERE id = ? AND owner_id = ?",
                               (photo_path, vehicle_id, owner_id))
        return cur.rowcount > 0


def get_vehicle_stats(owner_id: int | None = None, is_admin: bool = False) -> list[dict]:
    with _get_conn() as conn:
        if is_admin or owner_id is None:
            rows = conn.execute("""
                SELECT
                    v.id, v.name, v.vehicle_type, v.photo, v.is_default,
                    COUNT(f.filename) AS flight_count,
                    COALESCE(SUM(f.distance_km), 0) AS total_km,
                    COALESCE(SUM(f.duration_s), 0) AS total_duration_s,
                    COALESCE(SUM(f.home_distance_km), 0) AS total_home_km,
                    COALESCE(MAX(f.max_alt_m), 0) AS max_alt_m,
                    COALESCE(MAX(f.max_speed_kmh), 0) AS max_speed_kmh
                FROM vehicles v
                LEFT JOIN flights f ON f.vehicle_id = v.id
                GROUP BY v.id
                ORDER BY v.is_default DESC, v.name ASC
            """).fetchall()
        else:
            rows = conn.execute("""
                SELECT
                    v.id, v.name, v.vehicle_type, v.photo, v.is_default,
                    COUNT(f.filename) AS flight_count,
                    COALESCE(SUM(f.distance_km), 0) AS total_km,
                    COALESCE(SUM(f.duration_s), 0) AS total_duration_s,
                    COALESCE(SUM(f.home_distance_km), 0) AS total_home_km,
                    COALESCE(MAX(f.max_alt_m), 0) AS max_alt_m,
                    COALESCE(MAX(f.max_speed_kmh), 0) AS max_speed_kmh
                FROM vehicles v
                LEFT JOIN flights f ON f.vehicle_id = v.id AND f.owner_id = ?
                WHERE v.owner_id = ?
                GROUP BY v.id
                ORDER BY v.is_default DESC, v.name ASC
            """, (owner_id, owner_id)).fetchall()
    return [dict(r) for r in rows]


def get_battery_health_by_vehicle(vehicle_id: int | None = None, owner_id: int | None = None,
                                  is_admin: bool = False) -> list[dict]:
    with _get_conn() as conn:
        if vehicle_id:
            if is_admin or owner_id is None:
                rows = conn.execute("""
                    SELECT date, battery_start_v, battery_end_v, battery_min_v, battery_consumed_mah
                    FROM flights WHERE vehicle_id = ? AND battery_start_v > 0
                    ORDER BY date ASC
                """, (vehicle_id,)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT date, battery_start_v, battery_end_v, battery_min_v, battery_consumed_mah
                    FROM flights WHERE vehicle_id = ? AND owner_id = ? AND battery_start_v > 0
                    ORDER BY date ASC
                """, (vehicle_id, owner_id)).fetchall()
        else:
            if is_admin or owner_id is None:
                rows = conn.execute("""
                    SELECT f.date, f.battery_start_v, f.battery_end_v, f.battery_min_v,
                           f.battery_consumed_mah, v.id AS vehicle_id, v.name AS vehicle_name
                    FROM flights f LEFT JOIN vehicles v ON f.vehicle_id = v.id
                    WHERE f.battery_start_v > 0
                    ORDER BY v.name, f.date ASC
                """).fetchall()
            else:
                rows = conn.execute("""
                    SELECT f.date, f.battery_start_v, f.battery_end_v, f.battery_min_v,
                           f.battery_consumed_mah, v.id AS vehicle_id, v.name AS vehicle_name
                    FROM flights f LEFT JOIN vehicles v ON f.vehicle_id = v.id
                    WHERE f.battery_start_v > 0 AND f.owner_id = ?
                    ORDER BY v.name, f.date ASC
                """, (owner_id,)).fetchall()
    return [dict(r) for r in rows]


def assign_vehicle_to_flight(filename: str, vehicle_id: int | None, owner_id: int | None = None,
                             is_admin: bool = False) -> bool:
    with _get_conn() as conn:
        if is_admin or owner_id is None:
            cur = conn.execute("UPDATE flights SET vehicle_id = ? WHERE filename = ?",
                               (vehicle_id, filename))
        else:
            cur = conn.execute("UPDATE flights SET vehicle_id = ? WHERE filename = ? AND owner_id = ?",
                               (vehicle_id, filename, owner_id))
        return cur.rowcount > 0


def set_flight_track_source(filename: str, source: str, owner_id: int | None = None,
                            is_admin: bool = False) -> bool:
    with _get_conn() as conn:
        if is_admin or owner_id is None:
            cur = conn.execute("UPDATE flights SET track_source = ? WHERE filename = ?",
                               (source, filename))
        else:
            cur = conn.execute("UPDATE flights SET track_source = ? WHERE filename = ? AND owner_id = ?",
                               (source, filename, owner_id))
        return cur.rowcount > 0


def get_flight_tags(filename: str, owner_id: int | None = None, is_admin: bool = False) -> list[str]:
    with _get_conn() as conn:
        if is_admin or owner_id is None:
            row = conn.execute("SELECT tags FROM flights WHERE filename = ?", (filename,)).fetchone()
        else:
            row = conn.execute("SELECT tags FROM flights WHERE filename = ? AND owner_id = ?",
                               (filename, owner_id)).fetchone()
        if row and row[0]:
            return json.loads(row[0])
        return []


def set_flight_tags(filename: str, tags: list[str], owner_id: int | None = None,
                    is_admin: bool = False) -> bool:
    with _get_conn() as conn:
        if is_admin or owner_id is None:
            cur = conn.execute("UPDATE flights SET tags = ? WHERE filename = ?",
                               (json.dumps(tags), filename))
        else:
            cur = conn.execute("UPDATE flights SET tags = ? WHERE filename = ? AND owner_id = ?",
                               (json.dumps(tags), filename, owner_id))
        return cur.rowcount > 0


def _haversine_km(lat1, lon1, lat2, lon2):
    import math
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def recalculate_home_distances():
    import math as _m
    with _get_conn() as conn:
        rows = conn.execute("SELECT filename, coordinates FROM flights").fetchall()
    updated = 0
    for row in rows:
        fn, coords_json = row
        coords = json.loads(coords_json) if coords_json else []
        if len(coords) < 2:
            continue

        # home distance
        home_lat, home_lon = 0.0, 0.0
        for c in coords:
            lat, lon = c[0], c[1]
            if abs(lat) > 0.001 and abs(lon) > 0.001:
                home_lat, home_lon = lat, lon
                break
        if home_lat == 0.0 and home_lon == 0.0:
            continue
        home_dists = [_haversine_km(home_lat, home_lon, c[0], c[1]) for c in coords if abs(c[0]) > 0.001 or abs(c[1]) > 0.001]
        new_home_dist = round(max(home_dists), 3) if home_dists else 0

        alts = [c[2] for c in coords]
        total_dist_km = 0.0
        for i in range(1, len(coords)):
            total_dist_km += _haversine_km(coords[i-1][0], coords[i-1][1], coords[i][0], coords[i][1])

        # glide ratio
        alt_loss = max(alts) - min(alts)
        new_glide = round(total_dist_km * 1000 / alt_loss, 2) if alt_loss > 0 else 0

        # efficiency (km per 1000 mAh)
        capas = [c[30] for c in coords if len(c) > 30]
        consumed_mah = (capas[-1] - capas[0]) if capas else 0
        new_efficiency = round(total_dist_km / consumed_mah * 1000, 2) if consumed_mah > 0 else 0

        # vibration score
        new_vibration = 0.0
        if len(coords) >= 10:
            pitch_vals = [c[7] for c in coords if len(c) > 7]
            roll_vals = [c[8] for c in coords if len(c) > 8]
            if len(pitch_vals) >= 10:
                window = max(10, len(pitch_vals) // 20)
                pitch_var = sum((pitch_vals[i] - sum(pitch_vals[i:i+window]) / window) ** 2
                                for i in range(len(pitch_vals) - window)) / max(1, len(pitch_vals) - window)
                roll_var = sum((roll_vals[i] - sum(roll_vals[i:i+window]) / window) ** 2
                               for i in range(len(roll_vals) - window)) / max(1, len(roll_vals) - window)
                new_vibration = round(_m.sqrt(pitch_var + roll_var), 4)

        with _get_conn() as conn:
            conn.execute("""UPDATE flights SET
                home_distance_km = ?, glide_ratio = ?,
                efficiency_km_per_mah = ?, vibration_score = ?
                WHERE filename = ?""",
                (new_home_dist, new_glide, new_efficiency, new_vibration, fn))
        updated += 1
    return updated


def get_all_tags(owner_id: int | None = None, is_admin: bool = False) -> list[str]:
    with _get_conn() as conn:
        if is_admin or owner_id is None:
            rows = conn.execute("SELECT tags FROM flights").fetchall()
        else:
            rows = conn.execute("SELECT tags FROM flights WHERE owner_id = ?", (owner_id,)).fetchall()
    all_tags = set()
    for r in rows:
        if r[0]:
            all_tags.update(json.loads(r[0]))
    return sorted(all_tags)


# --- User auth CRUD ---

def _hash_password(password: str, salt: str = None) -> tuple[str, str]:
    if salt is None:
        salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000)
    return key.hex(), salt


def _verify_password(password: str, stored_hash: str, salt: str) -> bool:
    key, _ = _hash_password(password, salt)
    return key == stored_hash


def create_user(username: str, password: str, role: str = "viewer",
                status: str = "active", email: str | None = None,
                privacy_accepted_at: str | None = None) -> dict | None:
    hashed, salt = _hash_password(password)
    user_id = None
    with _get_conn() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, salt, role, status, "
                "email, privacy_accepted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (username, hashed, salt, role, status, email, privacy_accepted_at),
            )
            user_id = cur.lastrowid
        except sqlite3.IntegrityError:
            return None
    return get_user_by_id(user_id) if user_id is not None else None


def _user_row_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    try:
        data["preferences"] = json.loads(data.get("preferences") or "{}")
    except (ValueError, TypeError):
        data["preferences"] = {}
    return data


def get_user_by_id(user_id: int) -> dict | None:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT id, username, role, status, created_at, preferences, email "
            "FROM users WHERE id = ?", (user_id,)).fetchone()
    return _user_row_dict(row) if row else None


def get_user(username: str) -> dict | None:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT id, username, role, status, created_at, preferences, "
            "password_hash, salt FROM users WHERE username = ?",
            (username,)).fetchone()
    return _user_row_dict(row) if row else None


def get_user_by_email(email: str) -> dict | None:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT id, username, status, email FROM users WHERE email = ?",
            (email,)).fetchone()
    return dict(row) if row else None


def get_all_users() -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT id, username, role, status, email, created_at "
            "FROM users ORDER BY created_at ASC").fetchall()
    return [dict(r) for r in rows]


def update_user(user_id: int, username: str = None, role: str = None,
                status: str = None, email: str | None = None) -> dict | None:
    with _get_conn() as conn:
        existing = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not existing:
            return None
        new_username = username if username is not None else existing["username"]
        new_role = role if role is not None else existing["role"]
        new_status = status if status is not None else existing["status"]
        new_email = email if email is not None else existing["email"]
        if new_role not in ("admin", "viewer"):
            return None
        if new_status not in ("active", "pending", "disabled"):
            return None
        try:
            conn.execute(
                "UPDATE users SET username = ?, role = ?, status = ?, email = ? WHERE id = ?",
                (new_username, new_role, new_status, new_email, user_id),
            )
        except sqlite3.IntegrityError:
            return None
    return get_user_by_id(user_id)


def set_user_status(user_id: int, status: str) -> bool:
    if status not in ("active", "pending", "disabled"):
        return False
    with _get_conn() as conn:
        cur = conn.execute("UPDATE users SET status = ? WHERE id = ?", (status, user_id))
        return cur.rowcount > 0


def change_password(user_id: int, new_password: str) -> bool:
    hashed, salt = _hash_password(new_password)
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE users SET password_hash = ?, salt = ? WHERE id = ?",
            (hashed, salt, user_id),
        )
        return cur.rowcount > 0


def create_reset_token(user_id: int, token_hash: str, expires_at: str) -> bool:
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE users SET reset_token_hash = ?, reset_expires_at = ? WHERE id = ?",
            (token_hash, expires_at, user_id),
        )
        return cur.rowcount > 0


def get_reset_token_user(token_hash: str) -> dict | None:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT id, username FROM users "
            "WHERE reset_token_hash = ? AND reset_expires_at IS NOT NULL "
            "AND reset_expires_at > ? AND status = 'active'",
            (token_hash, datetime.now(timezone.utc).isoformat())).fetchone()
    return dict(row) if row else None


def clear_reset_token(user_id: int) -> None:
    with _get_conn() as conn:
        conn.execute(
            "UPDATE users SET reset_token_hash = NULL, reset_expires_at = NULL "
            "WHERE id = ?", (user_id,))


def create_confirm_token(user_id: int, token_hash: str, expires_at: str) -> bool:
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE users SET confirm_token_hash = ?, confirm_expires_at = ? WHERE id = ?",
            (token_hash, expires_at, user_id),
        )
        return cur.rowcount > 0


def get_confirm_token_user(token_hash: str) -> dict | None:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT id, username FROM users "
            "WHERE confirm_token_hash = ? AND confirm_expires_at IS NOT NULL "
            "AND confirm_expires_at > ? AND status = 'pending'",
            (token_hash, datetime.now(timezone.utc).isoformat())).fetchone()
    return dict(row) if row else None


def clear_confirm_token(user_id: int) -> None:
    with _get_conn() as conn:
        conn.execute(
            "UPDATE users SET confirm_token_hash = NULL, confirm_expires_at = NULL "
            "WHERE id = ?", (user_id,))


def activate_user(user_id: int) -> bool:
    with _get_conn() as conn:
        cur = conn.execute("UPDATE users SET status = 'active' WHERE id = ?", (user_id,))
        return cur.rowcount > 0


def set_user_preferences(user_id: int, preferences: dict) -> bool:
    with _get_conn() as conn:
        cur = conn.execute("UPDATE users SET preferences = ? WHERE id = ?",
                           (json.dumps(preferences), user_id))
        return cur.rowcount > 0


def delete_user(user_id: int) -> bool:
    with _get_conn() as conn:
        cur = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        return cur.rowcount > 0


def count_user_data(user_id: int) -> dict:
    with _get_conn() as conn:
        flights = conn.execute("SELECT COUNT(*) FROM flights WHERE owner_id = ?", (user_id,)).fetchone()[0]
        vehicles = conn.execute("SELECT COUNT(*) FROM vehicles WHERE owner_id = ?", (user_id,)).fetchone()[0]
        messages = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE sender_id = ? OR recipient_id = ?",
            (user_id, user_id)).fetchone()[0]
        photos = conn.execute(
            "SELECT COUNT(*) FROM flight_photos WHERE owner_id = ?", (user_id,)).fetchone()[0]
    return {"flights": flights, "vehicles": vehicles, "messages": messages, "photos": photos}


def delete_user_cascade(user_id: int) -> dict:
    """Delete a user plus all owned flights and vehicles (DB rows only).

    Returns the list of owned flight filenames (their CSV files must be
    removed by the caller), owned vehicle ids (for photo cleanup) and owned
    flight-photo stored_names (for file cleanup)."""
    with _get_conn() as conn:
        flight_files = [r[0] for r in conn.execute(
            "SELECT filename FROM flights WHERE owner_id = ?", (user_id,)).fetchall()]
        vehicle_ids = [r[0] for r in conn.execute(
            "SELECT id FROM vehicles WHERE owner_id = ?", (user_id,)).fetchall()]
        photo_files = [r[0] for r in conn.execute(
            "SELECT stored_name FROM flight_photos WHERE owner_id = ?",
            (user_id,)).fetchall()]
        conn.execute("DELETE FROM flight_photos WHERE owner_id = ?", (user_id,))
        conn.execute("DELETE FROM flights WHERE owner_id = ?", (user_id,))
        conn.execute("DELETE FROM vehicles WHERE owner_id = ?", (user_id,))
        # Remove messages and clean up conversation still linking the user.
        conv_ids = [r[0] for r in conn.execute(
            "SELECT id FROM conversations WHERE user_a = ? OR user_b = ?",
            (user_id, user_id)).fetchall()]
        for cid in conv_ids:
            conn.execute("DELETE FROM messages WHERE conversation_id = ?", (cid,))
            conn.execute(
                "DELETE FROM conversations WHERE id = ? AND NOT EXISTS "
                "(SELECT 1 FROM messages WHERE conversation_id = ?)",
                (cid, cid))
        cur = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return {"flights": flight_files, "vehicles": vehicle_ids,
            "photos": photo_files, "user_deleted": cur.rowcount > 0}


def backup_database(dest_dir: Path) -> Path | None:
    """Snapshot the SQLite database to dest_dir/flights-<timestamp>.db.

    Uses the online backup API so the snapshot is consistent even while the
    app is running. Returns the backup path or None on failure."""
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"flights-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
        src = _get_conn()
        try:
            dst = sqlite3.connect(str(dest))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        return dest
    except Exception:
        return None


def verify_user(username: str, password: str) -> dict | None:
    user = get_user(username)
    if not user:
        return None
    if not _verify_password(password, user["password_hash"], user["salt"]):
        return None
    return get_user_by_id(user["id"])


# --- Audit log (F4) ---


def log_audit(user_id: int | None, username: str | None, action: str,
              detail: str | None = None, ip: str | None = None) -> None:
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO audit_log (user_id, username, action, detail, ip) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, username, action, detail, ip))


def get_audit_log(limit: int = 200, username: str | None = None) -> list[dict]:
    with _get_conn() as conn:
        if username:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE username = ? "
                "ORDER BY ts DESC, id DESC LIMIT ?", (username, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM audit_log ORDER BY ts DESC, id DESC LIMIT ?",
                (limit,)).fetchall()
    return [dict(r) for r in rows]


# --- Messaging (F5) ---


def _conversation_id(user_a_id: int, user_b_id: int) -> int | None:
    """Id of the existing conversation between two users, or None."""
    lo, hi = sorted((int(user_a_id), int(user_b_id)))
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM conversations WHERE user_a = ? AND user_b = ?",
            (lo, hi)).fetchone()
    return row["id"] if row else None


def get_or_create_conversation(user_a_id: int, user_b_id: int) -> int:
    existing = _conversation_id(user_a_id, user_b_id)
    if existing is not None:
        return existing
    lo, hi = sorted((int(user_a_id), int(user_b_id)))
    with _get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO conversations (user_a, user_b) VALUES (?, ?)", (lo, hi))
        return int(cur.lastrowid)


def get_conversation_by_pair(user_a_id: int, user_b_id: int) -> dict | None:
    a, b = sorted((int(user_a_id), int(user_b_id)))
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM conversations WHERE user_a = ? AND user_b = ?", (a, b)).fetchone()
    return dict(row) if row else None


def send_message(sender_id: int, recipient_id: int, body: str,
                 flight_file: str | None = None) -> dict | None:
    if int(sender_id) == int(recipient_id) or not body.strip():
        return None
    conv_id = get_or_create_conversation(sender_id, recipient_id)
    with _get_conn() as conn:
        # Un-archive both participants when a new message arrives.
        conn.execute("""
            UPDATE conversations SET archived_by_a = 0, archived_by_b = 0
            WHERE id = ?
        """, (conv_id,))
        cur = conn.execute(
            "INSERT INTO messages (conversation_id, sender_id, recipient_id, "
            "body, flight_file) VALUES (?, ?, ?, ?, ?)",
            (conv_id, int(sender_id), int(recipient_id), body.strip(), flight_file),
        )
        msg_id = int(cur.lastrowid)
    return get_message(msg_id)


def get_message(message_id: int) -> dict | None:
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    return dict(row) if row else None


def get_conversations_for(user_id: int) -> list[dict]:
    """Conversations the user participates in (not archived for them), each
    with the other participant, last message and unread count."""
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.user_a, c.user_b, c.archived_by_a, c.archived_by_b,
                   ua.username AS user_a_name, ub.username AS user_b_name
            FROM conversations c
            JOIN users ua ON ua.id = c.user_a
            JOIN users ub ON ub.id = c.user_b
            WHERE (c.user_a = ? AND c.archived_by_a = 0)
               OR (c.user_b = ? AND c.archived_by_b = 0)
            ORDER BY COALESCE(
                (SELECT MAX(m.created_at) FROM messages m WHERE m.conversation_id = c.id),
                '') DESC
            """, (user_id, user_id)).fetchall()
    result = []
    for row in rows:
        other_id = row["user_b"] if row["user_a"] == user_id else row["user_a"]
        other_name = row["user_b_name"] if row["user_a"] == user_id else row["user_a_name"]
        last = conn.execute(
            "SELECT body, created_at, sender_id FROM messages "
            "WHERE conversation_id = ? AND deleted_by_sender = 0 "
            "AND deleted_by_recipient = 0 "
            "ORDER BY id DESC LIMIT 1", (row["id"],)).fetchone()
        unread = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id = ? "
            "AND recipient_id = ? AND read_at IS NULL "
            "AND deleted_by_recipient = 0", (row["id"], user_id)).fetchone()[0]
        result.append({
            "conversation_id": row["id"],
            "other_id": other_id,
            "other_username": other_name,
            "last_message": dict(last) if last else None,
            "unread": unread,
        })
    return result


def _thread_rows(conn: sqlite3.Connection, user_id: int, other_id: int,
                 limit: int | None) -> list[dict]:
    conv = get_conversation_by_pair(user_id, other_id)
    if not conv:
        return []
    q = (
        "SELECT * FROM messages WHERE conversation_id = ? "
        "AND NOT (sender_id = ? AND deleted_by_sender = 1) "
        "AND NOT (recipient_id = ? AND deleted_by_recipient = 1) "
        "ORDER BY id DESC"
    )
    args: list = [conv["id"], user_id, user_id]
    if limit:
        q += " LIMIT ?"
        args.append(int(limit))
    rows = conn.execute(q, args).fetchall()
    return [dict(r) for r in rows]


def get_thread(user_id: int, other_id: int, limit: int | None = None) -> list[dict]:
    """Messages exchanged between the two users, excluding ones deleted by the
    requesting user. Isolation: caller must be one of the two participants."""
    with _get_conn() as conn:
        msgs = _thread_rows(conn, int(user_id), int(other_id), limit)
    msgs.sort(key=lambda m: m["id"])
    return msgs


def mark_thread_read(user_id: int, other_id: int) -> int:
    """Mark all incoming unread messages in this conversation as read."""
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE messages SET read_at = datetime('now') "
            "WHERE conversation_id = ? AND recipient_id = ? AND read_at IS NULL",
            (get_or_create_conversation(user_id, other_id), user_id))
    return cur.rowcount


def unread_message_count(user_id: int) -> int:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE recipient_id = ? "
            "AND read_at IS NULL AND deleted_by_recipient = 0", (user_id,)).fetchone()
    return int(row[0])


def delete_conversation_for(user_id: int, other_id: int) -> bool:
    """Soft-delete the conversation from the requesting user's perspective.
    When both sides have deleted every message, the conversation is purged."""
    conv = get_conversation_by_pair(user_id, other_id)
    if not conv:
        return False
    with _get_conn() as conn:
        conn.execute(
            "UPDATE messages SET deleted_by_sender = 1 "
            "WHERE conversation_id = ? AND sender_id = ?", (conv["id"], user_id))
        conn.execute(
            "UPDATE messages SET deleted_by_recipient = 1 "
            "WHERE conversation_id = ? AND recipient_id = ?", (conv["id"], user_id))
        # Purge if the other side has deleted everything too.
        remaining = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id = ? AND "
            "(deleted_by_sender = 0 OR deleted_by_recipient = 0)",
            (conv["id"],)).fetchone()[0]
        if remaining == 0:
            conn.execute("DELETE FROM conversations WHERE id = ?", (conv["id"],))
            return True
        # Otherwise archive the whole conversation for this user.
        col = "archived_by_a" if conv["user_a"] == user_id else "archived_by_b"
        conn.execute(f"UPDATE conversations SET {col} = 1 WHERE id = ?", (conv["id"],))
        return True


def get_all_conversations() -> list[dict]:
    """Admin view of every conversation with participant usernames (F5 #27)."""
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT c.*, ua.username AS user_a_name, ub.username AS user_b_name,
                   (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS msg_count,
                   (SELECT MAX(m.created_at) FROM messages m WHERE m.conversation_id = c.id) AS last_ts
            FROM conversations c
            JOIN users ua ON ua.id = c.user_a
            JOIN users ub ON ub.id = c.user_b
            ORDER BY last_ts DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def delete_message_admin(message_id: int) -> bool:
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE messages SET deleted_by_sender = 1, deleted_by_recipient = 1 "
            "WHERE id = ? AND NOT (deleted_by_sender = 1 AND deleted_by_recipient = 1)",
            (message_id,))
        return cur.rowcount > 0


# --- Flight photos (F6) ---


def add_flight_photo(flight_filename: str, owner_id: int, stored_name: str,
                     original_name: str | None = None,
                     captured_at: str | None = None) -> dict | None:
    with _get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO flight_photos (flight_filename, owner_id, stored_name, "
            "original_name, captured_at) VALUES (?, ?, ?, ?, ?)",
            (flight_filename, owner_id, stored_name, original_name, captured_at))
        pid = int(cur.lastrowid)
    return get_flight_photo(pid)


def get_flight_photo(photo_id: int) -> dict | None:
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM flight_photos WHERE id = ?", (photo_id,)).fetchone()
    return dict(row) if row else None


def get_flight_photos(flight_filename: str) -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM flight_photos WHERE flight_filename = ? "
            "ORDER BY is_cover DESC, id ASC", (flight_filename,)).fetchall()
    return [dict(r) for r in rows]


def get_cover_photo(flight_filename: str) -> dict | None:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM flight_photos WHERE flight_filename = ? AND is_cover = 1 "
            "ORDER BY id ASC LIMIT 1", (flight_filename,)).fetchone()
    return dict(row) if row else None


def cover_map(filenames: list[str]) -> dict[str, int | None]:
    """{flight_filename: cover_photo_id} for the given files (batch, no N+1)."""
    if not filenames:
        return {}
    marks = ",".join("?" for _ in filenames)
    with _get_conn() as conn:
        rows = conn.execute(
            f"SELECT flight_filename, id FROM flight_photos "
            f"WHERE flight_filename IN ({marks}) AND is_cover = 1",
            filenames).fetchall()
    return {r["flight_filename"]: r["id"] for r in rows}


def delete_flight_photo(photo_id: int, owner_id: int, is_admin: bool = False) -> dict | None:
    """Remove a photo (DB row). Returns the photo including stored_name so the
    caller can delete the file. Enforces ownership unless admin."""
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM flight_photos WHERE id = ?", (photo_id,)).fetchone()
        if not row:
            return None
        flight = conn.execute(
            "SELECT owner_id FROM flights WHERE filename = ?", (row["flight_filename"],)).fetchone()
        if not flight:
            return None
        if not is_admin and flight["owner_id"] != owner_id:
            return None
        conn.execute("DELETE FROM flight_photos WHERE id = ?", (photo_id,))
        return dict(row)


def set_flight_cover(photo_id: int, flight_filename: str, owner_id: int,
                     is_admin: bool = False) -> bool:
    """Mark a photo as the flight cover. Enforces ownership unless admin."""
    with _get_conn() as conn:
        photo = conn.execute("SELECT * FROM flight_photos WHERE id = ?",
                             (photo_id,)).fetchone()
        flight = conn.execute("SELECT owner_id FROM flights WHERE filename = ?",
                              (flight_filename,)).fetchone()
        if not photo or not flight or photo["flight_filename"] != flight_filename:
            return False
        if not is_admin and flight["owner_id"] != owner_id:
            return False
        conn.execute("UPDATE flight_photos SET is_cover = 0 "
                     "WHERE flight_filename = ?", (flight_filename,))
        conn.execute("UPDATE flight_photos SET is_cover = 1 WHERE id = ?", (photo_id,))
        return True


def photo_files_for_flights(flight_filenames: list[str]) -> list[str]:
    """stored_names for all photos belonging to the given flights."""
    if not flight_filenames:
        return []
    marks = ",".join("?" for _ in flight_filenames)
    with _get_conn() as conn:
        rows = conn.execute(
            f"SELECT stored_name FROM flight_photos WHERE flight_filename IN ({marks})",
            flight_filenames).fetchall()
    return [r["stored_name"] for r in rows]


def delete_photos_for_flights(flight_filenames: list[str]) -> list[str]:
    """Delete photo rows for the flights and return their stored names."""
    if not flight_filenames:
        return []
    marks = ",".join("?" for _ in flight_filenames)
    with _get_conn() as conn:
        rows = conn.execute(
            f"SELECT stored_name FROM flight_photos WHERE flight_filename IN ({marks})",
            flight_filenames).fetchall()
        names = [r["stored_name"] for r in rows]
        conn.execute(f"DELETE FROM flight_photos WHERE flight_filename IN ({marks})",
                     flight_filenames)
    return names


init_db()
