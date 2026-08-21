# Design di fix — BUG #40 (typing Telegram/WhatsApp) e #41 (stato "consegnato")

> Redatto dall'architetto. Riferimenti: `BUGS.md` #40, #41.
> Perimetro: **solo** #40 e #41. Il #39 (bolla WhatsApp "grigia") è **fuori scope**,
> ma la sua con-causa n. 2 (mappatura ack divergente) è la stessa del #41-WhatsApp:
> vedi §7.
> Stato UI verificato: **nessuna modifica UI richiesta** (§6).

---

## 1. Analisi delle root cause (verificate sul codice e sulla docu WAHA)

### #40 — typing

| Protocollo | Stato | Root cause |
|---|---|---|
| Signal | ✅ funziona | `backend/rpc.py::_process_typing` → `backends/signal.py::envelope_to_event` (righe 759-764) emette `ChatEvent(type="typing")`. |
| Telegram | ❌ assente | Nessun handler per `UpdateUserTyping` / `UpdateChatUserTyping` / `UpdateChannelUserTyping`. L'unico `events.Raw` registrato (`backends/telegram.py`, righe 262-269) gestisce solo `UpdateReadHistoryOutbox`. |
| WhatsApp | ❌ pipeline spezzata in **3 punti** | (a) `_configure_webhook` (`backends/whatsapp.py`, righe 512-517) sottoscrive solo `message, message.any, message.ack, message.ack.group` — **`presence.update` non è nella lista** (idem `docker-compose.yml` `WAHA_WEBHOOK_EVENTS: "message,message.ack"`), quindi WAHA non invia nulla; (b) `_event_from_typing` (`backends/whatsapp_events.py`, righe 433-454) legge i campi scalari `presence`/`typing`/`type`, ma il payload ufficiale WAHA è `payload.presences[].lastKnownPresence` (con `payload.id` = chat) → anche se l'evento arrivasse, ricadrebbe **sempre** su `STOPPED`; (c) la docu WAHA richiede la **subscribe per-chat** (`POST /api/{session}/presence/{chatId}/subscribe`) affinché i presence di una chat vengano distribuiti. |

Payload ufficiale WAHA `presence.update` (docu `how-to/presence` e `how-to/events`):

```json
{
  "event": "presence.update",
  "payload": {
    "id": "39123@c.us",                      // chat (diretta o gruppo @g.us)
    "presences": [
      {"participant": "39123@c.us", "lastKnownPresence": "typing", "lastSeen": null}
    ]
  }
}
```

`lastKnownPresence` ∈ `online | offline | typing | recording | paused`.

### #41 — delivered

| Protocollo | Stato | Root cause |
|---|---|---|
| Signal | ✅ funziona | `backend/rpc.py::_process_receipt` (`isDelivery`/`isRead`). |
| WhatsApp | ❌ `delivered` scartato | `_event_from_ack` (righe 377-431) assume l'enum Baileys (`2=SERVER, 3=DELIVERY, 4=READ`): soglia `status < 3 → None`, `is_read = status >= 4`. La docu ufficiale WAHA dichiara **`-1=ERROR, 0=PENDING, 1=SERVER, 2=DEVICE (consegnato), 3=READ, 4=PLAYED`** → l'ack di consegna (2) viene scartato e il read (3) viene degradato a delivered. Stessa divergenza in `_ack_value` (mappa nomi Baileys `SERVER_ACK`/`DELIVERY_ACK`, mai emessi da WAHA) e in `fetch_history` (`ack >= 3` / `ack >= 4`, righe 949-952). |
| Telegram | ⚠️ per design di protocollo | MTProto per cloud chat **non ha** una conferma di consegna: esiste solo `UpdateReadHistoryOutbox` (lettura). `_handle_read_receipt` emette solo `is_read=True`; `process_receipt` supporta già il target `"delivered"` (righe 992-1000) ma nessun produttore lo emette. |

### Infrastruttura a valle (già pronta, verificata)

- `tui/events.py::_handle_typing_event` (r. 383+) consuma `{"action": STARTED|STOPPED}` e aggiorna la riga contatto **in-place** (`_update_typing_label`, no-op se il label non cambia) — pensata apposta per raffiche di eventi.
- `tui/polling.py` applica `_TYPING_TIMEOUT` (10 s) e `_TYPING_MUMBLING_DURATION`.
- `tui/events.py::_handle_receipt_event` (r. 265+) instrada i receipt generici verso `process_receipt` con rank guard; `tui/chat_view.py::_STATUS_RANK` e `ui_components.py` (delivered → **bold**) rendono già lo stato intermedio.
- I `process_receipt` di WhatsApp e Telegram accettano già `is_read=False` → target `"delivered"`.

---

## 2. Decisioni di design (sintesi)

| # | Decisione | Esito |
|---|---|---|
| D1 | **Telegram typing**: estendere il dispatch `events.Raw` esistente con un traduttore puro `_handle_typing_update` (stessa forma di `_handle_read_receipt`) | ✅ |
| D2 | **Telegram delivered**: **nessun delivered sintetico** — limitazione di protocollo documentata; si garantisce solo il passaggio `sent → read` pulito | ✅ (scelta b, motivata in §4) |
| D3 | **WhatsApp typing**: (1) aggiungere `presence.update` agli eventi webhook (session config + compose), (2) subscribe presence per-chat (sweep in background + lazy all'apertura chat), (3) riscrivere il mapping di `_event_from_typing` sulla shape ufficiale con **filtro online/offline** | ✅ |
| D4 | **WhatsApp ack**: adottare l'enum ufficiale WAHA con **costanti condivise** in `whatsapp_events.py`; soglia receipt `>= 2 (DEVICE)`, read `>= 3 (READ)`, `PLAYED (4) → read` | ✅ |
| D5 | **Nessun dedup/rate-limit lato backend** per gli eventi typing | ✅ (motivato in §5) |
| D6 | **UI invariata** | ✅ |

---

## 3. Telegram — typing (D1)

### 3.1 Punto di aggancio

Estendere l'handler `events.Raw` già registrato in `_connect_sync` (`backends/telegram.py`, righe 262-269). Telethon 1.44 (verificato) non espone un evento typing dedicato (`events.UserUpdate` copre status/foto/nome, non la digitazione): `events.Raw` + `isinstance` è il meccanismo canonico, già in uso nel file.

Firme Telethon verificate sull'ambiente (`telethon 1.44.0`):

- `UpdateUserTyping(user_id: int, action, top_msg_id=None)` — chat private: `user_id` **è** il peer della chat.
- `UpdateChatUserTyping(chat_id: int, from_id: Peer, action)` — gruppi legacy.
- `UpdateChannelUserTyping(channel_id: int, from_id: Peer, action, top_msg_id=None)` — canali/megagruppi.

### 3.2 Nuovo traduttore `_handle_typing_update(update) -> None`

Forma identica a `_handle_read_receipt` (righe 938-976): **traduttore puro** — nessuna mutazione di cache/SQLite, solo `self._events.put(ChatEvent(...))`.

**Risoluzione `contact_id`** — stessa convenzione di `_handle_read_receipt` e di `Message.chat_id` (coerenza obbligatoria: la UI chiava il typing per `contact_cache_key(protocol, contact_id)`):

| Update | `contact_id` |
|---|---|
| `UpdateUserTyping` | `str(update.user_id)` |
| `UpdateChatUserTyping` | `str(-update.chat_id)` |
| `UpdateChannelUserTyping` | `str(-1000000000000 - update.channel_id)` |

**Mapping azione** → payload `{"action": ...}`:

| `update.action` (Telethon) | `action` |
|---|---|
| `SendMessageCancelAction` | `STOPPED` |
| `SendMessageTypingAction` e **tutte le altre** `SendMessage*Action` (record audio/round, upload *, choose sticker, game play, …) | `STARTED` |

Motivazione: la UI ha un solo affordance (✍️); qualsiasi "chat action" non-cancel segnala attività di composizione. Alternativa scartata: mappare solo typing/record-audio → STARTED e ignorare upload/sticker — perderebbe segnali utili senza alcun guadagno (il rumore è già assorbito dalla UI).

**Note:**
- I typing update arrivano sul loop Telethon (thread daemon): il metodo è `async`, invocato dall'handler `_on_raw` esattamente come `_handle_read_receipt`.
- Per i gruppi, `from_id` (l'attore) **non serve**: l'indicatore è per-chat. MTProto non ci recapita le nostre stesse chat-action → nessun self-filter necessario.
- Nessun nuovo stato di istanza (niente dedup, vedi §5): i fixture `_make_backend()` che bypassano `__init__` (`tests/test_telegram_read_receipt_fix.py`) restano validi.
- Il ramo `else: logger.info("Telegram raw: %s", ...)` (r. 269) va **demotato a `logger.debug`**: con i typing update attivi (raffiche ogni ~5 s per chat) diventerebbe rumore a livello INFO.

### 3.3 Sequenza

```mermaid
sequenceDiagram
    participant TG as Telegram (MTProto)
    participant RAW as _on_raw (events.Raw)
    participant H as _handle_typing_update
    participant Q as _events queue
    participant UI as tui/events.py

    TG->>RAW: UpdateUserTyping(user_id, SendMessageTypingAction)
    RAW->>H: isinstance cascade
    H->>H: contact_id = str(user_id); action = STARTED
    H->>Q: ChatEvent(type="typing", contact_id, {"action": "STARTED"})
    UI->>Q: poll_once() (poll worker)
    Q-->>UI: ChatEvent
    UI->>UI: _handle_typing_event → _typing_contacts[key]=now → _update_typing_label (✍️)
```

---

## 4. Telegram — delivered (D2): decisione architetturale

### Scelta: **(b) documentare la limitazione, nessun delivered sintetico**

**Motivazione:**

1. **Il protocollo non espone il dato.** Per le cloud chat MTProto esiste una sola conferma lato destinatario: la lettura (`UpdateReadHistoryOutbox`). Il check singolo dei client ufficiali Telegram significa "accettato dal cloud" — che è ciò che il nostro stato `sent` già rappresenta (il send Telethon ritorna dopo l'ack del server).
2. **Il delivered sintetico sarebbe istantaneo e privo di informazione.** L'unica euristica onesta sarebbe "send riuscito → delivered", cioè una transizione `pending → delivered` immediata che (i) rende `sent` inosservabile, (ii) crea un affordance **falso** ("sul dispositivo del destinatario" — Telegram non può saperlo: i messaggi vivono nel cloud, il destinatario può essere offline), (iii) aggiunge eventi, scritture SQLite e refresh di widget per uno stato a valore zero.
3. **Il passaggio `sent → read` è già pulito** dopo il fix #35 (`_reconcile_read_state` + rank guard): non serve alcun intervento per garantirlo.
4. **L'infrastruttura resta pronta**: `process_receipt` (target `"delivered"`), `_STATUS_RANK` e il rendering bold restano protocol-agnostici — se un domani MTProto introducesse una conferma di consegna, il produttore si aggiunge senza toccare la UI.

**Azione concreta:** nessuna riga di codice nuova per il delivered Telegram; nota esplicativa nel docstring di `_handle_read_receipt`/`process_receipt` e chiusura della metà Telegram di #41 come *protocol limitation (by design)* su `BUGS.md`.

Alternativa (a) — delivered sintetico su send-success — **scartata** per i motivi 1-3.

---

## 5. WhatsApp — typing (D3)

Tre interventi coordinati; il primo e il terzo sono entrambi necessari (da soli non bastano), il secondo copre il requisito di subscribe per-chat.

### 5.1 Sottoscrizione dell'evento webhook

- `backends/whatsapp.py::_configure_webhook` (righe 512-517): `desired_events` += `"presence.update"`. La funzione **già** gestisce il caso "URL registrato ma eventi non aggiornati" con un `PUT /api/sessions/{name}` (riavvio sessione WAHA one-time, trade-off già accettato quando fu aggiunto `message.ack`) → le installazioni esistenti si allineano al primo connect dopo il deploy.
- `docker-compose.yml`: `WAHA_WEBHOOK_EVENTS: "message,message.ack"` → `"message,message.ack,presence.update"` per coerenza (gli env-hook sono secondari rispetto alla session config, ma la configurazione dichiarata deve riflettere la realtà).

### 5.2 Subscribe presence per-chat (nuovo)

La docu WAHA: i presence di una chat fluiscono dopo `POST /api/{session}/presence/{chatId}/subscribe`. Senza subscribe, `presence.update` può non arrivare anche se sottoscritto a livello di webhook.

- `backends/whatsapp_rest.py`: nuovo metodo `presence_subscribe(chat_id) -> dict | None` (POST, best-effort, stessa forma degli altri endpoint).
- `backends/whatsapp.py`:
  - nuovo attributo `self._presence_subscribed: set[str]` in `__init__` (i fixture test usano il costruttore reale → nessun impatto);
  - `_presence_subscribe(chat_id)`: guard di idempotenza sul set, POST fire-and-forget, mai eccezioni;
  - **sweep in background** al connect (dopo `_configure_webhook`): thread daemon sul modello `_lid_resolver_run` — itera gli id chat noti (`self.contacts`), subscribe con pausa ~0.3 s; non blocca il connect;
  - **lazy**: `fetch_history(contact_id)` (chat aperta) e `handle_webhook` al **primo** evento `message` da un contatto non sottoscritto → una subscribe one-time (thread daemon). Copre chat nuove e chat mai toccate dallo sweep (es. sweep interrotto).

### 5.3 Normalizzazione `_event_from_typing` (riscrittura del mapping)

`backends/whatsapp_events.py::_event_from_typing`:

1. **Chat** — invariata la cascata `chatId | from | remoteJid | id | participant | chat`: con la shape ufficiale il campo `id` (chat) risolve già correttamente, sia per dirette (`@c.us`/`@lid`) sia per gruppi (`@g.us`, indicatore sulla riga del gruppo, coerente con la granularità della UI).
2. **Stato** — nuova lettura, con fallback legacy:
   - shape ufficiale: raccogliere `presences[].lastKnownPresence` (lista di stati, lowercased);
   - fallback legacy: lo scalare `presence | typing | type` come singolo stato (compatibilità con l'attuale `test_typing_event` e build/engine con payload piatto).
3. **Mapping con filtro** (cambiamento di comportamento dichiarato):

| Stato(i) WAHA | `action` |
|---|---|
| `composing` / `typing` / `recording` / `true` (legacy) | `STARTED` |
| `paused` | `STOPPED` |
| `online` / `offline` / `unavailable` / sconosciuto / assente | **nessun evento (`None`)** |

   - Priorità per gruppi (più `presences` nello stesso payload): qualsiasi stato "composing-like" vince su `paused`, che vince su tutto il resto.
   - **Perché il filtro è obbligatorio**: oggi qualsiasi valore non-composing ricade su `STOPPED`, e `_handle_typing_event` su STOPPED imposta sempre lo stato 💭 *mumbling*. Senza filtro, ogni ping `online`/`offline` (frequenti e non legati alla digitazione) accenderebbe 💭 sui contatti: un bug visivo peggiore dell'assenza dell'indicatore.
   - `recording` → STARTED: la UI ha un solo affordance; registrare un vocale è attività di composizione. Alternativa scartata: ignorare `recording`.
   - Trade-off accettato: un contatto che smette di digitare andando `offline` **senza** `paused` non riceve STOPPED → l'icona ✍️ scade via `_TYPING_TIMEOUT` (10 s) in 💭. Corretto per design.

### 5.4 Dedup/rate (D5): **nessuno lato backend**

- Telegram ri-invia la chat-action ogni ~5 s mentre l'utente digita; WhatsApp invia `typing` a ripetizione e poi `paused`. Ogni STARTED ripetuto **rinfresca** `_typing_contacts[key] = now`: è il meccanismo di keep-alive che mantiene ✍️ oltre il timeout di 10 s. Un dedup dei STARTED ripetuti farebbe scadere l'indicatore mentre l'utente sta ancora scrivendo (flicker ✍️→💭→✍️).
- Il costo di un evento duplicato è trascurabile: un item in coda, un `dict` set, un `call_from_thread`, e `_update_typing_label` che è **no-op** se il testo della riga non cambia (guard `_label_text`, righe 444-446) — progettato esattamente per queste raffiche.
- Il vero flood-guard è il filtro online/offline di §5.3.

---

## 6. WhatsApp — delivered (D4)

### 6.1 Costanti condivise (nuove) in `backends/whatsapp_events.py`

Enum ufficiale WAHA (docu `how-to/events#message.ack`; il campo intero `ack` è l'autorità, `ackName` è il fallback leggibile):

| Nome | Valore | Semantica TUI |
|---|---|---|
| `WAHA_ACK_ERROR` | -1 | ignorato |
| `WAHA_ACK_PENDING` | 0 | ignorato |
| `WAHA_ACK_SERVER` | 1 | ignorato (= già `sent` per noi) |
| `WAHA_ACK_DEVICE` | 2 | **delivered** (`is_read=False`) |
| `WAHA_ACK_READ` | 3 | **read** (`is_read=True`) |
| `WAHA_ACK_PLAYED` | 4 | **read** (solo vocali: un vocale "played" è comunque letto; un solo rank `read` in UI) |

Le costanti vivono in `whatsapp_events.py` (modulo di normalizzazione protocol-specifica) e sono importate da `backends/whatsapp.py` per `fetch_history`. **Non** in `models.py` (che resta neutro per protocollo).

### 6.2 Punti di modifica (soglie)

1. `_event_from_ack` (righe 417-424): `status < WAHA_ACK_DEVICE → None`; `is_read = status >= WAHA_ACK_READ`. Aggiornare il docstring (oggi descrive l'enum Baileys).
2. `_ack_value` (righe 65-73): la mappa nomi diventa quella ufficiale `ERROR/PENDING/SERVER/DEVICE/READ/PLAYED` → `-1/0/1/2/3/4`. Gli alias Baileys (`SERVER_ACK`, `DELIVERY_ACK`) vengono **rimossi**: WAHA non li emette e, con i numeri Baileys, classificherebbero male. Il ramo int su `raw["ack"]` resta il primo controllo. Opzionale: `logger.debug` su `ackName` sconosciuto (strumentazione minima per verifica sul campo).
3. `fetch_history` (righe 949-952): receipt-worthy `ack >= WAHA_ACK_DEVICE`; read `ack >= WAHA_ACK_READ` (copre anche PLAYED), altrimenti delivered.
4. `handle_webhook` — **solo commenti** (righe 186-197 e 320-323 citano "status < 3 (SERVER_ACK)"): la logica dell'evento sintetico **non** dipende dalla soglia (il pre-pass costruisce il messaggio da qualsiasi ack `fromMe`+`id`; l'ordinamento "messaggio sintetico prima del receipt" alle righe 330-334 resta). Con la nuova soglia un ack=2 produce `message` + `receipt(delivered)` in quest'ordine — esattamente il comportamento voluto.

### 6.3 Verifica di non-regressione sul cambio di soglia (3 → 2)

- **Evento sintetico outgoing**: invariato strutturalmente (nessun gate su `status` nel pre-pass); il receipt `delivered` in più arriva **dopo** il messaggio nella stessa drain di `poll_once`, quindi la bolla nasce con l'id reale e poi avanza di stato (rank guard: `sent < delivered < read`; un `delivered` tardivo dopo `read` è no-op).
- **Dedup `_seen_message_keys`** e `_detect_edit`: non toccati (chiave `(contact, id, text)`).
- **Volume**: +1 receipt per messaggio uscente → +1 scrittura `_update_message_status_by_id` per messaggio. Trascurabile.
- **Edits via ack** (`status=2` con body nuovo): ora producono `message_edit` + `receipt(delivered)` — coerente; un test esistente va aggiornato (§8).

### 6.4 UI (D6)

**Nessuna modifica UI.** `_handle_receipt_event`, `process_receipt`, `_STATUS_RANK`, rendering bold/italic, typing label e timeout sono già pronti e protocol-agnostici (Signal li usa oggi).

---

## 7. Perimetro e sovrapposizione con #39

- **Fuori scope**: #39 con-causa n. 1 — early-return silenzioso di `_transition_outgoing_status` (`tui/send.py`) sul passaggio `pending → sent`. Non si tocca `tui/send.py`.
- **Sovrapposizione dichiarata**: il "Fix proposto B" di #39 **è** la stessa realignement dell'enum ack qui decisa per #41 (D4). Applicando D4, la con-causa n. 2 di #39 è risolta; #39 resta aperto e si riduce alla sola transizione `pending → sent` lato send worker (più l'eventuale strumentazione A, qui coperta solo in parte dal log opzionale su `ackName` sconosciuto).
- Nessun altro file fuori perimetro viene modificato.

---

## 8. Piano test

### Nuovi — `tests/test_telegram.py` (o `tests/test_telegram_read_receipt_fix.py`, pattern `_make_backend()` + `asyncio.run` + `UpdateReadHistoryOutbox` già usato)

1. `UpdateUserTyping(user_id=42, SendMessageTypingAction())` → un `ChatEvent(type="typing", contact_id="42", payload={"action": "STARTED"})` in coda; nessuna mutazione di cache.
2. `UpdateUserTyping` con `SendMessageCancelAction` → `STOPPED`.
3. `UpdateChatUserTyping(chat_id=123, ...)` → `contact_id="-123"`; `UpdateChannelUserTyping(channel_id=456, ...)` → `contact_id=str(-1000000000000-456)` (convenzioni identiche al test `_handle_read_receipt_peer_id_conventions`).
4. Azione non-typing (es. `SendMessageUploadPhotoAction`) → `STARTED`.
5. Dispatch `_on_raw`: un update non-typing/non-receipt non produce eventi (e non logga a INFO).
6. Update sconosciuto → nessun evento, nessuna eccezione.

### Nuovi — `tests/test_whatsapp_backend.py`

7. `_event_from_typing` shape ufficiale: `{"id": "39123@c.us", "presences": [{"participant": "...", "lastKnownPresence": "typing"}]}` → STARTED; `"recording"` → STARTED; `"paused"` → STOPPED; `"online"`/`"offline"` → `None`.
8. Multi-`presences` (gruppo): priorità composing-like > paused.
9. `_event_from_raw` end-to-end: envelope `{"event": "presence.update", "payload": {...}}` → evento typing in coda via `handle_webhook` (copre la regressione "normalizzazione esiste ma non arriva").
10. `_event_from_ack` nuovo enum: `status=1` → `None`; `status=2` → receipt `is_read=False`; `status=3` → `is_read=True`; `status=4` → `is_read=True`.
11. `_ack_value`: `ackName: "DEVICE"` → 2, `"SERVER"` → 1, `"READ"` → 3, `"PLAYED"` → 4; int `ack` prioritario.
12. `fetch_history`: ack=2 → receipt delivered; ack=3 → read; ack=1 → nessun receipt.
13. `handle_webhook` ack=2: sequenza `[message, receipt]` (contratto di ordinamento con la nuova soglia).
14. `_configure_webhook`: `desired_events` include `presence.update`; con config già aggiornata → nessun PUT; con config datata → PUT (mock di `_rest`).
15. `presence_subscribe`: chiamata REST corretta; guard di idempotenza (seconda chiamata stesso chat_id → nessuna nuova POST); sweep/lazy best-effort (nessuna eccezione se `_rest` torna `None`).

### Aggiornati (esistenti — codificano l'enum Baileys errato; **devono** cambiare)

| Test | Modifica |
|---|---|
| `test_whatsapp_backend.py::test_ack_delivery_event` | `status: 3 → 2` (delivered). |
| `test_whatsapp_backend.py::test_ack_read_event` | `status: 4 → 3` (read); opzionale caso PLAYED=4 → read. |
| `test_whatsapp_backend.py::test_ack_server_ack_ignored` | `status: 2 → 1` (SERVER ignorato). |
| `test_whatsapp_backend.py::test_ack_dispatch_via_raw` / `test_ack_slash_variant_dispatch` / `test_ack_no_chat_id_returns_none` / `test_ack_no_msg_id_returns_none` | Allineare i valori `status` al nuovo enum (le assertion sul tipo restano). |
| `test_whatsapp_backend.py::test_outgoing_echo_and_ack_read_keep_the_parent_message_id` | Primo ack (`status: 2`) ora produce `["message", "receipt"]` (delivered); il secondo (`status: 4`) resta read. |
| `test_whatsapp_backend.py::test_handle_webhook_image_message_ack_retains_image_fields`, `test_handle_webhook_image_ack_caption_in_body`, `test_handle_webhook_image_message_ack_without_hasMedia_is_plain` | Con `status: 2` gli eventi attesi diventano 2 (`message` + `receipt` delivered). |
| `test_whatsapp_read_receipt_fix.py::test_handle_webhook_ack_does_not_mutate_cache` | `status: 2` → eventi `["message", "receipt"]`; la cache resta vuota (single mutation point invariato). |
| `test_whatsapp_read_receipt_fix.py::test_fetch_history_emits_read_receipt` | `ack: 4 → 3` (read). |
| `test_whatsapp_read_receipt_fix.py::test_fetch_history_emits_delivery_receipt_for_ack3` | Diventa ack=2 → delivered (rinominare). |
| `test_whatsapp_read_receipt_fix.py::test_fetch_history_no_receipt_for_server_ack` | `ack: 2 → 1`. |
| `test_edit_whatsapp.py::test_synthetic_ack_edit_enqueues_message_edit_not_message` | Con `status: 2` atteso `["message_edit", "receipt"]` (delivered). |

### Invariati (confermato)

- `tests/test_typing_indicator.py` (UI, envelope Signal), `tests/test_backends.py::test_envelope_to_event_typing` (Signal), `test_telegram_read_receipt_fix.py` (receipt Telegram), `test_whatsapp_backend.py::test_typing_event` (shape legacy, mantenuta come fallback), tutta la suite receipt Signal (`test_backend_cache.py`, `test_cache_debounce.py`). La suite esistente **non** richiede altre modifiche oltre alla tabella sopra.

---

## 9. Rischi e limitazioni note

1. **WhatsApp typing dipende dalla build WAHA in uso**: la shape `presences[].lastKnownPresence` è quella ufficiale documentata (engine NOWEB/GOWS/WEBJS/WPP ✔️); alcune build possono emettere `composing` invece di `typing` (coperto dal set) o la shape piatta legacy (coperta dal fallback). Verifica sul campo: log temporaneo dei payload `presence.update` grezzi in fase di validazione manuale (strumentazione one-shot, non committata).
2. **Subscribe presence**: senza la subscribe per-chat WAHA può non distribuire affatto i presence; la subscribe è best-effort e la sua assenza (es. API down al connect) degrada silenziosamente al comportamento attuale (niente indicatore), mai a errori. I contatti `@lid` non risolti possono ricevere presence con JID diverso da quello in rubrica → nessun match sul `cache_key`, no-op innocuo (stessa famiglia del #38; fuori scope).
3. **WAHA session restart one-time**: l'aggiornamento di `desired_events` forza un `PUT /api/sessions/{name}` al primo connect post-deploy (restart della sessione WAHA) — trade-off già accettato in `_configure_webhook`.
4. **Telegram delivered**: assenza dichiarata (by design). L'utente Telegram vede `sent → read`; coerente con i client ufficiali (check singolo/doppio).
5. **PLAYED → read**: per i vocali il "played" mostra `read` anche senza apertura della chat testuale — semplificazione voluta (un solo rank `read`); impatto nullo sui messaggi non-vocali.
6. **Rumore typing**: senza dedup backend le raffiche arrivano in UI; il costo è stato valutato trascurabile e il keep-alive è funzionale (§5.4). Se in produzione emergesse pressione sul main thread, il punto di intervento corretto è il coalescing in `_handle_typing_event`, non nei backend.
7. **Nessuna migrazione DB/config**: lo schema messaggi è invariato; la session config WAHA si auto-allinea al connect.
