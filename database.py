import json
import hashlib
import os
import secrets
import sqlite3
from pathlib import Path
from models import FlightSummary, Vehicle

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "flights.db"


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
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
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        # Seed initial admin from env vars if no users exist
        existing = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if existing == 0:
            admin_user = os.environ.get("POCKET_USER", "admin")
            admin_pass = os.environ.get("POCKET_PASS", "admin")
            create_user(admin_user, admin_pass, role="admin")


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


def save_flight(summary: FlightSummary):
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
             coordinates)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                coordinates = excluded.coordinates
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


def update_flight_events(filename: str, events: list):
    with _get_conn() as conn:
        conn.execute("UPDATE flights SET events = ? WHERE filename = ?",
                     (json.dumps(events), filename))


def rename_flight(old_filename: str, new_filename: str) -> bool:
    with _get_conn() as conn:
        cur = conn.execute("UPDATE flights SET filename = ? WHERE filename = ?", (new_filename, old_filename))
        return cur.rowcount > 0


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


# --- Vehicle CRUD ---

def get_vehicles() -> list[Vehicle]:
    with _get_conn() as conn:
        rows = conn.execute("SELECT * FROM vehicles ORDER BY is_default DESC, name ASC").fetchall()
    return [Vehicle(**dict(r)) for r in rows]


def get_vehicle(vehicle_id: int) -> Vehicle | None:
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM vehicles WHERE id = ?", (vehicle_id,)).fetchone()
    return Vehicle(**dict(row)) if row else None


def get_default_vehicle() -> Vehicle | None:
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM vehicles WHERE is_default = 1 LIMIT 1").fetchone()
    return Vehicle(**dict(row)) if row else None


def create_vehicle(name: str, vehicle_type: str = "drone", is_default: bool = False) -> Vehicle:
    with _get_conn() as conn:
        if is_default:
            conn.execute("UPDATE vehicles SET is_default = 0")
        cur = conn.execute(
            "INSERT INTO vehicles (name, vehicle_type, is_default) VALUES (?, ?, ?)",
            (name, vehicle_type, 1 if is_default else 0),
        )
        return get_vehicle(cur.lastrowid)


def update_vehicle(vehicle_id: int, name: str = None, vehicle_type: str = None, is_default: bool = None) -> Vehicle | None:
    with _get_conn() as conn:
        existing = conn.execute("SELECT * FROM vehicles WHERE id = ?", (vehicle_id,)).fetchone()
        if not existing:
            return None
        new_name = name if name is not None else existing["name"]
        new_type = vehicle_type if vehicle_type is not None else existing["vehicle_type"]
        new_default = is_default if is_default is not None else bool(existing["is_default"])
        if new_default:
            conn.execute("UPDATE vehicles SET is_default = 0")
        conn.execute(
            "UPDATE vehicles SET name = ?, vehicle_type = ?, is_default = ? WHERE id = ?",
            (new_name, new_type, 1 if new_default else 0, vehicle_id),
        )
        return get_vehicle(vehicle_id)


def delete_vehicle(vehicle_id: int) -> bool:
    with _get_conn() as conn:
        cur = conn.execute("DELETE FROM vehicles WHERE id = ?", (vehicle_id,))
        if cur.rowcount:
            conn.execute("UPDATE flights SET vehicle_id = NULL WHERE vehicle_id = ?", (vehicle_id,))
        return cur.rowcount > 0


def set_vehicle_photo(vehicle_id: int, photo_path: str):
    with _get_conn() as conn:
        conn.execute("UPDATE vehicles SET photo = ? WHERE id = ?", (photo_path, vehicle_id))


def get_vehicle_stats() -> list[dict]:
    with _get_conn() as conn:
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
    return [dict(r) for r in rows]


def get_battery_health_by_vehicle(vehicle_id: int | None = None) -> list[dict]:
    with _get_conn() as conn:
        if vehicle_id:
            rows = conn.execute("""
                SELECT date, battery_start_v, battery_end_v, battery_min_v, battery_consumed_mah
                FROM flights WHERE vehicle_id = ? AND battery_start_v > 0
                ORDER BY date ASC
            """, (vehicle_id,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT f.date, f.battery_start_v, f.battery_end_v, f.battery_min_v,
                       f.battery_consumed_mah, v.id AS vehicle_id, v.name AS vehicle_name
                FROM flights f LEFT JOIN vehicles v ON f.vehicle_id = v.id
                WHERE f.battery_start_v > 0
                ORDER BY v.name, f.date ASC
            """).fetchall()
    return [dict(r) for r in rows]


def assign_vehicle_to_flight(filename: str, vehicle_id: int | None):
    with _get_conn() as conn:
        conn.execute("UPDATE flights SET vehicle_id = ? WHERE filename = ?", (vehicle_id, filename))


def set_flight_track_source(filename: str, source: str):
    with _get_conn() as conn:
        conn.execute("UPDATE flights SET track_source = ? WHERE filename = ?", (source, filename))


def get_flight_tags(filename: str) -> list[str]:
    with _get_conn() as conn:
        row = conn.execute("SELECT tags FROM flights WHERE filename = ?", (filename,)).fetchone()
        if row and row[0]:
            return json.loads(row[0])
        return []


def set_flight_tags(filename: str, tags: list[str]):
    with _get_conn() as conn:
        conn.execute("UPDATE flights SET tags = ? WHERE filename = ?", (json.dumps(tags), filename))


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


def get_all_tags() -> list[str]:
    with _get_conn() as conn:
        rows = conn.execute("SELECT tags FROM flights").fetchall()
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


def create_user(username: str, password: str, role: str = "viewer") -> dict | None:
    hashed, salt = _hash_password(password)
    with _get_conn() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO users (username, password_hash, salt, role) VALUES (?, ?, ?, ?)",
                (username, hashed, salt, role),
            )
            return get_user_by_id(cur.lastrowid)
        except sqlite3.IntegrityError:
            return None


def get_user_by_id(user_id: int) -> dict | None:
    with _get_conn() as conn:
        row = conn.execute("SELECT id, username, role, created_at FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def get_user(username: str) -> dict | None:
    with _get_conn() as conn:
        row = conn.execute("SELECT id, username, role, created_at, password_hash, salt FROM users WHERE username = ?", (username,)).fetchone()
    return dict(row) if row else None


def get_all_users() -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute("SELECT id, username, role, created_at FROM users ORDER BY created_at ASC").fetchall()
    return [dict(r) for r in rows]


def update_user(user_id: int, username: str = None, role: str = None) -> dict | None:
    with _get_conn() as conn:
        existing = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not existing:
            return None
        new_username = username if username is not None else existing["username"]
        new_role = role if role is not None else existing["role"]
        try:
            conn.execute(
                "UPDATE users SET username = ?, role = ? WHERE id = ?",
                (new_username, new_role, user_id),
            )
        except sqlite3.IntegrityError:
            return None
    return get_user_by_id(user_id)


def change_password(user_id: int, new_password: str) -> bool:
    hashed, salt = _hash_password(new_password)
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE users SET password_hash = ?, salt = ? WHERE id = ?",
            (hashed, salt, user_id),
        )
        return cur.rowcount > 0


def delete_user(user_id: int) -> bool:
    with _get_conn() as conn:
        cur = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        return cur.rowcount > 0


def verify_user(username: str, password: str) -> dict | None:
    user = get_user(username)
    if not user:
        return None
    if _verify_password(password, user["password_hash"], user["salt"]):
        return get_user_by_id(user["id"])
    return None


init_db()
