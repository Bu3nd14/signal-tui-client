# Design — identità dei messaggi, cache e batching

Come il sistema identifica i messaggi, quanti livelli di cache esistono, come funziona la dedup e perché il rendering è differito/raggruppato. Ricavato da `models.py`, `tui/events.py`, `tui/chat_view.py`, `tui/edit.py`, `tui/unread_reply.py`, `backends/signal.py`, `backends/whatsapp.py`, `backend/db.py` e dai relativi test (`test_refresh_chat.py`, `test_merge_cache_edit.py`, `test_cache_debounce.py`, `test_unread_filter.py`).

## 1. Il problema: l'identità non è un timestamp

I timestamp hanno granularità al secondo (e alcuni protocolli riscrivono il timestamp client con quello server). Due messaggi distinti nello stesso secondo sono indistinguibili se si usa solo `ts`; un edit cambia il testo mantenendo ts; un echo può arrivare prima o dopo il worker di invio. Da qui le scelte di design:

- **Chiave contatto namespaced**: `contact_cache_key(protocol, id) = f"{protocol}:{id}"` — lo stesso numero su Signal e WhatsApp è un contatto diverso.
- **Identità di rendering**: `(protocol, cache_key, timestamp, text)`; in aggiunta viene tracciata anche `(protocol, cache_key, message_id, text)` quando esiste un id.
- **Identità temporale immutabile**: un edit cambia SOLO il testo (e la flag `edited`) — mai `timestamp` né `id`.

Tre set di dedup convivono nella UI:

| Set | Chiave | Scopo |
|---|---|---|
| `_seen_timestamps` | `(protocol, key, ts)` | filtro "solo più nuovi" di `_refresh_chat` |
| `_seen_message_ids` | `(protocol,key,ts,text)` + `(protocol,key,id,text)` | dedup stessa-secondo e stabilità via id |
| `_shown_in_log` | come sopra | guardia finale dentro `_add_message`: mai montare due volte la stessa bolla nella vista corrente |

All'edit, `_rewrite_message_identity()` scarta le vecchie identità `(…, old_text)` e registra quelle nuove `(…, new_text)` nei set, altrimenti `_refresh_chat` rimonterebbe il messaggio modificato come nuovo duplicato. `_seen_timestamps` non si tocca (il ts non cambia).

## 2. I quattro livelli di cache

```
servizi esterni
      │  (webhook / SSE / MTProto / REST)
      ▼
[1] backend.cache            {contact_id grezzo: [msg_dict]}   — per backend, ordinata
      │  ingest_message() (dedup per protocollo)
      ▼
[2] UI self._cache           {cache_key "proto:id": [msg_dict]}
      │  mirror in tui/events.py::_handle_message_event
      ▼
[3] widget nel #chat-log     bolle MessageWidget/ImageWidget (identità in _shown_in_log)
      │
[4] SQLite messages.db       righe incrementali (INSERT per messaggio, _DB_LOCK)
```

- **Livello [1]** è gestito dal backend: dedup specifica di protocollo, ordinamento deterministico `(timestamp, id|testo)`, upgrade dell'id ottimistico con quello server.
- **Livello [2]** alimenta lista contatti (unread, last_message_ts), finestra di chat e reload; è add-only verso il backend (`_merge_backend_cache` non può mai farla "restringere").
- **Livello [3]** è volatile: svuotato a ogni cambio contatto (`_clear_chat` resetta anche i set).
- **Livello [4]** è la verità across-session: al boot ogni backend ricarica solo le proprie righe (`_load_cache(protocol=...)`) e la dedup per `msg_id` previene ri-ingest.

## 3. Regole di dedup per ingest (per protocollo)

### Signal (`SignalBackend._message_already_cached`)
- incoming: stesso testo + `|Δts| ≤ 2000 ms` (ridelivery/sync multi-device);
- outgoing echo: stesso testo + `|Δts| ≤ 5000 ms`;
- branch upgrade: un echo outgoing con id reale aggancia l'id al gemello ottimistico **senza** toccare il suo timestamp ottimistico.

### WhatsApp (`WhatsAppBackend._message_already_cached`)
- outgoing: **id-first** (l'id Baileys è stabile); fallback testo + `_ECHO_MATCH_WINDOW_MS = 600000` (10 min);
- incoming: id match quando possibile, altrimenti testo + fuzzy `±5000 ms` (gli id webhook e REST possono divergere);
- guardia anti-retry webhook: chiave `(contatto, id, testo whitespace-normalizzato)` in `_seen_message_keys`.

### Telegram
- set `_seen_msg_ids` + finestra incoming `2000 ms`; cache limitata a 50 messaggi per contatto.

### Cross-session (DB)
- `_dedup_messages_by_id()`: per `(protocol, contact_number, msg_id, text)` tiene la riga col rank status più alto (un receipt `read` non viene perso a favore di un doppione `sent`).

## 4. Merge cache backend → UI (`chat_view.py::_merge_backend_cache`)

Quando una chat WhatsApp viene aperta, lo storico remoto scaricato finisce nel backend cache e viene specchiato nella cache UI con regole add-only:

- match id-first (anche per gli edit: l'id resta, il testo cambia — il confronto sul testo deve venire dopo);
- incoming senza id affidabile: testo + `±5 s`; outgoing con id: testo + finestra echo 10 min;
- un esistente con `msg_type == "text"` e testo diverso viene aggiornato in place e marcato `edited=True` (riconciliazione dell'edit);
- gli status salgono mai scendono (`_status_rank`);
- ritorna `changed` solo se sono stati aggiunti messaggi o modificato un testo: un upgrade di soli status NON causa re-render.

## 5. Batching del rendering ("debounce")

La versione storica del cache usava un flush JSON debounced; oggi le scritture SQLite sono incrementali e il "debounce" sopravvive solo a livello di **rendering della lista contatti**:

- durante un batch di eventi, nessuna mutazione visiva immediata oltre alla bolla della chat aperta;
- flag `_contact_list_dirty` + `_dirty_contact_keys` (set dei cache_key toccati);
- a fine giro del poll worker (`tui/polling.py`):
  - unread ricalcolato **nei soli dati** (`_recompute_unread(key)` O(M) per contatto toccato; se i contatti toccati superano `_CONTACT_UPDATE_BATCH_MAX = 4`, ricalcolo completo);
  - UN solo sort + render (`_reorder_contact_list`);
  - refresh della status bar solo se nessun messaggio transiente è in mostra.
- il conteggio unread deduplica esso stesso per `(timestamp, text)` (funzione annidata `_count_unread` dentro `tui/unread_reply.py::_recompute_unread`) così le ridelivery non gonfiano i badge.

## 6. Finestra di chat e reload

- All'apertura: render immediato delle ultime **20** voci dalla cache locale (fase 1), poi per WhatsApp fetch dello storico remoto (fase 2, con retry breve se WAHA risponde vuoto) e merge; eventuale re-render unico solo se qualcosa è cambiato.
- Il banner "load more" appare quando la cache ha più di 20 messaggi; il click carica tutta la cache (`_load_all_messages`).
- `_refresh_chat` aggiunge solo messaggi più nuovi dell'ultimo mostrato (o distinti nello stesso secondo): evita che chiusura di picker/modali ri-appenda la storia.
- I worker verificano `_is_stale()` (token + contatto corrente) prima di ogni mount: nessuna bolla atterra nella chat sbagliata.

## Test che documentano questo design

- `tests/test_refresh_chat.py` — finestra 20, stessi-secondo, ordine stabile, no ri-append.
- `tests/test_merge_cache_edit.py` — merge add-only, riconciliazione edit, rank guard.
- `tests/test_cache_debounce.py` — scritture incrementali SQLite, unread incrementale, persistenza receipt.
- `tests/test_edit_flow.py`, `tests/test_edit_contract.py` — chirurgia identità all'edit.
- `tests/test_db_edit.py`, `tests/test_backend_cache.py`, `tests/test_open_or_create.py` — semantica DB/cache.
