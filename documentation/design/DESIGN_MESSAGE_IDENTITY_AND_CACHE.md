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
- **Livello [2]** alimenta lista contatti (unread, last_message_ts), finestra di chat e reload; è add-only verso il backend (`_merge_backend_cache` non può mai farla "restringere"). Attenzione: il merge **non copia i dict** — le entry appese nella cache UI sono GLI STESSI oggetti del backend cache, quindi le mutazioni dell'uno si riflettono nell'altro.
- **Livello [3]** è volatile: svuotato a ogni cambio contatto (`_clear_chat` resetta anche i set).
- **Livello [4]** è la verità across-session: al boot WhatsApp e Telegram ricaricano solo le proprie righe (`_load_cache(protocol=...)`); **Signal invece chiama `_load_cache()` SENZA filtro protocollo** (`backends/signal.py:330`), quindi la sua cache in-memory parte con righe di tutti i protocolli (chiavi raw miste). La dedup per `msg_id` previene in generale il ri-ingest, ma ha limiti documentati nella sezione "Limiti noti" qui sotto.

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
- `_dedup_messages_by_id()`: per `(protocol, contact_number, msg_id, text)` tiene la riga col rank status più alto (un receipt `read` non viene perso a favore di un doppione `sent`). Attenzione: è una euristica con limiti noti — vedere la sezione "Limiti noti" e l'avviso su `_update_message_id` in [../api-contracts/API_OVERVIEW.md](../api-contracts/API_OVERVIEW.md) §4.

Le finestre qui sopra sono **euristiche, non garanzie**: i casi di collasso documentati sono raccolti nella sezione 7.

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

## 7. Limiti noti (le finestre sono euristiche, non garanzie)

Casi di collasso e fragilità documentati, da tenere presenti leggendo le regole di dedup:

1. **Stesso testo entro la finestra**: due messaggi distinti con identico testo che cadono nella finestra di dedup del protocollo collidono in un'unica entry (incoming ±2 s Signal/Telegram, ±5 s WhatsApp; outgoing echo fino a 10 minuti su WhatsApp). È il costo accettato per assorbire ridelivery ed eco.
2. **Echo window WhatsApp senza veto sull'id**: nel ramo `elif msg_id:` di `_message_already_cached`, se l'id cached è diverso dal matchato NON c'è alcun veto — si matcha comunque per testo entro `_ECHO_MATCH_WINDOW_MS` (10 min). Due "ok" inviati a pochi minuti di distanza possono quindi collassare in uno.
3. **Doppia regola sul timestamp WAHA**: il path ack di `handle_webhook` converte con `int(ts) * 1000` INCONDIZIONATO, mentre `_event_from_message` usa l'euristica `ts < 10**12` (secondi vs ms). Una build WAHA che emettesse millisecondi nell'ack produrrebbe timestamp nell'anno ~51000: ordinamento e dedup rotti per quel messaggio. La conversione andrebbe centralizzata in un helper unico.
4. **Tuple d'identità type-mixed**: `_seen_message_ids` e `_shown_in_log` contengono nello stesso set sia `(protocol, key, int_ts, text)` sia `(protocol, key, str_message_id, text)` — due tipi nello stesso slot; funziona perché int e str non collidono, ma è fragile e poco leggibile dai test.
5. **Gate `ts=0`** (`tui/events.py::_handle_message_event`): un evento con `timestamp=0` (payload malformato) viene ingerito in cache backend/UI/DB ma MAI mostrato live (la guardia `if ts and ...` scarta il mount): divergenza cache/vista finché non si riapre la chat.

## Test che documentano questo design

- `tests/test_refresh_chat.py` — finestra 20, stessi-secondo, ordine stabile, no ri-append.
- `tests/test_merge_cache_edit.py` — merge add-only, riconciliazione edit, rank guard.
- `tests/test_cache_debounce.py` — scritture incrementali SQLite, unread incrementale, persistenza receipt.
- `tests/test_edit_flow.py`, `tests/test_edit_contract.py` — chirurgia identità all'edit.
- `tests/test_db_edit.py`, `tests/test_backend_cache.py`, `tests/test_open_or_create.py` — semantica DB/cache.
