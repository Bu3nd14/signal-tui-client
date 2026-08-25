# Design — editing dei messaggi (ottimistico con rollback, update in-place)

Come si modifica un messaggio proprio già inviato e come viene applicato un edit ricevuto da altri, su tutti e tre i protocolli. Ricavato da `tui/edit.py`, `tui/send.py`, `tui/events.py`, `tui/unread_reply.py`, `backends/base.py`, `backends/signal.py`, `backends/whatsapp.py`, `backends/telegram.py`, `backend/db.py` e dai relativi test (`test_edit_contract.py`, `test_edit_flow.py`, `test_edit_signal.py`, `test_edit_whatsapp.py`, `test_telegram_edit.py`, `test_db_edit.py`, `test_merge_cache_edit.py`).

## 1. Scope e gate di ingresso

Modificabili i **messaggi propri testuali già inviati**; gli edit ricevuti sono **update in place** della bolla esistente (mai una nuova). Fuori scope: edit di media/caption/sticker, cronologia multi-versione (il testo viene sovrascritto), delete-for-everyone.

Ingresso (Alt+click o Alt+E su una bolla → `MessageWidget.EditRequested`, gestito da `tui/edit.py::on_message_widget_edit_requested`). Il gate è a cascata, ogni rifiuto produce un messaggio in status bar e non tocca lo stato:

| Guardia | Effetto |
|---|---|
| download mode attivo (Ctrl+D) | richiesta ignorata (i click servono i file) |
| `is_mine=False` | ❌ "You can only edit your own messages" |
| `status ∈ {pending, failed}` | ❌ "Message not sent yet — cannot edit" |
| entry assente nella cache UI (match per `is_mine` + `timestamp`) | ❌ "Message not found in cache" |
| `msg_type != "text"` | ❌ "Only text messages can be edited" |
| id server assente (WhatsApp/Telegram) | ❌ "Server message ID unavailable — reopen the chat" |
| id server assente (Signal) | ⚠️ avviso non bloccante: `message_id = str(ts)` e "la modifica potrebbe non propagarsi" (5 s) — per Signal l'id di edit È il timestamp originale; se l'eco non ha ancora agganciato il vero ts server, l'edit può puntare al timestamp ottimistico sbagliato |

A valle del gate:

- **mutua esclusione reply↔edit**: aprire un edit cancella la reply pendente (`_cancel_reply`) e viceversa (l'arrivo di `MessageClicked`/`ReplyRequested` chiama `_cancel_edit`);
- stato `_editing_message`: snapshot `{protocol, contact_id, cache_key, timestamp, message_id, old_text, _widget}`; la bolla target viene evidenziata (`set_selected(True)`), l'input viene precaricato col testo vecchio (cursore a fine) e la reply bar mostra `✏️ Modifica: <testo>`;
- il submit passa dal normale flusso Enter: `send.py::on_message_text_area_submitted` vede `_editing_message` valorizzato e devia su `_submit_edit(message)` (dopo alias-emoji e strip).

## 2. Contratto cross-protocollo (`backends/base.py`)

Due metodi opzionali con default sicuri (pattern di `get_attachment_path`: nessuna rottura per backend futuri), fissati da `tests/test_edit_contract.py`:

| Metodo | Default | Semantica |
|---|---|---|
| `edit_message_sync(contact_id, message_id, new_text) -> bool` | `False` | bloccante, solo da worker thread. Semantica di `message_id`: **signal** = timestamp (ms) del messaggio originale come stringa; **telegram** = id server (int come stringa); **whatsapp** = Baileys message id (`true_…@c.us_ABC`) |
| `async edit_message(...)` | delega `edit_message_sync` via `asyncio.to_thread` | |
| `apply_edit(contact_id, message_id, new_text, *, is_mine=None, edit_timestamp=None) -> dict \| None` | `None` | punto UNICO di mutazione lato backend (cache in-memory + SQLite via `_update_message_text`); firma keyword-only per `is_mine`/`edit_timestamp`; mai eccezioni verso il chiamante |

`apply_edit` ritorna `None` quando non ha modificato nulla (target ignoto, `msg_type != "text"`, testo identico — idempotente sugli eco) e in caso di modifica:

```python
{"message_id": str, "timestamp": int, "old_text": str, "text": str, "is_mine": bool}
```

con `timestamp` MAI modificato (identità temporale stabile su tutti i protocolli: cambia solo il testo).

Routing `BackendManager.edit_message_sync(protocol, …)`: `False` per protocollo sconosciuto o backend senza override, altrimenti propaga il risultato.

Implementazioni concrete dell'invio:

- **Signal**: daemon → RPC `send` con `editTimestamp` (errore RPC → `RuntimeError` propagata al worker); fallback subprocess `--edit-timestamp`.
- **WhatsApp**: REST `PUT /api/{session}/chats/{chatId}/messages/{messageId}` body `{"text", "linkPreview": true}`.
- **Telegram**: `client.edit_message(entity, message_id, text)` sull'event loop del backend.

Nota: Signal solleva `RuntimeError` sull'errore RPC invece di ritornare `False` — il worker tratta eccezioni e `False` allo stesso modo (rollback).

## 3. Flusso ottimistico con rollback (`tui/edit.py`)

```
submit (UI thread)                worker thread                     UI thread
──────────────────                ─────────────                     ──────────
_submit_edit(new_text)
  ├ testo identico → solo cancel
  ├ _apply_local_edit(snap)   ─►  _edit_message_worker
  │   (cache UI + apply_edit      ├ backend assente ──────────────► _restore_local_edit
  │    + identità + widget)       ├ eccezione rete/server ────────► _restore_local_edit
  └ _cancel_edit()                ├ ok=False ("rejected") ────────► _restore_local_edit
                                  └ ok=True  ─────────────────────► status "✏️ Message edited"
```

- **Applica locale** (`_apply_local_edit`): aggiorna la entry della cache UI (`text`, `edited=True`), chiama `backend.apply_edit(...)` (che scrive cache backend + SQLite — **sul thread UI**: stesso limite noto documentato in [ARCHITECTURE_OVERVIEW.md §3.1](../architecture/ARCHITECTURE_OVERVIEW.md)), riscrive le identità (§4) e riscrive la bolla con `widget.update_text(new_text)`.
- **Worker** (`_edit_message_worker`, thread): `backend.edit_message_sync(...)`; qualunque esito negativo (`False`, eccezione, backend assente) torna alla UI con `call_from_thread(self._restore_local_edit, ...)`.
- **Rollback** (`_restore_local_edit`): ripristino speculare e completo — entry della cache UI al testo vecchio con `edited=False`, `apply_edit(old_text)` su DB+cache backend, chirurgia identità inversa (new→old), widget con `update_text(old_text, edited=False)`, status `❌ Edit failed: <causa>`.

## 4. Chirurgia dell'identità (`_rewrite_message_identity`)

L'identità di render è `(protocol, cache_key, ts, text)` (+ variante `(protocol, cache_key, id, text)`); un edit cambia SOLO il testo, quindi senza intervento `_refresh_chat` rimontarebbe il messaggio modificato come NUOVO (duplicato) e `_shown_in_log` non lo riconoscerebbe più.

```python
for s in (self._seen_message_ids, self._shown_in_log):
    s.discard((protocol, cache_key, int(ts), old_text)); s.add((protocol, cache_key, int(ts), new_text))
    if message_id:
        s.discard((protocol, cache_key, str(id), old_text)); s.add((protocol, cache_key, str(id), new_text))
# _seen_timestamps NON si tocca: il timestamp non cambia mai.
```

La stessa funzione è riusata dall'handler degli edit ricevuti e, in forma inversa, dal rollback.

## 5. Edit ricevuti: update in place (`tui/events.py::_handle_edit_event`)

Payload `message_edit`: `{edit_message_id, text, timestamp (ts ORIGINALE), edit_timestamp, is_mine, sender, contact?, msg_type: "text"}`. Il dispatch UI ordina typing → receipt → **message_edit** → message.

1. `backend.apply_edit(edit_id, new_text, is_mine=..., edit_timestamp=...)`: punto unico di mutazione (cache backend + DB). Ritorna `None` → evento scartato silenziosamente (idempotenza sugli eco).
2. Mirror nella cache UI: match **per id**, poi fallback `(timestamp, is_mine, text == old_text)`; su hit: riscrittura identità + `target["text"] = new_text`, `target["edited"] = True`.
3. Widget solo se la chat è aperta (`_update_edited_widget`): match per `_message_id`, poi `(_msg_timestamp, _msg_text == old_text)`; `update_text(new_text)` riscrive la bolla ESISTENTE — mai mount di una bolla nuova, mai bump unread.

### Edit riferito a un messaggio mai visto

Comportamento divergente per protocollo (contratto fissato nei test di flusso):

- **Signal / Telegram**: l'evento `message_edit` viene emesso incondizionatamente; `apply_edit` ritorna `None` e l'handler scarta l'evento → nessuna bolla finché un fetch storico non porta il messaggio (già editato).
- **WhatsApp**: `handle_webhook` rileva l'edit solo se il target è in cache (`_detect_edit`); se assente il pacchetto degrada a evento `message` sintetico → la bolla nasce direttamente col testo GIÀ editato.

## 6. Marker "(modificato)" e persistenza

- Rendering: `MessageWidget._build_content` appende `" (modificato)"` quando `_edited`; `update_text(new_text, edited=True)` aggiorna testo+flag in place (default `True`: ogni riscrittura marca modificato).
- Persistenza: colonna `edited INTEGER NOT NULL DEFAULT 0` in `messages` (aggiunta **fuori** dal gate `PRAGMA user_version`, perché anche DB già a v3 possono non averla); `_merge_backend_cache` marca `edited=True` quando la riconciliazione aggiorna un testo in place.
- Un doppio edit consecutivo è supportato: il secondo gate rilegge la cache aggiornata.

## 7. Limiti noti

1. **Doppio uso di `apply_edit`**: dichiarato per gli edit ricevuti ma usato anche dal path ottimistico locale (`_apply_local_edit`/rollback), con scrittura SQLite sul thread UI (freeze potenziale sotto contesa del lock DB). Separare nominalmente i due contratti (es. `apply_local_edit`) è la direzione consigliata.
2. **Edit su messaggio mai visto**: scartato in modo silenzioso su Signal/Telegram (nessuna bolla finché non arriva lo storico); su WhatsApp la bolla nasce come "nuovo" già editato. Coerenza DB garantita in entrambi i casi.
3. **Signal senza id server**: l'edit parte comunque con `message_id = str(ts ottimistico)`; se signal-cli aveva assegnato un ts diverso, la modifica può non propagarsi (avviso esplicito in status bar).
4. **Nessuna atomicità tra i layer**: cache UI → DB/backend → identità → widget sono mutazioni sequenziali; un crash a metà lascia stati divergenti fino al reload della chat.
5. **Solo ultima versione**: il testo precedente è perso (nessuna cronologia edit, scelta di scope).

## Test che documentano questo design

- `tests/test_edit_contract.py` — default `False`/`None`, firma keyword-only, routing manager, delega async.
- `tests/test_edit_flow.py` — gate (non-miei/pending/media), submit ottimistico, rollback su fallimento, mutua esclusione reply↔edit, nessun duplicato dopo edit, doppio edit consecutivo, edit ricevuto con chat aperta/chiusa.
- `tests/test_edit_signal.py`, `tests/test_edit_whatsapp.py`, `tests/test_telegram_edit.py` — invio edit per protocollo (editTimestamp / REST PUT / Telethon) ed echo.
- `tests/test_db_edit.py` — `_update_message_text` (match per id/timestamp, flag `edited`).
- `tests/test_merge_cache_edit.py` — riconciliazione edit nel merge cache (id-first, `edited=True`).
- `tests/test_ui_components.py` — suffisso "(modificato)", `update_text`, Alt+click/Alt+E.
