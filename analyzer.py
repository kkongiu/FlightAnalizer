import math
from models import TelemetryPoint, FlightSummary


def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def analyze(filename: str, points: list[TelemetryPoint]) -> FlightSummary:
    if not points:
        return FlightSummary(filename=filename, date="", start_time="",
            duration_s=0, distance_km=0, max_alt_m=0, min_alt_m=0, avg_alt_m=0,
            max_speed_kmh=0, avg_speed_kmh=0, max_vspd_ms=0,
            max_rssi_db=0, min_rssi_db=0, avg_rssi_db=0,
            min_rqly=0, avg_rqly=0,
            battery_start_v=0, battery_end_v=0, battery_min_v=0,
            battery_start_pct=0, battery_end_pct=0, battery_consumed_mah=0,
            max_current_a=0, txbat_v=0, flight_modes={}, sats_max=0)

    from datetime import datetime as dtmod
    start_dt = dtmod.fromtimestamp(points[0].timestamp)
    end_dt = dtmod.fromtimestamp(points[-1].timestamp)

    duration_s = points[-1].timestamp - points[0].timestamp

    total_dist = 0.0
    for i in range(1, len(points)):
        total_dist += haversine_km(points[i-1].lat, points[i-1].lon,
                                    points[i].lat, points[i].lon)

    alts = [p.alt for p in points]
    speeds = [p.gspd for p in points]
    vspds = [abs(p.vspd) for p in points]
    rssis = [p.rssi_1 for p in points if p.rssi_1 != 0]
    rqlys = [p.rqly for p in points]
    rxbt = [p.rxbt for p in points if p.rxbt > 0]
    currs = [p.curr for p in points]
    capas = [p.capa for p in points]
    bat_pcts = [p.bat_pct for p in points if p.bat_pct > 0]
    sats_list = [p.sats for p in points]

    modes = {}
    for p in points:
        m = p.flight_mode
        modes[m] = modes.get(m, 0) + 1

    coords = [[p.lat, p.lon, p.alt, p.gspd, p.timestamp, p.rssi_1, p.rxbt] for p in points]

    return FlightSummary(
        filename=filename,
        date=start_dt.strftime("%Y-%m-%d"),
        start_time=start_dt.strftime("%H:%M:%S"),
        duration_s=duration_s,
        distance_km=round(total_dist, 3),
        max_alt_m=round(max(alts), 1),
        min_alt_m=round(min(alts), 1),
        avg_alt_m=round(sum(alts) / len(alts), 1),
        max_speed_kmh=round(max(speeds), 1),
        avg_speed_kmh=round(sum(speeds) / len(speeds), 1),
        max_vspd_ms=round(max(vspds), 2) if vspds else 0,
        max_rssi_db=max(rssis) if rssis else 0,
        min_rssi_db=min(rssis) if rssis else 0,
        avg_rssi_db=round(sum(rssis) / len(rssis), 1) if rssis else 0,
        min_rqly=min(rqlys) if rqlys else 0,
        avg_rqly=round(sum(rqlys) / len(rqlys), 1) if rqlys else 0,
        battery_start_v=round(rxbt[0], 2) if rxbt else 0,
        battery_end_v=round(rxbt[-1], 2) if rxbt else 0,
        battery_min_v=round(min(rxbt), 2) if rxbt else 0,
        battery_start_pct=round(bat_pcts[0], 1) if bat_pcts else 0,
        battery_end_pct=round(bat_pcts[-1], 1) if bat_pcts else 0,
        battery_consumed_mah=round((capas[-1] - capas[0]) if capas else 0, 0),
        max_current_a=round(max(currs), 1) if currs else 0,
        txbat_v=round(points[0].txbat, 1),
        flight_modes=modes,
        sats_max=max(sats_list) if sats_list else 0,
        coordinates=coords,
    )
