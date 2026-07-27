# Pocket Log Analyzer

FPV drone telemetry log analyzer — Strava-like dashboard with map, charts, and flight statistics.

## Requirements

- Python 3.12+
- Apache with `mod_proxy` and `mod_proxy_http`

## Quick Start (Development)

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000 — login with `Admin` / `Arturo2026#`.

## Deploy on Remote Server (Apache + reverse proxy)

### 1. Upload files

```bash
scp -r pocket-log-analyzer/ user@server:/var/www/analisilog/
```

### 2. Setup Python environment

```bash
cd /var/www/analisilog
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Systemd service

Create `/etc/systemd/system/analisilog.service`:

```ini
[Unit]
Description=Pocket Log Analyzer
After=network.target

[Service]
User=www-data
WorkingDirectory=/var/www/analisilog
ExecStart=/var/www/analisilog/venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl enable analisilog
sudo systemctl start analisilog
```

### 4. Apache VirtualHost

Enable modules:

```bash
sudo a2enmod proxy proxy_http
sudo systemctl restart apache2
```

Create `/etc/apache2/sites-available/analisilog.conf`:

```apache
<VirtualHost *:80>
    ServerName analisilog.tuo-dominio.com

    ProxyPreserveHost On
    ProxyPass / http://127.0.0.1:8000/
    ProxyPassReverse / http://127.0.0.1:8000/

    ErrorLog ${APACHE_LOG_DIR}/analisilog-error.log
    CustomLog ${APACHE_LOG_DIR}/analisilog-access.log combined
</VirtualHost>
```

Enable site:

```bash
sudo a2ensite analisilog
sudo systemctl reload apache2
```

### 5. SSL (optional but recommended)

```bash
sudo certbot --apache -d analisilog.tuo-dominio.com
```

### 6. Firewall

```bash
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
```

## File Structure

```
analisilog/
├── app.py          # FastAPI application & routes
├── parser.py       # CSV log parser (POCKET format)
├── analyzer.py     # Flight metrics computation
├── database.py     # JSON file storage
├── models.py       # Data models
├── data/           # Flight database (flights.json)
├── templates/      # Jinja2 HTML templates
├── static/         # CSS, vendors (Leaflet, Chart.js)
└── requirements.txt
```

## Credentials

Default login: `Admin` / `Arturo2026#`

Change credentials in `app.py` (`USER` and `PASS` variables).
