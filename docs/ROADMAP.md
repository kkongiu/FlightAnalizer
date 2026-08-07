# Roadmap — Pocket Log Analyzer

Piano di evoluzione verso la **produzione multi-utente**. Il prefisso `[Fx]` indica
la fase di implementazione e la lista è in ordine di esecuzione: ogni fase va
completata prima di passare alla successiva.

Ogni voce corrisponde a un'issue GitHub: il numero `[#N]` è il riferimento.
Stato: **F0–F6 completate** (F2 include registrazione con conferma via email e consenso privacy; F4 include export dati, cancellazione self-service e audit log; F5 include messaggistica privata con badge non letti, notifiche email e allegato volo; F6 include galleria foto per volo con copertina); F7–F8 pending.

---

## F0 · Fondamenta

- [x] `[F0][INFRA]` Migrazioni DB versionate (prerequisito per ogni modifica dello schema) + Docker — [#1](https://github.com/kkongiu/FlightAnalizer/issues/1)
- [x] `[F0][INFRA]` Backup automatico (DB + CSV + foto) con ripristino — [#2](https://github.com/kkongiu/FlightAnalizer/issues/2)
- [x] `[F0][INFRA]` Logging/monitoraggio errori sul server — [#3](https://github.com/kkongiu/FlightAnalizer/issues/3)
- [x] `[F0][TEST]` Suite end-to-end/API con TestClient: auth (login/logout/sessioni), rotte voli (CRUD, note, tag, gpx), mission (track/preview/export), utenti — [#4](https://github.com/kkongiu/FlightAnalizer/issues/4)
- [x] `[F0][TEST]` Verifica regressioni CI: eseguire la suite su GitHub Actions — [#5](https://github.com/kkongiu/FlightAnalizer/issues/5)

## F1 · Sicurezza

- [x] `[F1][SICUREZZA]` Rate limiting / protezione brute-force su login, registrazione e reset password — [#6](https://github.com/kkongiu/FlightAnalizer/issues/6)
- [x] `[F1][SICUREZZA]` Protezione CSRF sugli endpoint che modificano lo stato (auth a sessioni) — [#7](https://github.com/kkongiu/FlightAnalizer/issues/7)
- [x] `[F1][SICUREZZA]` Cookie di sessione: httpOnly, SameSite, Secure e segreto da variabile d'ambiente — [#8](https://github.com/kkongiu/FlightAnalizer/issues/8)
- [x] `[F1][SICUREZZA]` Verifica copertura require_auth su tutte le rotte e endpoint sensibili — [#9](https://github.com/kkongiu/FlightAnalizer/issues/9)
- [x] `[F1][SICUREZZA]` Policy password minima (lunghezza/complessità) su registrazione e cambio password — [#10](https://github.com/kkongiu/FlightAnalizer/issues/10)
- [x] `[F1][SICUREZZA]` Credenziali admin senza password in chiaro nei file di sistema (systemd) — [#11](https://github.com/kkongiu/FlightAnalizer/issues/11)

## F2 · Account

- [x] `[F2][ACCOUNT]` Pagina di registrazione pubblica (abilitabile) con validazione e hash PBKDF2 — [#12](https://github.com/kkongiu/FlightAnalizer/issues/12)
- [x] `[F2][ACCOUNT]` Flusso reset password (token sicuro + email/config SMTP o link via admin) — [#13](https://github.com/kkongiu/FlightAnalizer/issues/13)
- [x] `[F2][ACCOUNT]` Gestione account self-service: cambio password, cambio dati, preferenze — [#14](https://github.com/kkongiu/FlightAnalizer/issues/14)
- [x] `[F2][ACCOUNT]` Multi-account: inviti/approvazione admin, gestione ruoli, disattivazione account — [#15](https://github.com/kkongiu/FlightAnalizer/issues/15)

## F3 · Isolamento dati

- [x] `[F3][ISOLAMENTO]` Aggiungere owner_id ai voli (colonna, migrazione voli esistenti all'admin) — [#16](https://github.com/kkongiu/FlightAnalizer/issues/16)
- [x] `[F3][ISOLAMENTO]` Assegnare il proprietario in upload/import/scan/reprocess — [#17](https://github.com/kkongiu/FlightAnalizer/issues/17)
- [x] `[F3][ISOLAMENTO]` Filtrare per owner tutte le query e rotte (lista, dettaglio, stats, tags, veicoli, battery-health, compare, mission); admin vede tutto — [#18](https://github.com/kkongiu/FlightAnalizer/issues/18)
- [x] `[F3][ISOLAMENTO]` Bloccare accesso ai voli altrui: check proprietario su get/export/delete/edit e 404/403 — [#19](https://github.com/kkongiu/FlightAnalizer/issues/19)
- [x] `[F3][ISOLAMENTO]` Test di isolamento: l'utente A non vede né modifica i voli dell'utente B — [#20](https://github.com/kkongiu/FlightAnalizer/issues/20)
- [x] `[F3][ISOLAMENTO]` Interfaccia: indicatore del proprietario, eventuale filtro/gestione per admin — [#21](https://github.com/kkongiu/FlightAnalizer/issues/21)

## F4 · Privacy

- [x] `[F4][PRIVACY]` Cancellazione account completa con i propri dati (voli, foto, messaggi) + esportazione dati utente — [#22](https://github.com/kkongiu/FlightAnalizer/issues/22)
- [x] `[F4][PRIVACY]` Log di accesso/audit (chi ha fatto cosa, chi ha visto cosa) — [#23](https://github.com/kkongiu/FlightAnalizer/issues/23)
- [x] `[F4][PRIVACY]` Pagina Privacy/Termini + gestione consensi — [#24](https://github.com/kkongiu/FlightAnalizer/issues/24)

## F5 · Messaggi e notifiche

- [x] `[F5][MESSAGGI]` Messaggistica privata tra utenti: invio, conversazioni, sola lettura dei propri messaggi — [#25](https://github.com/kkongiu/FlightAnalizer/issues/25)
- [x] `[F5][MESSAGGI]` Pagina Messaggi + badge non letti e notifiche in-app — [#26](https://github.com/kkongiu/FlightAnalizer/issues/26)
- [x] `[F5][MESSAGGI]` Eliminazione/archiviazione conversazioni e gestione per admin — [#27](https://github.com/kkongiu/FlightAnalizer/issues/27)
- [x] `[F5][NOTIFICHE]` Notifiche email/push per nuovi messaggi, commenti e condivisioni (cross-device) — [#28](https://github.com/kkongiu/FlightAnalizer/issues/28)
- [x] `[F5][MESSAGGI]` Eventuale allegato/menzione di un volo in un messaggio (rispetta l'isolamento) — [#29](https://github.com/kkongiu/FlightAnalizer/issues/29)

## F6 · Foto

- [x] `[F6][FOTO]` Caricamento/gestione foto per volo (backend, storage, validazione, dimensione) — [#30](https://github.com/kkongiu/FlightAnalizer/issues/30)
- [x] `[F6][FOTO]` Galleria nella pagina volo (thumbnails, lightbox, eliminazione singola) — [#31](https://github.com/kkongiu/FlightAnalizer/issues/31)
- [x] `[F6][FOTO]` Integrazione con isolamento: solo il proprietario (o admin) carica/gestisce le foto — [#32](https://github.com/kkongiu/FlightAnalizer/issues/32)
- [x] `[F6][FOTO]` Copertina del volo e anteprima nelle liste/dashboard — [#33](https://github.com/kkongiu/FlightAnalizer/issues/33)
- [ ] `[F6][FOTO]` Eventuale associazione foto al punto del tracciato (timestamp/geotag) e inserimento nella condivisione (dipende da F7) — [#34](https://github.com/kkongiu/FlightAnalizer/issues/34)

## F7 · Sharing e social

- [ ] `[F7][SHARING]` Link pubblico di condivisione per volo (token sicuro, condivisione solo se abilitata dal proprietario, opzione revoca) — [#35](https://github.com/kkongiu/FlightAnalizer/issues/35)
- [ ] `[F7][SHARING]` Pagina pubblica di visualizzazione volo (mappa/stats senza login) accessibile dal link — [#36](https://github.com/kkongiu/FlightAnalizer/issues/36)
- [ ] `[F7][SHARING]` Pulsanti di condivisione social (WhatsApp, Telegram, X/Twitter, Facebook) con anteprima — [#37](https://github.com/kkongiu/FlightAnalizer/issues/37)
- [ ] `[F7][SHARING]` Immagine/anteprima condivisibile (og:image, open graph) e eventuale download GPX dal link pubblico — [#38](https://github.com/kkongiu/FlightAnalizer/issues/38)
- [ ] `[F7][SOCIAL]` Commenti/like sui voli condivisi pubblicamente — [#39](https://github.com/kkongiu/FlightAnalizer/issues/39)
- [ ] `[F7][SOCIAL]` Gruppi/team: condividere un volo solo con un gruppo specifico — [#40](https://github.com/kkongiu/FlightAnalizer/issues/40)
- [ ] `[F7][SOCIAL]` Amici/contatti stile Strava-Garmin: richieste di amicizia, feed dei voli pubblici dei propri contatti, visibilità per-volo (pubblico/amici/privato)

## F8 · Dominio e integrazione

- [ ] `[F8][DOMINIO]` Manutenzione programmata: ore di volo per veicolo, scadenze parti/richiami, avvisi — [#41](https://github.com/kkongiu/FlightAnalizer/issues/41)
- [ ] `[F8][DOMINIO]` Export Excel/CSV dei dati aggregati e calendario/timeline dei voli — [#42](https://github.com/kkongiu/FlightAnalizer/issues/42)
- [ ] `[F8][INTEGRAZIONE]` API token per upload da script/app radio esterno (auto-upload) — [#43](https://github.com/kkongiu/FlightAnalizer/issues/43)
- [ ] `[F8][DOMINIO]` Meteo associato al volo (vento/temperatura storica del giorno del volo) — [#44](https://github.com/kkongiu/FlightAnalizer/issues/44)
