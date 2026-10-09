# RED TEAM REVIEW — Rubrica dinamica (`docs/DESIGN_DYNAMIC_ADDRESS_BOOK.md`)

**Reviewer:** adversarial red teamer
**Branch:** `fix/dynamic-address-book`
**Esito:** **RESPINTO** (gate chiuso; re-review richiesta dopo le fix ai BLOCCANTI)
**Metodo:** lettura integrale del design + verifica puntuale sul codice reale (nessuna fiducia nelle affermazioni del design).

---

## 0. Sintesi

Il design identifica correttamente la root cause (cache rubrica senza refresh dopo l'avvio) e la direzione (pipeline unica + 3 trigger) è ragionevole. Ma la pipeline proposta:

1. **non chiude il gap di identità-oggetto** che il refresh stesso introduce: oltre al `display_name`, si rompe l'aggiornamento di `last_message_ts` → ordinamento "recente in alto" perso dopo ogni swap (BLOCCANTE 1);
2. **può distruggere `self.contacts`/`_contacts_by_id` su un errore transitorio** di rete (WA `/chats`, TG `get_dialogs`), violando il contratto dichiarato "nessuna mutazione parziale dannosa" (BLOCCANTE 2);
3. **rompe l'identità degli oggetti Telegram** con l'overwrite `_contacts_by_id` e la perdita di ghost/access_hash (BLOCCANTE 3);
4. **il "timeout 30s/backend" è falso**: `ThreadPoolExecutor.__exit__` attende i task; con Signal subprocess a 120s il refresh worker si blocca molto oltre (BLOCCANTE 4);
5. **nessuna propagazione dei contatti NUOVI alla lista TUI** (contraddice §1.5/§8 caso #6 per Signal);
6. **lazy `force=True` non è scoped** → forza TUTTI i backend (incluso `get_dialogs(200)` Telethon) ogni cooldown (default 60s);
7. **helper inesistenti** (`_is_int`, `_tg_is_placeholder`) nel pseudocodice;
8. **trigger #3 web morto**: il frontend non passa mai `force=true`.

Di seguito le obiezioni ordinate per severità, con evidenza e mitigazione. Chiudo con i bloccanti.

---

## 1. Obiezioni per severità

### [BLOCCANTE 1] Gap di identità anche per `last_message_ts`: ordinamento rotto dopo il refresh
**Evidenza.**
- `_on_backend_ready` (`tui/backend_connect.py:164-174`) inserisce in `self.contacts` (TUI) **gli stessi oggetti** di `backend.contacts`.
- Dopo il refresh: WA `_load_contacts` (`protocols/whatsapp.py:902-904`) e Signal `_set_contacts` (`protocols/signal.py:478-479`) **swappano** `self.contacts` con oggetti NUOVI. Telegram `_load_contacts:762,782-783` idem.
- `_handle_message_event` (`tui/events.py:169-209`): risolve `contact` da `event.payload["contact"]` o `backend._identify_contact(...)` → **oggetto del backend** (`whatsapp.py:937-939`, `signal.py:1141-1149`, `telegram.py:1829-1833`), e aggiorna `contact.last_message_ts` (riga 203-209) **su quell'oggetto**. Se `contact.cache_key` è già presente in TUI (`existing`, riga 185) l'oggetto TUI **non viene riancorato**.
- La lista TUI legge `self.contacts` (oggetti vecchi) in `_sort_contacts`/`_reorder_contact_list` (`tui/contacts.py:54-66`). Risultato: dopo un refresh, i nuovi messaggi **non spostano più in alto** il contatto.
- Nota: `_handle_sent_mirror_event` (`tui/events.py:110-123`) *preferisce già la copia TUI* per gestire esattamente questo gap; `_handle_message_event` **no**.
- Il design §7 chiude solo il `display_name`, non il timestamp. §9.2 non testa il caso.

**Mitigazione.** In `_handle_message_event` adottare lo stesso pattern del sent-mirror: preferire l'oggetto TUI con `cache_key` uguale e, se assente, riancorare l'oggetto backend (`self.contacts[i] = contact`). In alternativa, far sì che il backend **non swappi** gli oggetti ma aggiorni in place (sostituendo il valore nel contenitore senza cambiare identità), oppure emettere `contact_update`/re-merge che porti la lista TUI a riferirsi agli oggetti nuovi. Aggiungere test che, dopo `refresh_contacts_sync`, un `_handle_message_event` aggiorni `last_message_ts` dell'oggetto presente in `app.contacts`.

---

### [BLOCCANTE 2] Il refresh può azzerare i contatti su errore transitorio (mutazione distruttiva)
**Evidenza.**
- WA: `whatsapp_rest.py:292-293` ritorna `None` su errore HTTP, `[]` se l'API risponde vuota. `_load_contacts` (`whatsapp.py:879`) fa `raw_contacts = self._rest.list_contacts() or []` e poi `self.contacts = contacts` / `self._contacts_by_jid = by_jid` (`902-904`). Un `/chats` fallito **azzera entrambe le strutture**.
- TG: `_load_contacts` (`telegram.py:764-770`), su eccezione `get_dialogs`, esegue `self.contacts = []` e `self._contacts_by_id = {}`.
- In entrambi i casi l'eccezione è **catturata internamente**, quindi `refresh_contacts_sync` NON la vede e ritorna `errors=None`; il design dichiara "su errore remoto ... nessuna mutazione parziale dannosa" (§3.2), ma non è così.
- Conseguenze: durante l'outage `_identify_contact`/`find_contact` perdono i contatti, il web group-sender resolver e il send fallback degradano; a ogni ritentativo periodico il problema si ripete.

**Mitigazione.** Distinguere "fetch fallita" da "fetch vuota valida": se `list_contacts()`/`get_dialogs` esce con errore, **non committare** il nuovo contenitore e ritornare un errore esplicito. Aggiungere un guard esplicito `if raw is None: raise/return error` (WA) e non svuotare su eccezione (TG). Test di regressione: errore transitorio → `self.contacts` invariati e `errors` valorizzato.

---

### [BLOCCANTE 3] Telegram: overwrite `_contacts_by_id` rompe identità/access_hash; `_load_contacts` spazza i ghost
**Evidenza.**
- `list_address_book_sync` oggi usa `setdefault` (`telegram.py:1075-1079`): preserva l'oggetto dialogo (con `access_hash`/`read_outbox_max_id`). Il design §4.2a lo sostituisce con **overwrite** con l'oggetto rubrica.
- `_identify_contact` ritorna `_contacts_by_id.get(eid)` (`telegram.py:921-927`) e viene usata per costruire `event.payload["contact"]` (`1554,1648,1744,1771`). Con l'overwrite `_identify_contact` ritorna l'oggetto rubrica, che è **un oggetto diverso** da quello nella lista TUI → stesso problema del BLOCCANTE 1 (ts/identità), in aggiunta all'eventuale perdita di `access_hash`/`read_outbox_max_id` dell'oggetto dialogo.
- `_load_contacts` (`telegram.py:783`) fa `self._contacts_by_id = by_id` **scartando** ogni entry creata da `register_contact` (`929-943`) e ogni ghost (`tui/contacts.py:699-708`, `web` book-only `test_web_send_address_book.py:552-577`). Con il refresh periodico questo diventa ricorrente: il fallback `_resolve_input_entity` (`telegram.py:1110-1118`) perde `access_hash` e un invio a un contatto non-dialogo può fallire.
- Il design liquida la cosa come "stesso pattern esistente (telegram.py:783)" ma 783 è chiamato **solo al connect** oggi; renderlo periodico cambia la semantica.

**Mitigazione.** Non sovrascrivere l'oggetto esistente: aggiornare **in place** `display_name` (e soli campi rubrica) dell'oggetto dialogo già in `_contacts_by_id`; inserire l'entry rubrica solo se l'id è assente (merge, non replace). In `refresh_contacts_sync`, **ripristinare i ghost** dopo `_load_contacts` (es. ri-registrare i contatti presenti in `self.contacts`/TUI con `ghost=True`), oppure non resettare `_contacts_by_id` ma fare merge dei dialoghi sull'indice esistente.

---

### [BLOCCANTE 4] Il "timeout 30s/backend" è falso; il worker refresh può stallare fino a 120s
**Evidenza.**
- `manager.list_address_book_sync` usa `with ThreadPoolExecutor(...) as pool:` (`manager.py:109`). `__exit__` di `ThreadPoolExecutor` chiama `shutdown(wait=True)`: su `future.result(timeout=...)` il `with` **attende comunque** la fine dei task in corso.
- Verificato empiricamente: task da 3s + `result(timeout=0.2)` → ritorna dopo ~3.0s (non 0.2s).
- Signal: `_load_contacts_rpc` (`signal.py:415-420`) su risposta vuota/inattesa **cade nel fallback** `_load_contacts_subprocess`, che usa `_run_subprocess` con `SUBPROCESS_TIMEOUT = 120` (`rpc.py:64,118-136`). Quindi il refresh worker può bloccare ~120s.
- WA/TG: `list_address_book_sync` interno usa `future.result(timeout=25/20)` (`manager.py:116`, `telegram.py:1029-1030`), ma anche qui il task resta in esecuzione. Il design §5 promette "timeout 30s/backend" e "non solleva mai": la prima affermazione è errata.

**Mitigazione.** Non usare il fallback subprocess nel path di refresh dinamico (metodo RPC-only con errore esplicito). Documentare/implementare un vero cap: usare `pool.shutdown(wait=False)` (o `future.cancel()` best-effort + non attendere) e/o timeout interni reali per ogni backend. Rendere il worker refresh annullabile su `stop`.

---

### [ALTA 5] Nessuna propagazione dei contatti NUOVI alla lista TUI (contraddice §8 caso #6)
**Evidenza.** `_handle_contact_update_event` attuale/§7 applica solo a un target già presente (`target = next((c for c in self.contacts ...), None)`; `if target is not None`). Non c'è append. Ma §1.5 dichiara che per Signal la lista principale è la rubrica completa e §8 caso #6 dice "Signal: scoprire contatti nuovi". Il refresh Signal riempie `backend.contacts` ma la TUI non lo sa → i nuovi contatti Signal non compaiono nella lista principale. (Per la web UI il problema è analogo: l'evento non aggiunge nulla.)

**Mitigazione.** In `_handle_contact_update_event`, se il `cache_key` non esiste e l'evento è un "new contact", appendere alla lista TUI (con eventuale filtro per protocollo), come fa `_handle_message_event` per i placeholder. Oppure invocare il merge di `_on_backend_ready` (o un suo estratto) dopo il refresh. Il design deve decidere esplicitamente la semantica "nuovo contatto" vs "rinomina".

---

### [ALTA 6] Lazy refresh con `force=True` non scoped: forza tutti i backend ogni cooldown
**Evidenza.** §6.2 chiama `manager.refresh_contacts_sync(force=True)` senza `protocols`. Il manager supporta lo scoping (`manager.py:89-104`). Quindi un messaggio non risolto su WhatsApp forza ogni 60s anche:
- Signal `listContacts` (con rischio fallback subprocess, vedi BLOCCANTE 4);
- Telegram `get_dialogs(limit=200)` **sul loop Telethon** + `GetContactsRequest`.
Su conversazioni attive questo è refresh full-book ogni minuto, non "costo controllato" (§8 caso #8).

**Mitigazione.** Scope al protocollo del contatto non risolto: `refresh_contacts_sync(protocols={contact.protocol}, force=True)`. Facoltativo: backoff esponenziale per il lazy (non solo cooldown fisso) e/o evitare `force=True` sul lazy (il TTL 300s spesso basta a decidere il refetch).

---

### [ALTA 7] Helper inesistenti nel pseudocodice (`_is_int`, `_tg_is_placeholder`)
**Evidenza.** `rg`/grep sull'intero repo: `_is_int` e `_tg_is_placeholder` compaiono **solo** nel design (righe 227-228), non esistono in `protocols/telegram.py`. Inoltre la condizione `... and not _tg_is_placeholder(c.display_name)` è sospetta: impedisce di aggiornare un nome placeholder con il nome reale della rubrica (l'opposto dell'obiettivo).
**Mitigazione.** Definire la semantica (probabile `_looks_like_placeholder`/`_is_numeric_id`) e invertire il guard così da sostituire i placeholder, non proteggerli. Rimuovere `_is_int` e usare `_to_int`/try-except inline.

---

### [MEDIA 8] Trigger #3 web inefficace: il frontend non passa `force=true`
**Evidenza.** `web/static/app.js:3658` chiama `/api/contacts/book?q=...` senza `force`. `web/api.py:1612-1624` con default `False` continuerà a servire la cache stale. Il design §12 non prevede modifiche al frontend. Il caso #2 (nuovo contatto, scrivo io per primo) resta scoperto per la web UI.
**Mitigazione.** Aggiornare `app.js` a passare `force=true` (o introdurre un refresh book esplicito) e aggiungere test web. In alternativa chiarire che la web resta non coperta e rimuovere il parametro.

---

### [MEDIA 9] Doppio refresh concorrente (picker force vs worker) senza lock per-backend
**Evidenza.** `_address_book_worker` (`tui/pickers.py:89-109`) chiama `manager.list_address_book_sync(force=True)` su worker Textual; il nuovo worker dinamico chiama `refresh_contacts_sync(force=True)` che per WA rientra in `_load_contacts` → `list_address_book_sync(force=True)`. Nessun lock per-backend: due `/contacts/all` concorrenti, doppio swap di `self.contacts`/`_address_book`, letture concorrenti con `handle_webhook` (`whatsapp.py:542-547`).
**Mitigazione.** Lock di refresh per-backend (es. `threading.Lock` non bloccante con skip se già in corso) o coalescing a livello manager. Documentare la politica su chi vince.

---

### [MEDIA 10] Thread-safety Telegram: mutazioni da due contesti (loop e refresh thread)
**Evidenza.** `refresh_contacts_sync` §4.2c esegue `_load_contacts` sul loop (OK) ma poi `list_address_book_sync` (tail `_contacts_by_id`, `_address_book`) e `_apply_book_names_to_contacts` **sul refresh thread**, mutando `self.contacts`/`_contacts_by_id` mentre il loop Telethon può iterarli (`telegram.py:759-784`, `801-846`, `1830`) e `register_contact` (`937-943`) può mutare da altre call.
**Mitigazione.** Concentrare tutte le mutazioni Telegram sul loop (eseguire anche `list_address_book_sync`/apply via `run_coroutine_threadsafe`) o introdurre un `_contacts_lock` anche in Telegram. Non accettare "residuo documentato" come sufficiente per un trigger automatico ogni 60-300s.

---

### [MEDIA 11] Signal: `_set_contacts`/`register_contact` non lockate; refresh e poll worker concorrenti
**Evidenza.** `signal.py:464-479` e `484-494` mutano `self.contacts`/`_contacts_by_key` senza lock; il nuovo refresh le invoca da un thread dedicato mentre il poll worker/SSE le leggono. §11 lo ammette ma lo rinvia ("non richiesto per il MVP"). Con un refresh automatico periodico il rischio diventa strutturale.
**Mitigazione.** Aggiungere `_contacts_lock` a Signal (coerente con WA) o marshalare l'apply sul poll worker.

---

### [MEDIA 12] Razza su `self.contacts` TUI durante l'iterazione nel poll worker
**Evidenza.** `_handle_contact_update_event` §7 itera `self.contacts` con `next((c for c in ...))` sul **poll worker**, mentre il UI thread esegue `self.contacts.sort()` (`tui/contacts.py:54-66`) via `call_from_thread`. `list.sort()` in CPython azzera temporaneamente il contenuto: l'iterazione concorrente può sollevare/saltare elementi. Lo stesso schema esiste già in `_handle_message_event:185`, ma il design aggiunge un secondo punto e §11 lo liquida come "attributo atomico" (falso per l'iterazione).
**Mitigazione.** Eseguire la ricerca/update del target sul thread UI (`call_from_thread`) o fare snapshot `list(self.contacts)` prima di iterare. Mega: rendere `_handle_contact_update_event` una callback UI.

---

### [MEDIA 13] Costo del refresh periodico `force=True` (e semantica TTL)
**Evidenza.** §6.1: ogni 300s `force=True` su tutti i backend, anche ad app inattiva. Poiché `get_address_book_ttl_s()` è 300 (`config.py:311-313`), un periodico con `force=False` sarebbe equivalente in pratica (il TTL è scaduto), senza bypassare atomically la cache. Inoltre il periodico condivide il cooldown col lazy: un lazy a t≈250s fa slittare il tick periodico (`< 60s` → `continue`), degradando la cadenza.
**Mitigazione.** Usare `force=False` per il periodico, disaccoppiare `_address_book_last_refresh` (lazy) dall'interval periodico, e/o allungare l'interval o introdurre backoff. Documentare i costi per account grandi (WAHA `/contacts/all`, Telethon `get_dialogs`).

---

### [MEDIA 14] Lifecycle del worker: stop senza join, start/init order
**Evidenza.** §6.1/§12: start in `on_mount`, stop in `on_exit_app` (`tui/app.py:614`). Non è specificato `join()`; se il worker è bloccato in `refresh_contacts_sync` (fino a 120s, BLOCCANTE 4) lo stop non lo interrompe. Se gli stati del mixin sono inizializzati in `on_mount` e non in `__init__`, un evento precoce può causare `AttributeError` in `schedule_address_book_refresh`. I test con `app_for_test` patchiano `on_mount` (`tests/conftest.py:261-273`), quindi il worker non parte nei test — ma va garantita l'inizializzazione in `__init__`.
**Mitigazione.** Inizializzare gli stati in `__init__`; `stop()` setta il flag, `wake.set()`, e fa `join(timeout=...)` best-effort; rendere il refresh interrompibile.

---

### [MEDIA 15] `new`/`renamed` e `before` letti senza lock; id che cambia Signal (`uuid`→`number`)
**Evidenza.** §4.1b/§4.3 fanno `before = {... for c in self.contacts}` e `new = [...]` fuori da ogni lock. Signal `_parse_and_update_contacts` sceglie `id = number or uuid` (`signal.py:453`): un contatto prima solo-uuid poi con numero cambia `cache_key` → risulta "nuovo" e la TUI non ha target per il vecchio key.
**Mitigazione.** Snapshot sotto lock e/o confronto per identità stabile (`cache_key` calcolato su ACI/number normalizzato). Documentare come viene trattato il cambio id.

---

### [BASSA 16] Il branch `contact is None` del lazy trigger è codice morto
**Evidenza.** In `_handle_message_event` viene sempre costruito un placeholder se `_identify_contact` ritorna `None` (`tui/events.py:176-181`); quindi "contact is None" dopo la risoluzione non è mai vero. La copertura "id ignoto" avviene di fatto via `_looks_unresolved` sul placeholder.
**Mitigazione.** Chiarire/semplificare la condizione; test che verifichi il trigger per id ignoto.

---

### [BASSA 17] Helper/contratto: firme e default
- `_contact_update_event` su base è additivo e ok.
- `AddressBookRefreshResult` ok, ma il design non dice cosa fa il consumer TUI con `errors` (solo log nel worker) → nessuna UX di errore.
- Il design afferma "il default no-op non rompe i test": verificato, `_MinimalBackend` (`tests/test_address_book.py:44-69`) eredita il default; `test_web_contact_update.py` resta compatibile perché `app.contacts` è vuoto in quei test (target `None`).
- `get_dynamic_refresh_interval_s`/`cooldown` seguono il pattern `_get_int` (`config.py:311-313`), ok.

---

## 2. Matrice casi→meccanismo: buchi

| # | Coperto? | Note |
|---|---|---|
| 1 | Parziale | Lazy → refresh, ma l'apply del nome funziona; il ts/ordinamento no (BLOCCANTE 1). |
| 2 | Parziale | TUI sì (picker force). **Web no** (frontend non passa `force`, MEDIA 8). |
| 3 | Sì (lento) | Solo periodico, fino a 300s; accettabile ma documentare. |
| 4 | Sì | `_looks_unresolved` + periodico. |
| 5 | Parziale | Resolver lid esistente; possibili identità stale post-swap. |
| 6 | **NO in lista principale** | `_handle_contact_update_event` non appende i nuovi (ALTA 5); per Signal la lista dovrebbe essere completa. |
| 7 | Sì per design | Fuori scope esplicito; va confermato col committente (il primo messaggio resta col numero). |
| 8 | **Debole** | Lazy force non scoped (ALTA 6) + periodico force non necessario (MEDIA 13). |

---

## 3. Bloccanti (devono essere risolti prima dello sviluppo)

1. **BLOCCANTE 1** — gap identità `last_message_ts` (ordinamento) dopo lo swap: riancorare l'oggetto TUI in `_handle_message_event` o non swappare gli oggetti.
2. **BLOCCANTE 2** — WA/TG non devono committare un fetch fallito (azzeramento contatti): distinguere errore da vuoto e ritornare `errors`.
3. **BLOCCANTE 3** — Telegram: niente overwrite distruttivo di `_contacts_by_id`; preservare oggetti/`access_hash`/ghost; merge non replace.
4. **BLOCCANTE 4** — il timeout del manager non esiste: eliminare il fallback subprocess da 120s dal path refresh e implementare un cap reale (shutdown non bloccante / cancel).

## 4. Condizioni per il via libera (dopo le fix)

- Scope del lazy per protocollo + backoff (ALTA 6).
- Propagazione nuovi contatti alla lista TUI con semantica esplicita (ALTA 5).
- Definire gli helper mancanti e correggere il guard placeholder Telegram (ALTA 7).
- Wiring `force=true` nel frontend web o rimozione del parametro (MEDIA 8).
- Lock per-backend / marshal sul thread giusto per Telegram e Signal (MEDIA 9-11).
- Snapshot di `self.contacts` nei handler per evitare race di iterazione (MEDIA 12).

---

*Report generato in modalità red team. Nessun codice di produzione modificato.*

---

# RE-REVIEW v2 (design 716 righe)

**Esito:** **APPROVATO CON RISERVE**
**Metodo:** verifica delle 17 obiezioni contro il codice reale; nessuna fiducia nelle dichiarazioni.

## A. Stato dei 4 bloccanti (verificato sul codice)

| Bloccante | Stato | Evidenza verifica |
|---|---|---|
| **B1** identità/`last_message_ts` | **CHIUSO** | WA `_merge_contacts_in_place` conserva gli oggetti esistenti (§4.1b); TG `_load_contacts_merge` idem (§4.2c); Signal merge keyed su `cache_key` (§4.3). Il re-anchor in `_handle_message_event` (§7.1, speculare a `_handle_sent_mirror_event:116-123`) chiude il gap per gli oggetti ricevuti dal payload/`_identify_contact`. Nota: §3.2 dice "non sostituisce mai la lista" ma il codice fa `self.contacts = kept` — l'invariante vera è l'identità-oggetto, che è rispettata. |
| **B2** wipe su errore | **CHIUSO** | `whatsapp_rest.list_contacts()` ritorna `None` su trasporto e `[]` su API viva (`whatsapp_rest.py:292-295`), verificato; il refresh controlla `raw is None` e non committa (§4.1a). TG `_load_contacts_merge` lascia propagare `get_dialogs` → `errors`, nessun commit (§4.2c). |
| **B3** TG overwrite/ghost/access_hash | **CHIUSO** | `_apply_book_to_contacts` usa merge: insert solo se `existing is None`, aggiorna l'oggetto dialogo in place, `access_hash` fill-if-missing (`telegram.py:253-261`); ghost preservati (`_load_contacts_merge:286-287`). Guard placeholder corretto (non più invertito). |
| **B4** timeout falso / subprocess 120s | **CHIUSO** | `_rpc._call` è bounded 30s e **non solleva** (`rpc.py:356-361`); Signal refresh è RPC-only, nessun `_load_contacts_subprocess` (§4.3). Manager `pool.shutdown(wait=False, cancel_futures=True)` (`cancel_futures` supportato: Python 3.12) → non blocca. |

## B. Alte verificate

| # | Stato | Note |
|---|---|---|
| 5 (append nuovi) | CHIUSO | `_handle_contact_update_event` appende quando `target is None` e contact presente (§7.2). Verificato che i mention risolti **non** passano da `_apply_contact_lid_resolution` (`whatsapp.py:2470-2471`, solo `kind=="contact"`) → nessun inquinamento della lista da parte di membri di gruppo. |
| 6 (lazy scoped) | CHIUSO | `schedule_address_book_refresh(protocol)` + `protocols={protocol}` (§6.2/§6.4). |
| 7 (helper) | CHIUSO con riserva | `_is_int`/`_to_int`/`_tg_is_placeholder` definiti e guard corretta. **Ma §4.3 usa `self._parse_contact(c)` che NON esiste** (grep: nessun `_parse_contact` in `protocols/`): serve estrarre il parsing da `_parse_and_update_contacts:451-461` o dichiararlo. |

## C. Riserve residue (attuabili)

- **R1 (ALTA, da implementare nello stesso change) — Duplicati Signal su cambio id `uuid`↔`number`.** La §4.3 merge-a per `cache_key`; se il `number` era assente e poi compare, `cache_key` cambia → il contatto risulta `new` e §7.2 lo **appende**, mentre il vecchio resta in TUI: riga duplicata permanente. L'obiezione 15 era "pre-esistente", ma l'append di §7.2 ne amplifica l'impatto. Mitigazione: in `_handle_contact_update_event`, prima di appendere, dedup per `extras["phone"]` normalizzato; oppure, nel merge Signal, rilevare il cambio id e sostituire in place la voce TUI/emettendo un evento di rename mappato. Aggiungere test dedicato.
- **R2 (MEDIA) — `_parse_contact` fantasma** (§4.3:327). Dichiarare l'helper reale.
- **R3 (MEDIA) — Ghost Signal non preservati.** `_merge_contacts_in_place` Signal non ha il guard ghost di WA/TG (§4.3:342-343): i ghost creati da `register_contact` vengono persi dal backend a ogni refresh. Il send Signal usa il numero grezzo quindi non si rompe, ma la coerenza con WA/TG manca. Decidere e testare.
- **R4 (MEDIA) — `_contacts_by_jid` di WA ricostruito** (`{c.id: c}`) perde gli alias `@lid` creati da `_register_lid_alias` (`whatsapp.py:982`); ora ricorrente. Self-heal via resolver, ma documentare/testare.
- **R5 (BASSA) — `_is_int` vs `int()`.** `_is_int("--1")` è `True` ma `int("--1")` solleva; in `_load_contacts_merge:293` si usa `int(c.id)` filtrando con `_is_int`. Usare `_to_int` per coerenza.
- **R6 (BASSA) — `_contacts_by_id` TG non lockato** tra `_apply_book_to_contacts` (refresh thread) e `list_address_book_sync` del picker (worker Textual). Sotto GIL è benigno, ma non è "last-writer-wins su attributo atomico" come dichiarato per `_address_book`. Valutare lock o marshal sul loop.
- **R7 (BASSA) — Thread del pool non daemon**: a fronte di `shutdown(wait=False)` il `ThreadPoolExecutor` registra un atexit che joina i worker all'uscita; bounded (~30s) ma da sapere.
- **R8 (BASSA) — Test**: manca un test manager realmente non-bloccante con timeout breve (il testo §9.1 usa 30s → lento/flaky) e un test che garantisca che un puro rename **non** appenda duplicati.

## D. Piano di test

Copre B1-B4 (§9.1: WA error/empty/identità `is`, TG raise/no-wipe/oggetto-stesso/ghost, Signal no-subprocess, manager shutdown) e B5/B7/B13/B14/B16 (§9.2). Mancano: R1 (duplicati Signal id-change), R3 (ghost Signal), R5 (`_is_int` edge), R8 (manager non bloccante con timeout corto). Da aggiungere.

## E. Divergenze di sfumatura dichiarate dall'architetto

- **#8 (web lazy)**: accettabile. Il refresh periodico tiene `_address_book` caldo entro TTL; il parametro `force` resta disponibile. Non blocca.
- **#9 (doppio refresh)**: accettabile con la precisazione R6: il picker non muta `self.contacts`, ma TG `list_address_book_sync` muta `_contacts_by_id`. Nessun crash atteso.
- **#11 (Signal non lockato)**: accettabile; il merge ora è in place e l'assegnazione lista è atomica. Resta la razza di iterazione (accettata come pattern pre-esistente).
- **#12 (iterazione TUI)**: accettabile come pattern pre-esistente; il re-anchor aggiunge una scansione O(n) sul poll thread.
- **#15 (Signal uuid↔number)**: NON accettabile come "solo rinviata" a impatto invariato, per via dell'append (vedi R1). Va mitigata nello stesso change.

**Verdetto re-review: APPROVATO CON RISERVE.** Lo sviluppo può partire; R1 è condizione di gate **prima del merge** (correttezza funzionale), le altre sono da tracciare/implementare durante lo sviluppo.
