# Deploy Checklist — Pocket Log Analyzer

Check-list operativa per il server di produzione. Riferimento: FastAPI + SQLite
(single-writer), Nginx reverse proxy, systemd, uso personale / piccolo gruppo.

## 1. Hardware minimo raccomandato

| Risorsa | Minimo | Consigliato |
|---|---|---|
| CPU | 1 vCPU | 2 vCPU |
| RAM | 1 GB | 2–4 GB |
| Disco | SSD 20 GB | SSD 50 GB+ (CSV + foto + backup) |

L'analisi (pandas) è burst, non continua. Lo spazio è la risorsa che cresce:
pianifica ~2× i dati per i backup.

## 2. Applicazione

- [ ] Python 3.11+, dipendenze da `requirements.txt` in un venv dedicato
- [ ] `data/` in gitignore; DB, CSV e foto mai nel repository
- [ ] SQLite in modalità **WAL** + `busy_timeout` (già impostati in `database.py`)
- [ ] **1 worker uvicorn** (SQLite è single-writer); al massimo 2 con WAL
- [ ] Healthcheck: `GET /login` risponde 200

## 3. Nginx

- [ ] Reverse proxy su `http://127.0.0.1:8099` (locazioni `/flight/`, `/replay3d/`, `/api/`, `/static/`)
- [ ] `client_max_body_size` ≥ 50 MB (upload CSV; alzare a 100 MB con le foto)
- [ ] gzip/brotli su HTML/JSON; cache delle statiche con `Cache-Control`/ETag
- [ ] Timeout lunghi (`proxy_read_timeout`) sulle richieste di analisi
- [ ] Header di sicurezza: `X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy`
- [ ] HTTPS con Let's Encrypt + rinnovo automatico (certbot)
- [ ] HSTS attivo

## 4. Backup (il punto più importante)

- [ ] Backup automatico di: `data/flights.db` (+ `-wal`), cartella dei CSV, foto
- [ ] Snapshot SQLite coerente: `sqlite3 db ".backup /dest/backup.db"` (mai copiare il file a caldo)
- [ ] Copia **off-site** (rclone/rsync/borg) + retention 14–30 giorni
- [ ] **Testato il ripristino** almeno una volta (es. in un ambiente di staging)

## 5. Sicurezza

- [ ] Utente dedicato non-root per il servizio
- [ ] Systemd hardening: `ProtectSystem=strict`, `NoNewPrivileges`, `PrivateTmp`,
      `ReadWritePaths` sulla sola cartella applicazione, `UMask=0027`
- [ ] Credenziali (POCKET_USER/POCKET_PASS) e `POCKET_SESSION_SECRET` in
      `EnvironmentFile` con permessi 640/600, non in chiaro nel file unit
      (vedi README "Systemd service")
- [ ] `POCKET_SESSION_SECRET` ≥ 32 caratteri (es. `openssl rand -hex 32`);
      se assente l'app genera un segreto persistente in `data/.session_secret` (600)
- [ ] Firewall: solo 80/443 + SSH
- [ ] fail2ban su SSH (e sul login dell'app, già protetto da rate limiting interno)
- [ ] `unattended-upgrades` solo per gli update di sicurezza
- [ ] Log rotation (logrotate) per uvicorn e nginx

## 6. Resilienza e operatività

- [ ] `Restart=always` + `RestartSec=5` nel servizio
- [ ] Monitoraggio base: disco, RAM, uptime (cron + mail o Uptime Kuma)
- [ ] Deploy riproducibile: `git pull` + restart, **con backup del DB prima** di ogni
      aggiornamento che introduce migrazioni
- [ ] Rollback noto: ripristino del backup + `git checkout <tag-previo>`

## 7. Cosa NON serve ora

- Cluster / HA multi-server / load balancer: eccessivi per un servizio self-hosted.
- Se si cresce oltre ~10 utenti in scrittura concorrente: migrare a PostgreSQL +
  storage foto separato (lo schema attuale lo consente senza rilavori).
