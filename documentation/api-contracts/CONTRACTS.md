# Contratti di dati

Contratti concreti ricavati dal codice e fissati dai test. Ogni sezione cita i test che la documentano. Tipi Python; timestamp in millisecondi Unix salvo indicazione diversa.

Indice:

1. [Modelli condivisi (`models.py`)](#1-modelli-condivisi-modelspy)
2. [Dizionario messaggio (cache entry)](#2-dizionario-messaggio-cache-entry)
3. [Persistenza SQLite](#3-persistenza-sqlite)
4. [Eventi `ChatEvent`](#4-eventi-chatevent)
5. [Stato dei messaggi inviati](#5-stato-dei-messaggi-inviati)
6. [Contratto di edit dei messaggi](#6-contratto-di-edit-dei-messaggi)
7. [Receipt per protocollo](#7-receipt-per-protocollo)
8. [Contatti, raggruppamento e unread](#8-contatti-raggruppamento-e-unread)
9. [Payload webhook WAHA](#9-payload-webhook-waha)
10. [Errori e convenzioni di ritorno](#10-errori-e-convenzioni-di-ritorno)

---

## 1. Modelli condivisi (`models.py`)

### 1.1 `ChatContact`

| Campo | Tipo | Semantica |
|---|---|---|
| `id` | `str` | identificativo unico nello scope del protocollo (numero Signal, JID WhatsApp, id Telegram) |
| `display_name` | `str` | nome mostrato (fallback: `id`) |
| `protocol` | `str` | `"signal"` \| `"whatsapp"` \| `"telegram"` |
| `extras` | `dict[str, Any]` | metadati protocollo: `phone` (E.164 senza `+`, chiave di grouping cross-backend), `last_message_ts` (ms), `address_book` (marker rubrica), `ghost`, ecc. |

Derivati: `cache_key = f"{protocol}:{id}"`; property `phone`, `last_message_ts`.

Test: `tests/test_contact_grouping.py`, `tests/test_address_book.py`.

### 1.2 `ChatMessage`

| Campo | Tipo | Default | Semantica |
|---|---|---|---|
| `id` | `str` | — | id messaggio scope-protocollo (ts per Signal, id server Telegram, Baileys id WhatsApp) |
| `contact_id` / `protocol` | `str` | — | appartenenza |
| `text` | `str` | — | corpo (vuoto per media puri) |
| `is_mine` | `bool` | — | direzione |
| `sender` | `str` | — | autore visualizzato |
| `timestamp` | `int` | — | ms |
| `quote_text` | `str \| None` | `None` | testo citato |
| `msg_type` | `str` | `"text"` | `text` \| `image` \| `sticker` \| `attachment` |
| `attachment_info` / `attachment_id` | `str \| None` | `None` | dettagli/id attachment |
| `status` | `str` | `"sent"` | vedi §5 |
| `reply_to_message_id` | `str \| None` | `None` | id server del messaggio a cui si risponde |

Nota d'uso: nel codice reale i messaggi viaggiano prevalentemente come **dict** (vedi §2); il dataclass è il contratto nominale.

### 1.3 Costanti di protocollo

`PROTOCOL_SIGNAL="signal"`, `PROTOCOL_WHATSAPP="whatsapp"`, `PROTOCOL_TELEGRAM="telegram"`; mappe `PROTOCOL_EMOJI` (`📱 💬 📨`) e `PROTOCOL_NAMES`; `protocol_emoji()` fallback `"💬"`.

## 2. Dizionario messaggio (cache entry)

Forma usata da backend cache, cache UI e mirror eventi (`tui/events.py::_handle_message_event`, fixture `sample_messages` in `tests/conftest.py`). Chiavi osservate:

```python
{
    "id": str | None,  # id server/stabile (None nelle righe ottimistiche)
    "text": str,
    "is_mine": bool,
    "sender": str,  # "You" per gli outgoing
    "timestamp": int,  # ms
    "quote_text": str | None,
    "msg_type": "text" | "image" | "sticker" | "attachment",
    "attachment_info": str | None,
    "attachment_id": str | None,  # per Telegram anche "tgref:<chat_id>:<msg_id>"
    "read": bool,  # incoming letti (outgoing sempre True)
    "status": "pending" | "failed" | "sent" | "delivered" | "read",
    # opzionali:
    "quote_timestamp": int | None,
    "quote_author": str | None,  # contact id, non display name
    "reply_to_message_id": str | None,
    "edited": bool,  # flag "(modificato)"
    "protocol": str,  # presente nelle righe caricate dal DB
}
```

Vincoli semantici verificati nei test:

- l'ingest è idempotente (`ingest_message` ritorna `True` solo se nuovo);
- le righe ottimistiche nascono `status="pending"`, `id=None`;
- `sender_color`: nei gruppi WhatsApp (`@g.us`) il prefisso `<nome:>` usa `#DAA520` (`test_image_caption.py`, `test_ui_components.py`).

## 3. Persistenza SQLite

### 3.1 Tabella `messages`

```
id INTEGER PK AUTOINCREMENT
protocol        TEXT NOT NULL DEFAULT 'signal'
contact_number  TEXT NOT NULL          -- id grezzo del contatto (senza prefisso protocollo)
text TEXT · is_mine INTEGER · sender TEXT · timestamp INTEGER NOT NULL
quote_text TEXT · msg_type TEXT DEFAULT 'text' · attachment_info TEXT · attachment_id TEXT
read INTEGER DEFAULT 0 · status TEXT DEFAULT 'read'
msg_id TEXT · quote_timestamp INTEGER · quote_author TEXT · reply_to_message_id TEXT
edited INTEGER NOT NULL DEFAULT 0
-- indice: idx_messages_contact (protocol, contact_number, timestamp)
```

Migrazioni additive idempotenti gate-ate da `PRAGMA user_version=3`; la colonna `edited` è aggiunta **sempre** se manca (anche oltre v3). Test: `tests/test_db_schema_versioning.py`, `tests/test_migrate_sqlite.py`, `tests/test_migrate_protocol.py`, `tests/test_migrate_status.py`.

### 3.2 Regole transazionali

- default status all'INSERT: `"sent"` se `is_mine` else `"read"`; `read = is_mine`.
- rank guard su ogni update di status: `pending=0 failed=0 < sent=1 < delivered=2 < read=3` (mai downgrade).
- **Invariante attesa sul `msg_id`**: un `msg_id` dovrebbe appartenere a una sola riga per `(protocol, contact_number)`. L'enforcement MANCA oggi: `_update_message_id` può assegnare lo stesso id a piu' righe id-less (UPDATE multi-riga senza finestra né LIMIT) e `_dedup_messages_by_id` — eseguita dentro `_load_cache` a ogni boot — riduce la partizione a una riga sola. Con messaggi ripetuti (es. retry con stesso testo) questo può **cancellare righe legittime** al riavvio: vedere l'avviso su `_update_message_id` in [API_OVERVIEW.md](API_OVERVIEW.md) §4 prima di considerare la dedup "idempotente e sicura".
- pruning: 200 righe più recenti per `(protocol, contact_number)` — applicata solo dal resync WhatsApp (unico call site di `_prune_cache`).

Test: `tests/test_backend_cache.py`, `tests/test_db_edit.py`, `tests/test_merge_cache_edit.py`.

## 4. Eventi `ChatEvent`

```python
ChatEvent(type: str, protocol: str, contact_id: str, payload: dict)
```

| type | Payload (campi e tipi) | Note |
|---|---|---|
| `message` | come §2 + `contact: ChatContact \| None`; WhatsApp ack sintetici aggiungono `is_group: bool` | ingest via `backend.ingest_message(contact_id, payload, ts)` |
| `message_edit` | `{edit_message_id: str, text: str, timestamp: int (ts ORIGINALE), edit_timestamp: int\|None, is_mine: bool, sender: str, contact: ChatContact\|None, msg_type: "text"}` | consumato da `_handle_edit_event`; chiama `backend.apply_edit(...)` |
| `typing` | `{action: "STARTED"\|"STOPPED"}` | effimero: mai in cache/chat log |
| `receipt` | Signal `{receipt: {isDelivery: bool, isRead: bool, timestamps: [int]}}`; WA/TG `{message_ids: [str], is_read: bool}` | vedi §7 |
| `contact_update` | definito in `models.ChatEvent` (payload ChatContact dict) | nessun dispatcher UI dedicato al momento |

Il dispatch UI (`tui/events.py::_handle_event`) gestisce nell'ordine: typing → receipt → message_edit → message; tipi ignoti → ignorati.

Test trasversali: `tests/test_ui_protocol.py`, `tests/test_backends.py`, fixture envelope in `tests/conftest.py` (`sample_envelope_text/image/receipt`).

## 5. Stato dei messaggi inviati

Valori e ordine (rank): `pending`(0) · `failed`(0) · `sent`(1) · `delivered`(2) · `read`(3). Transizioni ammesse solo in avanti (rank guard).

- bolla ottimistica: `pending` alla submit;
- worker OK → `sent` (+ echo id ingest);
- errore/no-backend → `failed` (bolla cliccabile per retry);
- receipt delivery/read → `delivered`/`read`.

Rendering (`MessageWidget`): pending dim, failed bold+classe `msg-failed`, sent italic, delivered bold, read normale.

Dettaglio completo del flusso: [../design/DESIGN_OUTGOING_MESSAGE_STATUS.md](../design/DESIGN_OUTGOING_MESSAGE_STATUS.md).
Test: `tests/test_failed_send_status.py`, `tests/test_outgoing_status_fallback.py`, `tests/test_send_persist_offthread.py`.

## 6. Contratto di edit dei messaggi

Fissato da `tests/test_edit_contract.py` e implementato in `backends/base.py` + `apply_edit` dei tre backend:

- `edit_message_sync(contact_id, message_id, new_text) -> bool`
  - default base: **`False`** (nessuna eccezione);
  - semantica di `message_id` per protocollo:
    - **signal**: timestamp (ms) del messaggio originale, come stringa;
    - **telegram**: id server (int come stringa);
    - **whatsapp**: Baileys message id (es. `true_39...@c.us_ABC`);
  - `BackendManager.edit_message_sync(protocol, ...)`: `False` per protocollo sconosciuto o backend senza supporto, altrimenti propaga il risultato del backend.
- `async edit_message(...)`: wrapper che delega a `edit_message_sync` via `asyncio.to_thread` (default `False`).
- `apply_edit(contact_id, message_id, new_text, *, is_mine=None, edit_timestamp=None) -> dict | None`
  - default base: **`None`**; firma keyword-only per `is_mine`/`edit_timestamp`;
  - punto UNICO di mutazione (cache backend + SQLite via `_update_message_text`);
  - ritorna `None` quando: target ignoto, `msg_type != "text"` (mai riscrivere label media), testo identico (idempotente, es. eco del proprio edit);
  - ritorna in caso di modifica:

    ```python
    {
        "message_id": str,
        "timestamp": int,  # ts della entry, MAI modificato
        "old_text": str,
        "text": str,
        "is_mine": bool,
    }
    ```

- Vincoli UI: solo messaggi propri, non `pending`/`failed`, solo testo; rollback completo (testo, flag edited, identity sets, widget) se il server rifiuta.

### 6.1 Uso duale di `apply_edit` ed edit su messaggio mai visto

- `apply_edit` è dichiarato come punto unico di mutazione per gli edit **ricevuti** (inbound), ma la UI lo usa anche per l'edit **locale ottimistico** (`tui/edit.py::_apply_local_edit`), eseguendo la scrittura SQLite sul thread UI. Il doppio uso rende il contratto ambiguo per chi implementa un nuovo backend: separare nominalmente i due casi (es. `apply_local_edit`) è la direzione consigliata.
- Edit riferito a un messaggio MAI visto (target non in cache): comportamento diverso per protocollo:
  - **Signal / Telegram**: l'evento `message_edit` viene emesso incondizionatamente; `apply_edit` ritorna `None` e l'handler UI scarta silenziosamente l'evento (`if not info: return False`) → nessuna bolla finché un fetch storico non porta il messaggio (già editato);
  - **WhatsApp**: `handle_webhook` rileva l'edit solo se il target è in cache (`_detect_edit`); se assente, il pacchetto degrada a evento `message` sintetico → la bolla viene creata col testo GIÀ editato (visibile, ma come messaggio nuovo).

Test correlati: `tests/test_edit_flow.py`, `tests/test_edit_signal.py`, `tests/test_edit_whatsapp.py`, `tests/test_telegram_edit.py`, `tests/test_db_edit.py`, `tests/test_merge_cache_edit.py`.

## 7. Receipt per protocollo

| Protocollo | Payload evento | Matching verso i messaggi | Test |
|---|---|---|---|
| Signal | `{receipt: {"isDelivery": bool, "isRead": bool, "timestamps": [int ms]}}` | fuzzy `±1000 ms` sui timestamp propri (`is_mine=True`), rank guard | `tests/conftest.py::sample_envelope_receipt`, `tests/test_ui_protocol.py` |
| WhatsApp | `{message_ids: [...], is_read: bool}`; enum ack WAHA `-1..4` (`DEVICE=2 → delivered`, `READ=3 → read`) | confronto tramite `canonical_msg_id()`: riduce `true_{jid}_{hex}` / hex puro allo stesso token upper-case; forme ambigue restano raw (log mismatch) | `tests/test_whatsapp_receipt_id_match.py`, `tests/test_whatsapp_read_receipt_fix.py` |
| Telegram | `{message_ids: [...], is_read: bool}` (raw update) | update DB per `_update_message_status_by_id(msg_id)` | `tests/test_telegram_read_receipt_fix.py` |

Effetti comuni: upgrade status in cache backend/UI/DB + refresh widget se la chat è aperta.

## 8. Contatti, raggruppamento e unread

- **Grouping**: `group_by_person(contacts)` (`contact_picker.py`) raggruppa i contatti per persona usando `extras["phone"]` (E.164 senza `+`); `PickerEntry.key` identifica il gruppo; ordine gruppi = recency del membro default (`entry_default_contact` + `contact_sort_key`); membri in ordine fisso Signal→WhatsApp→Telegram (`_protocol_priority`).
  Test: `tests/test_contact_grouping.py`, `tests/test_contact_grouping_integration.py`, `tests/test_contact_picker.py`.
- **Unread**: conteggio sui dati della cache UI: `not is_mine AND not read`, dedup per `(timestamp, text)`; chiave `cache_key`; badge `*N` sull'header aggregato (vista All) o sul membro filtrato; il contatto selezionato non mostra badge ed è "pinned" sotto filtro unread; totali per backend nella status bar (`📱 N 💬 N 📨 N`, `-` se 0).
  Test: `tests/test_unread_filter.py`, `tests/test_status_backend_unread.py`, `tests/test_typing_indicator.py` (label riga).
- **Typing**: `✍️` mentre `STARTED` attivo (auto-expire 10 s → stato mumbling `💭` per 60 s); un messaggio reale del contatto sposta in mumbling; `STOPPED` → mumbling.
  Test: `tests/test_typing_indicator.py`.

## 9. Payload webhook WAHA

Envelope ricevuto su `POST .../webhook`:

```json
{"event": "message", "session": "...", "payload": { ... }}
```

- `event` riconosciuti: `message`, `message.ack` (anche varianti con suffisso), presence/typing, receipt (ack ≥ 2).
- Campi `payload` consumati (via `_jid_string` tollerante alle forme stringa/dict): `from`, `to`/`chatId`/`remoteJid` (o `key.remoteJid`), `body`/`text`, `fromMe`, `timestamp` (**secondi** → convertiti in ms), `id`, `hasMedia`/`media{mimetype,url,caption,filename}`, `ack`/`ackName`, `type`/`messageType`.
- Contratto risposta: sempre `200 {"ok": true}` dopo l'inoltro; `404 {"ok": false}` path errato; `400` JSON malformato.

Ack enum (`whatsapp_events.py`): `ERROR=-1, PENDING=0, SERVER=1, DEVICE=2, READ=3, PLAYED=4`.
Test: `tests/test_backend_webhook.py`, `tests/test_whatsapp_backend.py`.

## 10. Errori e convenzioni di ritorno

| Superficie | Convenzione |
|---|---|
| `SignalRPCClient._call` | mai eccezioni: `{"error": "<msg>"}`; i wrapper convertono in `[]`/`RuntimeError` |
| Client REST WAHA (`_request`) | non-raising: `None`/`[]` su errore (401 senza API key incluso) |
| Download (`serve_*`) | URL stringa oppure messaggio `ERROR:` prefixato |
| `BackendManager.send_message/mark_read` | `KeyError` per protocollo non registrato |
| `find_signal_cli()` / `_require_user_number()` | eccezioni canoniche (`FileNotFoundError`, `RuntimeError`) solo al punto d'uso; varianti `_find_signal_cli()` / `_get_user_number()` non-raising |
| `list_address_book_sync` | mai eccezioni verso il chiamante: errori in `manager.address_book_errors[protocol]`, risultato parziale |
| Dedup by-id al boot (`_load_cache` → `_dedup_messages_by_id`) | riduce le partizioni `(protocol, contact_number, msg_id, text)` alla riga col rank status più alto; NON è safe se lo stesso `msg_id` è stato assegnato a più righe (vedi §3.2, invariante non ancora enforcement) |
| Config (`config.py`) | fallback a default (`""`/3005/8088/…) senza raise |

Test: `tests/test_backend_rpc.py`, `tests/test_config.py`, `tests/test_backend_download.py`, `tests/test_address_book.py`.
