# DESIGN — Plug-in web HTML5 (MVP read-only) per immagini high-res nel browser

**Stato:** Proposta — da approvare (2026-08-26). Documento di design che consolida
decisioni già prese e validate in revisione precedente; **nessuna funzionalità
nuova inventata**. Dove testo e codice divergeranno in fase di implementazione,
farà fede il codice.

**Vincoli:** Python 3.10+ · plug-in (TUI e backends a modifiche minime) ·
il web è **reader** (mai scrittore) · frontend HTML5 vanilla · dipendenze web
opzionali (`fastapi`/`uvicorn`) in `requirements-web.txt`, **mai** nel
`requirements.txt` principale.

---

## 1. Contesto

Il client è **multi-protocollo** (Signal / WhatsApp / Telegram) con TUI
**Textual** (`tui/app.py:59` — `class SignalTUI(App, ...)`). Le immagini native
funzionano **solo su kitty** (percorso `ImageSupport.KITTY` e
`KittyRenderer`, `tui/app.py:109`, `tui/images/kitty_renderer.py`): su
**Windows** non esiste un terminale compatibile (niente admin/WSL, tunnel
Cloudflare per raggiungere la macchina), quindi la resa nativa TUI non è
praticabile lì.

**Scelta validata:** un **frontend HTML5 nativo** servito da un web server
interno ed esposto via tunnel Cloudflare → le **immagini high-res native**
vengono mostrate nel **browser**, senza emulare alcun terminale.

## 2. Obiettivi (MVP)

1. Esporre in **sola lettura** contatti, messaggi e **media high-res nativi**
   a un browser (da Windows via tunnel Cloudflare).
2. Mantenere la TUI **pienamente funzionante** e invariata senza il flag web.
3. Non introdurre **nessuna** doppia ingestione/scrittura: un solo processo,
   un solo scrittore del DB, un solo consumatore dello stream.

## 3. Non-obiettivi (esclusi dall'MVP)

- **Invio** messaggi dal web (`POST /api/send`) — fase 2, fuori MVP.
- **Login/UI di autenticazione** — fase 2 (Bearer token fisso nell'MVP).
- **Editing/ricevute/typing/lettura** via web: il web riflette solo lo stato
  persistito; ogni mutazione resta appannaggio della TUI e dei backend.
- **Sostituire la TUI**: il web è un **reader** aggiuntivo, non un secondo
  client a pieno titolo.
- **Qualsiasi modifica al wire** dei backend: nessun protocollo viene toccato.

## 4. Vincoli del committente

1. **Plug-in** — TUI e backends subiscono **modifiche minime**; la TUI resta
   operativa e, senza `--web`, il comportamento è byte-identico a oggi.
2. **Worker interno, stesso processo** — la TUI istanzia il web server come
   **worker interno**:
   - parte con la TUI in `on_mount` (`tui/app.py:262-286`), dietro flag
     `--web` / config `web.enabled`, **default OFF**;
   - muore con la TUI in `on_exit` (`tui/app.py:537-545`), **prima** del
     disconnect dei backend;
   - la TUI mantiene il controllo di stream e DB (lock single-instance
     `/tmp/signal-tui.lock`, `signal_tui.py:23`, acquisito in
     `signal_tui.py:26-50`).

## 5. Architettura (validata)

### 5.1 Nuovo package `web/` (accanto a `tui/`)

| File | Contenuto |
|---|---|
| `web/server.py` | entry: `uvicorn` in **thread dedicato con loop proprio**. `uvicorn ≥ 0.20` salta `install_signal_handlers` quando è fuori dal main thread (requisito per non rompere i signal handler della TUI, `signal_tui.py:174-179`) |
| `web/api.py` | endpoint REST (read-only) |
| `web/ws.py` | push WebSocket (`/ws`) |
| `web/auth.py` | Bearer token + confronto `hmac.compare_digest` |
| `web/static/` | SPA HTML5/JS **vanilla** (chat list + thread + immagini) |
| `requirements-web.txt` | `fastapi` + `uvicorn` — **opzionali**, NON toccano `requirements.txt` |

### 5.2 Il web è un **reader**

- Legge la cache/SQLite con **query granulari** sotto `_DB_LOCK`
  (`backend/db.py:37` — `_DB_LOCK = threading.RLock()`), nello stesso processo.
- **MAI** `_load_cache` full-scan (`backend/db.py:178`), **MAI** lo stato
  condiviso di `app._cache` (`tui/app.py:182`).
- **MAI** istanzia un backend, **MAI** consuma lo stream: i backend e il
  poll thread (`on_mount` → `run_worker(self._poll_worker, ...)`,
  `tui/app.py:268`) restano gli **unici** proprietari di connessioni e scritture.

### 5.3 I 3 problemi della revisione precedente — esiti

| # | Problema | Esito |
|---|---|---|
| 1 | **Doppio consumatore dello stream** (web + TUI sullo stesso SSE) | **CHIUSO**: un solo processo, il web è reader (non tocca lo stream) |
| 2 | **Dedup DB cross-process** (due processi che scrivono lo stesso SQLite) | **CHIUSO**: `_DB_LOCK` è **intra-processo** (`backend/db.py:37`); unico processo scrittore; scritture serializzate da `_DB_LOCK` (poll + UI thread) |
| 3 | **Path traversal in `get_attachment_path`** (`backend/rpc.py:164-175`: `attachment_id` non sanificato) | **Da mitigare**: media endpoint con `Path(dir, aid).resolve().is_relative_to(dir.resolve())` + rifiuto di `..`/assoluti/`tgref:`; fix minimo (~3 righe) anche in `backend/rpc.py:get_attachment_path` (difesa in profondità, nessun impatto sui caller) |

## 6. Componenti e punti di aggancio (file/righe reali)

| Componente | Dove | Ruolo nel design |
|---|---|---|
| Registrazione backend + manager | `tui/app.py:129-146` — `self.manager = BackendManager()`, `register(...)` per Signal/WA/TG | il web **usa** `manager` solo in lettura (`list_contacts`, `get_attachment_path`); non lo istanzia |
| `on_mount` | `tui/app.py:262-286` | punto di **start** del web server (dietro flag) |
| `on_exit` | `tui/app.py:537-545` | punto di **stop** del web server, **prima** del disconnect backend (oggi solo Telegram, `tui/app.py:540-544`) |
| `post_display_hook` | `tui/app.py:327-329` | (riferimento) conferma che il loop Textual è proprietario del frame; il web vive su un **altro** loop/thread |
| Ingestione messaggi | `tui/events.py:41-172` — `_handle_message_event`; `added = ingest(...)` a `tui/events.py:90`, blocco `if added:` a `tui/events.py:93` | **punto unico** dove agganciare il push WS (`put_nowait`) **dopo** `added == True` |
| `_DB_LOCK` / WAL | `backend/db.py:37`; WAL `backend/db.py:144` (`PRAGMA journal_mode=WAL`) | il web legge con query granulari sotto lo stesso lock (WAL consente letture concorrenti) |
| `get_attachment_path` (Signal) | `backend/rpc.py:164-175` — `att_path = SIGNAL_CLI_ATTACHMENTS_DIR / attachment_id` | sorgente del fix anti-traversal (difesa in profondità) |
| `manager.list_contacts` | `backends/manager.py:65-75` | sorgente di `GET /api/contacts` |
| `manager.get_attachment_path` | `backends/manager.py:141-146` | sorgente di `GET /api/media/{proto}/{aid}` |
| Download server LAN-only | `backend/download.py:23-26` (`DOWNLOAD_PORT = 10042`), `_ensure_download_server` `backend/download.py:76-99` | sostituito **per il web** dal media endpoint con `FileResponse` |
| `tgref:` (Telegram) | `backends/telegram.py:57` (`_TGREF_PREFIX`), `get_attachment_path` `backends/telegram.py:153-179` | i riferimenti `tgref:` **non** sono path su disco: il media endpoint li rifiuta come path traversal |

## 7. API MVP (read-only) — su entrypoint backend ESISTENTI

| Endpoint | Implementazione | Note |
|---|---|---|
| `GET /api/contacts` | `manager.list_contacts()` | **copiare** `list(backend.contacts)` prima di serializzare (`backends/manager.py:65-75`): `list_contacts` restituisce già una lista nuova, ma i singoli oggetti `ChatContact` (mutabili) restano condivisi |
| `GET /api/messages?proto&contact_id` | query SQLite granulari sotto `_DB_LOCK` | mai `_load_cache`; filtri per `protocol`/`contact_number` come in `backend/db.py` |
| `GET /api/media/{proto}/{attachment_id}` | `manager.get_attachment_path(proto, aid)` + validazione path + `FileResponse` | sostituisce `backend/download.py` (LAN-only) per il web |
| `WS /ws` | push: `queue.Queue` bounded (~1000) alimentata con `put_nowait` nel punto unico di ingestione (`tui/events.py:41`, **dopo** `added == True` a `tui/events.py:90`); il thread uvicorn la drena (`asyncio.to_thread(q.get)`) e fa fan-out; coda piena ⇒ **drop + metrica** | backpressure confinata al thread web |
| *(fase 2, fuori MVP)* `POST /api/send` | `manager.send_message(...)` instradato dal loop uvicorn | non nell'MVP |

## 8. Autenticazione

- **Bearer token** (env `SIGNAL_TUI_WEB_TOKEN` o config), confronto
  **`hmac.compare_digest`** (costante in tempo).
- Vale per **REST, WS e media** indistintamente.
- Trasporto: tunnel Cloudflare **HTTPS**.
- Il token **mai in query-string** (solo header `Authorization: Bearer …`).
- Login/UI di autenticazione: **opzionale, fase 2**.

La WebSocket API nativa del browser non consente di impostare `Authorization`.
La SPA invia quindi `signal-tui-bearer` e il token codificato base64url come
`signal-tui-token.<token>` nell'header `Sec-WebSocket-Protocol` (mai nella
query-string); il server seleziona il sottoprotocollo neutro, decodifica il token
e lo valida con lo stesso controllo Bearer a tempo costante. I client non-browser
possono continuare a usare `Authorization: Bearer`.

## 9. Lifecycle e robustezza

1. **Start** in `on_mount` (`tui/app.py:262-286`) dietro flag (default **off**).
2. **Stop** in `on_exit` (`tui/app.py:537-545`) **prima** del disconnect backend:
   `server.should_exit = True`, `thread.join(3)`, chiusura connessioni WS.
3. **Bind failure** (porta occupata) ⇒ log + TUI **degradata**, **mai** crash.
4. **Eccezioni nel thread uvicorn** ⇒ isolate (stato "web down"), la TUI continua.
5. **Backpressure WS** ⇒ confinata al thread web (coda bounded ~1000, drop + metrica);
   nessun blocco del poll thread o del loop Textual.

## 10. Sicurezza

- **Path traversal** (`backend/rpc.py:164-175`): mitigazione a due livelli —
  1. media endpoint: `Path(dir, aid).resolve().is_relative_to(dir.resolve())`
     + rifiuto esplicito di `..`, path assoluti e `tgref:`;
  2. fix minimo (~3 righe) in `backend/rpc.py:get_attachment_path` (difesa in
     profondità, nessun impatto sui caller esistenti).
- **Token** solo header Bearer; confronto `compare_digest`; tunnel HTTPS.
- **Superficie esposta**: solo endpoint read-only + WS push; nessun comando
  di mutazione raggiungibile via web nell'MVP.

## 11. Rischi

| # | Rischio | Mitigazione |
|---|---|---|
| R1 | Doppio consumatore dello stream | **Chiuso** in design: un solo processo, web reader |
| R2 | Dedup/scritture cross-process sul DB | **Chiuso** in design: `_DB_LOCK` intra-processo, unico processo scrittore; scritture serializzate da `_DB_LOCK` (poll + UI thread) |
| R3 | Path traversal nei media | Mitigato (§10): validazione `resolve().is_relative_to` + fix in `rpc.py` |
| R4 | Regressione TUI senza `--web` | Flag default **off**; percorso esistente invariato (criterio di accettazione A1) |
| R5 | Thread uvicorn che blocca lo shutdown | `should_exit=True` + `join(3)` con timeout in `on_exit` |
| R6 | Esaurimento coda WS (raffiche di messaggi) | Coda bounded (~1000), `put_nowait` + drop con metrica |
| R7 | Uvicorn che interferisce coi signal handler della TUI | `uvicorn ≥ 0.20` (skip `install_signal_handlers` fuori dal main thread) |

## 12. Piano di implementazione (chunk)

| Chunk | Deliverable |
|---|---|
| **1** | Package `web/` + worker lifecycle in TUI (start `on_mount`, stop `on_exit`) + fix path traversal |
| **2** | REST (contacts/messages/media) + WS push |
| **3** | SPA HTML5 minima (chat list + thread + immagini high-res) |
| **4** | Tunnel Cloudflare + auth + test end-to-end da Windows |

## 13. Criteri di accettazione (MVP read-only)

1. La **TUI funziona invariata** senza `--web` (**zero regressioni**).
2. Con `--web`, il browser (da **Windows** via tunnel) mostra contatti,
   messaggi e **immagini high-res native**.
3. **Nessuna** doppia ingestione/scrittura; media **protetti** da path traversal.
4. **Shutdown pulito**: il web muore con la TUI (prima del disconnect backend).
