# DESIGN — Immagini native in chat (miniatura inline + modal hi-res)

**Stato:** IMPLEMENTATO (PR #57, `feat(images): miniature native kitty graphics
protocol con fallback catimg`). Il documento conserva l'analisi di design e la
POC che hanno motivato le scelte; dove testo e codice divergono, fa fede il
codice (`tui/images/`, `tui/app.py`, `ui_components.py::ImageWidget`,
`tui/chat_view.py`, `backends/config.py`; test `test_kitty_renderer`,
`test_image_detect`, `test_image_modal`, `test_chat_view_images`).
**Data:** 2026-08-24 · **Vincoli:** Python 3.10+ · nessuna modifica a backend
messaggistica/database · unica dipendenza nuova: Pillow

---

## 1. Obiettivo

1. **Miniature inline ad alta risoluzione** nella chat (messaggi immagine).
2. **Visualizzatore modal ad alta risoluzione**.
3. **Fallback automatico** al rendering attuale basato su `catimg` quando il
   terminale non supporta immagini native.

## 2. Esito della POC: perché NON si usa `textual-image`

La POC (cartella `poc-image/`, poi rimossa dal progetto a feature implementate) ha verificato su terminale reale via ssh:

| Approccio | Esito |
|---|---|
| `textual-image` TGP (`a=t` + `a=p,U=1` + diacritici) | ❌ nessuna resa su kitty 0.48 via ssh (e iTerm2 risponde `OK` alla query ma non lo renderizza: **falso positivo**) |
| `textual-image` Sixel | ❌ si rompe dentro Textual: garbage dopo l'immagine e scroll rotto (re-iniezione a ogni repaint) |
| **Kitty raw `a=T` / `a=p` + source rectangle** | ✅ **immagine perfetta, scroll fluido, clip nativo** |

**Decisione:** renderer kitty **custom** (≈150 righe) basato sul protocollo
ufficiale, **niente `textual-image`**. Unica dipendenza aggiuntiva: **Pillow**
(già richiesta comunque per le miniature), compatibile Python 3.10+.

## 3. Meccanismo (verificato dalla POC)

Protocollo kitty graphics (spec ufficiale), comandi **sempre in `q=2`** (quiet,
per non inquinare lo stdin letto da Textual):

### 3.1 Trasmissione dati — UNA sola volta per immagine
```
a=t, i=<image_id>, f=100, q=2, m=<0|1> ; <chunk base64 PNG>
```
- `i`: id immagine stabile (intero, non zero).
- `f=100`: PNG. I chunk successivi al primo contengono **solo** `m=1` (la spec:
  "subsequent chunks must have only the m and optionally q keys").
- Chunk ≤ 4096 byte, multipli di 4 (tranne l'ultimo).
- **`q=2` su TUTTI i comandi, incluso `a=t`** (correzione revisione C1): con `q=0`
  kitty risponde `\x1b_Gi=<id>;OK\x1b\\` su stdin; Textual non riconosce le APC e le
  reinterpreta come **eventi tasto reali** (`_xterm_parser.py:179-197`) → testo
  spazzatura nel `MessageTextArea#message-input`. `q=2` sopprime OK **ed** errori
  (sorgente kitty `graphics.c`: `if (is_ok_response || quiet > 1) return NULL`).

### 3.2 Placement / riposizionamento — pochi byte, senza ri-trasmettere i dati
```
\e[<row>;<col>H  +  a=p, i=<image_id>, p=<placement_id>,
                    x=0, y=<src_y_px>, w=<src_w_px>, h=<src_h_px>,
                    C=1, q=2
```
- **`p=<placement_id>` stabile per widget**: "two placements with the same
  image id and placement id → the second **replaces** the first, **without
  flicker**" (spec).
- **`x,y,w,h` = source rectangle in pixel**: ritaglio verticale **nativo**
  (l'immagine "entra/esce" dal bordo senza ri-codifiche → niente colori
  sbagliati/fantasmi).
- **`C=1`**: kitty non muove il cursore dopo il placement → nessuna
  interferenza col tracking del cursore di Textual.
- `\e[row;colH` posiziona il cursore **dopo** che Textual ha disegnato la frame.

### 3.3 Cleanup
- Fuori viewport: `a=d, d=i, i=<image_id>` (rimuove i placement, **mantiene** i dati).
- Unmount: `a=d, d=I, i=<image_id>` (libera anche i dati).

## 4. Integrazione con Textual

Il problema di fondo: Textual scorre **virtualmente** (ri-usa i render strips;
`render()` NON viene richiamato allo scroll). L'immagine va quindi
riposizionata a ogni frame, alla posizione **a schermo** corrente del widget.

### 4.1 Sincronizzazione col frame: `post_display_hook`
- Override di `App.post_display_hook()` (chiamato da `_display` subito **dopo**
  il flush di ogni frame) → riposiziona i placement nello **stesso istante** del
  testo → **niente sfasamento "due velocità"** durante lo scroll.
- La ri-emissione è **no-op se la posizione/clip non è cambiata** (costo ~0).

### 4.2 Posizione e clip
- `widget.content_region` (Textual) → posizione on-screen del contenuto del
  widget (correzione C2): l'immagine non copre bordo/padding del messaggio
  (`ImageWidget` ha bordo su focus/selezione, `ui_components.py:717-732`).
- Clip verticale **e orizzontale** rispetto a **`content_region` di `#chat-log`**
  (il contenitore reale, `chat_view.py:125`, con `border: solid $accent` in
  `tui/css.py:99`): `cut_top` → `y_src`, e `cut_left`/`cut_right` → `x_src`/`w_src`
  (la spec tronca solo al bordo schermo, non al widget).
- `cut_top = max(0, clip_top - region.y)` → `y_src = cut_top * cell_h`,
  `h_src = visible_h * cell_h`; analogo orizzontale con `cell_w`.
- Cap di larghezza in fase di prepare: `thumbnail_max_cols` (default ~60),
  clampato alla larghezza del contenitore.

### 4.3 Resize
- Handler `on_resize`: azzera la cache di posizione e forza ri-emissioni
  ritardate (subito + 0.1s + 0.3s) per coprire reflow Textual + rimappatura di
  kitty.

### 4.4 Screen stack (correzione C5 — non coperto dalla POC)
`App.query()` interroga la **default screen**, non quella attiva (`app.py:941-943`):
i placement della chat resterebbero visibili **sopra i modal** (emoji picker,
contact picker, `ImageModalScreen` stesso).
- Gate nel hook e nel timer: se `self.screen is not self.default_screen` →
  `a=d,d=i` una volta sui placement attivi della chat e sospensione emissioni.
- Al ritorno allo screen principale → invalida `_last_key` e ri-emetti.
- Il modal kitty gestisce i propri placement localmente (mount → trasmetti+piazza;
  dismiss → `d=I`; `on_resize` → ri-piazza).

### 4.5 Timer di sicurezza
- `set_interval(0.25s)` come rete di sicurezza (post_display_hook è il trigger
  principale).

## 5. Rilevamento terminale e fallback

### 5.1 Rilevamento (prima di `app.run()`)
La query TGP **non basta** (iTerm2 risponde `OK` ma non renderizza). Sequenza
(correzione R7):
1. **Override config** (`IMAGE_PROTOCOL` in `backends/config.py`): `auto | kitty | catimg | off`.
2. `isatty`? no → CATIMG (headless/CI-safe).
3. Guardia **tmux/screen**: `TMUX` senza `allow-passthrough` (≥3.3) o
   `TERM=screen*` → CATIMG.
4. **Kitty vero**: `TERM=xterm-kitty` **e** query TGP `OK` (timeout 0.15s,
   restore termios, mai su pipe) → KITTY. (`KITTY_WINDOW_ID` può mancare su ssh.)
5. Altrimenti → CATIMG se `shutil.which("catimg")`, altrimenti **OFF**.

Nota: `TERM=xterm-kitty` esclude iTerm2 (`xterm-256color`) e Ghostty.

### 5.2 Fallback
- Non-kitty → comportamento **attuale invariato**: placeholder inline cliccabile
  + `catimg` nel modal (percorso `ImageModalScreen._render_image`,
  `ui_components.py:795-835`).

## 6. Architettura e file

### Nuovo package `tui/images/`
| File | Contenuto |
|---|---|
| `detect.py` | `detect_image_support()` → enum `KITTY/CATIMG/OFF` (puro, iniettabile; guardia tmux/screen, `which(catimg)`) |
| `kitty_renderer.py` | `KittyRenderer`: trasmissione `a=t` (`q=2`), placement `a=p` con source rect, delete, **cell size proprio** (ioctl `TIOCGWINSZ` → fallback CSI 16 t → env → CATIMG; **invalidate su `on_resize`** — lo zoom font di kitty cambia i px/cella), generazione PNG via Pillow (`prepare_thumbnail`, `prepare_hi_res`, `png_size`, `compute_source_rect`) |
| `cellsize.py` | `get_cell_size_ioctl(fd)` / `get_cell_size()`: misurazione px/cella PRIMA di `run()` (il fallback CSI non gira mai dentro l'app) |

L'estensione nativa del widget NON è un file separato: vive in
`ui_components.py::ImageWidget` (`show_native_thumbnail()`, `native_cleanup()`,
stato `native_renderer/native_image_id/native_*_px`).

### Modifiche al progetto
| File | Cambiamento |
|---|---|
| `signal_tui.py` | detect **dopo** `_acquire_lock()` con try/except → CATIMG; passaggio a `SignalTUI(image_support=...)` + cell size pre-run (`get_cell_size`); cleanup kitty all'exit via `on_unmount` → `renderer.clear_all()` |
| `tui/app.py` | costruttore `image_support`, override `post_display_hook` + gate screen-stack |
| `backends/config.py` | getter `image_protocol()`, `thumbnail_max_lines()` (12), `thumbnail_max_cols()` (~60) |
| `ui_components.py` | **`ImageWidget` esteso** (metodo `show_native_thumbnail()` + renderer iniettato) — niente widget parallelo (C3); `ImageModalScreen` a strategia (kitty hi-res o catimg) con gestione placement locale + resize |
| `tui/chat_view.py` | `_finish_attachment_resolve` usa `show_native_thumbnail`; **risoluzione path anche per `_mount_window`/`_load_all_messages`** con semaforo 2-4 (C4); `_clear_chat` → cleanup |
| `tui/download.py` | passa renderer al modal |
| `requirements.txt` | `pillow>=10.3` e **pin `textual>=8.2,<9`** (R2: dipende da `post_display_hook` e `_driver`) — entrambi presenti nel file |

Note architetturali (C3): il placeholder testuale va **mantenuto come contenuto**
del Static sotto l'immagine (z-index default 0) → loading state gratuito,
fallback visivo se kitty evicte i dati, testo copiabile.

### Modal hi-res
- Kitty: placement a schermo intero (source rect = immagine intera, cap ~1600px).
- Non-kitty: percorso catimg **invariato**.

## 7. Performance

- Scroll = pochi byte (solo `a=p`), **nessuna** ri-trasmissione dati.
- **Split `_transmitted` / `_placed`** (correzione R5): i dati si trasmettono UNA
  volta per vita dell'immagine; `d=i` (placement) mantiene i dati in kitty
  (eviction solo sotto quota ~320MB) → rientro in viewport senza ri-trasmissione.
  Degrado sotto eviction coperto dal placeholder testuale (C3).
- Trasmissione `a=t` una volta per immagine (~10-20KB su ssh, una tantum).
- Cell size: cache con invalidazione su `on_resize`.
- Thumbnail: prepare + PNG encode in `run_worker(thread=True)` con concorrenza
  limitata (R4); (opzionale) cache su disco in `XDG_CACHE_HOME`.

## 8. Rischi

1. **Kitty-only**: valore nativo solo su kitty vero; Ghostty/iTerm2 → catimg
   (nessuna regressione). Accettato (decisione utente: kitty diventa il client
   principale).
2. **Race su stdout** (R1): Textual scrive i frame via `WriterThread`; il POC
   scriveva su `sys.__stdout__`. In produzione: emissione via **`self._driver.write()`**
   dentro il hook → stessa coda del frame, ordine garantito. `sys.__stdout__`
   solo per cleanup fuori loop, con try/except `BrokenPipeError`.
3. **API semi-interna** (`post_display_hook`, `_driver`) → **pin `textual>=8.2,<9`** (R2).
4. **Resize kitty**: rimappatura schermo → handler `on_resize` con ri-emissioni
   ritardate (mitigato). Cell-size cache invalidata su resize.
5. **Screen stack**: placement sopra i modal → gate nel hook (C5).
6. **Testabilità headless limitata** (`post_display_hook` non gira in headless)
   → unit test della logica pura + checklist manuale su kitty reale.
7. **Pillow** (R4): decode/resize su UI thread = freeze → thread worker + semaforo.
8. **Semantica OFF** (R8): placeholder invariato + status "immagini disabilitate" al click.
9. **Default `image_support=CATIMG`** (R9): ~10+ test istanziano `SignalTUI()`
   senza argomenti → suite esistente verde senza modifiche.
10. **Memoria kitty su chat lunghe** (R10, opzionale): LRU `d=I` oltre N posizioni
    fuori viewport.

## 9. Test

- **Unit (headless)**: detection (env parametrizzate, guardia tmux, catimg
  assente), calcolo source rect (verticale **e orizzontale**), formato DCS
  (golden bytes con `q=2` sul primo chunk), selezione renderer, gate screen-stack.
- **Integrazione headless**: app con `image_support=CATIMG` → zero scritture su
  stdout (monkeypatch) + comportamento invariato; smoke con scroll/resize.
- **Manuale su kitty reale**: scroll veloce/lento, resize piccolo, header/bordo
  chat protetti, focus border allineato, nessuna orfana dopo scroll-out, **nessun
  garbage nel box di input durante l'arrivo di un'immagine**, modal sopra chat
  senza immagini residue.

## 10. Piano d'implementazione

| Fase | Deliverable | Criteri di accettazione |
|---|---|---|
| **0 · Hardening meccanismo** | `q=2` anche su `a=t`; split `_transmitted`/`_placed`; emissione via `_driver.write()`; cell-size proprio; cap `max_cols` + clip orizzontale | Su kitty-ssh: nessun garbage con Input focusato; scroll flap senza ritrasmissioni; immagine larga clippata |
| **1 · Detection + config** | `detect.py` (guardie tmux/screen, `which(catimg)`), getter config, wiring `signal_tui.py` (dopo lock, try/except→CATIMG), default `CATIMG`, pin `textual>=8.2,<9`, `pillow>=10.3` | Suite esistente verde senza modifiche; unit test detection |
| **2 · Renderer + widget inline** | `kitty_renderer.py`; estensione di `ImageWidget`; risoluzione path anche per finestra da cache (semaforo 2-4); prepare in thread; clip a content_region; hook con gate screen-stack; `d=A` all'exit | Checklist kitty reale completa (scroll, resize, bordi, focus, orfane, placeholder durante load) |
| **3 · Modal** | `ImageModalScreen` strategia (ramo kitty + ramo catimg invariato); hide/restore placement chat su push/pop | Modal sopra chat pulita; resize in modal; OFF → status |
| **4 · Fallback + regressioni** | Matrice ambienti (kitty, kitty+ssh, Ghostty, iTerm2, tmux, pipe/CI, catimg mancante); README | Comportamento atteso osservato per ogni ambiente; suite completa verde |
| **5 · Opzionale** | Cache thumbnail su disco; LRU `d=I` | Budget memoria kitty su chat con 200+ immagini |

## 11. Decisioni registrate

| Decisione | Motivo |
|---|---|
| **Non adottare `textual-image`** | TGP non reso su kitty-ssh; sixel rotto in Textual; supporto iTerm2 assente |
| **Renderer custom `a=t`+`a=p`** | unico approccio verificato funzionante |
| **`TERM=xterm-kitty` come gate** | la query TGP è inaffidabile (falso positivo iTerm2) |
| **Niente floor Python 3.13** | il renderer custom richiede solo Pillow → 3.10+ basta |
| **`catimg` come fallback** | già integrato, testato, stabile |
| **Estendere `ImageWidget`, niente widget parallelo** | preserva eventi/selezione/focus e l'update in place esistente (`chat_view.py:398-409`) |
| **Niente placeholder/testo in modalità nativa** (revisione utente, sovrascrive il vecchio "placeholder sotto l'immagine") | in KITTY il widget è vuoto (né loading… né Click Enter to View né "[🖼️ ...]"); l'area è coperta dall'immagine nativa. Il placeholder testuale riappare SOLO nei fallback/degradazione (path non risolto, prepare fallito, renderer scomparso, CATIMG/OFF). Widget vuoto resta cliccabile/focusabile (`on_click` usa `attachment_path`/`attachment_id`, non il testo). Implementazione: `show_native_thumbnail` fa `update("")`; `chat_view._native_placeholder()` seleziona il testo ("" solo se KITTY + renderer + attachment_id) |
| **Emissione via `_driver.write()` nel hook** | stessa coda del WriterThread → ordine garantito, niente race |
| **Pin `textual>=8.2,<9`** | dipendenza da `post_display_hook` (API "used in tests") e `_driver` |
| **`q=2` su tutti i comandi** | le risposte OK di kitty diventano tasti spuri in Textual (verificato su `_xterm_parser.py` e sorgente kitty) |
| **Default `image_support=CATIMG`** | ~10+ test istanziano `SignalTUI()` senza argomenti |
