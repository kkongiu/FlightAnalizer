import pytest

from models import TelemetryPoint
import database


def make_point(**overrides):
    """Build a fictional TelemetryPoint (coordinates in the mid-Atlantic,
    far from any real flight data)."""
    defaults = dict(
        timestamp=0.0,
        lat=12.0,
        lon=-45.0,
        alt=2.0,
        gspd=0.0,
        vspd=0.0,
        heading=0.0,
        rssi_1=-70,
        rssi_2=-75,
        rqly=100,
        rsnr=12,
        trss=-70,
        tqly=100,
        tsnr=11,
        rxbt=16.8,
        curr=0.0,
        capa=0.0,
        bat_pct=100.0,
        pitch=0.0,
        roll=0.0,
        yaw=0.0,
        rud=0,
        ele=0,
        thr=0,
        ail=0,
        flight_mode="OK",
        sats=12,
        txbat=8.4,
        sa=0,
        sb=0,
        sc=0,
        sd=0,
        se=0,
        lsw="",
        p1=0,
    )
    defaults.update(overrides)
    return TelemetryPoint(**defaults)


def make_flight(n, dt=0.5, **point_kwargs):
    """A series of n fictional points at dt-second intervals, on a level
    eastbound cruise: alt 10 m, gspd 36 km/h, zero attitude."""
    pts = []
    for i in range(n):
        pts.append(make_point(
            timestamp=i * dt,
            alt=10.0,
            gspd=36.0,
            lon=-45.0 + i * 0.0001,
            **point_kwargs,
        ))
    return pts


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """Point the database at a throwaway SQLite file so tests never touch
    the real data/flights.db."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "flights.db")
    monkeypatch.setattr(database, "DATA_DIR", tmp_path)
    database.init_db()
    return tmp_path / "flights.db"


@pytest.fixture
def client(monkeypatch, tmp_path):
    """An authenticated-test ready HTTP client against a throwaway database.

    Skips the import-time CSV auto-sync (it would otherwise import the real
    flight CSVs from the repo root into the throwaway DB)."""
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "flights.db")
    monkeypatch.setattr(database, "DATA_DIR", tmp_path)
    database.init_db()
    monkeypatch.setenv("FLIGHT_ANALYZER_SKIP_STARTUP_SYNC", "1")
    import app as app_mod
    app_mod.login_limiter.clear()
    app_mod.password_limiter.clear()
    monkeypatch.setattr(app_mod, "LOG_DIR", tmp_path / "logs")
    (tmp_path / "logs").mkdir(exist_ok=True)
    from fastapi.testclient import TestClient
    with TestClient(app_mod.app, base_url="https://testserver") as c:
        c.app_mod = app_mod
        yield c
