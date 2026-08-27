# DESIGN — Thumbnail dell'immagine quotata nella bolla quote

**Stato:** Implementata e stabilizzata (2026-08-26 — PR #70, #71, #72, #73, #74; suite **1507 unit + 73 integration** verdi; stabilità confermata)
**Vincoli:** Python 3.10+ · unica dipendenza grafica ammessa: **Pillow** (già in `requirements.txt`) · il fallback non-kitty **non usa Pillow** · **zero regressioni** (contratti wire del #37 invariati)

---

## 1. Obiettivo

Mostrare una **miniatura molto piccola dell'immagine quotata** dentro la bolla di quote della TUI, in due casi:

1. **Uscita** — quando l'utente CREA una reply quotando un'immagine.
2. **Ingresso** — quando RICEVE una quote che cita un'immagine (dove il protocollo lo consente).

Sui terminali senza supporto kitty il comportamento resta **identico a prima della feature** (testo "▎ 🖼️ Immagine" o caption reale), senza alcun uso di Pillow.

## 2. Stato attuale (implementato, verificato sul codice)

| Aspetto | Dove |
|---|---|
| Widget bolla quote | `ui_components.py:805` — `QuoteWidget(Horizontal)`: `Static(f"▎ {quote_text}")` interno + slot thumbnail (`Static("", classes="quote-thumb")`, 3 righe × ~6 colonne); metadati `attachment_id`/`attachment_path`/`content_type`/`protocol`; `aligned_right` da classe `msg-quote-right`; `thumbnail_region()`; `show_native_thumbnail()`/`native_cleanup()` |
| Uso del widget | `tui/chat_view.py` — bubble live (~194) e da cache usano `QuoteWidget` con i metadati `quote_attachment_*`; `_maybe_resolve_quote_thumbnail` (~601), worker (~631), stash/consumo (~688) |
| Segnaposto nascosto in nativo | `models.py:99` — `is_media_quote_placeholder_composite` (canonici **E** compositi `"filename — placeholder"`); `QuoteWidget.show_native_thumbnail` nasconde lo `Static` interno solo per placeholder — la **caption reale resta visibile** |
| Posizionamento nativo | `tui/app.py` `_sync_native_images` (~380) itera `ImageWidget, QuoteWidget`: regione = slot interno (`thumbnail_region()`); quote **destre** ancorate al **bordo destro del CONTAINER** (`widget.content_region.right`) perché il placeholder nascosto collassa lo slot a sinistra; gate screen-stack e `_chat_native_ids` coprono entrambi i tipi di widget |
| Race mount risolta | Worker non montato → PNG **stashato** (`_pending_quote_png`); hook `_consume_pending_thumbnails` (app.py:352) lo registra al frame successivo; `native_cleanup` azzera. Stesso pattern per le miniature chat (`_pending_native_png`) |
| Semaforo dedicato | `tui/app.py:122` — `_quote_resolve_semaphore = Semaphore(2)` per le sole quote: nessuna starvation dietro i download delle miniature chat |
| Persistenza | `backend/db.py:87-95` — migrazione idempotente: colonne `quote_attachment_id`/`quote_content_type`/`quote_attachment_path`; serializzazione round-trip in tutti i backend |
| Ingresso Signal | `backends/signal.py:173` — `_extract_quote_thumbnail` (`quote.attachments[].thumbnail`/`thumbnailData`, base64 o bytes, validazione Pillow, salvataggio in `CACHE_DIR/quote-thumbs/<sha1>.<ext>`) + `_signal_quote_attachment_id` (fallback lazy) |
| Ingresso Telegram | `backends/telegram.py` — tgref dal target cached (`tgref:<chat_id>:<msg_id>` → download lazy via `get_attachment_path`) + path persistito |
| Cleanup | `tui/chat_view.py:756` — `_clear_chat` filtra `(ImageWidget, QuoteWidget)` e chiama `native_cleanup()` (d=I) su entrambi |

## 3. Design realizzato

### 3.1 Principio

La thumbnail è **display-only e locale**: non tocca mai il wire (#37 invariato) e non cambia la semantica di `quote_text`/`media_quote_placeholder`. È un arricchimento visivo del ramo nativo, esattamente come le miniature inline della chat.

### 3.2 `QuoteWidget`

Contenitore `Horizontal` che sostituisce il vecchio `Static` della bolla:

- il **testo è byte-identico**: lo `Static` interno riceve il testo raw al costruttore e antepone internamente `"▎ "`; `quote_text` conserva quindi il suo ruolo di sorgente wire/retry;
- slot thumbnail fisso (3 righe × ~6 colonne) accanto al testo, vuoto finché `show_native_thumbnail` non registra una miniatura;
- stato nativo speculare a `ImageWidget` (`native_renderer`/`native_image_id`/dimensioni px);
- **in modalità nativa il segnaposto è NASCOSTO**: se il testo raw è un placeholder media (canonico o composto, predicate `is_media_quote_placeholder_composite`) lo `Static` interno viene nascosto — la thumbnail sostituisce il segnaposto; una **caption reale resta sempre visibile** accanto alla thumbnail;
- `native_cleanup()` libera l'immagine kitty (d=I), azzera lo stash `_pending_quote_png` e **ri-mostra il testo** (fallback);
- `protocol` abilita la risoluzione lazy in ingresso; `thumbnail_region()` espone la content-region dello slot per il placement.

CSS additivo in `tui/css.py`: `height: auto` sul contenitore, testo `width: 1fr`, `.quote-thumb` `width: auto`. Su non-kitty il rendering è identico al passato (il widget contiene solo il testo).

### 3.3 Posizionamento

Il placement avviene **solo nel `post_display_hook`** (`_sync_native_images`), mai nel render path:

- regione calcolata dallo **slot interno** (`thumbnail_region()`), mai dal contenitore (coprirebbe il testo); clip a `#chat-log` come per le miniature;
- **quote allineate a destra**: la thumb si ancora al **bordo destro del CONTAINER** (`content_region.right`), non dello slot — quando il segnaposto è nascosto lo slot collassa a sinistra e il suo bordo destro non coincide più col bordo della bolla; `aligned_right` è derivato dalla classe `msg-quote-right` applicata allo `Static` interno;
- trasmissione `a=t` **una volta per vita** (split `_transmitted`/`_placed` nel renderer), placement id stabile, replace senza flicker; fuori viewport → drop placement con dati conservati (d=i);
- le id entrano in `_chat_native_ids` (gate screen-stack già coperto: cleanup al passaggio sopra picker/modal, ripristino al ritorno); `_clear_chat` pulisce `(ImageWidget, QuoteWidget)` con `d=I`.

### 3.4 Flusso in uscita

1. `ImageWidget.ReplyRequested` porta `attachment_path` (+ `attachment_id`, `content_type`): il messaggio ottimistico monta il `QuoteWidget` con i metadati.
2. Generazione thumbnail in worker thread sotto il **semaforo DEDICATO** `_quote_resolve_semaphore=2`; transmit; `show_native_thumbnail`.
3. Se manca il path ma esistono `attachment_id`+`protocol` → **lazy resolve** via `get_attachment_path` (copre anche WhatsApp in uscita).
4. Wire **invariato**: `quote_message`/`quote_attachments` (`contentType:filename:previewFile`, solo Signal) escono come prima del #37.

### 3.5 Flusso in ingresso (per protocollo)

Modello additivo `quote_attachment_id`/`quote_attachment_path`/`quote_content_type` (default `None`), popolato solo dove realmente possibile; risoluzione asincrona in worker, degrado pulito a testo senza errori in UI.

- **Signal**: `_extract_quote_thumbnail` legge `quote.attachments[0].thumbnail`/`thumbnailData` (base64 o bytes), valida con Pillow (decode completo: scarta dati troncati/corrotti) e salva in `CACHE_DIR/quote-thumbs/<sha1>.<ext>` — best-effort, mai raises. In aggiunta, `quote_attachment_id` da `attachments[].id`/`attachmentId` abilita il **fallback lazy** via `get_attachment_path` quando la thumbnail incorporata è assente o stale.
- **Telegram**: la quote risolve il target dalla cache; il target cached fornisce `attachment_id` (tgref) → risoluzione lazy via `get_attachment_path` (download on-demand); il path risolto è **persistito**.
- **WhatsApp**: ingresso **solo testo per design** (l'evento WA non espone riferimenti al file quotato); questo include gli echo di reply create da un altro client, come il web. In uscita dalla TUI tutto funziona via path/id noto.

### 3.6 Race mount e stabilità

Le instabilità emerse nei PR #70-#74 sono tutte risolte:

| Problema | Soluzione |
|---|---|
| **Race mount**: il worker completava prima del `Mount` (async) del widget | PNG **stashato** su `_pending_quote_png`; l'hook `_consume_pending_thumbnails` (post-frame) lo consuma al frame successivo — niente timer né leak; `native_cleanup` azzera lo stash. Stesso pattern per le miniature chat |
| **Starvation**: le quote condividevano il semaforo dei download chat | Semaforo **dedicato** `_quote_resolve_semaphore=2` |
| **Persistenza asimmetrica**: path salvato solo per Signal | Path persistito ora anche per **WhatsApp e Telegram** (round-trip cache completo) |
| **Catch silenziosi** nei fallback | Degradazioni loggate con `logger.warning` (path stale, id non risolvibile, prepare fallito) |

### 3.7 Persistenza e riavvio

- Migrazione DB **idempotente**: tre colonne additive su `messages` (presenti anche nel `CREATE TABLE` fresco).
- Path persistito per **Signal** (thumbnail da envelope, file in `CACHE_DIR/quote-thumbs/`, mai ripulito) e ora anche **WhatsApp/Telegram** (path noto in uscita).
- Al load di una chat: **path stale** (file rimosso/cleanup) → warning e **fallback lazy** sull'id (se presente); senza id → bolla testuale. La risoluzione lazy può ri-scaricare (Telegram/WAHA) o risolvere dall'id Signal.

### 3.8 Cleanup e memoria

- `native_cleanup()` su smontaggio, su `_clear_chat` e sul gate screen-stack (entrambi i tipi di widget).
- Thumbnail piccole (~2-6 KB): impatto memoria kitty marginale; accumulo su chat con molte quote rientra nel tema #64 (LRU) già tracciato.

## 4. Tabella ambienti (comportamento reale)

| Ambiente / caso | Comportamento |
|---|---|
| **kitty** — uscita (qualsiasi protocollo) | Thumbnail nativa accanto al testo; segnaposto nascosto; con caption → thumbnail + caption |
| **kitty** — ingresso **Signal** | Thumbnail se l'envelope porta la thumbnail incorporata oppure l'attachment id è risolvibile (fallback lazy); altrimenti testo (**verifica V20 residua**) |
| **kitty** — ingresso **Telegram** | Thumbnail se il target cached è risolvibile (tgref → download lazy); altrimenti testo |
| **kitty** — ingresso **WhatsApp** | Nessuna thumbnail (per design): bolla testuale |
| **Web** — quote media | `/api/messages` non espone `quote_attachment_*`: la quote resta testuale; thumbnail web fuori scope |
| **Ghostty / iTerm2 / Windows Terminal / xterm** | Testo invariato "▎ 🖼️ Immagine"/caption — **senza Pillow**, byte-identico al passato |
| **tmux / screen** | Testo invariato (detection → CATIMG, nessuna thumbnail) |
| **Non-tty / CI** | Testo invariato |

## 5. Rischi (esiti)

| # | Rischio | Esito |
|---|---|---|
| R1 | Regressione layout/wrap della bolla quote | **Chiuso**: contenitore `height: auto`, slot fisso, vertical-align top; su non-kitty layout identico; coperto da test |
| R2 | Regressione contratto wire #37 | **Chiuso**: thumbnail display-only; test del wire body preservati senza modifiche |
| R3 | Regressione `quote_text`/`is_media_quote_placeholder` (retry) | **Chiuso**: il testo resta la fonte del retry (`is_media_quote_placeholder` riconosce solo i canonici); il nuovo predicate `_composite` è display-only |
| R4 | Ingresso senza allegato risolvibile | **Chiuso**: degrado best-effort a testo, warning loggato, nessun errore UI |
| R5 | Performance su quote multiple | **Chiuso**: semaforo dedicato (2), PNG piccoli, trasmissione una volta per vita |
| R6 | Memoria kitty su molte quote | Delegato a #64 (LRU) — non bloccante |
| R7 | Interazione con miniature esistenti | **Chiuso**: l'hook itera entrambi i widget; placement indipendenti; verificato da test e checklist (V23/V24) |

## 6. Piano d'implementazione — completato

| Fase | Deliverable | Esito |
|---|---|---|
| **A · Widget + uscita** (#70) | `QuoteWidget` in `ui_components.py`; live + cache su `QuoteWidget`; flusso uscita; hook itera `QuoteWidget` | ✅ Testo bolla identico; thumbnail nella reply ottimistica |
| **B · Ingresso** (#70) | Colonne `quote_attachment_*`; Signal (`_extract_quote_thumbnail` + id), Telegram (tgref lazy), WhatsApp testo-only | ✅ Con fallback lazy e degrado testato |
| **C · Fallback + regressioni** | Matrice ambienti; aggiornamento checklist kitty (voci **V18-V24**) | ✅ Suite verde, ruff pulito |
| **Stabilizzazione** | #71 nascondere il segnaposto in nativo + cleanup bolla; #72 allineamento a destra (ancoraggio al container) + persistenza al riavvio; #73 regressioni da review architetto; #74 stabilità (race mount/stash, semaforo dedicato, persistenza WA/TG, warning loggati) | ✅ Dichiarata stabile |

**Residuo:** verifica sul filo reale dell'ingresso Signal (checklist voce **V20**: ricevere una quote immagine su Signal e verificare la thumbnail — dipende dalla presenza di `quote.attachments[].thumbnail` nell'envelope; il supporto strutturale e il fallback su id sono comunque in opera). Versionamento di questo documento di design in corso.

## 7. Test

**Unit (headless):**
- `tests/test_quote_widget.py` — contenuto testuale identico al `Static` precedente ("▎ …"); `show_native_thumbnail`/`native_cleanup` (incluso reset dello stash e ri-mostra testo); nascondimento placeholder (canonico/composto) vs caption reale; `aligned_right`; zero chiamate renderer su non-kitty.
- `tests/test_quote_thumbnail_diagnostics.py` — degradazioni loggate (path stale, id non risolvibile, prepare fallito).
- Flussi uscita/ingresso (renderer fake): path noto → prepare → transmit → registrazione; race mount → stash + consumo; lazy resolve via `get_attachment_path`; `quote_attachment_id=None` o risoluzione fallita → testo, nessun errore.
- Golden bytes: riuso del formato DCS esistente (nessun nuovo formato).

**Regressione (esistenti):**
- `tests/test_refresh_chat.py` — assert aggiornati in modo motivato: isinstance su `QuoteWidget` + contenuto dello `Static` interno; nessun'altra modifica.
- `tests/test_live_quote_media.py`, `tests/test_image_caption.py`, test del wire body (`quote_wire_body`, `is_media_quote_placeholder`, retry): **senza modifiche**.

**Manuali:** `docs/CHECKLIST_MANUAL_KITTY.md` voci **V18-V24** (uscita, ingresso TG/SIG/WA, non-kitty, scroll/resize/zoom, gate screen-stack). Da spuntare ancora: **V20**.

**Suite corrente:** **1507 unit + 73 integration** verdi; `ruff check`/`ruff format` puliti.

## 8. Nessuna regressione (contratti preservati)

- `quote_message`/`quote_wire_body`/`quote_attachments` (wire #37): **invariati** — la thumbnail non esce mai sul filo.
- `MEDIA_QUOTE_PLACEHOLDERS`/`media_quote_placeholder`/`is_media_quote_placeholder`: semantica e valori **invariati** (il testo resta la sorgente del retry); `is_media_quote_placeholder_composite` è aggiuntivo e display-only.
- `ReplyRequested`/Alt+click/Enter/retry: comportamenti **invariati**.
- Rendering non-kitty: **byte-identico** al passato.
- Schema DB: solo colonne additive con default `NULL`, migrazione idempotente (nessuna breaking change su cache esistenti).

## 9. File toccati (come implementato)

- `ui_components.py` — `QuoteWidget` (+ `thumbnail_region()`, `show_native_thumbnail()`, `native_cleanup()`, stash `_pending_quote_png`)
- `tui/css.py` — CSS `.msg-quote`/`.msg-quote-right` (`height: auto`, testo `width: 1fr`) + `.quote-thumb`
- `tui/chat_view.py` — live e cache usano `QuoteWidget`; `_maybe_resolve_quote_thumbnail` + worker + `_finish/_register_quote_thumbnail` (stash); `_clear_chat` esteso a `(ImageWidget, QuoteWidget)`
- `tui/app.py` — `_quote_resolve_semaphore` (2); `_sync_native_images` itera `ImageWidget, QuoteWidget` (ancoraggio destro al container); `_consume_pending_thumbnails`
- `models.py` — campi additivi `quote_attachment_id`/`quote_attachment_path`/`quote_content_type`; `is_media_quote_placeholder_composite`
- `backends/signal.py` — `_extract_quote_thumbnail`, `_signal_quote_attachment_id`, `_signal_quote_content_type`; `backends/telegram.py` — tgref da target cached + persistenza path; `backends/whatsapp.py` — persistenza path/id in uscita
- `tui/send.py` — metadati quote nel payload ottimistico (wire invariato); `tui/events.py`, `tui/unread_reply.py` — propagazione metadati
- `backend/db.py` — migrazione idempotente + colonne nello schema
- `tests/test_quote_widget.py` (nuovo), `tests/test_quote_thumbnail_diagnostics.py` (nuovo), `tests/test_refresh_chat.py` (assert motivati); `docs/CHECKLIST_MANUAL_KITTY.md` (voci V18-V24)
