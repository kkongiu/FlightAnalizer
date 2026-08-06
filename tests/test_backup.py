import tarfile

import database
from database import save_flight
from models import FlightSummary
import backup


def _summary(filename="backupme.csv"):
    return FlightSummary(
        filename=filename,
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
        events=[],
        coordinates=[],
    )


def _make_log_dir(tmp_path, name="flight1.csv", content="a,b\n1,2\n"):
    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    (log_dir / name).write_text(content)
    return log_dir


def _make_photo(tmp_path):
    photo_dir = database.DATA_DIR / "vehicle_photos"
    photo_dir.mkdir(parents=True, exist_ok=True)
    (photo_dir / "v1.jpg").write_bytes(b"\xff\xd8fake")


def test_run_backup_creates_archive_with_all_parts(tmp_db, tmp_path):
    save_flight(_summary())
    log_dir = _make_log_dir(tmp_path)
    _make_photo(tmp_path)

    archive = backup.run_backup(dest_dir=tmp_path / "backups", log_dir=log_dir)

    assert archive.exists()
    assert archive.name.startswith("backup-") and archive.name.endswith(".tar.gz")
    with tarfile.open(archive, "r:gz") as tar:
        names = sorted(tar.getnames())
        assert "flights.db" in names
        assert "logs/flight1.csv" in names
        assert "vehicle_photos/v1.jpg" in names
        assert "manifest.json" in names
        manifest = tar.extractfile("manifest.json").read().decode()
        assert "flight1.csv" in manifest
        assert "v1.jpg" in manifest


def test_list_backups(tmp_db, tmp_path):
    log_dir = _make_log_dir(tmp_path)
    archive = backup.run_backup(dest_dir=tmp_path / "backups", log_dir=log_dir)
    listed = backup.list_backups(tmp_path / "backups")
    assert [b["name"] for b in listed] == [archive.name]
    assert listed[0]["size_bytes"] > 0


def test_prune_removes_old_backups(tmp_db, tmp_path):
    dest = tmp_path / "backups"
    dest.mkdir(parents=True, exist_ok=True)
    old = dest / "backup-20200101-000000.tar.gz"
    new = dest / "backup-20300101-000000.tar.gz"
    old.write_bytes(b"x")
    new.write_bytes(b"y")

    removed = backup.prune_backups(dest, retention_days=30)
    assert removed == [old]
    assert old.exists() is False
    assert new.exists() is True


def test_restore_backup_roundtrip(tmp_db, tmp_path):
    save_flight(_summary())
    log_dir = _make_log_dir(tmp_path)
    _make_photo(tmp_path)
    archive = backup.run_backup(dest_dir=tmp_path / "backups", log_dir=log_dir)

    new_data = tmp_path / "restored" / "data"
    new_logs = tmp_path / "restored" / "logs"
    result = backup.restore_backup(archive, new_logs, new_data)

    assert result["flights_db"] is True
    assert result["csv"] == ["flight1.csv"]
    assert result["vehicle_photos"] == ["v1.jpg"]
    assert (new_data / "flights.db").exists()
    assert (new_logs / "flight1.csv").read_text() == "a,b\n1,2\n"
    assert (new_data / "vehicle_photos" / "v1.jpg").exists()

    database.init_db()
    f = database.get_flight("backupme.csv")
    assert f is not None
    assert f["max_g"] == 2.45


def test_restore_rejects_path_traversal(tmp_path):
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as tar:
        data = b"boom"
        info = tarfile.TarInfo("../../evil.txt")
        info.size = len(data)
        tar.addfile(info, __import__("io").BytesIO(data))

    import pytest
    with pytest.raises(ValueError):
        backup.restore_backup(evil, tmp_path / "logs", tmp_path / "data")
