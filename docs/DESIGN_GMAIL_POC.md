# Design POC — Integrazione Gmail nella Web UI

Data: 2026-10-23 · Stato: proposta da implementare · Ambito: POC single-user, sola lettura/scrittura via IMAP/SMTP.

---

## 1. Obiettivi e non-obiettivi

**Obiettivi (POC)**
1. Accesso Gmail via IMAP/SMTP con App Password (NO OAuth, NO Gmail API).
2. Lettura inbox, apertura email, invio nuova email, risposta (reply), download e upload allegati.
3. Nuova tab "Gmail" nella web UI, senza rompere le tre tab esistenti.
4. Integrazione diretta nel progetto (non plugin), single-account, config da `.env`.
5. Zero DB/migrations. Dipendenze aggiuntive: `nh3` (sanitizzazione HTML) e `tinycss2` (CSS inline con allow-list), installate da `requirements-web.txt`.

**Non-obiettivi (esclusi esplicitamente)**
Threading avanzato, ricerca server-side, label/cartelle multiple, notifiche/push, sincronizzazione bidirezionale, multi-account, OAuth/refresh-token, bozze, firma HTML, calendario. La cartella è fissa `INBOX` (parametrizzabile ma non gestita come UI).

---

## 2. Architettura

### 2.1 Componenti e confini

```
┌─ Browser ──────────────────────────────────────────────────────────┐
│  app.js (chat Signal/WA/TG, invariato)    gmail.js (vista dedicata)│
│  index.html: + tab Gmail (data-view) + #gmail-view + gmail.css     │
└───────────────┬───────────────────────────────────┬────────────────┘
                │ Bearer token                      │ Bearer token
┌───────────────▼───────────────────────────────────▼────────────────┐
│ web/auth.py   middleware su /api/*  (INVARIATO, copre /api/gmail/*)│
│ web/api.py    router chat (INVARIATO)                               │
│ web/gmail_api.py  ── create_gmail_router()  (NUOVO, /api/gmail)    │
│        │ asyncio.to_thread (event loop MAI bloccato)                │
│ web/gmail.py ── trasporto IMAP/SMTP + parsing MIME (sync, no FastAPI)│
│ protocols/config.py ── getter Gmail (NUOVI) + _load_dotenv/_load_config│
└───────────┬─────────────────────────────────────────────────────────┘
            │ TLS obbligatorio
    imap.gmail.com:993 (SSL)    smtp.gmail.com:587 (STARTTLS)
```

### 2.2 Decisioni architetturali

| # | Decisione | Motivazione |
|---|-----------|-------------|
| D1 | Servizio in **`web/gmail.py`** (trasporto+parsing, sync, senza FastAPI) + **`web/gmail_api.py`** (router REST) | Separa trasporto IMAP/SMTP dall'API layer; i router attuali vivono in `web/`, le credenziali in `protocols/config.py`. `web/api.py` (100KB) resta intatto. |
| D2 | **Config in `protocols/config.py`** (nuovi getter) che riusano `_load_dotenv()`/`_load_config()` | Coerente con WAHA/Telegram; unica fonte di verità dei segreti. |
| D3 | **Connessione-per-richiesta** (open→usa→close in `finally`), niente pool | Thread-safety banale: ogni richiesta ha la sua connessione, nessuno stato condiviso. Costo di handshake accettabile per single-user. |
| D4 | Tutte le chiamate bloccanti via **`asyncio.to_thread`** (Python 3.12, verificato disponibile) | Non blocca l'event loop uvicorn (stesso pattern già usato in `api.py`/`uploads.py`). |
| D5 | **Timeout** via `socket.setdefaulttimeout`/`timeout=` sui costruttori (30s default, override env) | Evita richieste appese. |
| D6 | **TLS obbligatorio**: IMAP `IMAP4_SSL` (993), SMTP `starttls()` (587). Nessun fallback plaintext | Sicurezza: credenziali mai in chiaro. |
| D7 | **HTML email sanitizzato server-side con `nh3` + `tinycss2`**, più `text` come fallback. Allow-list di tag/attributi; CSS inline sicuro preservato (allow-list proprietà/valori, niente `url()`/`position`/`var()`/`calc()`), mentre `<style>`, classi/id, script, iframe, CSS esterno ed event handler sono rimossi. Attributi tabellari legacy preservati; link solo `http`/`https`/`mailto` con `rel="noopener noreferrer"` e URL relativi scartati. Le immagini vengono rinviate (`data-src`, niente `src`) e caricate solo dal bottone **Mostra immagini**. Il client può commutare HTML/Testo. | Rendering più fedele senza XSS/tracking automatico: l'HTML è controllato dal mittente e il Bearer token sta in `localStorage`. L'assenza di `<style>`/classi può ridurre la fedeltà sulle email che dipendono da CSS esterno. Vedi `DESIGN_GMAIL_HTML_PHASE1.md`. |
| D8 | **Identificazione messaggi: `UID` + `UIDVALIDITY` + `folder`**. Il client passa `uid` e `folder`; la risposta porta `uidvalidity` (il client la confronta, la usa per invalidare stato locale). | UID è la chiave stabile IMAP; UIDVALIDITY rileva i reset di folder. |
| D9 | **Reply/threading**: `In-Reply-To` + `References` dal `Message-ID` dell'originale; **quoting** plain-text con prefisso `> `. | Threading corretto senza DB. |
| D10 | **Nessuna persistenza/cache** (IMAP è la fonte). | POC: niente DB, niente migrations. |
| D11 | **Upload allegati: validazione dedicata leggera** (size + `sanitize_filename`), NON `web/uploads.py`. | `uploads.py` fa sniff magic-bytes per media e rifiuterebbe file generici (.txt/.csv); Gmail deve accettare qualsiasi file. Riutilizzo invece `filename_utils.sanitize_filename`. |
| D12 | **Frontend: vista dedicata `gmail.js` + `#gmail-view`**, tab Gmail con `data-view="gmail"` (NON `data-protocol`). | Evita di forzare Gmail nel modello "chat" di `app.js` (PROTOCOLS/`protocolFilter`): nessun refactor. Vedi §5. |
| D13 | **Lista: `BODY.PEEK[HEADER]` only** (niente corpo/allegati). `snippet` resta vuoto; `has_attachments` è un hint da `Content-Type`. | Il primo `BODY.PEEK[]` scaricava corpo e allegati di 50 email: su 25k messaggi ~5,5s e forte pressione sul GIL (TUI/web lenti). Con gli header la risposta scende a ~15KB e ~2s. Il corpo completo si scarica solo aprendo il messaggio. |
| D14 | **Ordine per data su finestra di 200 UID**, paginazione a `offset` (la UI pagina localmente). | Gmail IMAP non supporta `SORT` (`UID SORT` → `BAD`, verificato live). Si prendono i 200 UID più alti, si ordinano per `Date` desc (tie-break UID) e si pagina in memoria. `total`/`has_more` valgono sulla finestra, non sull'intera casella. |
| D15 | **Delete = spostamento nel Cestino** (`UID COPY` verso `\Trash`, poi `\Deleted` + `EXPUNGE`). | Gmail non espone `MOVE`/`UIDPLUS`; il COPY+EXPUNGE evita la cancellazione permanente. `EXPUNGE` non è per-UID: può espungere altri messaggi già marcati `\Deleted` nella cartella selezionata. |

---

## 3. Configurazione e segreti

Variabili in `.env` (stesso parsing di WAHA/Telegram):

| Var | Default | Note |
|-----|---------|------|
| `GMAIL_ADDRESS` | — | obbligatoria (es. `user@gmail.com`) |
| `GMAIL_APP_PASSWORD_FILE` | — | **preferita**: file con permessi `600` (es. `~/.config/signal-tui/gmail-app-password`) |
| `GMAIL_APP_PASSWORD_CMD` | — | alternativa: comando che stampa il segreto (es. `gpg --decrypt`, keyring) |
| `GMAIL_APP_PASSWORD` | — | fallback legacy in chiaro (sconsigliato; utile solo per test/CI) |
| `GMAIL_IMAP_HOST` | `imap.gmail.com` | opzionale |
| `GMAIL_IMAP_PORT` | `993` | opzionale |
| `GMAIL_SMTP_HOST` | `smtp.gmail.com` | opzionale |
| `GMAIL_SMTP_PORT` | `587` | opzionale |
| `GMAIL_FOLDER` | `INBOX` | opzionale, validata (charset sicuro) |
| `GMAIL_TIMEOUT_S` | `30` | opzionale |

- `gmail_enabled()` → `True` solo se `GMAIL_ADDRESS` e una delle fonti del segreto non sono vuote.
- Precedenza del segreto: `GMAIL_APP_PASSWORD_FILE` → `GMAIL_APP_PASSWORD_CMD` → `GMAIL_APP_PASSWORD`. Il file `.env` va tenuto a `600`.

### Onboarding

- `python3 scripts/gmail_setup.py` (o `./install.sh --gmail`) chiede email + App Password, verifica IMAP/SMTP, scrive il segreto in `~/.config/signal-tui/gmail-app-password` (600) e aggiorna `.env`.
- Funziona anche su un checkout esistente/forkato dopo `git pull`, senza reinstallare.
- La tab Gmail "non configurato" mostra il comando da eseguire; la password non passa dal browser.
- Dopo il setup si può cifrare con `scripts/gmail_gpg_init.sh` o passare a TPM con `scripts/gmail_tpm_init.sh`.

### GPG (opzionale, per cifratura a riposo)

- `scripts/gmail_gpg_init.sh` cifra il file segreto con GPG simmetrico AES-256, elimina il plaintext con `shred` e imposta `GMAIL_APP_PASSWORD_CMD`.
- `scripts/gmail_gpg_unlock.sh` scalda la cache di `gpg-agent` dopo un reboot o alla scadenza della cache (default 10 minuti; aumentabile in `~/.gnupg/gpg-agent.conf`).
- Con cache fredda e nessun TTY, `gpg` non può chiedere la passphrase: il comando fallisce e Gmail risulta non configurato finché non si esegue lo script di unlock.

### TPM2 (Linux, automatico)

- `scripts/gmail_tpm_init.sh` migra il segreto dal file GPG a una credenziale TPM2 (`systemd-creds encrypt --with-key=tpm2`), verifica il decrypt e aggiorna `.env` con `GMAIL_APP_PASSWORD_CMD=systemd-creds decrypt --name=gmail-app-password <file.cred> -`.
- Prerequisito: `sudo usermod -aG tss $USER` e nuovo login (accesso a `/dev/tpmrm0`).
- Nessuna passphrase e nessun prompt: il decrypt è automatico finché il TPM e lo stato di boot (PCR 7 di default) sono gli stessi. Dopo aggiornamenti di firmware/Secure Boot che cambiano PCR 7, va rigenerata la credenziale.
- Il file GPG resta come backup cifrato; il plaintext non viene mai scritto su disco durante la migrazione (pipe GPG → `systemd-creds`).
- Validazione **lazy** (alla prima richiesta) ma `status` riporta `configured` senza contattare la rete.
- **Mai** esporre la password in risposte API/log; log di errore redattati (no traceback con credenziali).

---

## 4. Contratto API REST `/api/gmail/*`

Tutti gli endpoint sono **già protetti** dal middleware Bearer (path `/api/*`). Errori: `{"detail": "..."}` con status coerenti (400 input, 401 auth, 404 non trovato, 413 troppo grande, 502 errore upstream IMAP/SMTP, 503 non configurato). Le risposte JSON non contengono mai credenziali.

| Metodo | Path | Request | Response (rilevante) |
|--------|------|---------|----------------------|
| GET | `/api/gmail/status` | — | `{configured, address, folder, connected, limits}` (password mai inclusa) |
| GET | `/api/gmail/messages` | query `folder`, `offset` (def. 0), `limit` (def. 50, max 200) | `{uidvalidity, folder, offset, limit, window, total, has_more, messages:[{uid, from, to, subject, date_ts, snippet, has_attachments, seen, flags}]}` — ordinati per data desc; `snippet` è `""`; `total`/`has_more` sulla finestra di 200 |
| GET | `/api/gmail/messages/{uid}` | query `folder` | `{uid, from, to, cc, subject, date_ts, message_id, in_reply_to, references, text, html, attachments:[{index, filename, content_type, size, content_id}]}` — `html` è già sanitizzato |
| GET | `/api/gmail/messages/{uid}/attachments/{index}` | query `folder` | `FileResponse` (content-type corretto, `Content-Disposition: attachment; filename*=UTF-8''<sanitized>`) |
| POST | `/api/gmail/messages/{uid}/read` | query `folder` | `{ok:true}` (imposta flag `\Seen`) |
| DELETE | `/api/gmail/messages/{uid}` | query `folder` | `{ok:true, trashed:true}` (sposta nel Cestino Gmail) |
| POST | `/api/gmail/messages/bulk-delete` | JSON `{uids:[...], folder}` (max 200) | `{ok:true, trashed:N, missing:[...]}`; una sola COPY/STORE/EXPUNGE per batch |
| POST | `/api/gmail/send` | multipart o JSON: `to`, `cc`, `bcc`, `subject`, `text`, `reply_to_uid`, `reply_to_folder`, `file[]` | `{ok:true, message_id}` (il `Message-ID` dell'inviato) |

**Regole di validazione / limiti (coerenti col repo)**
- `to`/`cc`/`bcc`: rifiuta `\r`/`\n` (anti header-injection); `From` fisso = `GMAIL_ADDRESS`, mai controllato dal client.
- `subject`: CR/LF rifiutati; `text` max 64KB (`_MAX_TEXT_LENGTH` coerente).
- Allegati in invio: max 10 file, 25MB ciascuno, totale 25MB (limite Gmail); nome file via `sanitize_filename`.
- Download allegato: cap sicurezza 50MB (oltre → 413).
- `folder`: whitelist charset `[A-Za-z0-9._/\- ]`, rifiuto `* % "` (anti iniezione IMAP/list-ref); default `INBOX`.
- Paginazione a `offset`/`limit` sulla finestra di 200 messaggi ordinati per data; la UI pagina localmente (50 per pagina).
- `reply_to_uid` presente → l'endpoint imposta `In-Reply-To`/`References` dal `Message-ID` dell'originale e quota il corpo con `> `.

---

## 5. Frontend — integrazione e componenti

### 5.1 Integrazione a basso rischio (tab bar)
- In `index.html` aggiungo alla tab bar un pulsante **`<button class="protocol-tab gmail-tab" data-view="gmail">Gmail</button>`** (usa `data-view`, NON `data-protocol`).
- `app.js` **non viene modificato**: i selettori `[data-protocol]` e `PROTOCOLS` ignorano la tab Gmail (niente conflitto su classe "active", unread-dot, `protocolFilter`).
- `gmail.js` gestisce autonomamente l'apertura/chiusura della vista:
  - click su `[data-view="gmail"]` → `document.body.classList.add("gmail-active")` (CSS nasconde sidebar+thread-panel, mostra `#gmail-view`), carica lo stato.
  - listener su `#protocol-tabs` per click su `[data-protocol]` (le 3 tab esistenti) → rimuove `gmail-active` e lascia proseguire `app.js` (i due listener sono complementari, ordine di registrazione ininfluente).
- Token/auth: `gmail.js` riusa `apiFetch()` (funzione globale di `app.js`) per avere la stessa gestione 401; fallback a fetch proprio se assente.

### 5.2 Componenti UI (`#gmail-view`)
- **Toolbar**: titolo + stato (`configurato`/`non configurato`/`errore`), bottone "Ricarica", "Scrivi".
- **Lista messaggi** (colonna sinistra, responsive): righe con mittente, oggetto, data, badge allegati e **distinzione letto/non letto** (grassetto + badge "Non letto"; conteggio "N non letti" nel riepilogo), **senza filtro** unread. **Paginazione locale** su una finestra di 200 messaggi ordinati per data (50 per pagina).
- **Lettore** (pannello destro): header (From/To/Date/Oggetto), corpo **HTML sanitizzato** con CSS inline sicuro e attributi tabellari legacy preservati (`<style>`/classi/script/iframe rimossi), toggle **Testo**, **Mostra immagini** on-demand (le immagini remote non vengono caricate automaticamente), link aperti in nuova tab con `noopener noreferrer`, elenco allegati con download.
- **Compose/Reply**: form (To/CC/BCC/Oggetto/Corpo), allegati (file picker), modalità reply (riempie To+Oggetto `Re:`; il quote lo aggiunge il backend). Invio multipart. Pulsante **Elimina** con conferma → Cestino.
- **Selezione multipla**: bottone "Seleziona" a destra del riepilogo; solo allora compaiono le checkbox di riga e il bottone "Elimina" accanto ad "Annulla" (nessuna barra separata). "Annulla" esce dalla modalità selezione e svuota la selezione. Il delete bulk usa un unico endpoint e un solo `EXPUNGE`.
- **Stati**: loading (spinner), errore (banner con messaggio redattato), non configurato (istruzioni `.env`).
- **Responsive**: su mobile lista/lettore a schermo intero con bottone indietro, coerente con `style.css` esistente.

---

## 6. Modifiche file-per-file

**Creare**
| File | Contenuto |
|------|-----------|
| `web/gmail.py` | Trasporto IMAP/SMTP + parsing MIME (funzioni sync, no FastAPI): `imap_connect`, `fetch_message_list`, `fetch_message`, `fetch_attachment`, `send_email`, `mark_read`; validazioni header/folder; strip HTML→text; sanitizzazione HTML (`nh3`) e CSS inline (`tinycss2`). |
| `web/gmail_api.py` | `create_gmail_router()` con gli endpoint §4; wrappa ogni chiamata in `asyncio.to_thread`; mappa eccezioni in `HTTPException` senza leak. |
| `web/static/gmail.js` | Controller/vista dedicata (stato separato, render lista/lettore/compose, allegati, stati). |
| `web/static/gmail.css` | Stili `#gmail-view`, `.gmail-active`, responsive. |
| `tests/test_gmail_config.py` | Getter config, `gmail_enabled()` (env/`.env`/config.json). |
| `tests/test_gmail_parse.py` | Parsing MIME (plain-text, HTML→text, header injection, sanitize filename, quoting). |
| `tests/test_gmail_html.py` | Sanitizzazione HTML: script/event handler, `<style>`, classi, iframe e immagini remote rimossi; link sicuri, tabelle e cap dimensione. |
| `tests/test_gmail_html_phase1.py` | Fase 1 CSS inline: allow-list proprietà/valori, attributi tabellari legacy, immagini differite/firmate, cap anti-DoS e idempotenza. |
| `tests/test_gmail_api.py` | Endpoint con `monkeypatch` di `imaplib.IMAP4_SSL`/`smtplib.SMTP` + `TestClient` (httpx2 già presente). |

**Modificare**
| File | Modifica |
|------|----------|
| `protocols/config.py` | Aggiungere getter Gmail (`get_gmail_address`, `get_gmail_app_password`, host/port/folder/timeout, `gmail_enabled()`). |
| `web/server.py` | `app.include_router(create_gmail_router())` accanto a `create_api_router()` (import da `web.gmail_api`). |
| `web/static/index.html` | Tab `data-view="gmail"` + sezione `#gmail-view` + `<script src="/gmail.js?v=1">` + `<link href="/gmail.css?v=1">`. |
| `.env.example` (opzionale) | Documentare le variabili Gmail. |

---

## 7. Piano di implementazione (task ordinati)

1. **Config** (`protocols/config.py`): getter + `gmail_enabled()` + test config.
2. **Trasporto** (`web/gmail.py`): IMAP connect/list/detail/attachment/mark-read + SMTP send; parsing MIME e validazioni; test parse con messaggi finti.
3. **API layer** (`web/gmail_api.py`): `create_gmail_router()`, `to_thread`, error mapping; test API con mock imaplib/smtplib.
4. **Wiring** (`web/server.py`): include router; verifica smoke che `/api/gmail/status` risponda e che il Bearer protegga.
5. **Frontend** (`index.html`, `gmail.css`, `gmail.js`): tab + vista lista/lettore/compose/reply/allegati/stati.
6. **Verifica manuale** con App Password reale (§9) + `ruff check`/`ruff format`/`pytest`.

---

## 8. Rischi e mitigazioni

| Rischio | Mitigazione |
|---------|-------------|
| **Header injection** (To/Subject) | Validazione esplicita `\r`/`\n` rifiutati; `From` fisso server-side; test dedicati. |
| **XSS via HTML email** | Sanitizzazione server-side con `nh3` + `tinycss2` (allow-list tag/attributi; `<style>`/classi/script/iframe/event handler rimossi; CSS inline ri-serializzato da allow-list proprietà/valori senza `url()`/`position`/`var()`/`calc()`; URL scheme limitati e URL relativi negati); il client parsea l'output già sanitizzato in modo inerte (`DOMParser`). Immagini remote solo su click (tracking esplicito). Toggle Testo disponibile. |
| **Path traversal allegati** | Download per `uid`+`index` (mai path/filename forniti dal client); nome file `sanitize_filename`; niente scrittura su disco da input utente per il download. |
| **Blocco event loop** (IMAP/SMTP bloccanti) | Tutto in `asyncio.to_thread` + timeout socket; connessione-per-richiesta. |
| **Leak credenziali / errori IMAP** | Password mai in risposta/log; errori mappati a 502/503 generici senza traceback con segreti. |
| **App Password non valida / account non configurato** | `status.configured=false` → tab mostra stato "non configurato" senza rompere il resto; errori 503 gestiti. |
| **IMAP `UIDVALIDITY` change** | Il client confronta `uidvalidity` e invalida; nessuna cache persistente nella POC. |

---

## 9. Criteri di accettazione (verificabili)

**Automatici (fast track)**
- `ruff check` e `ruff format --check` passano sui file nuovi/modificati.
- `pytest tests/test_gmail_config.py tests/test_gmail_parse.py tests/test_gmail_html.py tests/test_gmail_html_phase1.py tests/test_gmail_api.py` verde.
- `python -c "import web.gmail_api"` non fallisce e **non** importa FastAPI a livello di `web/gmail.py`.

**Manuali (con App Password reale, account Gmail dedicato)**
1. Con `GMAIL_ADDRESS`/`GMAIL_APP_PASSWORD` in `.env`, la tab "Gmail" appare e `/api/gmail/status` → `configured:true`.
2. La lista inbox mostra messaggi recenti con paginazione; apertura email mostra corpo HTML sanitizzato (CSS inline sicuro se presente) con toggle Testo, e allegati.
3. Download allegato produce file integro con nome sanitizzato.
4. Invio nuova email → appare in Posta inviata; reply a una mail ricevuta → thread corretto (`In-Reply-To`/`References`), quoting `>`.
5. Invio con 1+ allegati → ricevuti integri; allegato >25MB o >10 file → 413/400.
6. Senza `.env` configurato → tab presente ma stato "non configurato", nessun crash delle altre 3 tab.

---

## 10. Note per lo sviluppatore

- Non toccare `web/api.py` né `app.js`: l'integrazione è per addizione (`gmail_api.py`, `gmail.js`, tab `data-view`).
- Il router Gmail è protetto automaticamente dal middleware Bearer di `web/auth.py` (nessun controllo auth da replicare).
- Per i test usare `unittest.mock`/`monkeypatch` su `imaplib.IMAP4_SSL` e `smtplib.SMTP`; nessuna rete nei test.
