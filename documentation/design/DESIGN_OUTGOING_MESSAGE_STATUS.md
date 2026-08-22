# Design — protocollo di stato dei messaggi in uscita

Come una bolla inviata passa da `pending` a `sent`/`delivered`/`read` (o `failed`), su quali chiavi avvengono le transizioni e come si evitano downgrade e doppioni. Ricavato da `tui/send.py`, `tui/events.py`, `tui/unread_reply.py`, `backend/db.py`, `backends/signal.py`, `backends/whatsapp_events.py`, `ui_components.py::MessageWidget`.

## 1. Macchina a stati

```
            send submit                    worker OK                 receipt delivery        receipt read
 (UI thread) ────────►  PENDING  ─────────►  SENT  ────────────►  DELIVERED  ────────────►  READ
                            │
                            └── errore backend / no backend / eccezione ──►  FAILED
```

- Valori ammessi (vedi `models.ChatMessage.status`): `"pending"`, `"failed"`, `"sent"`, `"delivered"`, `"read"` (default `"sent"`).
- Rank canonico, definito identicamente in più punti (`chat_view._STATUS_RANK`, `db._update_message_status*`, `rpc._process_receipt`, dedup DB):

  ```
  pending=0  failed=0  sent=1  delivered=2  read=3     # mai in downgrade
  ```

  Attenzione: la tabella è **duplicata in 7 punti** (3 volte come SQL `CASE` in `backend/db.py`, 4 come dict in `backends/whatsapp.py`, `telegram.py`, `backend/rpc.py`, `tui/chat_view.py`, `tui/backend_connect.py`): qualsiasi modifica agli stati va fatta in tutti — rischio drift concreto. Mitigazione proposta: costante unica in `models.py` importata ovunque.

- Rendering (`MessageWidget._apply_status_style`, solo per `is_mine=True`):
  - `pending` → dim + classe `msg-pending`;
  - `failed` → bold + classe `msg-failed` (la bolla è cliccabile per il retry);
  - `sent` → *italic*;
  - `delivered` → **bold**;
  - `read` → stile normale.

## 2. Flusso ottimistico (tui/send.py)

1. **Submit** (UI thread): timestamp client `ts = int(time.time()*1000)`; il messaggio viene ingerito nel backend corretto con `status="pending"` e `ingest_message(..., persist=False)` (cache senza DB) e specchiato nella cache UI; la bolla nasce subito grigia.
2. **Persist off-thread**: il worker `_send_message_worker` riceve `(backend, contact_id, data, ts)` e chiama `_persist_message(...)` PRIMA dell'invio di rete: così l'echo (spesso più veloce del worker) trova sempre la riga da aggiornare (`test_send_persist_offthread.py`).
3. **Invio sincrono**: `backend.send_message_sync(contact_id, text, quote_*, reply_to_message_id)` — bloccante ma in worker thread; la durata è loggata (warning sopra i 1000 ms).
4. **Transizione a `sent`**: `_transition_outgoing_status(protocol, contact_id, ts, text, "sent", ("pending",))` aggiorna atomicamente tutti i layer.
5. **Ingest echo**: l'id server restituito dal backend viene ingerito (`ingest_message`) e scritto via `_update_message_id`: la riga ottimistica (senza id) acquisisce l'id reale senza duplicati e senza cambiare il proprio timestamp.

## 3. Chiavi delle transizioni e fallback

`_transition_outgoing_status` (e le controparti DB) provano in ordine:

1. **`(protocol, contact_number, timestamp, [text])`** — via `_update_message_status`; il match è sempre scoped per protocollo+contatto perché millisecondi diversi possono coincidere tra chat diverse;
2. **by-text**: `_update_message_status_by_text` — riga outgoing più recente con lo stesso testo (stesso expected-status e rank guard); serve quando l'echo ha già sostituito il timestamp ottimistico con quello server;
3. **by-id**: `_update_message_status_by_id(msg_id, ...)` — usata soprattutto dai receipt Telegram/WhatsApp che identificano i messaggi per id server.

Se anche il fallback fallisce, viene loggato un warning diagnostico con i conteggi righe-per-testo/righe-per-timestamp (visibile in `send.py`).

### Limiti noti della macchina a stati

1. **`sent` → `failed` è impossibile col rank guard**: `sent=1 > failed=0` e la guardia `<=` rifiuta il downgrade; inoltre il ramo di fallimento di `_transition_outgoing_status` usa `expected_statuses=("pending",)`. Se l'echo arriva prima dell'errore, la bolla resta "sent" per sempre. Permettere la transizione richiederebbe una decisione esplicita (es. `expected_statuses=("pending","sent")` nel path fallimento).
2. **Fallback by-text ambiguo su testi ripetuti**: `_update_message_status_by_text` aggiorna "la riga outgoing più recente con quel testo": con testi ripetuti ("ok") può avanzare la bolla sbagliata lasciandone un'altra `pending`. Servirebbe anche un vincolo temporale (finestra attorno al timestamp atteso).
3. **Aggiornamento a 4 store non atomico**: `_transition_outgoing_status` aggiorna in sequenza DB → backend cache → UI cache → widget (via `call_from_thread`): nessuna atomicità; un crash a metà lascia stati divergenti fino al reload della chat.
4. **Fallback id-less dei receipt WhatsApp**: quando manca lo scope del contatto, la ricerca si estende ad altre chat — con una sola chat pending l'upgrade può colpire la chat sbagliata; con più chat il receipt va perso (safe ma silenzioso).

## 4. Receipt in arrivo

- **Signal**: envelope `receiptMessage` con `isDelivery`/`isRead` e lista `timestamps`; matching fuzzy `±1000 ms` (signal-cli può perturbare leggermente il timestamp passato a `send`), rank guard, ritorna la lista dei messaggi aggiornati (`backend/rpc.py::_process_receipt`).
- **WhatsApp**: eventi `message.ack` tradotti in receipt con l'enum ufficiale WAHA (`WAHA_ACK_DEVICE = 2 → delivered`, `WAHA_ACK_READ = 3 → read`); gli id sono confrontati tramite `canonical_msg_id()` che riduce le forme `true_{jid}_{hex}` / `{hex}` a un unico token (`backends/whatsapp_events.py`). Il matching per id è coperto dai test `test_whatsapp_receipt_id_match.py`.
- **Telegram**: read/delivery receipt per message-id server (`process_receipt` + `_update_message_status_by_id`).

La UI applica gli aggiornamenti (`events.py::_handle_receipt_event`):
1. mirror dello status nella cache UI (match per id, poi per ts, poi fuzzy per testo entro ±2000 ms con self-heal dell'id);
2. aggiornamento visuale dei widget aperti con indici O(M) (`_update_message_widgets_status`: by_id → (ts,text) → fuzzy bound-to-text).

## 5. Fallimenti

- Backend assente per il protocollo → `failed` immediato + status bar persistente.
- Eccezione durante `send_message_sync` → `failed` + status bar con l'errore.
- Reply Telegram senza `reply_to_message_id` valido → rifiuto PRIMA di creare la bolla (nessun pending orfano).
- Click su bolla `failed` → `_retry_failed_message(timestamp, text)` (re-invio).

## Test che documentano questo design

- `tests/test_failed_send_status.py` — transizione a failed e retry.
- `tests/test_outgoing_status_fallback.py` — fallback by-text della transizione pending→sent.
- `tests/test_send_persist_offthread.py` — persistenza ottimistica nel worker prima della rete.
- `tests/test_send_timing.py`, `tests/test_signal_real_timestamp.py` — timing/echo del vero timestamp server.
- `tests/test_whatsapp_receipt_id_match.py`, `tests/test_telegram_read_receipt_fix.py`, `tests/test_whatsapp_read_receipt_fix.py` — receipt per id/canonicalizzazione.
- `tests/test_ui_components.py`, `tests/test_ui_protocol.py` — stili visuali per status.
