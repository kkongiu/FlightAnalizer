from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TelemetryPoint:
    timestamp: float
    lat: float
    lon: float
    alt: float
    gspd: float
    vspd: float
    heading: float
    rssi_1: int
    rssi_2: int
    rqly: int
    rsnr: int
    trss: int
    tqly: int
    tsnr: int
    rxbt: float
    curr: float
    capa: float
    bat_pct: float
    pitch: float
    roll: float
    yaw: float
    rud: int
    ele: int
    thr: int
    ail: int
    flight_mode: str
    sats: int
    txbat: float
    sa: int = 0
    sb: int = 0
    sc: int = 0
    sd: int = 0
    se: int = 0
    lsw: str = ""
    p1: int = 0


@dataclass
class FlightSummary:
    filename: str
    date: str
    start_time: str
    duration_s: float
    distance_km: float
    max_alt_m: float
    min_alt_m: float
    avg_alt_m: float
    max_speed_kmh: float
    avg_speed_kmh: float
    max_vspd_ms: float
    max_rssi_db: int
    min_rssi_db: int
    avg_rssi_db: float
    min_rqly: int
    avg_rqly: float
    battery_start_v: float
    battery_end_v: float
    battery_min_v: float
    battery_start_pct: float
    battery_end_pct: float
    battery_consumed_mah: float
    max_current_a: float
    txbat_v: float
    flight_modes: dict
    sats_max: int
    home_distance_km: float = 0.0
    glide_ratio: float = 0.0
    efficiency_km_per_mah: float = 0.0
    vibration_score: float = 0.0
    events: list = field(default_factory=list)
    coordinates: list = field(default_factory=list)
    vehicle_id: Optional[int] = None


@dataclass
class Vehicle:
    id: int
    name: str
    vehicle_type: str
    photo: str = ""
    is_default: bool = False
