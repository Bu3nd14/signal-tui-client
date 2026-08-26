# Delta BUGS.md — Review 26/08/2026

> Seconda revisione indipendente sul codice `master` al 26/08/2026.
> Metodo: rivalidazione dei 34 bug APERTI contro il codice reale, caccia a nuovi
> bug, aggiornamento dei riferimenti file:riga (quasi tutti driftati).
> Prodotto da: architetto-2 (seconda opinione). Documento aggiuntivo: non
> sostituisce né modifica `BUGS.md`.
> **Validazione (26/08/2026):** validato da `architetto` — esito **APPROVATO CON
> RISERVE**. Correzioni applicate al presente documento: #66 riclassificato da
> bug a hardening/igiene (scenario bloccato da `_ensure_contact_selectable`),
> aggravante aggiunta al #65 (falso stato di salute in status bar), riferimenti
> riga affinati (rpc.py:283-289, chat_view.py:1065, emoji_picker.py:45-48,
> chat_view.py:1168, signal_tui.py:23-50), miglioria "Testing" riformulata,
> esempi di drift estesi (#43, #49, #52, #53, #54, #62).

## 1. Nuovi bug (non tracciati in BUGS.md)

> Nota: dopo la validazione dell'architetto, il #66 è stato riclassificato da bug
> a voce di hardening/igiene (scenario non raggiungibile nei flussi attuali).
> I nuovi bug effettivi sono quindi **#65 e #67**.

| # | Severità | Titolo | Riferimento file:riga | Impatto |
|---|----------|--------|----------------------|---------|
| #65 | Medio | Fallimento di bind del server webhook inghiottito: `ensure_webhook_server` cattura `OSError` e ritorna `0`; l'unico chiamante ignora il valore di ritorno | `backend/webhook.py:94-101`; `tui/backend_connect.py:230, 236` | Porta 8088 occupata o non bindabile (seconda istanza, conflitto, permessi): la ricezione WhatsApp realtime muore in silenzio. **Aggravante (validazione architetto):** a `backend_connect.py:236` la status bar dichiara `✅ WAHA: N contatti (webhook :8088)` attivo anche a bind fallito → **falso stato di salute**, non solo assenza di segnalazione. Stessa famiglia operativa del drift #46. Fix: propagare/loggare il fallimento, mostrare lo stato webhook degradato nella status bar e non dichiarare il webhook attivo |
| #66 | Igiene/Hardening (non bug attivo) | `manager.get(...)` senza None-guard nella selezione contatto | `tui/contacts.py:789-791`; guardia: `tui/contacts.py:699-701` | **Riclassificato dopo validazione architetto:** lo scenario `manager.get()` → `None` → `AttributeError` è bloccato a monte da `_ensure_contact_selectable` (contacts.py:699-701, testato in `tests/test_open_or_create.py:103-108`) e i backend non sono mai deregistrati a runtime. Il None-guard resta lecito come difesa-in-profondità (costo zero), da accorpare al fix #53. Nota: il rischio reale sulla stessa riga è l'**eccezione `requests.*` non gestita** in `mark_read_sync` quando WAHA muore con backend registrato — sovrapposizione con #43 |
| #67 | Minore | Lock single-instance TOCTOU: check-then-act non atomico su `/tmp/signal-tui.lock`, PID-reuse, posizione world-writable | `signal_tui.py:23-50` | Due avvii concorrenti possono entrambi superare il check (`os.path.exists` → leggi pid → `kill(0)` → scrivi) e girare in parallelo (doppio daemon HTTP, doppia coda, DB condiviso). Un PID riciclato blocca l'avvio legittimo. Qualsiasi eccezione → lock ignorato. Fix: `open(LOCK_FILE, 'x')` / `flock` invece di check-then-act (`flock` preferibile: auto-rilascio su crash e gestione stale lock) |

## 2. Bug in BUGS.md non più validi o da aggiornare

Nessun bug APERTO risulta interamente risolto (lo stato del tracker è corretto).
Rilevati però: (a) un bug già segnato RISOLTO confermato tale con evidenza;
(b) drift massivo dei riferimenti riga su tutte le voci.

| # BUGS.md | Stato attuale rilevato | Evidenza (file:riga) | Azione consigliata sul tracker |
|-----------|------------------------|----------------------|-------------------------------|
| #44 | RISOLTO — confermato sul codice: `_update_message_id` aggancia UNA riga con finestra echo e tie-break deterministico; `_dedup_messages_by_id` ha la guardia difensiva sui timestamp divergenti (logga e NON cancella) | `backend/db.py:311-361` (subquery `ABS(timestamp-?)<=window`, `LIMIT 1`, `ORDER BY ABS(timestamp-?), rowid`); `backend/db.py:614-694` (CTE con `(max_ts-min_ts)<=window` su `rn>1`) | Nessuna azione: stato già corretto. Aggiornare i riferimenti (da `db.py:261-289, 531-579`) |
| Tutti i 34 APERTI | Riferimenti riga obsoleti (drift sistematico) | Esempi: #28 era `signal.py:366-400,605-608` → oggi `signal.py:612-645` + `envelope_to_event` drop a `signal.py:977-979`; #33/#45/#6 erano `signal.py:1004-1088,761-782` → oggi `signal.py:1204-1255`; #51 era `db.py:104-142` → oggi `db.py:133-176`, dedup-in-load a `db.py:189-190`, cache Signal non filtrata a `signal.py:481-483`; anche #43 (`chat_view.py:653` → ~1065), #49 (`send.py:240-244,316-319` → più avanti nel file), #52 (`telegram.py:189-204,216-228` → 230-245/249-257; `app.py:262-269` → 537-545), #53 (`tui/send.py:117-119` → 156-158; `tui/backend_connect.py:209-259` → 254+), #54, #62, #49 | Refresh bulk dei riferimenti in una passata dedicata; il contenuto delle descrizioni resta valido |

Conferme di stati speciali già corretti nel tracker:
- **#40 (WA WON'T FIX)**: confermato — subscription presence disabilitata by default via env `WAHA_PRESENCE_SUBSCRIBE`, commento esplicito "lavoro inutile su WEBJS" (`backends/whatsapp.py:904-920`). WAD/WON'T FIX giustificato.
- **#18 (WAD)**: confermato — `_clean_download_dir()` a ogni publish (`backend/download.py:102-129,175-179`), comportamento voluto.
- **#56**: ANCORA VALIDO — `--receive-mode on-connection` confermato (`backends/signal.py:283-284`), `connect()` fa solo `sendSyncRequest` best-effort (`signal.py:320-334`), `receive()` RPC inusato dalla TUI (solo `poll_once` svuota la coda SSE, `signal.py:1273-1288`). Nessun backfill.

## 3. Bug parzialmente validi (mitigati ma problema di fondo presente)

| # BUGS.md | Cosa è stato mitigato | Cosa resta | Evidenza |
|-----------|----------------------|------------|----------|
| #52 | Nel percorso pairing QR il vecchio event loop viene ora fermato prima di crearne uno nuovo (`call_soon_threadsafe(self._loop.stop)` + close) — niente accumulo tra tentativi di link successivi | `disconnect_sync` NON ferma mai il loop (`run_forever` senza stop): su reconnect/Ctrl+L il `join(timeout=5)` scade e il thread zombie resta → leak su ogni riconnessione. Fan-out rubbrica ancora seriale per-future (25 s × N). `on_exit` disconnette solo Telegram: webhook/download server mai fermati, `disconnect_all()` inusato | Stop loop solo QR: `backends/telegram.py:1396-1402`; leak: `telegram.py:230-245` + `:334-340`; fan-out: `backends/manager.py:99-113` (`future.result(timeout=25)` in serie); on_exit: `tui/app.py:537-545` |
| #54 | Mitigazioni pregresse già note al tracker (reload token, clear path) | `_add_load_more_widget` monta comunque `Button(id="load-more-msg")` a id fisso senza query/remove preventivo; il secondo `_mount_window` senza `_clear_chat` intermedia riproduce il `DuplicateIds` | `tui/chat_view.py:1163-1168` (id fisso a 1166, mount a 1168); chiamante senza guardia: `tui/chat_view.py:963` |
| #62 | Header/footer attuali a 1 riga: nessun impatto oggi | Margini nativi ancora costanti hardcoded (=1), non derivati dal layout reale | `ui_components.py:915-916`, uso a `:980,990,993` |
| #34 | (conferma della valutazione del tracker del 25/08) | Finalizzazione ancora dipendente dal match DB; identità euristica a finestre invariata in `_merge_backend_cache._find_existing` (finestre ±5 s / 10 min duplicate rispetto ai backend) | `tui/chat_view.py:992-1035,1058-1063` |

## 4. Migliorie di architettura/design/refactoring

| Area | Descrizione | Beneficio atteso | Complessità |
|------|-------------|------------------|-------------|
| Stati messaggi | Centralizzare la tabella rank in `models.py`: oggi duplicata ≥7 volte (SQL CASE in `db.py:462,500,543,672`; dict in `rpc.py:283-289`; `_STATUS_RANK` in `tui/backend_connect.py:21`; `rank` in `whatsapp.py:1774-1780`) | Elimina drift tra layer (rischio diretto per #49 e per ogni nuova transizione); un solo punto di evoluzione (es. stato "played") | Bassa |
| Identità messaggi | Unificare la logica di identità/dedup: tre implementazioni affini (`_message_already_cached` in `signal.py:1004-1029`; `_find_existing` in `chat_view.py:992-1035`; equivalente WhatsApp) con finestre e chiavi leggermente diverse | Riduce i falsi dedup/perdite (cluster C/D); contratto unico testabile; facilita il fix #34 | Media-Alta |
| Concorrenza | Introdurre un single-writer per la mutazione di cache+DB+UI (producer enqueuono, un consumer applica) al posto dei lock puntuali assenti | Root cause dei cluster B/D; elimina le race check-then-act (#43) strutturalmente, non a pezzi | Alta |
| API backends | Contratto sync-first dichiarato: `*_sync` primari, wrapper async generici via `asyncio.to_thread`; allineare `get_pairing_qr` (oggi async in `base.py:188`, sync in `telegram.py:1375`, async in `whatsapp.py:422`) | Chiude #48; elimina il doppio modello mentale per contributori/agenti | Media |
| Config | Helper unico `_get(key)` con precedenza `os.environ → config.json → .env → default` in `backends/config.py` (oggi `_load_dotenv` usato solo a `:149,224,242`; le altre chiavi leggono solo env a `:99,181,196`) | Chiude #46; elimina la trappola docker-compose | Bassa |
| Persistenza | `_init_db()` una tantum per processo (oggi chiamato all'inizio di ogni funzione: `db.py:189,335,367,394,414,...`) + connessione singola per operazione; spostare `_dedup_messages_by_id` fuori da `_load_cache` (`db.py:190`, full-scan a ogni boot ×3) | Chiude il grosso di #51; boot O(1) anziché O(N) scan ripetuti | Media |
| Lifecycle | `on_exit` completo: `manager.disconnect_all()` + shutdown server webhook/download (oggi solo Telegram a `app.py:537-545`); `concurrent.futures.wait(..., timeout=25)` complessivo al posto dei result seriali (`manager.py:104`) | Completa #52; shutdown deterministico, portabile a test/embedded | Bassa-Media |
| Kitty media | Controller unico di placement/emissione (placement geometry, restore cursore, margini da layout, transmit fuori dal thread UI, LRU dati) al posto della reconciliation post-frame sparso tra `app.py:380-455` e `ui_components.py` | Root cause del cluster E (#58-#64); una sola fonte di verità geometrica | Alta |
| Sicurezza processi | Lock file con `open(...,'x')`/`flock` e percorso in XDG runtime dir (chiude #67); far fallire rumorosamente il bind webhook (chiude #65) | Robustezza operational a costo minimo | Bassa |
| Testing | Test di regressione per il doppio remount di `_mount_window` senza `_clear_chat` intermedia (#54: oggi solo test sul mount singolo, `tests/test_refresh_chat.py:753-790`, nessuno sul doppio mount); test che `mark_read_sync` con REST WAHA down non propaghi eccezioni `requests.*` nell'handler di selezione (il caso "backend assente" è già coperto da `tests/test_open_or_create.py:103-108`) | Le due regressioni emerse dai log runtime non hanno oggi alcuna copertura specifica | Bassa |

## 5. Verdetto complessivo

**Salute del progetto:** buona discipline di tracciamento (BUGS.md è accurato nei
contenuti, con cluster/radici corrette) ma debito strutturale concentrato in tre
aree che il tracker stesso identifica bene e che questa review conferma integralmente:

1. **Sicurezza (cluster A)** — i due daemon HTTP restano esatti come descritti:
   `download.py:91-93` e `webhook.py:95-97` bindano su `0.0.0.0` con `TCPServer`
   vanilla, zero auth, zero timeout, payload illimitato (`webhook.py:43-44`),
   ack-before-persist. È la priorità #1: il fix è contenuto (loopback default +
   ThreadingTCPServer + tetto Content-Length + token HMAC) e sblocca #26/#27/#42/#65 insieme.
2. **Perdita dati (cluster C)** — #44 è davvero risolto (verificato); restano #28
   (drop silenzioso a `signal.py:977-979`), #56 (nessun backfill, confermato) e
   l'igiene #51: sono i rischi maggiori per l'utente finale.
3. **Ownership/concorrenza (cluster B)** — nessun cambiamento strutturale dall'ultima
   review: shared-dict tra cache backend/UI confermato (`chat_view.py:1065`, append
   senza copia a 1057-1065),
   REST sincrono sul thread UI confermato (`contacts.py:789-791`), hot-loop del
   poll worker confermato (`polling.py:93-98`), race SSE confermata
   (`signal.py:1204-1234`, `restart_sse` ancora senza chiamanti).

**Priorità consigliata:** (1) cluster A con #65 incluso; (2) #28 (piccolo,
autonomo, chiude la seconda via di perdita Signal); (3) refactor listener SSE
(#45+#33+#6 con generation token); (4) refresh bulk dei riferimenti riga del
tracker (drift sistematico che rallenta ogni futura verifica).

**Novità positive rilevate:** fix #44 completo e difensivo; stop-loop nel percorso
QR Telegram (prima mitigazione concreta al #52); indice alias emoji ora popolato
in `_ALIAS_TO_EMOJI` (`emoji_picker.py:45-48`) — resta solo da usarlo anche in
`search_emoji` e nel picker (#9/#10).
