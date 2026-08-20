# Design di fix — BUG #32 (foto Telegram dello storico scaricabili/apribili)

> Redatto dall'architetto (input), trascritto su file dall'orchestratore.
> Riferimento: `BUGS.md` #32.

---

## 1. Analisi di impatto

### Flusso attuale (verificato)

- **Storico (connect)**: `_connect_telegram` (worker) → `fetch_recent_history(limit=20)` per ogni contatto → `_message_to_chat_event(msg)` con `attachment_id=None` → `ingest_message` (SQLite + cache, `attachment_id` persistito).
- **Live**: `_handle_new_message` → `download_media` → `/tmp/telegram-media` → `_message_to_chat_event(msg, path)`.
- **UI**: `_build_message_widgets` (cache) monta `ImageWidget(attachment_path=None, attachment_id=...)`; al click/Enter/Ctrl+D → `on_image_widget_image_clicked` (thread UI) → `manager.get_attachment_path(protocol, id)` → `TelegramBackend.get_attachment_path`: `Path(id).is_file() ? path : None`.

### I due casi rotti

| Caso | `attachment_id` persistito | Risultato al click |
|---|---|---|
| **Foto storico** | `None` (manca il fallback che hanno i documenti) | `ImageWidget` montato con `attachment_id=""` → non emette nemmeno `ImageClicked` (guard in `ui_components.py:426/448`). Completamente morto. |
| **Documento storico** | `str(msg.id)` (es. `"99"`) | Click emesso, ma `get_attachment_path` → `Path("99").is_file()` → `None` → "❌ Image file not found". Rotto anche lui, in modo più silenzioso. |
| Foto/documento **live** | path assoluto scaricato | Funziona (fast path). |

### Punti di codice impattati

1. `backends/telegram.py::_message_to_chat_event` (~righe 727-743): fallback `att_id` per foto e documenti.
2. `backends/telegram.py::get_attachment_path` (righe 96-99): da "solo path esistente" a "path esistente **oppure** riferimento lazy".
3. Nuovo metodo privato `backends/telegram.py::_download_media_by_ref(chat_id, msg_id)` (coroutine sul loop Telethon).

**Non impattati** (verificato): `backends/manager.py`, `backends/base.py`, `tui/chat_view.py`, `tui/download.py`, `ui_components.py`, `backend/db.py`, `tui/events.py`. Le euristiche caption Telegram dipendono solo da `text`: invariate.

---

## 2. Decisione di design

### Scelta: **A — Download lazy on-demand (pattern WhatsApp)**

- **B (eager in `fetch_recent_history`) scartata**: il fetch gira per tutti i contatti a ogni backend-ready (`tui/backend_connect.py:280`) → scaricherebbe ~20 media × N contatti a ogni avvio/ri-link: burst di rete/disco, connect più lento, media mai aperti.
- **C (ibrido: eager per la chat aperta) scartata**: il click lazy copre già "i media effettivamente visti"; un pre-fetch all'apertura reintroduce download di massa senza bisogno.
- **A scelta** perché: (1) replica il precedente approvato `WhatsAppBackend.get_attachment_path` (righe 1055-1089); (2) zero rete in fase di connect; (3) tocca solo il backend Telegram; (4) bonus: recupera anche i live download falliti (oggi `attachment_id=None` se `download_media` solleva; col fallback diventano tgref cliccabili).

### Forma dell'`attachment_id`

- **Live con download riuscito**: invariato — path assoluto (`/tmp/telegram-media/...`).
- **Storico (foto e documenti) e live con download fallito**: riferimento strutturato **`tgref:<chat_id>:<msg_id>`** (es. `tgref:42:99`, `tgref:-1001234567890:1234`).

Motivi del prefisso esplicito:
- Il bare `msg.id` non contiene il peer: `msg.id` è unico **per chat**, serve `(chat_id, msg_id)` per ri-scaricare via `get_messages(entity, ids=...)`. Il fallback attuale dei documenti è quindi inutilizzabile by design.
- Il prefisso rende la discriminazione in `get_attachment_path` dichiarativa e immune da falsi positivi.
- `Path("tgref:42:99").is_file()` è `False`: il fast-path resta il primo controllo.

### Discriminazione in `get_attachment_path` (ordine fisso)

1. `attachment_id` vuoto → `None`.
2. `Path(attachment_id).is_file()` → ritorna il path (live, e righe legacy già in DB con path).
3. Prefisso `tgref:` → parse (`rsplit(":", 1)` sul resto; `int()` di entrambi; chat_id negativi ok, contengono `-` non `:`) → lazy download.
4. Altrimenti → `None` (incluse le vecchie righe con bare `msg.id`: non risolvibili, manca il chat_id).

### Download lazy (contratto `_download_media_by_ref`)

- **Guard non-connesso**: `self._client is None or self._loop is None or not self._connected or not self._loop.is_running()` → `None` immediato (evita blocchi da loop chiuso/fermo).
- **Invio sul loop Telethon**: `asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=30)` — pattern già usato in `fetch_recent_history`/`send_message_sync`.
- **Coroutine**: `entity = await client.get_input_entity(chat_id)` → `msg = await client.get_messages(entity, ids=msg_id)` (ids int → singolo Message) → se `msg is None` o non ha `photo`/`document` → `None` → `await msg.download_media(file=<target>)`.
- **Dir di destinazione**: `Path(tempfile.gettempdir()) / "telegram-media"` — estrarre helper di modulo `_media_dir()` usato da entrambi i percorsi (oggi il live lo costruisce inline).
- **Nome file deterministico + dedup**: `f"{chat_id}-{msg_id}-{Path(nome_originale).name}"`, `nome_originale = msg.file.name` se presente, altrimenti `f"photo{msg.file.ext or '.jpg'}"`. Fast path: se `target.is_file()` → ritorna subito senza rete.
- **Fallimenti**: eccezioni → log + `None`. La UI usa i fallback esistenti ("❌ Image file not found on server", `ERROR:` in download mode, placeholder non risolto).

### Blocco UI thread — nota

`on_image_widget_image_clicked` chiama `get_attachment_path` sul thread UI. WhatsApp già blocca con REST sincrona lì; Telegram adotta lo stesso comportamento (bloccante, max 30s). Follow-up opzionale fuori scope: spostare la risoluzione del click in un worker thread per tutti i protocolli.

### Impatto su dedup/ingest/SQLite

`attachment_id` non fa parte dell'identità di dedup (`ingest_message` deduplica per `id` o `(text, timestamp ±2s)`). Coesistono righe con path (live) e con `tgref:` — entrambe risolvibili. **Nessun cambio di identità, nessuna migrazione.**

---

## 3. Vincoli di compatibilità

- **Schema SQLite**: invariato (`attachment_id TEXT` accetta entrambe le forme).
- **`ChatEvent`/`models.py`**: invariati.
- **Dedup**: invariato.
- **Dati storici già persistiti**: foto vecchie con `attachment_id=NULL` e documenti con bare `msg.id` restano non apribili (limite noto; il dedup impedisce la riscrittura). Recupero = migrazione dati, fuori scope.
- **Test esistenti da aggiornare**:
  - `tests/test_telegram.py::test_message_to_event_uses_document_filename_and_id_fallback` (~r.207): `attachment_id == "99"` → `== "tgref:42:99"`.
  - `Telegram/test_telegram_backend.py::TestMessageMapping::test_message_document_type` (r.277): `== "5"` → `== "tgref:111:5"`.
  - `tests/test_telegram.py::test_attachment_path_returns_existing_file_only`: invariato (fast path), estendere con caso tgref non-connesso → `None`.
  - `test_message_to_event_normalizes_media` (passa `"/tmp/media"`): invariato (path scaricato ha precedenza).
  - `test_message_photo_with_caption_uses_text_as_info`: invariato sulle asserzioni esistenti; aggiungere `attachment_id == "tgref:42:99"`.
  - `_make_backend()` in `Telegram/test_telegram_backend.py` usa `__new__` con init manuale: il design non richiede nuovi attributi d'istanza → nessun aggiornamento del factory.

---

## 4. Piano di test

Mock duck-typed (pattern esistente): `SimpleNamespace` per messaggi, `AsyncMock` per il client, monkeypatch di `asyncio.run_coroutine_threadsafe` con `lambda coro, _loop: SimpleNamespace(result=lambda timeout: asyncio.run(coro))`. Monkeypatch di `_media_dir()` verso `tmp_path`.

**`tests/test_telegram.py` — aggiornamenti:**
1. `test_message_to_event_uses_document_filename_and_id_fallback` → asserzione tgref.

**`tests/test_telegram.py` — nuovi:**
2. `test_message_photo_without_download_gets_tgref`: foto storico senza caption → `msg_type="image"`, `attachment_info="Photo"`, `attachment_id="tgref:42:99"`.
3. `test_message_photo_with_caption_gets_tgref`: caption `"che bello"` → `attachment_info="che bello"`, stesso tgref.
4. `test_get_attachment_path_tgref_not_connected`: tgref con `_client=None` → `None` senza eccezioni.
5. `test_get_attachment_path_tgref_loop_not_running`: loop mock con `is_running()=False` → `None` senza chiamare `run_coroutine_threadsafe`.
6. `test_get_attachment_path_tgref_lazy_download_success`: `get_input_entity`/`get_messages` AsyncMock; `msg.download_media = AsyncMock(return_value=str(target))` → ritorna `Path(target)`; verificare `get_messages` chiamato con `ids=99` (int).
7. `test_get_attachment_path_tgref_dedup_existing_file`: file target deterministico in `tmp_path` → path senza chiamare `download_media`.
8. `test_get_attachment_path_tgref_message_gone`: `get_messages` ritorna `None` → `None`.
9. `test_get_attachment_path_tgref_no_media`: messaggio senza `photo`/`document` → `None`, niente download.
10. `test_get_attachment_path_tgref_download_fails`: `download_media` solleva → `None`.
11. `test_get_attachment_path_tgref_future_raises`: `run_coroutine_threadsafe` solleva o `result()` solleva `TimeoutError` → `None`.
12. `test_fetch_history_photo_persists_tgref`: estendere il pattern di `test_disconnect_fetch_history_and_complete_2fa` (r.384-421) con messaggio foto: `ingest_message` riceve `attachment_id="tgref:42:<id>"` e `download_media` NON è chiamato durante il fetch (garanzia anti-eager).
13. Download mode: nessun test nuovo necessario in `test_download_mode.py` (risoluzione mockata via `manager.get_attachment_path`).

**`Telegram/test_telegram_backend.py` — aggiornamento + nuovo:**
14. `test_message_document_type` → tgref.
15. `test_message_photo_type`: aggiungere `attachment_id == "tgref:111:3"`.

---

## 5. Passi di implementazione ordinati

1. **`backends/telegram.py`**: helper di modulo `_media_dir()` (spostare import `os`/`tempfile` fuori dal metodo live).
2. **`_handle_new_message` (~682-692)**: usare `_media_dir()`. Nessun altro cambio.
3. **Costante modulo** `_TGREF_PREFIX = "tgref:"` e helper statico `_media_ref(chat_id, msg_id)` → `f"tgref:{chat_id}:{msg_id}"` (o `None` se `msg.id` manca).
4. **`_message_to_chat_event` (~727-743)**: fallback condiviso: se `att_id` vuoto **e** (`msg.photo` o `msg.document`) **e** `msg.id` → `att_id = _media_ref(chat_id, msg.id)`. Sostituisce l'attuale fallback solo-documento; il path live ha precedenza.
5. **`get_attachment_path` (96-99)**: catena a 4 passi (empty → is_file → parse tgref → lazy). Parse con `rsplit(":", 1)` + `int()` in `try/except (ValueError, TypeError)`.
6. **Nuovo `async _download_media_by_ref(chat_id: int, msg_id: int) -> Path | None`**: get_input_entity → get_messages(ids=int) → guard media → nome deterministico → dedup `is_file()` → `download_media(file=str(target))`.
7. Nessun cambio a `fetch_recent_history`, `ingest_message`, `_persist_message`, manager, base.

### Test
8. Aggiornare le 2 asserzioni documento.
9. Aggiungere i test 2-12 in `tests/test_telegram.py` e 15 in `Telegram/test_telegram_backend.py`.

### Verifica
10. `pytest tests/test_telegram.py Telegram/test_telegram_backend.py tests/test_download_mode.py tests/test_image_async_download.py tests/test_image_caption.py` + suite completa.

### Cosa NON fare
- Non scaricare media in `fetch_recent_history` (niente eager).
- Non aggiungere worker di risoluzione al mount da cache (`_build_message_widgets`).
- Non toccare `backends/manager.py`, `backends/base.py`, `tui/download.py`, `tui/chat_view.py`, `ui_components.py`, schema DB.
- Non riusare il bare `msg.id` come riferimento (manca il chat_id).
- Non fare migrazioni dati per le vecchie righe `NULL`/bare-id.
- Non estendere a sticker/video/voice/audio in questo fix (follow-up banale segnalato: il fallback tgref li coprirebbe, ma il loro `msg_type="attachment"` non monta `ImageWidget` → richiederebbe anche lavoro UI).
- Non chiamare il loop Telethon se `is_running()` è falso; non bloccare oltre 30s sul thread UI.
- Non cambiare la firma di `get_attachment_path` né il contratto `ChatBackend`.

**Rischio residuo principale**: freeze UI fino a 30s al click su una foto storica con rete lenta (identico al comportamento WhatsApp già accettato). Mitigazione documentata come follow-up, non parte di questo fix.
