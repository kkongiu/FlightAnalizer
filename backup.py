"""Automatic backups of the Pocket Log Analyzer data.

What is backed up, in a single timestamped tar.gz archive:
  - flights.db      consistent SQLite snapshot (online backup API, safe at runtime)
  - logs/*.csv      every flight CSV in the log directory
  - vehicle_photos/ every vehicle photo in the data directory
  - manifest.json   creation time and the exact file list

Old archives are pruned by retention days (default 30, configurable via
BACKUP_RETENTION_DAYS or the --retention flag).

CLI:
    python backup.py                  # run a backup now (into data/backups)
    python backup.py list             # list existing backups
    python backup.py restore FILE     # restore a backup archive
"""
import argparse
import io
import json
import logging
import os
import sqlite3
import tarfile
from datetime import datetime, timedelta
from pathlib import Path

import database

logger = logging.getLogger("backup")

DEFAULT_RETENTION_DAYS = int(os.environ.get("BACKUP_RETENTION_DAYS", "30"))


def backups_dir() -> Path:
    return database.DATA_DIR / "backups"


def default_log_dir() -> Path:
    env = os.environ.get("POCKET_LOG_DIR")
    if env:
        return Path(env)
    return Path(__file__).parent


def _db_snapshot(dest_dir: Path) -> Path:
    """Snapshot the live DB to dest_dir using the online backup API."""
    dest = dest_dir / f"flights-snapshot-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    src = database._get_conn()
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return dest


def run_backup(dest_dir: Path | None = None, log_dir: Path | None = None,
               retention_days: int | None = None) -> Path:
    """Create a full backup archive and prune old ones. Returns the archive path."""
    dest_dir = Path(dest_dir) if dest_dir else backups_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    log_dir = Path(log_dir) if log_dir else default_log_dir()
    ts = datetime.now()
    archive = dest_dir / f"backup-{ts.strftime('%Y%m%d-%H%M%S')}.tar.gz"

    manifest = {
        "app": "pocket-log-analyzer",
        "created_at": ts.isoformat(),
        "files": {"flights_db": True, "csv": [], "vehicle_photos": []},
    }

    db_snapshot = _db_snapshot(dest_dir)
    try:
        with tarfile.open(str(archive), "w:gz") as tar:
            tar.add(str(db_snapshot), arcname="flights.db")
            for f in sorted(log_dir.glob("*.csv")):
                tar.add(str(f), arcname=f"logs/{f.name}")
                manifest["files"]["csv"].append(f.name)
            photo_dir = database.DATA_DIR / "vehicle_photos"
            if photo_dir.exists():
                for f in sorted(photo_dir.iterdir()):
                    if f.is_file():
                        tar.add(str(f), arcname=f"vehicle_photos/{f.name}")
                        manifest["files"]["vehicle_photos"].append(f.name)
            data = json.dumps(manifest, indent=2).encode("utf-8")
            info = tarfile.TarInfo("manifest.json")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    finally:
        db_snapshot.unlink(missing_ok=True)

    pruned = prune_backups(dest_dir, retention_days)
    logger.info("backup written to %s (%d CSV, %d photos, pruned %d)",
                archive.name, len(manifest["files"]["csv"]),
                len(manifest["files"]["vehicle_photos"]), len(pruned))
    return archive


def prune_backups(dest_dir: Path | None = None, retention_days: int | None = None) -> list[Path]:
    retention_days = retention_days if retention_days is not None else DEFAULT_RETENTION_DAYS
    dest_dir = Path(dest_dir) if dest_dir else backups_dir()
    cutoff = datetime.now() - timedelta(days=max(0, retention_days))
    removed = []
    for archive in sorted(dest_dir.glob("backup-*.tar.gz")):
        try:
            stem = archive.name[len("backup-"):-len(".tar.gz")]
            ts = datetime.strptime(stem, "%Y%m%d-%H%M%S")
        except ValueError:
            continue
        if ts < cutoff:
            archive.unlink(missing_ok=True)
            removed.append(archive)
    return removed


def list_backups(dest_dir: Path | None = None) -> list[dict]:
    dest_dir = Path(dest_dir) if dest_dir else backups_dir()
    if not dest_dir.exists():
        return []
    out = []
    for p in sorted(dest_dir.glob("backup-*.tar.gz"), reverse=True):
        st = p.stat()
        out.append({
            "name": p.name,
            "size_bytes": st.st_size,
            "modified": datetime.fromtimestamp(st.st_mtime).isoformat(),
        })
    return out


def _assert_safe_member(member: tarfile.TarInfo):
    name = member.name
    if name.startswith("/") or ".." in name.split("/") or ":" in name:
        raise ValueError(f"unsafe archive member: {name}")


def restore_backup(archive: Path, dest_log_dir: Path | None = None,
                   dest_data_dir: Path | None = None) -> dict:
    """Restore a backup archive into the data and log directories.

    Overwrites flights.db, adds any missing CSV logs and restores vehicle
    photos. Returns a summary dict. The DB is restored through a fresh
    connection so a running server keeps a consistent file."""
    archive = Path(archive)
    if not archive.exists():
        raise FileNotFoundError(f"backup archive not found: {archive}")
    dest_log_dir = Path(dest_log_dir) if dest_log_dir else default_log_dir()
    dest_data_dir = Path(dest_data_dir) if dest_data_dir else database.DATA_DIR
    restored = {"flights_db": False, "csv": [], "vehicle_photos": []}

    with tarfile.open(str(archive), "r:gz") as tar:
        for member in tar.getmembers():
            _assert_safe_member(member)
        for member in tar.getmembers():
            f = tar.extractfile(member)
            if f is None:
                continue
            name = member.name
            if name == "flights.db":
                dest_data_dir.mkdir(parents=True, exist_ok=True)
                db_path = dest_data_dir / "flights.db"
                tmp_path = db_path.with_suffix(".db.tmp")
                tmp_path.write_bytes(f.read())
                tmp_path.replace(db_path)
                restored["flights_db"] = True
            elif name.startswith("logs/"):
                dest_log_dir.mkdir(parents=True, exist_ok=True)
                out = dest_log_dir / Path(name).name
                out.write_bytes(f.read())
                restored["csv"].append(out.name)
            elif name.startswith("vehicle_photos/"):
                photo_dir = dest_data_dir / "vehicle_photos"
                photo_dir.mkdir(parents=True, exist_ok=True)
                out = photo_dir / Path(name).name
                out.write_bytes(f.read())
                restored["vehicle_photos"].append(out.name)
    logger.info("restored %s: db=%s csv=%d photos=%d", archive.name,
                restored["flights_db"], len(restored["csv"]),
                len(restored["vehicle_photos"]))
    return restored


def _cli(argv=None):
    parser = argparse.ArgumentParser(prog="backup", description="Pocket Log Analyzer backup tool")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_backup = sub.add_parser("backup", help="run a backup now")
    p_backup.add_argument("--dest", type=Path, help="backup directory (default: data/backups)")
    p_backup.add_argument("--log-dir", type=Path, help="CSV log directory")
    p_backup.add_argument("--retention", type=int, help="retention in days")

    p_list = sub.add_parser("list", help="list existing backups")
    p_list.add_argument("--dest", type=Path, help="backup directory (default: data/backups)")

    p_restore = sub.add_parser("restore", help="restore a backup archive")
    p_restore.add_argument("archive", type=Path, help="backup-*.tar.gz file")
    p_restore.add_argument("--log-dir", type=Path, help="CSV log directory")
    p_restore.add_argument("--data-dir", type=Path, help="data directory (DB + photos)")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s [%(name)s] %(message)s")

    if args.cmd == "backup":
        archive = run_backup(args.dest, args.log_dir, args.retention)
        print(archive)
    elif args.cmd == "list":
        for b in list_backups(args.dest):
            print(f"{b['name']}  {b['size_bytes']} bytes  {b['modified']}")
    elif args.cmd == "restore":
        result = restore_backup(args.archive, args.log_dir, args.data_dir)
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    _cli()
