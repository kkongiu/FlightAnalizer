import math
import statistics
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

    # Home point = first GPS position
    home_lat, home_lon = points[0].lat, points[0].lon

    # Max distance from home
    home_dists = [haversine_km(home_lat, home_lon, p.lat, p.lon) for p in points if p.lat != 0 or p.lon != 0]
    home_distance_km = round(max(home_dists), 3) if home_dists else 0

    # Glide ratio: horizontal distance / altitude lost (only when descending)
    alt_loss = max(alts) - min(alts)
    glide_ratio = round(total_dist / alt_loss, 2) if alt_loss > 0 else 0

    # Efficiency: km per 1000 mAh
    consumed_mah = (capas[-1] - capas[0]) if capas else 0
    efficiency_km_per_mah = round(total_dist / consumed_mah * 1000, 2) if consumed_mah > 0 else 0

    # Vibration score: stddev of pitch and roll over a sliding window
    if len(points) >= 10:
        pitch_vals = [p.pitch for p in points]
        roll_vals = [p.roll for p in points]
        window = max(10, len(points) // 20)
        pitch_var = sum((pitch_vals[i] - sum(pitch_vals[i:i+window])/window)**2 for i in range(len(points)-window)) / max(1, len(points)-window)
        roll_var = sum((roll_vals[i] - sum(roll_vals[i:i+window])/window)**2 for i in range(len(points)-window)) / max(1, len(points)-window)
        vibration_score = round(math.sqrt(pitch_var + roll_var), 4)
    else:
        vibration_score = 0

    # Event detection
    events = []
    was_on_ground = True
    prev_mode = points[0].flight_mode
    alt_threshold = 3.0

    for i, p in enumerate(points):
        # Takeoff: ground -> airborne
        if was_on_ground and p.alt > alt_threshold and p.gspd > 2:
            events.append({"type": "takeoff", "ts": p.timestamp, "i": i})
            was_on_ground = False
        # Landing: airborne -> ground
        if not was_on_ground and p.alt < 1.5 and p.gspd < 1:
            events.append({"type": "landing", "ts": p.timestamp, "i": i})
            was_on_ground = True
        # Signal loss: low RSSI + !ERR
        if p.rssi_1 < -90 and p.rssi_1 != 0 and p.flight_mode == "!ERR":
            if not events or events[-1]["type"] != "signal_loss" or p.timestamp - events[-1]["ts"] > 3:
                events.append({"type": "signal_loss", "ts": p.timestamp, "i": i})
        # Flight mode change
        if p.flight_mode != prev_mode:
            events.append({"type": "mode_change", "ts": p.timestamp, "i": i, "mode": p.flight_mode})
            prev_mode = p.flight_mode

    coords = [[p.lat, p.lon, p.alt, p.gspd, p.timestamp, p.rssi_1, p.rxbt,
               p.pitch, p.roll, p.yaw, p.rud, p.ele, p.thr, p.ail,
               p.vspd, p.heading,
               p.sa, p.sb, p.sc, p.sd, p.se, p.lsw, p.p1, p.flight_mode,
               p.rssi_2, p.rsnr, p.trss, p.tqly, p.tsnr, p.curr, p.capa, p.bat_pct, p.txbat] for p in points]

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
        battery_consumed_mah=round(consumed_mah, 0),
        max_current_a=round(max(currs), 1) if currs else 0,
        txbat_v=round(points[0].txbat, 1),
        flight_modes=modes,
        sats_max=max(sats_list) if sats_list else 0,
        home_distance_km=home_distance_km,
        glide_ratio=glide_ratio,
        efficiency_km_per_mah=efficiency_km_per_mah,
        vibration_score=vibration_score,
        events=events,
        coordinates=coords,
    )
