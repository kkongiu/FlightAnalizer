"""Seed demo data for the 'testviewer' account.

Generates fully invented flights (coordinates, telemetry, events, stats),
vehicles, tags, notes, visibility settings, a group, a public share link with
comments and likes. Safe to re-run: flights are upserted by filename, users
and vehicles are created only if missing.

Usage:
    python scripts/seed_demo.py
"""
import datetime
import json
import math
import random
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import database
from models import FlightSummary

DEMO_USER = "testviewer"
DEMO_PASS = "demo123"
DEMO_EMAIL = "testviewer@example.com"

# Home field (fictional coordinates near Saint-Cirq-Lapopie, Lot, France)
HOME_LAT, HOME_LON = 44.4667, 1.6667


def make_coords(lat0, lon0, n, dt=0.5, speed_kmh=42.0, alt_start=8.0,
                alt_max=180.0, heading0=None, battery=16.4,
                mode="ANGL", with_flight=False):
    """Build a synthetic telemetry coordinate array in the app's format:
    [lat, lon, alt, gspd, ts, rssi1, rxbt, pitch, roll, yaw,
     rud, ele, thr, ail, vspd, heading, sa..se, lsw, p1, mode,
     rssi2, rsnr, trss, tqly, tsnr, curr, capa, bat_pct, txbat, rqly, g]
    """
    rng = random.Random(secrets.token_hex(8))
    base_ts = 1785000000 + rng.randint(0, 20_000_000)
    heading = heading0 if heading0 is not None else rng.uniform(0, 360)
    coords = []
    lat, lon = lat0, lon0
    alt = alt_start
    capa = 0.0
    for i in range(n):
        ts = base_ts + i * dt
        # gentle sinusoidal course changes
        heading += rng.uniform(-4, 4)
        speed = speed_kmh * (1 + 0.15 * math.sin(i / 40.0) + rng.uniform(-0.05, 0.05))
        lat += (speed / 111_320) * math.cos(math.radians(heading)) * dt / 3.6
        lon += (speed / 111_320 / math.cos(math.radians(lat))) * math.sin(math.radians(heading)) * dt / 3.6
        # altitude profile: climb, cruise, descend
        if with_flight:
            if i < n * 0.15:
                alt += 6.0 * dt
            elif i < n * 0.75:
                alt += (alt_max - alt) * 0.02 + rng.uniform(-0.5, 0.5)
            else:
                alt -= 6.0 * dt
        else:
            alt += rng.uniform(-0.5, 0.5)
        alt = max(0.0, alt)
        vspd = (alt - alt_prev) / dt if i else 0.0
        vspd = vspd if i else 0.0
        rssi = rng.randint(-85, -45)
        curr = 6 + 26 * max(0, (speed - 10) / 80) + rng.uniform(0, 2)
        capa += curr * dt / 3600 * 1000
        bat = battery - capa / 3500 * 1.2
        pitch = rng.uniform(-0.15, 0.15)
        roll = rng.uniform(-0.3, 0.3)
        yaw = math.radians(heading)
        coords.append([
            round(lat, 6), round(lon, 6), round(alt, 1), round(speed, 1), round(ts, 2),
            rssi, round(bat, 2), round(pitch, 2), round(roll, 2), round(yaw, 2),
            rng.randint(-100, 100), rng.randint(-100, 100), rng.randint(0, 1024), rng.randint(-100, 100),
            round(vspd, 1), round(heading, 1),
            1, 1, -1, -1, -1, "0x0000000000000000", 1008, mode,
            0, rng.randint(10, 16), -14, 100, 14, round(curr, 1),
            round(capa, 0), round(98.0 - capa / 3500 * 80, 0), 8.2, rng.randint(90, 100),
            round(1 / max(0.3, abs(math.cos(roll))), 2),
        ])
        alt_prev = alt
    return coords


def summary(filename, date, start_time, coords, modes, notes="", tags=None,
            vehicle_id=None, battery=16.4, events=None):
    """Build a FlightSummary dataclass from synthetic coords + computed stats."""
    n = len(coords)
    dt = coords[1][4] - coords[0][4]
    dist = 0.0
    for a, b in zip(coords[:-1], coords[1:]):
        dlat = math.radians(b[0] - a[0])
        dlon = math.radians(b[1] - a[1])
        h = (math.sin(dlat / 2) ** 2
             + math.cos(math.radians(a[0])) * math.cos(math.radians(b[0]))
             * math.sin(dlon / 2) ** 2)
        dist += 6371.0 * 2 * math.asin(math.sqrt(h))
    alts = [c[2] for c in coords]
    spds = [c[3] for c in coords]
    vspds = [c[14] for c in coords]
    rssis = [c[5] for c in coords if c[5] < 0]
    rqlys = [c[33] for c in coords]
    volts = [c[6] for c in coords]
    curr = [c[29] for c in coords]
    gs = [c[34] for c in coords]
    return FlightSummary(
        filename=filename, date=date, start_time=start_time,
        duration_s=round(n * dt, 1), distance_km=round(dist, 3),
        max_alt_m=max(alts), min_alt_m=min(alts), avg_alt_m=round(sum(alts) / n, 1),
        max_speed_kmh=round(max(spds), 1), avg_speed_kmh=round(sum(spds) / n, 1),
        max_vspd_ms=round(max(abs(v) for v in vspds), 1),
        max_rssi_db=max(rssis) if rssis else -1, min_rssi_db=min(rssis) if rssis else -1,
        avg_rssi_db=round(sum(rssis) / len(rssis), 1) if rssis else -1,
        min_rqly=min(rqlys), avg_rqly=round(sum(rqlys) / len(rqlys), 1),
        battery_start_v=volts[0], battery_end_v=volts[-1], battery_min_v=min(volts),
        battery_start_pct=98.0, battery_end_pct=round(98.0 - (volts[0] - volts[-1]) * 55, 1),
        battery_consumed_mah=round(coords[-1][30], 0),
        max_current_a=round(max(curr), 1),
        txbat_v=8.2,
        flight_modes=modes,
        sats_max=12 + secrets.randbelow(4),
        max_g=round(max(gs), 2), avg_g=round(sum(gs) / len(gs), 2),
        home_distance_km=round(max(
            (math.hypot((c[0] - HOME_LAT) * 111.32, (c[1] - HOME_LON) * 111.32 * math.cos(math.radians(HOME_LAT)))
             for c in coords)), 1),
        glide_ratio=round(dist / max(0.001, (max(alts) - min(alts)) / 1000), 1) if max(alts) > min(alts) else 0,
        efficiency_km_per_mah=round(dist / max(0.001, coords[-1][30]), 3),
        vibration_score=round(random.uniform(0.2, 1.8), 2),
        events=events or [],
        coordinates=coords,
        vehicle_id=vehicle_id,
    )


def make_events(n, dt, coords, modes_with_acro=True):
    """Synthetic events: takeoff, mode changes, optional acro, landing."""
    ts0 = coords[0][4]
    events = [
        {"type": "takeoff", "ts": round(ts0 + 6 * dt, 2), "i": 12},
        {"type": "mode_change", "ts": round(ts0 + 15 * dt, 2), "i": 30, "mode": "ANGL"},
    ]
    if modes_with_acro:
        mid = n // 2
        events.append({
            "type": "acro", "kind": "loop",
            "ts": round(ts0 + mid * dt, 2), "i": mid, "end_i": mid + 20,
            "dur": 9.8, "peak_pitch": 2.6, "peak_roll": 0.5, "peak_rotation": 4.1,
        })
        events.append({"type": "mode_change", "ts": round(ts0 + (mid + 25) * dt, 2),
                       "i": mid + 25, "mode": "HOR"})
    events.append({"type": "landing", "ts": round(ts0 + (n - 8) * dt, 2), "i": n - 8})
    return events


def seed():
    # --- Cleanup: remove previously seeded demo flights (names may change
    # between runs), keeping the account's other flights untouched. ---
    with database._get_conn() as conn:
        conn.execute(
            "DELETE FROM flights WHERE filename LIKE 'DEMO-%'")
        conn.execute(
            "DELETE FROM shares WHERE flight_filename LIKE 'DEMO-%'")

    # --- User ---
    user = database.get_user_by_email(DEMO_EMAIL) or None
    if not user:
        for u in database.get_all_users():
            if u["username"] == DEMO_USER:
                user = u
                break
    if not user:
        user = database.create_user(DEMO_USER, DEMO_PASS, role="viewer",
                                    status="active", email=DEMO_EMAIL)
        print(f"user created: {DEMO_USER} (pass: {DEMO_PASS})")
    else:
        database.change_password(user["id"], DEMO_PASS)
        print(f"user exists: {user['username']} (id {user['id']}) — password reset to {DEMO_PASS}")
    uid = user["id"]

    # --- Vehicles ---
    v_multi = None
    v_glider = None
    for v in database.get_vehicles(uid, False):
        if v.name == "Demo Quad 5\" 3D":
            v_multi = v
        if v.name == "Demo Glider":
            v_glider = v
    if not v_multi:
        v_multi = database.create_vehicle("Demo Quad 5\" 3D", "drone", False, uid)
        print("vehicle created:", v_multi.name)
    if not v_glider:
        v_glider = database.create_vehicle("Demo Glider", "glider", False, uid)
        print("vehicle created:", v_glider.name)

    # --- Flights (fully invented) ---
    rng = random.Random(20260807)
    flights = [
        # (days_ago, start_time, duration_pts, speed, alt_max, mode, notes, tags, vehicle, battery, acro)
        (3, "18:42:10", 300, 45, 170, "HOR",
         "Perfect afternoon flight over the demo valley. Light breeze, smooth air.",
         ["demo", "sunset"], "glider", 16.4, False),
        (6, "09:15:44", 260, 52, 140, "ANGL",
         "Speed passes along the ridge. Tested new propellers.",
         ["demo", "speed"], "multi", 16.8, True),
        (9, "20:05:03", 340, 38, 120, "HOR",
         "Evening cruise, GPS fully locked, relaxed sticks.",
         ["demo", "evening"], "glider", 16.2, False),
        (12, "12:30:27", 280, 58, 95, "ACRO",
         "Freestyle session with loops and rolls. Watch the acro event!",
         ["demo", "freestyle", "acro"], "multi", 16.6, True),
        (15, "17:22:55", 220, 35, 160, "ANGL",
         "Thermal hunting with the glider; nice climb rate over the hill.",
         ["demo", "thermal"], "glider", 16.5, False),
        (19, "11:08:19", 310, 48, 130, "HOR",
         "Long range test, stayed under 2 km from home point.",
         ["demo", "range"], "multi", 16.3, False),
        (23, "19:47:38", 250, 41, 110, "ANGL",
         "Quick battery check after charging cycle; voltages all healthy.",
         ["demo", "battery"], "multi", 16.7, False),
        (28, "08:55:12", 360, 44, 190, "HOR",
         "Morning glide, best altitude of the month. Super calm conditions.",
         ["demo", "morning", "record"], "glider", 16.1, False),
        (34, "16:13:46", 200, 55, 105, "ACRO",
         "Freestyle warm-up before the weekend; quick flips.",
         ["demo", "freestyle"], "multi", 16.9, True),
        (42, "10:40:02", 290, 39, 125, "ANGL",
         "Lazy Sunday flight. Ground team enjoying the view.",
         ["demo", "sunday"], "glider", 16.4, False),
    ]

    vids = {"multi": v_multi.id, "glider": v_glider.id}
    for days_ago, start_time, pts, speed, alt_max, mode, notes, tags, vkey, battery, acro in flights:
        d = (datetime.date.today() - datetime.timedelta(days=days_ago)).isoformat()
        # deterministic but varied flight geometry
        heading0 = rng.uniform(0, 360)
        loc = rng.choice(["ValleeDuLot", "FalaiseOuest", "ParcNord", "CollineSud"])
        filename = f"DEMO-{d}-{start_time.replace(':', '')}-{loc}.csv"
        coords = make_coords(HOME_LAT + rng.uniform(-0.004, 0.004),
                             HOME_LON + rng.uniform(-0.004, 0.004),
                             pts, speed_kmh=speed, alt_max=alt_max,
                             heading0=heading0, battery=battery,
                             mode=mode, with_flight=True)
        modes = {mode: pts - 12, "OK": 12}
        events = make_events(pts, 0.5, coords, modes_with_acro=acro)
        s = summary(filename, d, start_time, coords, modes,
                    notes=notes, tags=tags, vehicle_id=vids[vkey],
                    battery=battery, events=events)
        database.save_flight(s, uid)
        if tags:
            database.set_flight_tags(filename, tags, uid, False)
        print(f"flight: {filename} ({pts*0.5:.0f}s, {s.distance_km:.2f} km)")

    # --- Visibility: one public, one contacts ---
    demo_flights = [f["filename"] for f in database.get_all_flights(uid, False)]
    if len(demo_flights) >= 2:
        database.set_flight_visibility(demo_flights[0], "public", uid, False)
        database.set_flight_visibility(demo_flights[1], "contacts", uid, False)
        print("visibility set: public + contacts")

    # --- Group + membership (self group for feed demo) ---
    g = None
    for grp in database.get_all_groups():
        if grp["name"] == "Demo Team":
            g = grp
            break
    if not g:
        g = database.create_group("Demo Team", uid)
        print("group created: Demo Team")
    database.add_group_member(g["id"], uid)
    if len(demo_flights) >= 3:
        database.set_flight_shared_group(demo_flights[2], g["id"], uid, False)
        print("flight shared with group: Demo Team")

    # --- Public share link with comments + likes on the public flight ---
    share = None
    for s in database.get_shares_for_flight(demo_flights[0]):
        share = s
        break
    if not share:
        share = database.create_share(demo_flights[0], uid)
        print("share created for", demo_flights[0])
    # comments
    comments = [
        "Amazing line over the valley! What camera setup?",
        "That glide ratio is impressive for a glider.",
        "Great conditions today, nice smooth track!",
    ]
    if not database.get_comments(share["id"]):
        for i, body in enumerate(comments):
            database.add_comment(share["id"], f"Pilot_{i+2}", body)
    # likes
    if not database.get_likes(share["id"]):
        for name in ["Pilot_2", "Pilot_3", "Pilot_4"]:
            database.add_like(share["id"], name)
        print("comments + likes added")
    print("public link: /flight/r/" + share["token"])

    print("\nDemo seed done. Login as:", DEMO_USER, "/", DEMO_PASS)


if __name__ == "__main__":
    seed()
