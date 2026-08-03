"""Heuristic detection of acrobatic maneuvers (loops / flips / rolls) and
possible incidents (rapid loss of altitude ending in an impact).

These detectors work on the raw telemetry points (pitch/roll in radians,
vertical speed, smoothed GPS altitude, flight mode, RSSI) and are intentionally
conservative: they flag *possible* events rather than certainties, so they can
be tuned with the constants below on real flight data.
"""
from statistics import median
# ---- Acro (loop / flip / roll) ----
ACRO_MAX_DURATION_S = 22.0        # max length of one maneuver window
ACRO_MIN_WINDOW_ROTATION_RAD = 3.0  # sustained rotation inside the window
ACRO_MIN_PITCH_RAD = 1.0            # peak |pitch| for a loop (~57 deg)
ACRO_MIN_ROLL_RAD = 1.2             # peak |roll| for a roll/flip (~69 deg)
ACRO_MIN_AIR_ALT_M = 5.0            # median altitude must be above this
ACRO_MIN_AIR_SPEED_KMH = 10.0       # fallback when GPS alt is all zero

# ---- Incident (loss of altitude + impact) ----
INCIDENT_MIN_SAMPLES = 4        # run length (~2 s at 2 Hz)
INCIDENT_MEMBER_VSPD_MS = -5.0  # samples belonging to a descent run
INCIDENT_MEAN_VSPD_MS = -8.0    # run mean vertical speed must be steeper
INCIDENT_MIN_DROP_M = 20.0      # total altitude loss required
INCIDENT_GROUND_TOL_M = 6.0     # end altitude within this of the takeoff ground
INCIDENT_FAILSAFE_WINDOW_S = 5.0
INCIDENT_STOP_SPEED_KMH = 5.0   # GPS considered stopped below this
INCIDENT_LANDING_SKIP_S = 4.0   # don't flag a descent near a detected landing


def _median_filter(values, size=5):
    n = len(values)
    out = list(values)
    half = size // 2
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        out[i] = median(values[lo:hi])
    return out


def _peak_rotation(pitches, rolls, ts, lo, hi):
    """Max |d(pitch)|+|d(roll)| per second inside index range [lo, hi)."""
    peak = 0.0
    n = len(ts)
    for k in range(max(lo, 1), min(hi, n)):
        dt = ts[k] - ts[k - 1]
        if dt > 0:
            rate = (abs(pitches[k] - pitches[k - 1]) + abs(rolls[k] - rolls[k - 1])) / dt
            peak = max(peak, rate)
    return peak


def _acro_event(pitches, rolls, ts, start, end, peak_rotation):
    peak_pitch = max(abs(x) for x in pitches[start:end + 1])
    peak_roll = max(abs(x) for x in rolls[start:end + 1])
    if peak_pitch < ACRO_MIN_PITCH_RAD and peak_roll < ACRO_MIN_ROLL_RAD:
        return None
    kind = "loop" if peak_pitch >= ACRO_MIN_PITCH_RAD and peak_roll < ACRO_MIN_ROLL_RAD else "flip_roll"
    return {
        "type": "acro",
        "kind": kind,
        "ts": ts[start],
        "i": start,
        "end_i": end,
        "dur": round(ts[end] - ts[start], 1),
        "peak_pitch": round(peak_pitch, 2),
        "peak_roll": round(peak_roll, 2),
        "peak_rotation": round(peak_rotation, 2),
    }


def detect_acros(points) -> list[dict]:
    """Find loop/flip/roll maneuvers from pitch/roll rotation + altitude profile.

    A maneuver requires *sustained* rotation (the rotation can't come from a
    single sensor jump) together with a large pitch (loop) or roll (flip/roll)
    excursion, performed while airborne.
    """
    n = len(points)
    if n < 20:
        return []
    alts = _median_filter([p.alt for p in points])
    pitches = [p.pitch for p in points]
    rolls = [p.roll for p in points]
    gspds = [p.gspd for p in points]
    ts = [p.timestamp for p in points]

    has_gps_alt = any(abs(a) > 0.1 for a in alts)

    rot = [0.0] * n
    for i in range(1, n):
        rot[i] = rot[i - 1] + abs(pitches[i] - pitches[i - 1]) + abs(rolls[i] - rolls[i - 1])

    dt = [0.5] * n
    for i in range(1, n):
        if ts[i] > ts[i - 1]:
            dt[i] = ts[i] - ts[i - 1]
    med_dt = median(dt[5:]) if n > 5 else 0.5
    window = max(6, min(int(ACRO_MAX_DURATION_S / max(med_dt, 0.01)), n))

    hit = [False] * n
    for i in range(window, n):
        lo = i - window
        peak_pitch = max(abs(x) for x in pitches[lo:i])
        peak_roll = max(abs(x) for x in rolls[lo:i])
        peak_angle_ok = peak_pitch >= ACRO_MIN_PITCH_RAD or peak_roll >= ACRO_MIN_ROLL_RAD
        rotation = rot[i] - rot[lo]
        med_alt = median(alts[lo:i])
        airborne = med_alt >= ACRO_MIN_AIR_ALT_M if has_gps_alt else \
            median(gspds[lo:i]) >= ACRO_MIN_AIR_SPEED_KMH
        hit[i] = peak_angle_ok and rotation >= ACRO_MIN_WINDOW_ROTATION_RAD and airborne

    events = []
    start = None
    for i in range(n + 1):
        active = i < n and hit[i]
        if active:
            if start is None:
                start = i
            continue
        if start is not None:
            # the rotation that triggered these hits lies within the window
            # ending at `start`; measure the peak rate over that whole span so
            # the fast entry transition into the maneuver is not missed
            peak_rot = _peak_rotation(pitches, rolls, ts, max(0, start - window), i)
            if i - start > window:
                for s in range(start, i, window):
                    ev = _acro_event(pitches, rolls, ts, s, min(s + window, i) - 1, peak_rot)
                    if ev:
                        events.append(ev)
            else:
                ev = _acro_event(pitches, rolls, ts, start, i - 1, peak_rot)
                if ev:
                    events.append(ev)
            start = None
    return events


def detect_incidents(points, existing_events=None) -> list[dict]:
    """Find possible crashes: a steep altitude loss ending in an impact."""
    n = len(points)
    if n < 20:
        return []
    alts = _median_filter([p.alt for p in points])
    vspds = [p.vspd for p in points]
    gspds = [p.gspd for p in points]
    ts = [p.timestamp for p in points]
    modes = [p.flight_mode for p in points]
    rssis = [p.rssi_1 for p in points]

    landing_ts = [e.get("ts", 0) for e in (existing_events or []) if e.get("type") == "landing"]

    ground_alt = 0.0
    for a in alts[:6]:
        if abs(a) > 0.1:
            ground_alt = median([x for x in alts[:6] if abs(x) > 0.1])
            break

    events = []
    i = 0
    while i < n:
        if vspds[i] > INCIDENT_MEMBER_VSPD_MS:
            i += 1
            continue
        j = i
        while j + 1 < n and vspds[j + 1] <= INCIDENT_MEMBER_VSPD_MS:
            j += 1
        run_len = j - i + 1
        mean_vspd = sum(vspds[i:j + 1]) / run_len
        drop = alts[i] - alts[j]
        if run_len >= INCIDENT_MIN_SAMPLES and mean_vspd <= INCIDENT_MEAN_VSPD_MS and drop >= INCIDENT_MIN_DROP_M:
            end_alt = alts[j]
            hit_ground = end_alt <= ground_alt + INCIDENT_GROUND_TOL_M
            deep_below = end_alt < ground_alt - INCIDENT_GROUND_TOL_M

            stop = False
            if median(gspds[i:j + 1]) > 20:
                for k in range(j + 1, min(n, j + 6)):
                    if gspds[k] < INCIDENT_STOP_SPEED_KMH:
                        stop = True
                        break

            fs = any(modes[m] == "!ERR" or rssis[m] <= -100 for m in range(i, j + 1))
            near_landing = any(abs(ts[j] - lt) <= INCIDENT_LANDING_SKIP_S for lt in landing_ts)

            # A run ending well below ground level can't be a landing: the GPS
            # altitude going metres under the recorded ground is an impact, so it
            # must not be suppressed by the near-landing heuristic.
            if (hit_ground or stop or fs or deep_below) and (not near_landing or deep_below):
                events.append({
                    "type": "incident",
                    "ts": ts[j],
                    "i": j,
                    "end_i": j,
                    "alt": round(end_alt, 1),
                    "drop": round(drop, 1),
                    "vspd": round(vspds[j], 1),
                    "signal_loss": bool(fs),
                    "stopped": bool(stop),
                })
        i = j + 1
    return events
