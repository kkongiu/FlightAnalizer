# Roadmap — Pocket Log Analyzer

Piano di evoluzione verso la **produzione multi-utente**. Il prefisso `[Fx]` indica
la fase di implementazione e la lista è in ordine di esecuzione: ogni fase va
completata prima di passare alla successiva.

Stato: tutte le voci sono **pending** (nessuna avviata).

---

## F0 · Fondamenta

- [ ] `[F0][INFRA]` Migrazioni DB versionate (prerequisito per ogni modifica dello schema) + Docker
- [ ] `[F0][INFRA]` Backup automatico (DB + CSV + foto) con ripristino
- [ ] `[F0][INFRA]` Logging/monitoraggio errori sul server
- [ ] `[F0][TEST]` Suite end-to-end/API con TestClient: auth (login/logout/sessioni), rotte voli (CRUD, note, tag, gpx), mission (track/preview/export), utenti
- [ ] `[F0][TEST]` Verifica regressioni CI: eseguire la suite su GitHub Actions

## F1 · Sicurezza

- [ ] `[F1][SICUREZZA]` Rate limiting / protezione brute-force su login, registrazione e reset password
- [ ] `[F1][SICUREZZA]` Protezione CSRF sugli endpoint che modificano lo stato (auth a sessioni)
- [ ] `[F1][SICUREZZA]` Cookie di sessione: httpOnly, SameSite, Secure e segreto da variabile d'ambiente
- [ ] `[F1][SICUREZZA]` Verifica copertura require_auth su tutte le rotte e endpoint sensibili
- [ ] `[F1][SICUREZZA]` Policy password minima (lunghezza/complessità) su registrazione e cambio password
- [ ] `[F1][SICUREZZA]` Credenziali admin senza password in chiaro nei file di sistema (systemd)

## F2 · Account

- [ ] `[F2][ACCOUNT]` Pagina di registrazione pubblica (abilitabile) con validazione e hash PBKDF2
- [ ] `[F2][ACCOUNT]` Flusso reset password (token sicuro + email/config SMTP o link via admin)
- [ ] `[F2][ACCOUNT]` Gestione account self-service: cambio password, cambio dati, preferenze
- [ ] `[F2][ACCOUNT]` Multi-account: inviti/approvazione admin, gestione ruoli, disattivazione account

## F3 · Isolamento dati

- [ ] `[F3][ISOLAMENTO]` Aggiungere owner_id ai voli (colonna, migrazione voli esistenti all'admin)
- [ ] `[F3][ISOLAMENTO]` Assegnare il proprietario in upload/import/scan/reprocess
- [ ] `[F3][ISOLAMENTO]` Filtrare per owner tutte le query e rotte (lista, dettaglio, stats, tags, veicoli, battery-health, compare, mission); admin vede tutto
- [ ] `[F3][ISOLAMENTO]` Bloccare accesso ai voli altrui: check proprietario su get/export/delete/edit e 404/403
- [ ] `[F3][ISOLAMENTO]` Test di isolamento: l'utente A non vede né modifica i voli dell'utente B
- [ ] `[F3][ISOLAMENTO]` Interfaccia: indicatore del proprietario, eventuale filtro/gestione per admin

## F4 · Privacy

- [ ] `[F4][PRIVACY]` Cancellazione account completa con i propri dati (voli, foto, messaggi) + esportazione dati utente
- [ ] `[F4][PRIVACY]` Log di accesso/audit (chi ha fatto cosa, chi ha visto cosa)
- [ ] `[F4][PRIVACY]` Pagina Privacy/Termini + gestione consensi

## F5 · Messaggi e notifiche

- [ ] `[F5][MESSAGGI]` Messaggistica privata tra utenti: invio, conversazioni, sola lettura dei propri messaggi
- [ ] `[F5][MESSAGGI]` Pagina Messaggi + badge non letti e notifiche in-app
- [ ] `[F5][MESSAGGI]` Eliminazione/archiviazione conversazioni e gestione per admin
- [ ] `[F5][NOTIFICHE]` Notifiche email/push per nuovi messaggi, commenti e condivisioni (cross-device)
- [ ] `[F5][MESSAGGI]` Eventuale allegato/menzione di un volo in un messaggio (rispetta l'isolamento)

## F6 · Foto

- [ ] `[F6][FOTO]` Caricamento/gestione foto per volo (backend, storage, validazione, dimensione)
- [ ] `[F6][FOTO]` Galleria nella pagina volo (thumbnails, lightbox, eliminazione singola)
- [ ] `[F6][FOTO]` Integrazione con isolamento: solo il proprietario (o admin) carica/gestisce le foto
- [ ] `[F6][FOTO]` Copertina del volo e anteprima nelle liste/dashboard
- [ ] `[F6][FOTO]` Eventuale associazione foto al punto del tracciato (timestamp/geotag) e inserimento nella condivisione

## F7 · Sharing e social

- [ ] `[F7][SHARING]` Link pubblico di condivisione per volo (token sicuro, condivisione solo se abilitata dal proprietario, opzione revoca)
- [ ] `[F7][SHARING]` Pagina pubblica di visualizzazione volo (mappa/stats senza login) accessibile dal link
- [ ] `[F7][SHARING]` Pulsanti di condivisione social (WhatsApp, Telegram, X/Twitter, Facebook) con anteprima
- [ ] `[F7][SHARING]` Immagine/anteprima condivisibile (og:image, open graph) e eventuale download GPX dal link pubblico
- [ ] `[F7][SOCIAL]` Commenti/like sui voli condivisi pubblicamente
- [ ] `[F7][SOCIAL]` Gruppi/team: condividere un volo solo con un gruppo specifico

## F8 · Dominio e integrazione

- [ ] `[F8][DOMINIO]` Manutenzione programmata: ore di volo per veicolo, scadenze parti/richiami, avvisi
- [ ] `[F8][DOMINIO]` Export Excel/CSV dei dati aggregati e calendario/timeline dei voli
- [ ] `[F8][INTEGRAZIONE]` API token per upload da script/app radio esterno (auto-upload)
- [ ] `[F8][DOMINIO]` Meteo associato al volo (vento/temperatura storica del giorno del volo)
