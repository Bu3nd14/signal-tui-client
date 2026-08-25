# DESIGN — Thumbnail dell'immagine quotata nella bolla quote

**Stato:** Proposta formale (implementazione prevista in un momento successivo)
**Data:** 2026-08-25
**Vincoli:** Python 3.10+ · unica dipendenza grafica ammessa: **Pillow** (già in `requirements.txt`) · il fallback non-kitty **non usa Pillow** · **zero regressioni** (suite 1421 unit + 72 integration verdi; contratti wire del #37 invariati)

---

## 1. Obiettivo

Mostrare una **miniatura molto piccola dell'immagine quotata** dentro la bolla di quote nella TUI, in due casi:

1. **Uscita** — quando l'utente CREA una reply quotando un'immagine.
2. **Ingresso** — quando RICEVE una quote che cita un'immagine.

Sui terminali senza supporto kitty il comportamento resta **identico a oggi** (segnaposto testuale "🖼️ Immagine" o caption reale), senza alcun uso di Pillow.

## 2. Stato attuale (verificato sul codice)

| Aspetto | Dove |
|---|---|
| Bolla quote (live) | `tui/chat_view.py:194-195` — `quote_widget = Static(f"▎ {quote_text}", classes=quote_class)` |
| Bolla quote (da cache) | `tui/chat_view.py:832-833` — stesso `Static(f"▎ {quote_text}")` |
| Segnaposto tipizzati | `models.py:58-96` — `MEDIA_QUOTE_PLACEHOLDERS` (`"image": "🖼️ Immagine"`, …), `media_quote_placeholder()`, `is_media_quote_placeholder()` |
| Contratto wire uscita | `tui/send.py:264-323` — `quote_message = quote_wire_body or ""`; `quote_attachments` (Signal, piano B #37: `contentType:filename:previewFile`) |
| Evento reply da immagine | `ui_components.py` — `ImageWidget.ReplyRequested`/`_reply_text()` (caption reale o segnaposto tipizzato) |
| CSS quote | `tui/css.py:170-182` — `.msg-quote` / `.msg-quote-right` (testo mute, italic) |
| Feature kitty (in master) | `tui/images/` (`KittyRenderer`: `transmit`/`place`/`delete`, source rect, placement id stabile, split `_transmitted`/`_placed`); `tui/app.py` `post_display_hook`/`_native_sync_tick` (gate screen-stack, `_chat_native_ids`); `ui_components.py` `ImageWidget.show_native_thumbnail`/`native_cleanup`; `tui/chat_view.py` risoluzione path con semaforo 4; design `documentation/design/DESIGN_NATIVE_IMAGES.md` |

**Nota importante:** la bolla quote è oggi un `Static` testuale puro. Il segnaposto "🖼️ Immagine" ha un doppio ruolo: **display** (bolla) e **sorgente del wire body** su retry (`is_media_quote_placeholder` in `tui/send.py`). Qualunque modifica alla bolla NON deve alterare il valore di `quote_text` né il contratto di retry.

## 3. Design proposto

### 3.1 Principio

La thumbnail è **display-only e locale**: non tocca mai il wire (#37 invariato) e non cambia la semantica di `quote_text`/`media_quote_placeholder`. È un "arricchimento visivo" del ramo nativo, esattamente come le miniature inline della chat.

### 3.2 Nuovo widget: `QuoteWidget`

Sostituire il `Static` della bolla quote con un contenitore dedicato **`QuoteWidget(Horizontal)`** in `ui_components.py`, che mantiene:

- il **testo esistente invariato** come contenuto di un `Static("▎ {quote_text}")` interno (stessa classe `msg-quote`/`msg-quote-right`, stesso stile CSS → zero cambiamenti di rendering su non-kitty);
- un'area **thumbnail** (contenitore `Static` vuoto, `height` fissa in righe: **3 righe × ~6 colonne**, `align` top) a fianco del testo, presente e vuota in modalità nativa;
- stato nativo come `ImageWidget`: `native_renderer`, `native_image_id`, `native_width_px`, `native_height_px`, metodi `show_native_thumbnail()`/`native_cleanup()` (riuso del pattern esistente, niente codice duplicato oltre lo stretto necessario).

**CSS additivo necessario** (revisione architetto): `Horizontal` di Textual ha `height: 1fr` di default → la bolla si espanderebbe. Aggiungere a `tui/css.py` (`.msg-quote`/`.msg-quote-right`): `height: auto` sul contenitore, `width: 1fr` sullo `Static` interno (testo), `width: auto` sull'area thumbnail. Il resto dello stile esistente resta invariato.

Regole visive:

| Caso | kitty | non-kitty |
|---|---|---|
| Caption reale presente | thumbnail + caption testuale | caption testuale (oggi) |
| Nessuna caption | thumbnail da sola (niente "🖼️ Immagine") | "🖼️ Immagine" (oggi) |
| Path non risolvibile (ingresso) | nessuna thumbnail → testo | testo (oggi) |

La **caption** resta sempre testo (non viene sostituita dalla thumbnail): l'immagine non è mai coperta dal testo.

### 3.3 Posizionamento (riuso del meccanismo esistente)

- Il placement avviene **SOLO nel `post_display_hook`** dell'app (`tui/app.py` `_native_sync_tick`), mai nel render path: per ogni `QuoteWidget` con thumbnail nativa e `visible`, calcolo di `row/col/y_src/w_px/h_px` dalla **`content_region` dell'AREA THUMBNAIL interna** (non del contenitore: coprirebbe il testo), clip a `#chat-log` come per le miniature, e `renderer.place(...)` con placement id stabile (replace senza flicker).
- La thumbnail della quote si **unisce a `_chat_native_ids`** (gate screen-stack già coperto). **Attenzione (revisione architetto)**: `_clear_chat` (`tui/chat_view.py:~528`) filtra `isinstance(widget, ImageWidget)` e `_sync_native_images` (`tui/app.py:~359`) querya solo `ImageWidget`: vanno estesi a `(ImageWidget, QuoteWidget)` perché i `QuoteWidget` ricevano `native_cleanup()` (d=I) e il placement.
- Trasmissione `a=t` **una volta per vita** (split `_transmitted`/`_placed` già nel renderer): PNG piccolo generato da `prepare_thumbnail` (Pillow, target 3 righe × 6 colonne ≈ 48×54 px, proporzionale, downscale-only).

### 3.4 Flusso in uscita (creazione reply)

1. `ImageWidget.ReplyRequested` → il messaggio ottimistico con quote viene montato (già oggi) e il `QuoteWidget` nasce con `quote_text` invariato.
2. Il **path è già risolto** sul widget della reply (`attachment_path`): si genera la thumbnail in worker thread (semaforo 4 riusato), si `transmit`, si registra `show_native_thumbnail` sul `QuoteWidget`.
3. Wire **invariato**: `quote_message`/`quote_attachments` escono come oggi (#37).

### 3.5 Flusso in ingresso (ricezione quote)

1. Il backend produce il messaggio quotato con `quote_text` (placeholder o caption) — **nessun cambio ai backend** in questa fase.
2. **Verità emersa dall'investigazione del codice** (25/08/2026): oggi **nessuno dei 3 backend espone un riferimento al file dell'immagine quotata in ingresso** — esiste solo `quote_text`:
   - **Signal** (`backends/signal.py:78-113` `_signal_quote_text`): l'envelope espone i **metadati** dell'attachment quotato (`quote.attachments[].contentType`, `filename`), ma nessun path locale né attachment id risolvibile con `get_attachment_path`. Da **verificare empiricamente** se l'envelope include la **thumbnail incorporata** del quotato (`quote.attachments[].thumbnail`): se presente → salvarla e usarla come thumbnail; se assente → ingresso Signal senza thumbnail (testo).
   - **WhatsApp** (`backends/whatsapp_events.py:103-123` `_wa_quote_text`): nessun riferimento al file (solo testo/placeholder). → ingresso senza thumbnail.
   - **Telegram** (`backends/telegram.py:75-97` `_tg_quote_text_from_cached`): la quote risolve il **target dalla cache** (`msg_type`/`attachment_info`); il target cached ha `attachment_id` (tgref) → **risoluzione best-effort** via `get_attachment_path` (download lazy, pattern già usato per lo storico, #32).
3. **Decisione**: modello additivo `quote_attachment_id`/`quote_attachment_path`/`quote_content_type` (default `None`) popolato **solo dove realmente possibile**:
   - Telegram: dal target cached (tgref → `get_attachment_path`);
   - Signal: SOLO dopo verifica empirica della thumbnail incorporata (in fase B del piano);
   - WhatsApp: mai (resta `None`).
   Risoluzione asincrona (worker + semaforo 4); se `None` o risoluzione fallita → **degrado pulito**: testo invariato, nessun errore in UI.
4. **Mai bloccante**: la risoluzione è asincrona (worker), la chat si renderizza subito col testo; la thumbnail compare quando arriva.

### 3.6 Cleanup e memoria

- `native_cleanup()` su smontaggio e su `_clear_chat` (revisione architetto: estendere il filtro di `_clear_chat` in `tui/chat_view.py:~528` da `ImageWidget` a `(ImageWidget, QuoteWidget)` — oggi un `QuoteWidget` non riceverebbe il cleanup `d=I`).
- Thumbnail piccole (~2-6KB): impatto memoria kitty marginale; se su chat con molte quote si accumula, rientra nel tema #64 (LRU) già tracciato — nessuna nuova architettura di memoria ora.

## 4. Tabella ambienti (comportamento atteso)

| Ambiente | Comportamento |
|---|---|
| **kitty** (diretto o ssh) | Thumbnail nativa piccola nella bolla quote (uscita sempre; ingresso se allegato quotato risolvibile) + caption testuale quando presente |
| **kitty** con allegato quotato non risolvibile (ingresso) | Testo invariato (nessuna thumbnail) |
| **Ghostty / iTerm2 / Windows Terminal / xterm** | Testo invariato "▎ 🖼️ Immagine"/caption — **senza Pillow** |
| **tmux / screen** | Testo invariato (detection → CATIMG, nessuna thumbnail) |
| **Non-tty / CI** | Testo invariato |

## 5. Rischi e mitigazioni

| # | Rischio | Mitigazione |
|---|---|---|
| R1 | **Regressione layout/wrap** della bolla quote (testo multi-riga con thumbnail accanto) | `QuoteWidget(Horizontal)` con thumbnail `height` fissa e `vertical-align: top`; su non-kitty il layout è identico al passato (il contenitore contiene solo il testo); test di contenuto e di rendering |
| R2 | **Regressione del contratto wire #37** (`quote_message`/retry/`quote_attachments`) | La thumbnail è display-only: nessun campo wire toccato; test esistenti del wire body preservati senza modifiche |
| R3 | **Regressione di `quote_text`/`is_media_quote_placeholder`** (retry ricostruisce il wire body dal testo) | `quote_text` resta la fonte del wire e il contenuto del `Static` interno; `media_quote_placeholder`/`is_media_quote_placeholder` non cambiano semantica |
| R4 | **Ingresso senza allegato quotato risolvibile** | Degrado best-effort con `None` → testo invariato; nessun errore in UI |
| R5 | **Performance** (generazione thumbnail su quote multiple) | Worker + semaforo 4 (pattern esistente); PNG piccoli; trasmissione una volta per vita |
| R6 | **Memoria kitty su chat con molte quote** | Rimandata a #64 (LRU) — non bloccante, thumbnails piccole |
| R7 | **Interazione con le miniature esistenti** (doppio placement per messaggio immagine con quote) | Nessuna interferenza: ogni widget gestisce il proprio placement; il hook itera `ImageWidget` **e** `QuoteWidget` |
## 6. Piano d'implementazione

| Fase | Deliverable | Criteri di accettazione |
|---|---|---|
| **A · Widget + uscita** | `QuoteWidget(Horizontal)` in `ui_components.py`; `chat_view.py` (live + cache) usa `QuoteWidget` col testo invariato; flusso uscita (path noto → thumbnail in worker → transmit → hook place); aggiornamento `_native_sync_tick` per iterare i `QuoteWidget` | Testo della bolla identico al passato (assert su contenuto); su kitty la thumbnail compare nella reply ottimistica; suite 1421+72 verde |
| **B · Ingresso** | Modello additivo `quote_attachment_*` (default `None`); **verifica empirica della forma dell'envelope quote Signal** (thumbnail incorporata in `quote.attachments[].thumbnail`); popolamento best-effort: Telegram dal target cached (tgref), Signal solo se la verifica empirica dà esito positivo, WhatsApp mai; risoluzione asincrona con semaforo | Ingresso con allegato risolvibile → thumbnail; non risolvibile → testo invariato, nessun errore; test di degrado |
| **C · Fallback + regressioni** | Matrice ambienti (test + checklist kitty aggiornata `docs/CHECKLIST_MANUAL_KITTY.md`); suite completa + lint + format | Tutta la matrice verificata; suite completa verde; ruff pulito |

## 7. Test previsti

**Unit (headless):**
- `QuoteWidget`: contenuto testuale identico al `Static` precedente (regressione sul testo "▎ …"); `show_native_thumbnail`/`native_cleanup`; layout con/senza caption; non-kitty → nessuna chiamata al renderer (zero byte su `_driver.write`).
- Flusso uscita: path noto → `prepare_thumbnail` → `transmit` → registrazione (renderer fake); fallback su prepare fallito → testo.
- Flusso ingresso: allegato risolvibile → thumbnail; `quote_attachment_id=None` o risoluzione fallita → testo, nessun errore.
- Golden bytes: riuso del formato DCS esistente (nessun nuovo formato).

**Regressione (esistenti):**
- `tests/test_refresh_chat.py:931-932` — ha **due assert** (`isinstance(widgets[0], Static)` + `_Static__content == "▎ 🖼️ Immagine"`): con `QuoteWidget` romperebbe entrambi. **Aggiornamento motivato obbligatorio** (revisione architetto): verificare il contenuto dello `Static` interno del `QuoteWidget` (stessa stringa "▎ 🖼️ Immagine") e l'isinstance sul `QuoteWidget`; aggiungere un test che asserisce il contenuto del `Static` interno. Nessun'altra modifica.
- `tests/test_live_quote_media.py`, `tests/test_image_caption.py`, test del wire body (`quote_wire_body`, `is_media_quote_placeholder`, retry): **senza modifiche**.

**Nuovi:** `tests/test_quote_thumbnail.py` (uscite/ingressi/fallback/degrado), aggiornamento `docs/CHECKLIST_MANUAL_KITTY.md` (voci quote).

## 8. Nessuna regressione (contratti preservati)

- `quote_message`/`quote_wire_body`/`quote_attachments` (wire #37): **invariati** — la thumbnail non esce mai sul filo.
- `MEDIA_QUOTE_PLACEHOLDERS`/`media_quote_placeholder`/`is_media_quote_placeholder`: semantica e valori **invariati** (il testo resta la sorgente del retry).
- `ReplyRequested`/Alt+click/Enter/retry: comportamenti **invariati**.
- Rendering non-kitty: **byte-identico** al passato.
- Suite **1421 unit + 72 integration** verde; `ruff check`/`ruff format` puliti.
- Test esistenti: unico aggiornamento motivato = i 2 assert di `tests/test_refresh_chat.py:931-932` (verifica dello `Static` interno del `QuoteWidget`); tutto il resto invariato (nessuna modifica salvo aggiunte additive).

## 9. File da toccare (stima)

- `ui_components.py` — nuovo `QuoteWidget` (+ metodi nativi), wiring
- `tui/css.py` — CSS additivo per `.msg-quote`/`.msg-quote-right` (`height: auto`, testo `width: 1fr`, thumb `width: auto`)
- `tui/chat_view.py` — live (194-195) e cache (832-833) usano `QuoteWidget`; estensione filtro `_clear_chat` (~528) a `(ImageWidget, QuoteWidget)`; risoluzione ingresso best-effort
- `tui/app.py` — `_native_sync_tick` itera anche `QuoteWidget`; `_sync_native_images` query `(ImageWidget, QuoteWidget)`
- `models.py` — campi additivi `quote_attachment_id`/`quote_attachment_path`/`quote_content_type` (default `None`)
- `backends/signal.py` — popolamento `quote_attachment_*` solo dopo **verifica empirica** della thumbnail incorporata nell'envelope quote (`quote.attachments[].thumbnail`); `backends/telegram.py` — target cached → tgref per `get_attachment_path` (best-effort); WhatsApp: nessuna modifica
- `tests/test_refresh_chat.py` — aggiornamento motivato dei 2 assert (931-932); `tests/test_quote_thumbnail.py` (nuovo); `docs/CHECKLIST_MANUAL_KITTY.md` (voci quote)
