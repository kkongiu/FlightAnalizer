import sqlite3

import database


def _prisma_owner_cols(path):
    with sqlite3.connect(path) as conn:
        flights = [r[1] for r in conn.execute("PRAGMA table_info(flights)")]
        vehicles = [r[1] for r in conn.execute("PRAGMA table_info(vehicles)")]
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    return flights, vehicles, version


def test_fresh_db_is_at_latest_version(tmp_db):
    flights, vehicles, version = _prisma_owner_cols(tmp_db)
    assert version == database.SCHEMA_BASELINE_VERSION + len(database.MIGRATIONS)
    assert "owner_id" in flights
    assert "owner_id" in vehicles


def test_migration_applied_exactly_once(tmp_db):
    flights, _, version = _prisma_owner_cols(tmp_db)
    assert version == 7
    # owner_id appears exactly once per table
    assert flights.count("owner_id") == 1
    database._apply_migrations()
    _, _, version2 = _prisma_owner_cols(tmp_db)
    assert version2 == 7


def test_migration_002_backfills_flights_owner_to_first_admin(monkeypatch, tmp_path):
    db_file = tmp_path / "flights.db"
    monkeypatch.setattr(database, "DB_PATH", db_file)
    monkeypatch.setattr(database, "DATA_DIR", tmp_path)
    database.init_db()
    with sqlite3.connect(db_file) as conn:
        admin_id = conn.execute("SELECT id FROM users WHERE role='admin'").fetchone()[0]
        # Roll the schema back to the pre-multi-user baseline (version 1)
        conn.execute("DROP INDEX IF EXISTS idx_flights_owner")
        conn.execute("DROP INDEX IF EXISTS idx_vehicles_owner")
        conn.execute("ALTER TABLE flights DROP COLUMN owner_id")
        conn.execute("ALTER TABLE vehicles DROP COLUMN owner_id")
        conn.execute("PRAGMA user_version = 1")

    database._apply_migrations()

    with sqlite3.connect(db_file) as conn:
        flights = [r[1] for r in conn.execute("PRAGMA table_info(flights)")]
        vehicles = [r[1] for r in conn.execute("PRAGMA table_info(vehicles)")]
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 7
        assert "owner_id" in flights
        assert "owner_id" in vehicles
        # the single admin is still there (users untouched by migrations)
        assert conn.execute("SELECT COUNT(*) FROM users WHERE id=?", (admin_id,)).fetchone()[0] == 1
