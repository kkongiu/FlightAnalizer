import pandas as pd
from pathlib import Path
from datetime import datetime
from models import TelemetryPoint


def parse_timestamp(date_str: str, time_str: str) -> float:
    dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S.%f")
    return dt.timestamp()


def safe_float(val, default=0.0):
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def safe_int(val, default=0):
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return default


def parse_log(filepath: str | Path) -> list[TelemetryPoint]:
    df = pd.read_csv(filepath, dtype=str, keep_default_na=False)
    points = []
    for _, row in df.iterrows():
        try:
            ts = parse_timestamp(row.get("Date", ""), row.get("Time", ""))
            gps = str(row.get("GPS", "0 0")).split()
            lat = safe_float(gps[0]) if len(gps) > 0 else 0.0
            lon = safe_float(gps[1]) if len(gps) > 1 else 0.0
            pt = TelemetryPoint(
                timestamp=ts,
                lat=lat, lon=lon,
                alt=safe_float(row.get("Alt(m)", "0")),
                gspd=safe_float(row.get("GSpd(kmh)", "0")),
                vspd=safe_float(row.get("VSpd(m/s)", "0")),
                heading=safe_float(row.get("Hdg(°)", "0")),
                rssi_1=safe_int(row.get("1RSS(dB)", "0")),
                rssi_2=safe_int(row.get("2RSS(dB)", "0")),
                rqly=safe_int(row.get("RQly(%)", "0")),
                rsnr=safe_int(row.get("RSNR(dB)", "0")),
                trss=safe_int(row.get("TRSS(dB)", "0")),
                tqly=safe_int(row.get("TQly(%)", "0")),
                tsnr=safe_int(row.get("TSNR(dB)", "0")),
                rxbt=safe_float(row.get("RxBt(V)", "0")),
                curr=safe_float(row.get("Curr(A)", "0")),
                capa=safe_float(row.get("Capa(mAh)", "0")),
                bat_pct=safe_float(row.get("Bat%(%)", "0")),
                pitch=safe_float(row.get("Ptch(rad)", "0")),
                roll=safe_float(row.get("Roll(rad)", "0")),
                yaw=safe_float(row.get("Yaw(rad)", "0")),
                rud=safe_int(row.get("Rud", "0")),
                ele=safe_int(row.get("Ele", "0")),
                thr=safe_int(row.get("Thr", "0")),
                ail=safe_int(row.get("Ail", "0")),
                flight_mode=str(row.get("FM", "OK")).strip(),
                sats=safe_int(row.get("Sats", "0")),
                txbat=safe_float(row.get("TxBat(V)", "0")),
            )
            points.append(pt)
        except Exception:
            pass
    return points
