# DESIGN — Filtro "solo non letti" (Ctrl+U) + status bar clickabile per-icona

> Verifica: architetto (verdetto approvato con 3 correzioni). Decisioni UX: utente.
> Branch: `feat/unread-filter` · Base: master `8e3203c`.

## A. Filtro "solo non letti" (toggle `ctrl+u`)

Ortogonale al filtro protocollo Ctrl+W: si compone (unread + WhatsApp = solo non letti WhatsApp).

### A.1 `_filtered_contacts()` (`tui/contacts.py:95-99`)
Aggiungere l'intersezione:
```python
def _filtered_contacts(self):
    if self._protocol_filter in ("signal", "whatsapp", "telegram"):
        contacts = [c for c in self.contacts if c.protocol == self._protocol_filter]
    else:
        contacts = list(self.contacts)
    if self._unread_only:
        contacts = [c for c in contacts if self._unread_counts.get(c.cache_key, 0) > 0]
    return contacts
```
`visible_keys` è l'unico produttore per `_apply_contact_visibility` e `_render_next_chunk` → si propaga ovunque. **Nessuna modifica a `_row_visible`/`_apply_contact_visibility`** (header visibile iff ≥1 membro in `visible_keys`; in filter mode i member sono masked).

### A.2 Nuovo stato e action
- `self._unread_only: bool = False` in `app.py.__init__`.
- `action_toggle_unread_filter(self)`: inverte `_unread_only`, poi `_apply_contact_filter()` (che riapplica visibility + titolo/bordo).
- Binding app-level: `Binding("ctrl+u", "toggle_unread_filter", "Unread", priority=True)`.
  - ⚠️ `ctrl+u` è legato a "delete to line start" in `TextArea`/`Input` (Textual 8.2.8) e `MessageTextArea` li eredita: con `priority=True` l'azione app scatta ma **sottrae lo shortcut di editing all'input** → RIMUOVERE `ctrl+u` dai `BINDINGS` di `MessageTextArea` (documentando la perdita). Il focus resta all'input, il testo non viene toccato.
- `check_action` (app.py:252): `toggle_unread_filter` → `False` quando il picker modale è attivo (stesso pattern di `cycle_protocol_filter`).

### A.3 Refresh in `_select_contact`
Dopo `self._unread_counts[cache_key] = 0` (~riga 686), chiamare `_apply_contact_visibility()` PRIMA della logica di highlight: con unread_only attivo il contatto appena letto sparisce (riga → header → first-visible; la chat resta aperta, `selected_contact` non deselezionato). UX voluta ("cartella unread").

### A.4 Titolo e classe bordo
- `_filter_title_suffix()`: comporre, es. `" - WhatsApp · Unread"`, `" - All · Unread"`.
- `_apply_contact_filter()`: 4ª classe `chat-filter-unread` nel loop remove/add (~540-557); CSS: bordo dedicato (es. `#contact-list.chat-filter-unread { border: solid $accent; }` — colore da scegliere, proposto `#ffcc00`/accent).

### A.5 Comportamenti attesi
- `All + unread`: vista raggruppata, solo gruppi con ≥1 membro non letto.
- `Proto + unread`: vista flat (masking) con solo i non letti di quel backend.
- Zero non letti → lista vuota; il contatore in status bar resta informativo.

## B. Status bar clickabile per-icona

### B.1 Struttura widget (dentro `#bottom-bar`)
```
StatusBar(Horizontal, id="status-bar")
 ├── StatusSegment(Static, id="status-signal")    📱 N
 ├── StatusSegment(Static, id="status-whatsapp")  💬 N
 ├── StatusSegment(Static, id="status-telegram")  📨 N
 └── Static(id="status-text")                     (messaggi transienti/errori)
```
- `StatusSegment` = `Static` sottoclassato con `on_click` → emette `StatusSegment.Pressed(protocol)` (custom Message) + `event.stop()`. **NON `Button`**: Button ruba il focus all'input (e casca in `on_button_pressed`); Static non è focusable → zero focus-steal (precedente: `MessageWidget.on_click`, ui_components.py:377).
- Precedenza via `display` toggle dei due gruppi di figli (nessun mount/unmount): `_status_active=True` → solo `status-text`; idle → solo segmenti.
- API widget: `show_message(text)`, `show_default(totals)`, `set_counts(dict)`, `sync_active(protocol_filter, unread_only)` (classe `-active` sul segmento dello stato corrente).

### B.2 Adattamento metodi (`tui/app.py`)
- `_status`: invariato (timer/`_status_active`); `query_one("#status-bar").update(text)` → `.show_message(text)`.
- `_status_clear` / `_render_backend_unread_status`: invariati i contratti; il render diventa calcolo dei 3 totali → `set_counts` + `show_default`. Estrarre `_backend_unread_text()` pura (per preservare i test di formato).
- Handler `on_status_segment_pressed(proto)` → nuovo `_activate_backend_unread(proto)`:
  ```
  if self._protocol_filter == proto and self._unread_only:
      self._unread_only = False
  else:
      self._protocol_filter = proto; self._unread_only = True
  self._apply_contact_filter()      # visibility + title + bordo
  self._sync_status_segments()      # classe -active
  ```
  (NON riusare `action_cycle_protocol_filter`: cicla; qui è un set mirato. Il secondo click spegne SOLO unread, non il protocollo.)
- Guard: ignorare il click se `isinstance(self.screen, ModalScreen)`.

### B.3 CSS
`#status-bar` width 45 resta; segmenti `width:auto`, padding `0 1`; `:hover { background: $boost; }` opzionale; classe `.status-segment-active` per lo stato corrente.

## C. Test e coverage

- **A**: intersezione in `_filtered_contacts` (All+unread, Proto+unread); toggle + suffix/classi; header nascosto se gruppo senza unread; `_select_contact` con unread_only (riga sparisce, chat resta, highlight fallback); binding `ctrl+u` con input focalizzato (scatta, testo intatto) e con picker aperto (no-op); contatto ghost con unread_only (nascosto ma chat apribile).
- **B**: `set_counts`/`show_default` (ordine fisso, `-` se 0); click segmento → stato filtro atteso + re-click toggle-off; `_status` nasconde segmenti / `_status_clear` li ripristina coi totali aggiornati; `-active` sincronizzata dopo Ctrl+W/Ctrl+U e dopo click.
- **Esistenti da aggiornare**: `tests/test_status_backend_unread.py` (il fake `update`-only + `query_one` mock globale → metodi `show_message/show_default/set_counts`); i 2 test integration che leggono `query_one("#status-bar", Static).content` → nuovo widget/segmenti. Gli altri file toccano `_status` solo come chiamata/mock → impatto nullo se la firma resta.

## D. Rischi / regressioni (dal verdetto architetto)

1. `ctrl+u` senza `priority=True` = binding mai attivo; con priority = shadow di delete-to-line-start nell'input (mitigato: rimozione esplicita da `MessageTextArea` + gate `check_action` + voce nel Footer).
2. Senza A.3 la riga del contatto letto resta stale-visibile in unread view fino al prossimo flush.
3. La suite status si rompe se fake/test integration non aggiornati nello stesso commit.
4. Nessun costo perf: `visible_keys` già O(N) per pass; nessun timer/worker nuovo.
5. Nessuna migrazione dati.

## Cosa NON fare

- Non riusare `action_cycle_protocol_filter` per il click (cicla, non set).
- Non usare `Button` per i segmenti (focus-steal).
- Non rimuovere la precedenza default/transiente/errore (i segmenti sono solo nello stato default).
- Non toccare `_row_visible`/`_apply_contact_visibility` (già sufficienti).

## Refinement post-test manuale (utente)

1. **Rimuovere il breakdown per-backend in vista filtro** (`_group_label`, ramo filtro): in ogni filtro mostrare SOLO gli unread relativi alla vista filtrata → badge bare ` *N` = unread del solo membro del protocollo filtrato (niente icone, niente breakdown degli altri backend). Il contatore per-backend in status bar è il nuovo meccanismo informativo. Ramo "all" invariato (somma aggregata + decisione 5). Aggiornare docstring e test dei breakdown.
2. **Colore banner**: in `_apply_contact_filter`, la classe del protocollo (`chat-filter-signal/whatsapp/telegram`) deve restare applicata anche quando `_unread_only` è attivo (il colore del backend non va perso). `chat-filter-unread` solo per il caso `_unread_only and _protocol_filter == "all"`.
3. **Titolo senza wrap**: quando `_unread_only`, `#ContactsTitle` diventa `📇 {suffix}` (senza la parola "Contacts", che fa andare a capo col suffisso lungo); altrimenti `📇 Contacts{suffix}`.
4. **Status bar allineata a destra**: i 3 segmenti `StatusSegment` vanno allineati a destra nel contenitore `#status-bar` (width 45) con il meccanismo Textual idiomatico (`align-horizontal: right` sul contenitore o equivalente verificato).

## Refinement 2 (post-test, utente)

5. **Shortcut `ctrl+a` (non documentato, `show=False`) → vista All**: `_protocol_filter = "all"` + `_unread_only = False` + `_apply_contact_filter()` + `_sync_status_segments()`. Binding app-level `priority=True`. Verificato: `ctrl+a` in Textual 8.2.8 è "Go to start" solo su `Input` (search del picker — perdita accettabile, resta `home`), NON su `TextArea`/`MessageTextArea`. Nuova action `go_to_all` (o nome analogo).
6. **Click segmento condizionale** (`_activate_backend_unread(proto)`): 
   - se già `(proto, unread_only=True)` → `unread_only=False` (re-click spegne l'unread view, protocollo resta);
   - altrimenti → `_protocol_filter = proto` e `_unread_only = (self._backend_unread_total(proto) > 0)`:
     - backend CON non letti → vista filtro+unread (Ctrl+U equivalente);
     - backend SENZA non letti → solo filtro protocollo (Ctrl+W equivalente).
   Poi `_apply_contact_filter()` + `_sync_status_segments()`.
