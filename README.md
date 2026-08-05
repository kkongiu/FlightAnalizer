# Pocket Log Analyzer

> Self-hosted flight log analyzer for EdgeTX / OpenTX drone telemetry.  
> Import CSV logs, explore flights on an interactive map, analyze telemetry charts, and manage your fleet — all on your own server.

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.104%2B-009688)
![License](https://img.shields.io/badge/license-MIT-green)
![Status](https://img.shields.io/badge/status-alpha-yellow)

---

## Features

| | |
|---|---|
| **Interactive Map** | Leaflet map with satellite/street/terrain layers, playback controls, RC gimbal visualization |
| **Telemetry Charts** | Altitude, speed, RSSI, battery voltage/current/capacity, vertical speed, heading, link quality, throttle vs current, stick response |
| **RC Controller View** | Live gimbal position, switch states (SA–SE), flight mode, P1 dial |
| **Battery Health** | Per-flight voltage start/end/min trends, degradation over time per vehicle |
| **Vehicle Management** | Register drones, assign logs, set default vehicle, photos |
| **Flight Modes Strip** | Color-coded timeline of flight mode changes |
| **Events Timeline** | Auto-detected takeoff, landing, signal loss events |
| **Notes & Tags** | Free-form notes per flight, tag system for filtering |
| **Export** | GPX and KML download for use in Google Earth / other tools |
| **3D Replay** *(experimental)* | Three.js terrain with elevation, camera modes (Ground/FPV/Free), aircraft trail |
| **Multi-User** | Admin and viewer roles with separate logins |
| **Dark / Light Theme** | System-aware toggle, persisted in localStorage |
| **PWA Ready** | Service worker for offline-capable install on mobile |
| **Responsive** | Tailwind CSS polished UI, works on desktop and mobile |

---

## Screenshots

| Dashboard | Flight Detail | User Management |
|---|---|---|
| *(screenshot coming)* | *(screenshot coming)* | *(screenshot coming)* |

---

## Requirements

- **Python 3.11+**
- pip
- ~200 MB disk for dependencies + logs

No database server required — uses SQLite.

---

## Quick Start (Development)

```bash
git clone https://github.com/kkongiu/FlightAnalizer.git
cd FlightAnalizer
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

POCKET_USER=admin POCKET_PASS=changeme uvicorn app:app --host 0.0.0.0 --port 8000
```

Open **http://localhost:8000** and log in with `admin` / `changeme`.

> The first user is seeded from `POCKET_USER` / `POCKET_PASS` environment variables.  
> After that, you can create additional users (viewer or admin) from the **Users** page.

---

## Production Deployment (Nginx + Systemd)

### 1. Setup

```bash
git clone https://github.com/kkongiu/FlightAnalizer.git /opt/pocket-log-analyzer
cd /opt/pocket-log-analyzer
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Systemd service

Create `/etc/systemd/system/pocket-log-analyzer.service`:

```ini
[Unit]
Description=Pocket Log Analyzer
After=network.target

[Service]
User=www-data
WorkingDirectory=/opt/pocket-log-analyzer
Environment="POCKET_USER=admin"
Environment="POCKET_PASS=your-secure-password"
ExecStart=/opt/pocket-log-analyzer/venv/bin/uvicorn app:app --host 127.0.0.1 --port 8099
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable pocket-log-analyzer
sudo systemctl start pocket-log-analyzer
```

### 3. Nginx reverse proxy

```nginx
server {
    listen 80;
    server_name your-domain.com;

    location /flight/ {
        proxy_pass http://127.0.0.1:8099/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /replay3d/ {
        proxy_pass http://127.0.0.1:8099/;
        proxy_set_header Host $host;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:8099/;
        proxy_set_header Host $host;
    }

    location /static/ {
        proxy_pass http://127.0.0.1:8099/;
        proxy_set_header Host $host;
    }
}
```

### 4. SSL (recommended)

```bash
sudo certbot --nginx -d your-domain.com
```

---

## Importing Flight Logs

### Logging Telemetry on a RadioMaster Pocket (EdgeTX)

The analyzer reads telemetry CSV logs produced by the radio itself. On a
RadioMaster Pocket running EdgeTX, the built-in **SD Logs** special function
writes everything (link quality, GPS, battery, attitude, stick positions) to a
CSV on the SD card. To enable it:

1. **Make sure telemetry arrives** — the sensors (RSSI, LQ, GPS, altitude,
   speed, attitude Pitch/Roll/Yaw) must be visible on the radio:

   - **ExpressLRS / CRSF**: in Betaflight go to **Receiver** and enable
     **Telemetry** (CRSF). The GPS and attitude data are sent by the flight
     controller through the receiver.
   - On the radio: **MDL → Telemetry → Discover new sensors** (restart the
     radio if sensors are still missing).

2. **Enable logging** — open **MDL → Special Functions**, press **+** and set:

   | Option     | Value                                                        |
   |------------|--------------------------------------------------------------|
   | **Trigger**| Your arm switch (recommended), `ON` (always) or `TELE` (when a receiver is connected) |
   | **Function**| `SD Logs`                                                    |
   | **Value**  | `0.5s` — the logging interval (~2 Hz, matching the attitude sensors) |
   | **Enable** | **ON** (the #1 reason logging does not start is a disabled function) |

3. **Where the logs go** — on the SD card under `LOGS/`, named
   `<model>-<date>-<time>.csv`.

4. **Import into the app** — copy the CSV to the app folder and use
   **Scan Folder**, or upload it via **Import CSV**. Files can also be
   transferred with the USB cable (SD card mass storage).

Tips:

- EdgeTX stops logging when the SD card has **less than 50 MB free**.
- Unwanted sensors can be excluded: **Telemetry page → Edit sensor → uncheck
  `Logs`**.
- With ExpressLRS, a higher *telemetry ratio* gives denser logs; changes apply
  after a radio power cycle.

### CSV Format

The parser expects EdgeTX / POCKET telemetry CSV files with the following columns (case-insensitive):

```
date,time,lat,lon,alt,spd,hdg,sats,volt,curr,capa,rssi,rqly,...
```

Files are imported via:

1. **Upload** — click "Import CSV" on the Dashboard or Flights page
2. **Scan Folder** — scans the app directory for `.csv` files not yet imported
3. **GPX Import** — upload a GPX track to replace or augment flight coordinates

---

## API Endpoints

All API routes are prefixed with `/api/` and require authentication.

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/flights` | List all flights |
| `GET` | `/api/flights/{filename}` | Get flight details |
| `DELETE` | `/api/flights/{filename}` | Delete a flight |
| `PUT` | `/api/flights/{filename}` | Rename a flight |
| `PUT` | `/api/flights/{filename}/notes` | Update notes |
| `POST` | `/api/flights/{filename}/tags` | Set tags |
| `PUT` | `/api/flights/{filename}/vehicle` | Assign vehicle |
| `POST` | `/api/flights/{filename}/import-gpx` | Import GPX track |
| `POST` | `/api/flights/{filename}/rescan-nav` | Re-scan navigation data |
| `GET` | `/api/stats` | Aggregate statistics |
| `GET` | `/api/tags` | List all tags |
| `GET` | `/api/vehicles` | List vehicles |
| `POST` | `/api/vehicles` | Create vehicle |
| `PUT` | `/api/vehicles/{id}` | Update vehicle |
| `DELETE` | `/api/vehicles/{id}` | Delete vehicle |
| `POST` | `/api/scan` | Scan for unimported CSV files |
| `POST` | `/api/upload` | Upload a CSV file |
| `POST` | `/api/reprocess` | Re-analyze all flights from CSV |
| `GET` | `/api/export/{filename}` | Export as GPX or KML |
| `GET` | `/api/battery-health` | Battery voltage trends |
| `GET` | `/api/users` | List users (admin only) |
| `POST` | `/api/users` | Create user (admin only) |
| `PUT` | `/api/users/{id}` | Update user (admin only) |
| `DELETE` | `/api/users/{id}` | Delete user (admin only) |
| `POST` | `/api/users/{id}/change-password` | Change password |

---

## Technology Stack

| Component | Technology |
|---|---|
| **Backend** | Python 3.11+, FastAPI, Uvicorn |
| **Database** | SQLite |
| **Templates** | Jinja2 |
| **Maps** | Leaflet.js (self-hosted) |
| **Charts** | Chart.js (self-hosted) |
| **3D** | Three.js r160 (self-hosted) |
| **UI** | Tailwind CSS (CDN) + custom CSS |
| **Auth** | Session-based with PBKDF2 password hashing |

---

## Project Structure

```
pocket-log-analyzer/
├── app.py              # FastAPI routes and application logic
├── database.py         # SQLite queries, user auth, migrations
├── parser.py           # CSV telemetry parser
├── analyzer.py         # Flight metric computation
├── models.py           # Pydantic / dataclass models
├── requirements.txt
├── templates/
│   ├── base.html       # Shell layout with nav, Tailwind, theme toggle
│   ├── login.html      # Standalone login page
│   ├── dashboard.html  # Main dashboard with stats, charts, records
│   ├── flights.html    # Flight list with search, tags, pagination
│   ├── flight.html     # Flight detail: map, charts, controller, stats
│   ├── vehicles.html   # Vehicle management
│   ├── users.html      # User management (admin only)
│   ├── report.html     # Summary report page
│   ├── compare.html    # Multi-flight comparison
│   └── replay3d.html   # 3D replay (experimental)
├── static/
│   ├── style.css       # Custom CSS (light/dark theme)
│   ├── lib/three/      # Self-hosted Three.js
│   └── vendor/         # Leaflet, Chart.js
└── data/               # SQLite database (auto-created, gitignored)
```

---

## Roadmap

The detailed multi-user roadmap (44 items, phases F0–F8) is tracked in
[`docs/ROADMAP.md`](docs/ROADMAP.md) and as GitHub issues
([project board](https://github.com/users/kkongiu/projects/1)).

Done so far:

- [x] CSV import and telemetry parsing
- [x] Interactive map with playback
- [x] Telemetry charts (altitude, speed, RSSI, battery, RC)
- [x] Vehicle management
- [x] Multi-user with roles (admin/viewer)
- [x] Tailwind UI polish
- [x] INAV waypoint mission builder
- [x] Production hardening (SQLite WAL, systemd, deploy checklist)

Next (by phase):

- **F0 · Fondamenta** — versioned DB migrations + Docker, automated backups, monitoring, E2E test suite + CI
- **F1 · Sicurezza** — rate limiting, CSRF, secure session cookies, password policy
- **F2 · Account** — public registration, password reset, self-service account
- **F3 · Isolamento** — per-user flight ownership (`owner_id`), access control, isolation tests
- **F4 · Privacy** — account deletion + data export, audit log, privacy policy
- **F5 · Messaggi** — private messaging, in-app notifications, email/push alerts
- **F6 · Foto** — per-flight photo gallery, covers, geotagged photos
- **F7 · Sharing** — public share links, social buttons, comments/likes, team groups
- **F8 · Dominio** — maintenance reminders, Excel/CSV export, API upload tokens, flight weather

Other ideas from the original roadmap are still on the list: battery cell-level analysis,
PDF report export, drone model auto-detection, Betaflight / ArduPilot log support,
Docker image, and i18n.

---

## License

[MIT](LICENSE)

---

## Disclaimer

This project is in **alpha** stage. Features and APIs may change.  
Not affiliated with EdgeTX, FrSky, or any drone manufacturer.

Use at your own risk — always verify critical flight data through official channels.
