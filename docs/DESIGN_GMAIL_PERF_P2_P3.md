# Design — Ottimizzazioni P2+P3 Gmail (pool IMAP, sessione HTTP, fetch parziale)

Data: 2026-10-24 · Branch: `master` (su `1e24ee4`, P0/P1 già mergiati) · Stato: **revisione 6** (post red-team round 5, gate 6 obbligatorio) · Ambito: solo Gmail (`web/gmail.py`, `web/gmail_api.py`, `web/link_preview.py`, `web/server.py`, test Gmail/link-preview).

> **Contesto**: P0/P1 su `master`. Misure live: `Gmail status` cold ≈0,80s / warm ≈7ms; lista 200 cold ≈1,76s / warm ≈11ms. Il doc copre **P2** (riuso connessioni IMAP), **P3a** (sessione HTTP condivisa), **P3b** (fetch parziale), incrementale e reversibile.

> **Revisione 6** — chiusi i punti del round 5 (BLK1/BLK2, major): **BLK1** `to_close` chiuso su **ogni uscita** di `_imap_acquire`/`_acquire_fresh` tramite `try/finally` esterno (incluso `busy`), nessuna connessione orfana; **BLK2** regola di `size` esplicita e verificabile — **`body-fld-octets` quando la struttura è disponibile** (percorso parziale **e** fallback full con struttura già fetchata), **`_encoded_size` (con `None`⇒`null`) solo quando la struttura è assente** (flag-off o BODYSTRUCTURE fallita); flag-off = rollback, può differire solo su parti malformate. Major: test "logout bloccanti" specificato con fake che rispetta `settimeout`/close socket (niente affermazione "zero thread"), MJ10 corretto (l'allegato è `text/plain` **base64**, non 7bit — verificato), golden Gmail valida `_encoded_size` vs `body-fld-octets`. Riepilogo §13.

---

## 1. Obiettivi e non-obiettivi

**Obiettivi**
- **P2** — pool IMAP autenticato: registro attivo + generation, identità stabile, NOOP solo oltre soglia idle, reconnect, acquire-timeout, chiusura parallela a deadline, nessun leak di stato, nessuna connessione orfana.
- **P3a** — una sola `aiohttp.ClientSession` condivisa (lifecycle FastAPI), `DummyCookieJar`, resolver SSRF-safe invariato, chiusura deterministica race-free.
- **P3b** — dettaglio senza scaricare allegati (`BODYSTRUCTURE` + `BODY.PEEK[<part>]`), allegati on-demand, **contratto `size` unico = encoded**, fallback sicuro a `BODY.PEEK[]`.

**Non-obiettivi**
- Nessun breaking change REST/UX, salvo la **deroga esplicita** del campo `size` (§5.6): `size` = **encoded** (o `null` per part non calcolabile, documentato).
- Nessuna nuova dipendenza runtime.
- Nessuna modifica a WhatsApp/Signal/Telegram.
- **SMTP resta per-send**.
- Nessun retry di comandi non idempotenti; reconnect solo a livello di acquisizione.
- Non si duplica la cache inbox: cache indice (§5.7) separata e opzionale.

**Baseline** come da contesto; target `k·RTT + budget` (§8).

---

## 2. Stato attuale e fatti verificati

| Punto | File | Righe |
|---|---|---|
| `imap_connect()` / `smtp_connect()` | `web/gmail.py` | 1354–1370 / 1373–1395 |
| `_fetch_one()` | `web/gmail.py` | 1004–1018 |
| `fetch_message_list()` | `web/gmail.py` | 1028–1132 |
| `fetch_message()` / `fetch_attachment()` | `web/gmail.py` | 1135–1152 / 1155–1179 |
| `mark_read()` / `delete_messages()` | `web/gmail.py` | 1182–1195 / 1198–1242 |
| `send_email()` | `web/gmail.py` | 1282–1351 |
| `parse_message()` / `_extract_*` | `web/gmail.py` | 805–824 / 746–802 |
| `_now()` | `web/gmail.py` | 215–217 |
| `_run()` | `web/gmail_api.py` | 25–53 |
| `create_gmail_image_router()` / `_IMAGE_PROXY_MAX_BYTES` | `web/gmail_api.py` | 59–93 / 56 |
| `_session()` / `_public_resolver()` | `web/link_preview.py` | 227–238 / 91–110 |
| `_fetch_preview_uncached()` / `get_preview_image()` / `fetch_external_image()` | `web/link_preview.py` | 241–263 / 334–358 / 361–377 |
| include router / shutdown | `web/server.py` | 116–124 / 216–224 |

**Fatti verificati (rev. 6)**
- `Message.walk()` discende in `message/rfc822` (`is_multipart()==True`); `_extract_attachments` salta il container ma enumera gli allegati incapsulati.
- **`get_payload(decode=True)` = byte wire esatti con CRLF preservato** per 8bit (`"café €\r\nsecond\r\n"` → 19 byte); `get_payload(decode=False)` per 8bit restituisce **str** (charset-decoded) ⇒ `len(str)` sbagliato.
- **base64/QP (anche text)**: `get_payload(decode=False)` restituisce la **rappresentazione ASCII encoded** (QP → `"caf=C3=A9 =E2=82=AC"`, base64 → `"Y2Fmw6kg4oKs"`) ⇒ `_encoded_size` calcolabile.
- **Formula CRLF (BL1)**: `len(raw) + raw.count(b"\n") - raw.count(b"\r\n")` normalizza CRLF e LF al wire CRLF (verificato: LF `21`→`22`, CRLF `22`→`22`).
- **L'allegato del test `size` è `text/plain` con CTE `base64`** (verificato: `add_attachment(b"primo", maintype="text", subtype="plain")` → `Content-Transfer-Encoding: base64`; decoded `5`, encoded `10`). **Non** 7bit (il body `_plain_message("corpo")` è 7bit, l'allegato no).
- `imaplib` restituisce literal FETCH come `(meta, literal)`, **UID solo sulla prima tupla**.
- `aiohttp` 3.14.1 **non chiude** un resolver passato esplicitamente → chiuso esplicitamente (M7).
- La UI (`web/static/gmail.js`) tollera `attachment.size == null` (`!== undefined && !== null`) → `size: null` UI-compatibile.

---

## 3. P2 — riuso/pool connessioni IMAP

### 3.1 Decisione

**Pool limitato di connessioni autenticate, per account (fingerprint §3.2), registro attivo + generation, identità stabile `conn_id`, checkout esclusivo, NOOP solo oltre soglia idle, idle-reap con detach+close fuori lock, acquire-timeout, chiusura parallela a deadline, discard su qualunque eccezione inattesa, SELECT esplicito per op.**

| Alternativa | Valutazione | Esito |
|---|---|---|
| Connessione per thread | >15 idle; lifecycle opaco. | Scartata |
| Singola serializzata | serializza tutto. | Scartata |
| Per-op (oggi) | non limita, non riusa. | Scartata |
| **Pool limitato N=4** | riuso, cap < 15, checkout esclusivo, shutdown deterministico. | **Scelta** |

**Concorrenza non peggiorata**: letture calde cache-inbox; op brevi; N=4 + acquire-timeout 2s ⇒ 502.

### 3.2 Fingerprint di pool (B2)

- **`_pool_fingerprint(settings)`** = `hmac.new(_POOL_FP_KEY, f"{address}\0{imap_host}\0{imap_port}\0{timeout_s}\0{password}".encode("utf-8"), hashlib.sha256).hexdigest()`; `_POOL_FP_KEY = secrets.token_bytes(32)`; **mai loggato**; distinto da `_account_fingerprint`.

### 3.3 Stato, registro, generation, identità, clock (B1, BR1, MJ7, NB2)

```text
_IMAP_POOL_MAX_CONNECTIONS  = 4
_IMAP_POOL_ACQUIRE_TIMEOUT_S = 2.0
_IMAP_POOL_MAX_IDLE_S        = 600
_IMAP_POOL_NOOP_IDLE_S       = 30
_IMAP_POOL_NOOP_TIMEOUT_S    = 1.0
_IMAP_POOL_CLOSE_TIMEOUT_S   = 0.5
_IMAP_POOL_CLOSE_TOTAL_S     = 1.0
_IMAP_POOL_ENABLED           = True   # rollback di deploy

_pool_cond = threading.Condition(threading.Lock())   # UNICA primitiva
_pool_generation = 0
_pool_fingerprint = None
_pool_idle: list[_PooledConn] = []
_pool_active: dict[int, _PooledConn] = {}   # key = conn_id
_pool_conn_seq = 0

@dataclass
class _PooledConn:
    conn_id: int
    conn: imaplib.IMAP4 | None
    fingerprint: str
    generation: int
    idle: bool
    discarded: bool
    created_at: float
    last_used_at: float
```

- **Invariante**: `len(idle)+len(active) <= MAX`; `_PooledConn` in esattamente uno di idle/active o detach-per-close.
- **MJ7**: pool usa `_now()` (mai `monotonic()` diretto).
- **NB2**: nessun flag `_pool_closed` sticky; close/clear naturalmente idempotenti; re-init su acquire.

### 3.4 Zero I/O sotto lock, `_close_conn` non-bloccante, `_close_many` (B3, MA4, MJ3, BL3, major thread)

```text
_close_conn(conn):                       # best-effort, mai solleva
    try:
        try: conn.sock.settimeout(CLOSE_TIMEOUT_S)
        except Exception: pass
        try: conn.logout()
        except Exception:
            try: conn.shutdown()
            except Exception: pass
    finally:
        try: conn.sock.shutdown(socket.SHUT_RDWR)   # non-bloccante
        except Exception: pass
        try: conn.sock.close()                      # non-bloccante: sblocca un logout socket-bound
        except Exception: pass

_close_many(conns):
    conns = [c for c in conns if c is not None]
    if not conns: return
    ex = ThreadPoolExecutor(max_workers=min(len(conns), 8))
    try:
        futures = [ex.submit(_close_conn, c) for c in conns]
        try:
            for f in as_completed(futures, timeout=_IMAP_POOL_CLOSE_TOTAL_S): pass
        except (TimeoutError, concurrent.futures.TimeoutError):   # BL3: non propaga
            log.debug("imap pool close reached deadline; abandoning sockets")
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
```

- **Major thread — comportamento residuo documentato (niente affermazione "zero thread")**: `_close_conn` imposta `settimeout` e chiude il **socket** in `finally`. Per un `imaplib` reale, `logout()` è socket-bound e viene sbloccato dal `sock.close()` ⇒ il thread termina entro ~`CLOSE_TIMEOUT_S`+ε. **La garanzia testata è: il chiamante ritorna ≤ `CLOSE_TOTAL_S` e il socket è chiuso.** Non si asserisce "zero thread non-daemon" (un fake patologico il cui `logout()` ignora sia `settimeout` sia `sock.close()` può mantenere un thread vivo fino al ritorno — non rappresentativo di `imaplib`, non usato per il test di timing).
- **Test (major)**: fake `logout()` che **rispetta `settimeout`** (o blocca su un `Event` sbloccato da `sock.close()`, oppure solleva `OSError` dopo il settimeout) ⇒ il chiamante ritorna entro `CLOSE_TOTAL_S`, socket chiuso.

### 3.5 Acquisizione — `to_close` chiuso su OGNI uscita (BLK1, BR1, B4, M1, M2, MA5, MA6, MJ5/BL4, MJ7)

```text
_imap_acquire(settings):
    fp = _pool_fingerprint(settings)
    to_close = []
    try:
        with _pool_cond:
            to_close += _ensure_fingerprint_locked(fp)
            deadline = _now() + ACQUIRE_TIMEOUT
            while True:
                to_close += _reap_idle_locked(_now())
                if _pool_idle:
                    pc = _pool_idle.pop(); pc.idle = False
                    _pool_active[pc.conn_id] = pc          # BR1: stesso blocco lock
                    break
                if len(_pool_active) < MAX:
                    pc = _PooledConn(conn_id=_next_conn_id_locked(), conn=None, fp=fp,
                                     generation=_pool_generation, idle=False, ...)
                    _pool_active[pc.conn_id] = pc
                    break
                remaining = deadline - _now()
                if remaining <= 0: raise GmailUpstreamError("busy")
                _pool_cond.wait(remaining)
        # successo: chiudi subito le idle detachate (fuori lock)
        _close_many(to_close); to_close = []
        if pc.conn is None:
            _create_and_login(pc, settings)                # B4: finally cleanup slot + notify
        elif _needs_noop(pc):                              # M1
            pc = _noop_or_reconnect(pc, settings)          # MA6
        return pc
    finally:
        _close_many(to_close)        # BLK1: su OGNI uscita, incluso busy/eccezione
```

- **BLK1**: `to_close` inizializzato a `[]`, accumulato sotto lock, e chiuso nel **`finally` esterno** ⇒ le idle detachate da `_ensure_fingerprint_locked`/`_reap_idle_locked` sono chiuse **anche sul ramo `busy`** (e su qualunque eccezione). Nessuna connessione orfana. (Sul successo si chiude prima e si svuota, così il `finally` è no-op.)
- **`_ensure_fingerprint_locked(fp)`**: se fp diverso, detach idle (`to_close`), marca discarded idle+attive, `generation++`, `_pool_fingerprint=fp`, `notify_all`.
- **`_reap_idle_locked(now)`**: detach idle scadute, marca discarded, ritorna per close fuori lock.
- **B4 `_create_and_login`**: `finally` su login fallita → `_pool_active.pop` + `notify_all` + `_close_conn` fuori lock.
- **M1/M2**: NOOP oltre soglia, `sock.settimeout(NOOP_TIMEOUT_S)` + ripristino.
- **MA6 `_noop_or_reconnect`**: su NOOP fallito, pop+notify+close, poi `_acquire_fresh`.
- **MJ5/BL4 — `_acquire_fresh(settings)`** (stessa struttura, `to_close` chiuso su ogni uscita):

```text
_acquire_fresh(settings):
    fp = _pool_fingerprint(settings)
    to_close = []
    try:
        with _pool_cond:
            to_close += _ensure_fingerprint_locked(fp)     # BL4: salva to_close
            deadline = _now() + ACQUIRE_TIMEOUT
            while True:
                to_close += _reap_idle_locked(_now())
                if len(_pool_active) < MAX:
                    pc = _PooledConn(conn_id=_next_conn_id_locked(), conn=None, fp=fp,
                                     generation=_pool_generation, idle=False, ...)
                    _pool_active[pc.conn_id] = pc
                    break
                remaining = deadline - _now()
                if remaining <= 0: raise GmailUpstreamError("busy")   # BL4: wait coerente
                _pool_cond.wait(remaining)
        _close_many(to_close); to_close = []
        _create_and_login(pc, settings)
        return pc
    finally:
        _close_many(to_close)        # BLK1
```

- **Test (BLK1)**: pool saturo (active=MAX) + idle scadute/vecchio fingerprint (detachate in `to_close`) + acquire-timeout ⇒ `busy` sollevato **e** `logout`/close chiamati sulle idle detachate; nessuna connessione orfana.
- **Test (BL4)**: NOOP fallito + cambio fingerprint con idle ⇒ idle chiuse + wait coerente.

### 3.6 Release e classificazione eccezioni (M3)

```text
_imap_release(pc, exc_info):
    keep = exc_info is None or _is_keep_exception(exc_info[1])
    with _pool_cond:
        reusable = keep and (not pc.discarded) and (pc.generation == _pool_generation) and (pc.fingerprint == _pool_fingerprint)
        _pool_active.pop(pc.conn_id, None)
        if reusable: pc.idle=True; pc.last_used_at=_now(); _pool_idle.append(pc)
        else: pc.discarded=True
        _pool_cond.notify_all()
    if not reusable: _close_conn(pc.conn)
```

- `_is_keep_exception` = solo `(GmailNotFound, GmailValidationError, GmailNotConfigured)`; **tutto il resto ⇒ discard**.

### 3.7 `close_imap_pool` / `clear_imap_pool` (BR2, NB2, MA4, MJ3)

```text
_teardown_locked():
    to_close = list(_pool_idle)                 # BR2: SOLO idle
    _pool_idle.clear()
    for pc in to_close: pc.discarded = True
    for pc in _pool_active.values(): pc.discarded = True   # marcatura, non chiusura
    _pool_generation += 1
    _pool_fingerprint = None
    _pool_cond.notify_all()
    return to_close

close_imap_pool():                             # idempotente naturale (NB2)
    with _pool_cond: to_close = _teardown_locked()
    _close_many(to_close)

clear_imap_pool():
    with _pool_cond: to_close = _teardown_locked()
    _close_many(to_close)
```

### 3.8 Stato cartella, API, chiusura

- Il pool **non** traccia/riusa `SELECT`; ogni op esegue il proprio `select()` dopo l'acquisizione. `imap_connect` = context manager acquire/release; chiamanti invariati; `smtp_connect` invariato. Limite Gmail: 4 slot su 15.

---

## 4. P3a — sessione HTTP condivisa

### 4.1 Problema e decisione

`_session()` crea una `ClientSession` per fetch. Decisione: **sessione condivisa lazy legata al loop, `DummyCookieJar`, chiusa race-free sullo stesso loop (BR5/MJ9); rollback via context manager transitorio (MJ1), rispettando `_closing`.**

### 4.2 Stato, scope, flag rollback (MJ1, major `_closing`)

```text
_shared_session: aiohttp.ClientSession | None = None
_shared_session_loop: asyncio.AbstractEventLoop | None = None
_shared_resolver: Any | None = None
_closing = False
_SHARED_SESSION_ENABLED = True

@asynccontextmanager
async def shared_session():
    if _closing:                                    # major: anche il ramo transitorio
        raise RuntimeError("shared session is closing")
    if not _SHARED_SESSION_ENABLED:
        s = _make_transient_session()
        try:
            yield s
        finally:
            await s.close()                          # MJ1
        return
    yield _get_or_create_shared_session()

def _get_or_create_shared_session():
    loop = asyncio.get_running_loop()
    if _shared_session is not None and _shared_session_loop is loop and not _shared_session.closed:
        return _shared_session
    if _closing:
        raise RuntimeError("shared session is closing")   # BR5
    resolver = _public_resolver()
    connector = aiohttp.TCPConnector(resolver=resolver, limit=_SESSION_CONNECTOR_LIMIT)
    session = aiohttp.ClientSession(connector=connector, cookie_jar=aiohttp.DummyCookieJar(),
                                    timeout=aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS),
                                    headers={"User-Agent": _USER_AGENT, "Accept-Language": "it,en;q=0.8"},
                                    trust_env=False)
    _shared_resolver, _shared_session, _shared_session_loop = resolver, session, loop
    return session
```

### 4.3 Shutdown (BR5, B8, MJ9)

```text
async def close_shared_session():
    global _closing
    _closing = True
    try:
        loop = asyncio.get_running_loop()
        session = _shared_session
        resolver = _shared_resolver
        owner  = _shared_session_loop
        _shared_session = _shared_resolver = _shared_session_loop = None
        if session is None: return
        if owner is not loop:
            log.debug("shared session owned by another loop; skip close")
            return
        try:
            if not session.closed:
                await session.close()
        finally:
            if resolver is not None:
                await resolver.close()          # MJ9
    finally:
        _closing = False
```

### 4.4 Parametri e sicurezza (invariata)

connector `TCPConnector(resolver=_public_resolver(), limit=8)`; `_SESSION_CONNECTOR_LIMIT=8`; timeout 6s; headers UA+Accept-Language; `trust_env=False`; `cookie_jar=DummyCookieJar()`. SSRF invariato.

### 4.5 File coinvolti

| File | Modifica |
|---|---|
| `web/link_preview.py` | `shared_session()` + `_get_or_create_shared_session`/`_make_transient_session`/`close_shared_session` + `_closing` + `_SHARED_SESSION_ENABLED`; 3 chiamanti `async with shared_session() as session:`; cleanup `_CACHE`/`_INFLIGHT`. |
| `web/server.py` | lifecycle shutdown → `await close_shared_session()` + `close_imap_pool()`. |
| `web/gmail_api.py` | nessun cambio. |

---

## 5. P3b — fetch parziale corpo/allegati

### 5.1 Problema e decisione

`_fetch_one` fa `BODY.PEEK[]`; `fetch_attachment` rifà tutto. Decisione: due fasi (BODYSTRUCTURE+header, poi `BODY.PEEK[<part>]`) con fallback full sulla **stessa** connessione (MA1) e contratto `size` unico (§5.6, BLK2).

### 5.2 Acquisizione singola, fallback, `_fetch_structure` (MA1, MJ2, major)

```text
fetch_message(uid, folder):
    with imap_connect(settings) as conn:
        conn.select(_mailbox(folder), readonly=True)
        if _PARTIAL_FETCH_ENABLED:
            rec = _fetch_structure(conn, uid)         # None solo per esiti logici
            if rec is not None:
                try: parts = _fetch_render_parts(conn, uid, rec.index)
                except _PartFetchError: parts = None
                if parts is not None:
                    return _assemble(rec, parts)
        raw = _fetch_full_on(conn, uid)
        return _parse_full(raw)
```

- **MA1**: mai `with imap_connect` annidato; fallback full sulla stessa connessione; `_fetch_one` → `_fetch_full_on(conn, uid)`.
- **`_fetch_structure` (major)**: restituisce `None` (⇒ fallback) **solo** per esiti **logici** (status NO/BAD, `_extract_bodystructure`/`parse_bodystructure` falliti, BODYSTRUCTURE literal `{N}`). **abort/timeout/`OSError`/`ssl.SSLError`/`IMAP4.abort` PROPAGANO** ⇒ discard via `_imap_release`. Mai fallback su connessione desincronizzata.
- **MJ2**: `_PartFetchError` solo per esiti logici; transport propaga.

### 5.3 BODYSTRUCTURE — estrazione (MA3, MJ4), parser, numerazione, indici (B5)

- **Comando**: `UID FETCH <uid> (BODYSTRUCTURE FLAGS BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID IN-REPLY-TO REFERENCES)])`.
- **MA3 `_extract_bodystructure(data) -> bytes | None`**: parentesi bilanciate (quote con escape, literal).
- **MJ4**: `{N}` ovunque nel blocco (incluso `body-fld-param`) ⇒ `None` ⇒ fallback full.
- **`parse_bodystructure(blk) -> _Bodystructure | None`**: tokenizer IMAP, no eval, profondità ≤ 64, token ≤ 10 000, literal ≤ 64 KiB.
- **`walk_index`** (RFC 3501: `1`, `2.1`, `2.2`, …) e **`attachment_index`** (filtro `_extract_attachments` esatto; container multipart/rfc822 saltati + discesa).

### 5.4 Parser FETCH dedicato (BR3, B6, MJ2)

- **`_parse_part_fetch(data, expected_uid)`**: UID verificato **una volta** (prima tupla/trailer), tuple successive ereditano, UID diverso/assente ⇒ rifiuto; literal→`BODY[<section>]` del proprio meta; duplicato/ambigua ⇒ rifiuto.
- Errori di parse/label ⇒ `_PartFetchError` (fallback); transport ⇒ propaga (discard).
- Regex part-number unica `^[1-9][0-9]*(\.[1-9][0-9]*)*$`.

### 5.5 Selezione parti e rendering (M4, M5, MA2)

- **M4**: filename (disposition > content-type name), RFC 2231/2047 (MA2), NIL, container con filename non elencato, root single-part, golden fixture Gmail sanitizzate.
- **M5**: foglie text/html nell'ordine di walk (incluso rfc822); cap `_MAX_RENDER_PARTS = 8` ⇒ fallback full.
- Decode parte: mini `EmailMessage` (Content-Type+CTE) + `_payload_text`; sanitizzazione invariata; cid non scaricati/embedded.

### 5.6 Contratto `size` — UNICO, regola di sorgente esplicita (NB1, BL1, BL2, BLK2)

- **Contratto unico**: `size` = **encoded size** (octet transfer-encoded) su tutti i percorsi; `null` solo per part non calcolabile.
- **Regola di sorgente (BLK2, verificabile)**:

| Percorso | Struttura disponibile? | Sorgente `size` |
|---|---|---|
| **Parziale** (BODYSTRUCTURE parsed) | sì | **`body-fld-octets`** (autoritativo) |
| **Fallback full da parziale** (structure OK, part-FETCH fallito) | sì | **`body-fld-octets`** (la `_PartMap` è ancora disponibile) |
| **Fallback full senza struttura** (BODYSTRUCTURE fetch/parse fallita) | no | `_encoded_size(part)`; `None` ⇒ `null` |
| **Flag-off** (`_PARTIAL_FETCH_ENABLED=False`) | no | `_encoded_size(part)`; `None` ⇒ `null` |

  - **Dichiarazione esplicita**: il percorso parziale usa `body-fld-octets`; il `_encoded_size` è usato **solo** quando la struttura non è disponibile.
  - **Flag-off = rollback**: può differire dal percorso parziale **solo** su parti in cui `_encoded_size` restituisce `None` (in pratica mai per base64/QP, che sono ASCII — verificato); in tal caso `size: null`.
  - **Nessuna tolleranza**: il differenziale asserisce uguaglianza **esatta** tra `octets` (parziale) e `_encoded_size` (full) per le CTE computabili; per la CTE non garantita, `null` su entrambi i lati ove la struttura manca.

```text
_encoded_size(part) -> int | None:
    cte = (part.get("Content-Transfer-Encoding") or "").strip().lower()
    if cte in ("base64", "quoted-printable"):
        raw = part.get_payload(decode=False)          # rappresentazione encoded ASCII (anche per text)
        if isinstance(raw, bytes):
            enc = raw
        elif isinstance(raw, str):
            try: enc = raw.encode("ascii")
            except UnicodeEncodeError: return None    # BL2: difensivo (mai per base64/QP)
        else:
            return None
        # BL1: normalizzazione CRLF/LF → wire CRLF
        return len(enc) + enc.count(b"\n") - enc.count(b"\r\n")
    # 7bit/8bit/binary/CTE-manacante/ignota: nessun transfer-encoding
    return len(part.get_payload(decode=True) or b"")  # byte, non len(str)
```

  - **BL1**: `len(raw) + raw.count(b"\n") - raw.count(b"\r\n")` (verificato). **BL2**: `None` ⇒ `size: null` solo per quel part (mai "fallback full" circolare).
  - **Download**: `Content-Length` = decoded (invariato); **413 sul decoded**; **MA9**: early-reject solo 7bit/8bit/binary.
  - **CTE sconosciuta / `octets` NIL** (coerente con la tabella BLK2):
    - struttura disponibile + `octets` int ⇒ `size = octets` **anche con CTE sconosciuta**;
    - struttura disponibile + `octets` NIL ⇒ `size = null`;
    - struttura assente / parse fallita ⇒ fallback full e `size = _encoded_size(part)` (`None` ⇒ `null`).
    - **Download**: `octets` NIL/ignota ⇒ salta il pre-check e applica solo il post-check decoded (invariato).
- **Test (NB1/BL1/BL2/BLK2)**: 8bit UTF-8 `"café €"` → 9; base64/QP CRLF e LF; golden fixture Gmail; full-vs-parziale su parte base64/QP **malformata** senza tolleranza (octets == `_encoded_size`, o `null` se CTE non garantita).

### 5.7 Cache indice (M9) — opzionale, Fase 3b

- Chiave `(folder, _pool_fingerprint, uid)` + `uidvalidity`; invalida su mismatch/delete/send/clear; `mark_read` non invalida; `_MimePart` frozen con `params` annidato immutabile/copiato. Reversibile.

---

## 6. Modifiche file-per-file (riepilogo)

| File | Modifica |
|---|---|
| `web/gmail.py` | **P2**: costanti `_IMAP_POOL_*`; `_POOL_FP_KEY`; stato; `_PooledConn(conn_id)`; `_pool_fingerprint`; `_imap_acquire`/`_imap_release`/`_create_and_login`/`_noop_or_reconnect`/`_acquire_fresh`/`_close_conn`/`_close_many`/`_ensure_fingerprint_locked`/`_reap_idle_locked`/`_teardown_locked`/`clear_imap_pool`/`close_imap_pool`; `imap_connect` wrapper; `_now()` ovunque. **P3b**: `_PARTIAL_FETCH_ENABLED`; `_extract_bodystructure`; `parse_bodystructure`; `_decode_param`; `_encoded_size`; `_MimePart`/`_PartMap`; `_parse_part_fetch`; `_fetch_structure`; `_fetch_render_parts`; `_fetch_full_on`; refactor `fetch_message`/`fetch_attachment`; `_extract_attachments` **`size`→`octets` o `_encoded_size`** (con `null`); cache indice. |
| `web/gmail_api.py` | nessun cambio di contratto (documentare `size` nullable). |
| `web/link_preview.py` | `shared_session()` + `close_shared_session` + `_closing` + `_SHARED_SESSION_ENABLED`; 3 chiamanti `async with`. |
| `web/server.py` | lifecycle shutdown → `await close_shared_session()` + `close_imap_pool()`. |
| `tests/conftest.py` | fixture centralizzate + async `close_shared_session` (in-loop) + cleanup `_CACHE`/`_INFLIGHT`. |
| `tests/test_gmail_api.py` | `FakeIMAP` esteso (MJ8): `noop`, `sock`+`settimeout`, `shutdown`/`close`; asserzioni login/reuse count. |
| `tests/test_gmail_parse.py` | `_encoded_size` (8bit/base64/QP/CRLF/LF), `_decode_param`, differenziale (tranne `size`), `size` encoded/null. |
| `tests/test_web_link_preview.py`, `tests/test_gmail_image_proxy.py` | fixture async in-loop; test sessione/loop/close/no-cookie/resolver/flag-off. |

---

## 7. Piano test

### 7.1 P2 — pool IMAP

- Riutilizzo (login count); **BR1** (4 idle + 5 richieste ⇒ mai 5); **BR2** (attiva bloccata non chiusa/riusata); **B2** (cambio host/password); cap/timeout (monkeypatch costante + clock); **MA5** (reap/fingerprint detach+close); **M1/M2/MA6/MJ5** (NOOP soglia, peer nero 1s, due NOOP falliti ⇒ `_acquire_fresh`); **B4** (5 login falliti poi successo); **M3** (discard/keep); **B3/MA4/MJ3/BL3** (logout bloccante, `_close_many` non solleva, `_close_many([])` no-op); **NB2** (close→acquire→close); **BL4** (NOOP fallito + cambio fingerprint con idle ⇒ idle chiuse + wait coerente); **BLK1** (pool saturo + idle scadute/vecchio fingerprint + acquire-timeout ⇒ `busy` **e** close sulle idle detachate, nessuna orfana); **MJ7** (clock congelato); **M11** (flag); **MJ8** (FakeIMAP `noop`/`sock`/`settimeout`/`shutdown`/`close`); **major thread** (fake `logout` che rispetta `settimeout`/close socket ⇒ chiamante ≤ deadline, socket chiuso).

### 7.2 P3a — sessione condivisa

- Una sessione per loop; cookie non persistiti; SSRF invariato; loop isolation; **BR5** (owner, cross-loop, `_closing`); **MA8** (close in-loop, cleanup `_CACHE`/`_INFLIGHT`); **MJ1** (flag off ⇒ transitoria creata e chiusa); **major** (flag off + `_closing` ⇒ nessuna sessione durante shutdown); **MJ9** (resolver.close in finally); idempotente (no `ResourceWarning`); M7.

### 7.3 P3b — fetch parziale

- **Parser BODYSTRUCTURE (MA3/MJ4)**: quote/escape/NIL/parentesi; `{N}` ovunque ⇒ fallback; profondità ⇒ `None`.
- **Differenziale full-vs-parziale**: corpus (simple, alternative, mixed, inline cid, charset non-UTF8, CTE base64/QP, malformato, inoltro con PDF) ⇒ identici **tranne `size`**; `size` encoded/null, **senza tolleranza**; **BLK2**: parte base64/QP malformata ⇒ `octets` (parziale) == `_encoded_size` (full) o `null` se CTE non garantita.
- **`_encoded_size` (NB1/BL1/BL2)**: 8bit `"café €"`→9; base64/QP CRLF **e** LF; golden fixture Gmail; `None` ⇒ `null`.
- **Ordine indice (B5)**: `attachment_index` == ordine `_extract_attachments`; numerazione `2.1`/`2.2`.
- **Parser FETCH (BR3/B6/MJ2)**: UID prima tupla/trailer, ereditano, UID diverso ⇒ rifiuto; abort/timeout ⇒ propaga (discard).
- **`_fetch_structure` (major)**: transport ⇒ propaga (mai `None`); NO/BAD/parse ⇒ `None` (fallback).
- **RFC 2231 (MA2)**: `filename*`, continuazioni, percent-decode, 2047.
- **Riduzione dati**: dettaglio non scarica allegati; `fetch_attachment` scarica solo la parte.
- **413 (MA9)**: base64/QP/boundary; early-reject solo 7bit/8bit/binary; `octets` NIL/mentita ⇒ (dettaglio) `size` per BLK2 (`octets`/`null`), (download) salta pre-check + post-check decoded.
- **Acquisizione singola (MA1)**: fallback forzato + N=4 ⇒ una connessione per op.
- **multipart/alternative (M5)**: html preferito; oltre `_MAX_RENDER_PARTS` ⇒ fallback full.
- Inline/cid metadati; `_PARTIAL_FETCH_ENABLED=False` ⇒ full.

### 7.4 Isolamento e test `size` (M6, MA8, MJ10, major golden)

- Fixture centralizzate `conftest.py`; clock mockato; env isolato; logging vietato.
- **MJ10 — elenco esplicito dei test `size` da aggiornare** (assumevano decoded):
  - `tests/test_gmail_parse.py::test_attachments_have_deterministic_indices_and_sanitized_names` (riga **155**): `assert first["attachments"][0]["size"] == len(b"primo")` → ora asserisce la **size encoded** = `10` (l'allegato è `text/plain` **CTE base64** — verificato — non 7bit; base64 di `b"primo"` = `"cHJpbW8="` + line-break CRLF-normalizzato). Il body `_plain_message("corpo")` è 7bit, l'**allegato** no.
  - `tests/test_gmail_parse.py::test_public_attachments_strips_payload` (riga **204**): la chiave `size` resta nel set (nessun cambio; verificare che il valore sia encoded/null).
  - Nessun assert `size` decoded hardcoded in `tests/test_gmail_api.py` (verificato: solo `test_send_rejects_oversized_attachment`, invariato).
  - **Fixture base64/QP dedicate** (payload CRLF e LF) per `_encoded_size`.
  - **Changelog**: voce che documenta la deroga `size` = encoded (e `null` per part non calcolabile).
- **Major golden Gmail**: validare `_encoded_size(part) == body-fld-octets` su fixture BODYSTRUCTURE **reali**; se un CTE mostra mismatch, la regola BLK2 lo copre (`null` nel percorso senza struttura, documentato).

---

## 8. Misure e criteri (MA7, MJ6)

Metodo: server reale + `curl -w '%{time_total}'`; N≥100 warm, N≥20 cold/rete; p50/p95. **RTT** = mediana `NOOP` su connessione calda (N≥20).

**Formula**: `target = k·RTT + budget` (`k` = solo round-trip; TLS/login e costo server SEARCH nel budget). **"warm" (MJ6)** = idle ≤ `NOOP_IDLE_S` (nessun NOOP); NOOP oltre soglia = +1 a `k`.

| Misura | k | round-trip contati | budget |
|---|---|---|---|
| **M4** dettaglio cold (pool vuoto) | 3 | SELECT + STRUCTURE/headers + parts | 0,60s (TLS+login) |
| **M5** dettaglio warm (idle ≤ soglia) | 3 | SELECT + STRUCTURE + parts | 0,05s |
| **M6** allegato warm — index miss | 3 | SELECT + STRUCTURE + part | 0,05s |
| **M6** allegato warm — index hit | 2 | SELECT + part | 0,05s |
| **M7** `mark_read` warm | 2 | SELECT + STORE | 0,02s |
| **M8** lista miss cold (pool caldo, lista svuotata) | 3 | SELECT + SEARCH + FETCH headers | 0,42s (SEARCH) |
| **M9** proxy immagine warm (sessione condivisa, stesso host) | 1 | HTTP GET | 0,10s |

**Vincoli (M10)**: piccoli/medi/grandi; non-regressione senza allegati; M9 stesso host; cold senza tempo di clear.

**Criteri**: P2 — seconda op non riloggia, M5/M7 sotto target, nessun 502 sotto burst ≤ MAX, cleanup verificato, nessuna orfana; P3a — una sessione per loop, cookie non persistiti, SSRF invariato, chiusura deterministica; P3b — dettaglio/allegato identici (tranne `size`), byte ridotti, fallback/413/early-reject verificati; test aggiornati; nessuna dipendenza runtime nuova; contratto REST invariato salvo `size` (encoded/null, documentato).

---

## 9. Rischi e mitigazioni

1. Coda oltre N: N=4 + acquire-timeout 2s; Fase 1b se il SELECT domina.
2. Connessione morta: NOOP oltre soglia (M1) + timeout breve (M2) + idle-reap + discard (M3).
3. Limite Gmail 15: pool ≤ 4.
4. Leak folder/account: SELECT esplicito + fingerprint + registro/generation/conn_id.
5. `imaplib` non thread-safe: checkout esclusivo.
6. Chiusura: `_close_many` parallela a deadline + `_close_conn` close socket in `finally`; `to_close` chiuso su ogni uscita (BLK1).
7. Cookie: `DummyCookieJar` + test.
8. Parser BODYSTRUCTURE: difensivo, `{N}` ovunque ⇒ fallback (MJ4).
9. Iniezione part-number: regex stretta unica.
10. `size`/octets (NB1/BL1/BL2/BLK2): sorgente esplicita (`octets` se struttura, `_encoded_size`/`null` altrimenti); 413 sul decoded; post-check.
11. Fallback full = comportamento odierno.
12. Cache indice: UIDVALIDITY + generation + epoch (M9).
13. Non idempotenza delete: nessun retry di comandi.

---

## 10. Rollback e reversibilità

- Flag: `_IMAP_POOL_ENABLED`, `_PARTIAL_FETCH_ENABLED`, `_SHARED_SESSION_ENABLED` — **rollback di deploy** (costanti di modulo). Nessun cambio di firma pubblica; commit atomici per fase.

---

## 11. Ordine di implementazione (fasi)

1. **Fase 1 — P2 pool** (registro/generation/conn_id, fingerprint HMAC, zero-I/O-under-lock, NOOP+cleanup+`_acquire_fresh`, close parallela non-bloccante, `to_close` chiuso su ogni uscita, `_now()`) + test §7.1 + M4/M5/M7/M8.
2. **Fase 2 — P3a sessione** (context manager condiviso/transitorio che rispetta `_closing`, DummyCookieJar, resolver in finally, owner+_closing) + test §7.2 + M9.
3. **Fase 3 — P3b fetch parziale** (BODYSTRUCTURE+estrazione+`{N}`, RFC 2231, parser FETCH, `_encoded_size` + regola sorgente `size`, fallback singola connessione con `_PartFetchError` logico) + test §7.3 + M6/M10.
4. **Fase 3b (opzionale)** — cache indice con UIDVALIDITY.
5. Misurazione finale M4–M10 + review red team gate 6.

---

## 12. Punti aperti / rischi principali

1. **N del pool (4)** da validare con le misure di contesa; Fase 1b se il SELECT domina.
2. **Deroga `size` = encoded / `null`** (unica deviazione dal "no breaking change"): confermare UI/changelog e aggiornare i test `size` (elenco §7.4 MJ10).
3. **Parser BODYSTRUCTURE + RFC 2231 + `_encoded_size`** restano i più delicati (golden fixture M4).
4. **Uguaglianza esatta `octets` vs `_encoded_size` per base64/QP** dipende dalla normalizzazione CRLF (formula BL1 verificata); la validazione su golden fixture Gmail (major) è il gate finale; se mismatch ⇒ `null` per quel CTE (BLK2).
5. **Soglia NOOP 30s** e `k` da tarare con RTT misurato (MJ6).

---

## 13. Risoluzione review red team (rev. 6)

### 13.1 Round 5 (BLK/major)

| # | Riserva | Stato | Modifica / decisione | Test |
|---|---|---|---|---|
| **BLK1** | `to_close` perso sul busy | ✅ | `try/finally` esterno a `_imap_acquire`/`_acquire_fresh`; `to_close` accumulato sotto lock e chiuso nel `finally` (anche su `busy`/eccezione) | pool saturo + idle scadute/vecchio fp + timeout ⇒ `busy` **e** close sulle idle, nessuna orfana |
| **BLK2** | `size` full vs parziale | ✅ | regola esplicita: **`body-fld-octets`** quando la struttura è disponibile (parziale **e** fallback-full-con-struttura); **`_encoded_size` (None⇒null)** solo quando assente (flag-off / BODYSTRUCTURE fallita); flag-off=rollback, può differire solo su parti malformate | full-vs-parziale su parte base64/QP malformata, nessuna tolleranza |
| major | `_close_many` thread | ✅ | test "logout bloccanti" con fake che rispetta `settimeout`/close socket; comportamento residuo documentato (niente affermazione "zero thread"; `sock.close()` sblocca il logout socket-bound) | chiamante ≤ deadline, socket chiuso |
| major | MJ10 descrizione test `size` | ✅ | corretto: l'allegato è `text/plain` **CTE base64** (verificato), non 7bit; size decoded `5` → encoded `10`; elenco/changelog completati | test `size` aggiornati |
| major | Golden Gmail `_encoded_size` vs `octets` | ✅ | validare su fixture BODYSTRUCTURE reali; se mismatch, la regola BLK2 lo copre (`null` nel percorso senza struttura) | fixture reali |

### 13.2 Round 1–4 (carry-over, integrati nel corpo)

Le riserve dei round 1–4 restano risolte e integrate: **B1–B8**, **M1–M11**, **BR1–BR5**, **MA1–MA9**, **NB1–NB2**, **MJ1–MJ9**, **BL1–BL4**. Punti salienti conservati: registro attivo + generation + `conn_id` (BR1), fingerprint HMAC separato (B2), zero-I/O-under-lock (B3), cleanup slot in `finally` (B4), `walk_index`/`attachment_index` RFC 3501 con discesa rfc822 (B5/BR3), parser FETCH con UID singolo (B6), session owner+`_closing` (BR5/B8/MJ9), RFC 2231 (MA2), estrazione BODYSTRUCTURE (MA3/MJ4), close parallela (MA4/MJ3), formula RTT (MA7→MJ6), cleanup in-loop (MA8), early-reject base64/QP (MA9), sessione transitoria context manager (MJ1), `_PartFetchError` logico (MJ2), `_acquire_fresh` diretto con wait (MJ5/BL4), clock iniettabile (MJ7), FakeIMAP esteso (MJ8), `_encoded_size` formula CRLF (BL1), `size` nullable (BL2), `_close_many` TimeoutError (BL3).

**Conferma gate 6**: il documento è pronto per il sesto gate del red team. Nessun codice implementato, nessun commit eseguito.
