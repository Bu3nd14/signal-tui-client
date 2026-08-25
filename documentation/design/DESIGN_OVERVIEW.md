# Design — visione d'insieme

Principi di design, convenzioni UI/UX e struttura del data flow della TUI. Ricavato da `tui/` (app.py, css.py, events.py, chat_view.py, contacts.py, send.py, edit.py, unread_reply.py, polling.py, backend_connect.py, pickers.py, download.py), `ui_components.py` e `models.py`.

## 1. Principi di design osservati nel codice

1. **Protocol-agnosticismo della UI**: la TUI consuma solo modelli neutrali (`ChatContact`, `ChatEvent`) e il `BackendManager`; le differenze di protocollo emergono come accenti visivi (`protocol-signal|whatsapp|telegram`, emoji `📱 💬 📨`) mai come rami di logica nella UI.
2. **Reattività percepita prima di tutto**: invio ed edit sono ottimistici (bolla subito, rollback su errore); la lista contatti è aggiornata in batch; il render è progressivo a chunk.
3. **Mai bloccare il loop Textual**: tutte le operazioni potenzialmente lente girano in worker thread e tornano sulla UI con `call_from_thread`.
4. **Un solo punto di ingresso per i dati**: ogni byte che entra passa da `tui/events.py::_handle_event` — la UI non conosce envelope nativi.
5. **Idempotenza e dedup ovunque**: identità messaggio `(protocol, cache_key, timestamp, text)`, token anti-stale per worker, rank guard sugli status, dedup su più livelli (webhook, ingest, DB).
6. **Degradazione graceful**: backend opzionali assenti = funzione disattivata, non errore; fallback subprocess per Signal; errori rubrica parziale segnalati ma non bloccanti.

## 2. Composizione dell'app Textual

`SignalTUI(App)` (`tui/app.py`) è composta per **mixin funzionali**:

```
SignalTUI(App,
  ChatViewMixin        # rendering bolle/storico      (chat_view.py)
  EventHandlingMixin   # dispatch eventi in arrivo    (events.py)
  ContactListMixin     # lista contatti/filtri        (contacts.py)
  BackendConnectMixin  # connect worker per backend   (backend_connect.py)
  PollingMixin         # poll worker thread           (polling.py)
  SendMixin            # invio ottimistico + worker   (send.py)
  EditMessageMixin     # edit con rollback            (edit.py)
  UnreadReplyMixin     # badge unread + reply bar     (unread_reply.py)
  DownloadModeMixin    # modalità download            (download.py)
  PickerMixin)         # emoji/contatti/device-link   (pickers.py)
```

### Albero dei widget (`compose()`)

```
Header
Horizontal
├── ContactListWidget (width 30)     # sinistra
│     ├── Label #ContactsTitle ("📇 Contacts")
│     └── ContactListView #contact-list (ListItem: group header ▸/▾ | member row)
└── ChatAreaWidget                   # destra
      ├── Label #ChatTitle ("💬 Chat")
      ├── Vertical #chat-log        (MessageWidget / ImageWidget / quote Static /
      │                              msg-info / Button "load-more-msg" / DownloadLinkWidget)
      ├── Horizontal #reply-bar     (Static #reply-text + Button "reply-cancel"; nascosta di default)
      ├── EmojiCompletionWidget #emoji-completion
      └── Horizontal #input-row
            ├── Button #emoji-btn
            └── MessageTextArea #message-input
#bottom-bar
├── Footer
└── StatusBar #status-bar   (StatusSegment per protocollo + Static #status-text;
                             entrambi sempre nel DOM, commutati via display)
```

### Bindings globali (app-level)

`Ctrl+E` emoji picker · `Ctrl+S` contact picker · `Ctrl+D` download mode · `Ctrl+W` ciclo filtro protocollo · `Ctrl+U` toggle unread-only · `Ctrl+A` reset All · `Ctrl+L` device link · `Ctrl+N/Ctrl+P` navigazione suggerimenti emoji. La palette comandi built-in è disabilitata (`ENABLE_COMMAND_PALETTE = False`) per liberare `Ctrl+P`. `check_action()` disattiva i filtri quando un picker modale è aperto.

## 3. Convenzioni CSS (`tui/css.py::APP_CSS`)

- Tema basato sulle variabili Textual (`$surface`, `$accent`, `$text`, `$success`, `$error`, `$text-muted`).
- **Accenti per protocollo** applicati come classi: bordi di lista/chat/banner `chat-filter-signal` (#3b82f6), `chat-filter-whatsapp` (#25d366), `chat-filter-telegram` (#0088cc), `chat-filter-unread` (#f59e0b); colori testo riga `.protocol-*`.
- **Bolle messaggio**: `.msg-left` (ricevuti) / `.msg-right` (inviati, colore `$success`); stati `.msg-pending` (muted/dim), `.msg-failed` (error/bold); citazioni `.msg-quote` / `.msg-quote-right` (italic, muted); banner `.msg-load-more`.
- La riga selezionata usa sempre lo stile cursor "blurred" anche con focus, per evitare cambi di colore tra primo e secondo click su un contatto (decisione commentata direttamente nel CSS).
- Header gruppo `contact-group` bold compatto; membri `contact-member` indentati.

## 4. Widget custom principali (`ui_components.py`)

- `MessageWidget(Static)`: bolla cliccabile e focalizzabile. Posta i messaggi `MessageClicked` (reply/download/retry failed) e `EditRequested` (Alt+click o `Alt+E`). Stile status per messaggi propri: *sent* italic, pending dim, delivered/failed bold, read normale. `update_text()` riscrive la bolla in place (edit) con suffisso `" (modificato)"` se `edited`. Accento protocollo via classe CSS, rimosso quando selezionata (vince l'highlight reply).
- `ImageWidget`: bolla immagine cliccabile e focalizzabile; placeholder risolto in modo asincrono (`update_attachment`); su terminale kitty mostra la miniatura nativa (`show_native_thumbnail`, nessun testo sotto l'immagine) con fallback placeholder+`catimg` altrove; Enter/click apre `ImageModalScreen` (kitty hi-res o catimg), Alt+click/Alt+R emette `ReplyRequested` (reply all'immagine, con caption fedele + `attachment_id`/`content_type` per la quote), download mode serve il file. Dettagli: [DESIGN_NATIVE_IMAGES.md](DESIGN_NATIVE_IMAGES.md).
- `StatusBar` + `StatusSegment`: segmenti clickabili per protocollo con totale unread (`📱 N  💬 N  📨 N`, `-` se 0); `show_message` per messaggi transienti/persistenti, `sync_active` per evidenziare filtro attivo. I segmenti si nascondono quando è mostrato un messaggio transiente.
- `ContactListWidget` / `ContactListView`: contenitore e ListView specializzata con azione toggle gruppo (Enter/space sull'header).
- `MessageTextArea`: TextArea con submit su Enter (`Submitted`), newline con Shift+Enter/Ctrl+J/Ctrl+Enter, normalizzazione dei newline nel testo incollato, `insert_at_cursor`/`replace_completion` per gli emoji; `ctrl+u` è deliberatamente rimosso dai binding di editing (liberato per il filtro unread).
- `DownloadLinkWidget`: Input selezionabile con l'URL di download + evento `URLCopied`.

## 5. Data flow e gestione eventi/thread

```
worker threads                          UI thread (Textual)
──────────────                          ────────────────────
poll worker (polling.py)      ──cfm──►  _handle_event (events.py)
  ├ drain backend.poll_once()             ├ ingest → cache UI → bolla (_add_message)
  ├ typing timeout/mumbling               ├ receipt → set_status sui widget
  └ dirty flush: _recompute_unread        └ typing → label riga contatto in place
    + UN sort/render (_reorder_contact_list)
send worker (send.py)         ──cfm──►  transizioni status + status bar
load-messages worker          ──cfm──►  mount finestra atomica (mount(*widgets))
connect workers               ──cfm──►  _on_backend_ready (merge cache+contatti)
address book worker           ──cfm──►  screen.set_contacts (se token valido)
attachment resolver           ──cfm──►  update ImageWidget
```

Regole chiave:

- **Batching fine-batch**: durante un batch, `_contact_list_dirty` e `_dirty_contact_keys` raccolgono cosa è cambiato; a fine giro UN solo ricalcolo unread (incrementale O(M) fino a 4 contatti toccati, altrimenti full) + UN solo sort/render + refresh status bar se idle.
- **Token anti-stale**: `_chat_reload_token` (incrementato ad ogni selezione contatto) e `_address_book_token` (per apertura/chiusura picker) invalidano worker in volo: un worker obsoleto smette di montare widget.
- **Render progressivo**: la lista proiettata in righe (`_visible_rows()`) viene montata a chunk da 50 (`_start_progressive_render`) per evitare freeze all'avvio/Ctrl+W.
- **Mount atomico**: la finestra di chat viene svuotata e rimontata con un solo `mount(*widgets)` così Textual fa un solo layout pass.

## 6. Stato della UI (attributi chiave di `SignalTUI`)

| Attributo | Contenuto |
|---|---|
| `_cache` | cache UI `{cache_key: [msg_dict]}` (chiave `protocol:id`) |
| `_seen_timestamps` / `_seen_message_ids` | dedup render: `(protocol,key,ts)` / `(protocol,key,ts,text)` e `(protocol,key,id,text)` |
| `_shown_in_log` | identità già montate nel chat log corrente (dedup a livello render) |
| `_unread_counts` | `{cache_key: n}` ricalcolato dai dati, mai dalla UI |
| `_typing_contacts` / `_typing_mumbling` | `cache_key → timestamp` per ✍️ (timeout 10 s) e 💭 (60 s) |
| `_contact_widgets` / `_group_widgets` / `_member_to_group` / `_expanded_groups` / `_group_members` | strutture O(1) per label-in-place, raggruppamento e collapse |
| `_protocol_filter` / `_unread_only` | stato filtri Ctrl+W / Ctrl+U |
| `_reply_to` / `_editing_message` | target reply / edit attivo (mutuamente esclusivi) |
| `_pending_backends` | backend attesi per l'auto-selezione post-connessione |
| `_native_renderer` / `_native_last_key` / `_chat_native_ids` | stato immagini kitty: renderer, cache placement per no-op skip, id delle immagini della chat (gate screen-stack) — vedi [DESIGN_NATIVE_IMAGES.md](DESIGN_NATIVE_IMAGES.md) |

## 7. Interazioni notevoli

- **Reply**: click su una bolla → `MessageClicked` → `_reply_to` compilato (testo, ts, sender, eventuale `message_id`), highlight verde, reply bar `↩️ Replying to:`. Click ripetuto annulla. Per Telegram una reply senza id server valido è rifiutata esplicitamente (evita bolle impossibili); per WhatsApp vale la stessa regola. Su un'immagine la reply si cattura con Alt+click/Alt+R (`ReplyRequested`, caption reale separata dal display placeholder — vedi [CONTRACTS.md §11](../api-contracts/CONTRACTS.md#11-quote-media-reply-con-immagine)).
- **Edit**: Alt+click / `Alt+E` su messaggio proprio testuale già inviato → input precaricato, bar `✏️ Modifica:`; submit ottimistico + worker con rollback completo su rifiuto server. Dettagli: [DESIGN_EDIT_MESSAGES.md](DESIGN_EDIT_MESSAGES.md).
- **Retry failed**: click su una propria bolla `failed` → `_retry_failed_message`.
- **Contact picker (Ctrl+S)**: mostra subito le chat attive e in parallelo carica la rubrica completa dei backend (worker asincrono, token anti-stale); ricerca su nome/id/phone, raggruppamento cross-backend per persona con eventuale scelta del backend (`BackendChoiceScreen`), open-or-create per contatti sconosciuti.
- **Download mode (Ctrl+D)**: i click servono contenuti via HTTP invece di selezionare; esce automaticamente dopo un download.
- **Filtri**: `Ctrl+W` cicla all→signal→whatsapp→telegram; nei filtri singoli la lista diventa flat (solo header senza chevron, badge = unread della vista filtrata); `Ctrl+U` aggiunge il filtro unread (il contatto selezionato resta "pinned" finché è aperto); `Ctrl+A` reset. Click su segmento status bar → vista unread del backend se ha unread, altrimenti filtro semplice.

## Documenti di dettaglio

- [DESIGN_MESSAGE_IDENTITY_AND_CACHE.md](DESIGN_MESSAGE_IDENTITY_AND_CACHE.md) — modello a cache, identità dei messaggi e dedup/debounce del rendering.
- [DESIGN_OUTGOING_MESSAGE_STATUS.md](DESIGN_OUTGOING_MESSAGE_STATUS.md) — protocollo di stato dei messaggi inviati (pending→sent→delivered→read, failed).
- [DESIGN_EDIT_MESSAGES.md](DESIGN_EDIT_MESSAGES.md) — editing ottimistico con rollback e update in-place degli edit ricevuti.
- [DESIGN_NATIVE_IMAGES.md](DESIGN_NATIVE_IMAGES.md) — miniature native kitty + modal hi-res, fallback catimg.
