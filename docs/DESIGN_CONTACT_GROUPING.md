# DESIGN — Raggruppamento contatti per persona nella lista principale (Sprint 2)

> Redatto dall'architetto (input), trascritto su file dall'orchestratore.
> Worktree: `/home/rob/signal-tui-client-worktree`, branch `feature/contact-grouping`, base `94cd6b0`.
> Baseline test: **1080** · Approccio: **Opzione A2** (righe flat, DOM completo, collasso e filtro come toggle `display`).

---

## 1. Modello dati e proiezione

### 1.1 Riutilizzo della macchina di grouping (nessuna modifica a `contact_picker.py`)

Si riusano **in sola lettura** dalla main list (i 46 test del picker restano indenni):

- `group_by_person(contacts)` (`contact_picker.py:119-141`) → `list[PickerEntry]`; `_group_key` (`:72-96`) si applica già a **tutti** i contatti: chi ha numero → `phone:<digits>`, altrimenti `raw:<protocol>:<id>` (gruppi/canali/`@lid` non risolti/TG senza numero — incluso "Mamma Vod" — diventano entry single-member con chiave `raw:`).
- `PickerEntry` (`:63-69`): `key`, `display_name` (da `_best_display_name`, `:109-116`), `members: dict[protocol → ChatContact]`.
- `entry_default_contact` (`:144-158`): usato **solo per la posizione del gruppo** (recency del membro più recente — decisione 4), NON per l'apertura (decisione 3).
- `contact_sort_key` (`:164-184`): ordinamento dei gruppi.
- `_protocol_priority` (`:59-60`): **ordine fisso dei membri** Signal → WhatsApp → Telegram (decisione 6). Se si preferisce, esporre alias pubblico `protocol_priority` — dettaglio libero.

**Divergenza picker vs main-list (da NON unificare):** il picker ordina i membri per `(-ts, priority)` (`contact_picker.py:448-451`, `:585-588`) e concatena emoji nel label (`:440-455`); la main list usa ordine **solo priorità** e header **senza emoji** (decisioni 5-6). Nessuna modifica al picker.

### 1.2 Chiavi di riga

- Riga **gruppo**: `item._contact_id = f"person:{entry.key}"` (es. `person:phone:393331234567`, `person:raw:telegram:12345`).
- Riga **membro**: `item._contact_id = c.cache_key` (**invariato**).

`cache_key` è già protocol-scoped (`models.py:36`, `:71-74` → `signal:+391`), quindi il prefisso `member:<protocol>:` è ridondante: mantenere il bare `cache_key` preserva intatti `_contact_widgets` (`app.py:161`), la risoluzione di selezione (`contacts.py:517-527`) e 6 test esistenti. I due namespace sono disgiunti per costruzione (un `cache_key` inizia col protocollo, mai con `person:`).

Ogni riga porta due attributi nuovi:
- `_row_kind: str` — `"group"` | `"member"`;
- `_group_key: str` — la `entry.key` del gruppo di appartenenza (sulle righe gruppo = la propria).

### 1.3 Proiezione `_visible_rows()`

Nuova funzione in `contacts.py`. Ritorna una lista piatta di descrittori riga (`_Row(kind, key, group_key, contact=None, entry=None)`):

```
entries = group_by_person(self.contacts)
entries.sort(key=lambda e: contact_sort_key(entry_default_contact(e)))   # decisione 4
rows = []
for e in entries:
    members = sorted(e.members.values(), key=lambda c: _protocol_priority(c.protocol))  # decisione 6
    rows.append(_Row("group", f"person:{e.key}", e.key, entry=e))
    rows += [_Row("member", m.cache_key, e.key, contact=m) for m in members]
```

**Proprietà chiave (A2):** la proiezione **non dipende dallo stato di collasso** né dal filtro → `want_ids` è stabile; collasso e filtro agiscono solo su `display`.

### 1.4 Stato nuovo (in `app.py.__init__`, accanto a `_contact_widgets` r. 161)

- `_group_widgets: dict[str, ListItem]` — `group_key` → riga header;
- `_member_to_group: dict[str, str]` — `cache_key` → `group_key`;
- `_expanded_groups: set[str]` — **insieme dei gruppi ESPANSI** (default vuoto ⇒ **default COLLASSATO**). **Decisione utente: all'avvio della TUI tutti i contatti sono collassati** (si vedono solo gli header); l'espansione è opt-in per gruppo.

---

## 2. Rendering

### 2.1 `_render_contact_list` (`contacts.py:205-300`) — meccanica invariata, input nuovo

- `want_ids` (r. 229) → `[row.key for row in rows]` con `rows = self._visible_rows()`.
- `_sync_item(item, c)` (r. 237-247) → **`_sync_row(item, row)`**:
  - `kind == "member"`: logica attuale identica (label da `_contact_label`, classi `protocol-*`), più `add_class("contact-member")`, `item._group_key = row.group_key`;
  - `kind == "group"`: label da `_group_label(row.entry)`, classe `contact-group`, **nessuna** classe `protocol-*` (header neutro cross-protocol — decisione 5), `item._group_key = row.group_key`.
- Fast-path 1 (r. 249-252): `zip(existing, rows)` + `_sync_row`.
- Fast-path 2 reorder (r. 253-268): `move_child` sposta header+membri come blocco (consecutivi in `reordered`).
- Fast-path 3 superset (r. 269-292): itera `rows`; creazione via nuovo helper condiviso **`_build_row_item(row)`** (estratto da r. 186-194); registrazione nelle tre mappe (membri in `_contact_widgets`, header in `_group_widgets`, sempre `_member_to_group`).
- Fallback (r. 293-296) → `_start_progressive_render`: r. 169 svuota anche `_group_widgets` e `_member_to_group`; `_pending_contacts` → `_pending_rows`.
- R. 300 (`_apply_contact_visibility()` finale): invariato.

### 2.2 `_render_next_chunk` (`contacts.py:172-203`)

Itera `_Row`; `item = _build_row_item(row)`; `item.display = self._row_visible(row, visible_keys)`; registrazione mappe. Chunk su **righe** (~700 → 14 chunk da 50, ~0,7s progressivi). Opzione follow-up: chunk=100 se il boot risulta percepibile.

### 2.3 `_apply_contact_visibility` (`contacts.py:93-138`) — riscrittura con regole gruppo/membro

Helper condiviso:

```
_row_visible(row, visible_keys):   # visible_keys = {c.cache_key for c in self._filtered_contacts()} (r. 107, invariato)
    member → (row.key in visible_keys) and (row.group_key in self._expanded_groups)   # default collassato: nascosto finché non espanso
    group  → almeno un membro dell'entry ha cache_key in visible_keys   # decisione 7
```

Cache consigliata `_group_members: dict[str, list[str]]` (group_key → member cache_keys) per evitare di ricorrere `group_by_person` due volte per render.

Risoluzione highlight (sostituisce r. 109-131):
1. `selected_contact` con riga membro visibile → `index` = sua posizione (come oggi);
2. altrimenti, header del suo gruppo visibile (gruppo collassato o membro filtrato ma gruppo no) → `index` = posizione header (`_member_to_group` + `_group_widgets`, posizione reale in `children`);
3. altrimenti prima riga visibile; se nulla → `selected_contact = None` (invariato).

Chiusura con `_sync_contact_highlight(contact_list, contact_list.index)` (r. 138) — **invariata**.

**Vincolo di compatibilità (obbligatorio):** leggere il kind con `getattr(child, "_row_kind", "member")` e i lookup di gruppo con `.get(...)` guarded → i test con righe finte solo-membro (`test_ui_protocol.py:122-334`) passano invariati.

### 2.4 Toggle — `_toggle_group(group_key)`

```
if group_key in self._expanded_groups: discard else add
header = self._group_widgets.get(group_key) → aggiorna label (chevron ▸/▾) via _group_label
self._apply_contact_visibility()     # SOLO display: nessun render, nessun clear
```

Non chiama `_render_contact_list`, non sposta il focus.

**Default collassato (decisione utente)**: all'avvio `_expanded_groups` è vuoto → si vedono solo gli header; i membri compaiono alla prima espansione del gruppo. La chat si apre espandendo il gruppo e cliccando la riga membro. L'header mostra il badge unread aggregato anche da collassato (il contatto non va "perso" quando arrivano messaggi).

**Dispatch input:**
- `on_list_view_selected` (`contacts.py:511-531`): in testa, `if getattr(event.item, "_row_kind", "member") == "group": self._toggle_group(event.item._group_key); return`. Il resto invariato → click/Enter su header = **solo toggle** (decisione 3); su membro = flusso attuale. Enter e click convergono qui (binding nativo `ListView` + `_on_list_item__child_clicked`, `ui_components.py:118-138`).
- **Binding `space`** su `ContactListView` (`ui_components.py:98-138`): `Binding("space", "toggle_group", show=False)`; l'action legge la riga evidenziata e se è gruppo chiama `app._toggle_group`. Verificato: Textual 8.2.8 `ListView.BINDINGS` = `enter/up/down` → nessun conflitto; widget-scoped (non ruba spazio all'input messaggi).

### 2.5 Label header — `_group_label(entry)` (in `contacts.py`; `events.py` non si tocca)

```
chevron = "▸" if entry.key not in self._expanded_groups else "▾"   # ▸ collassato (default), ▾ espanso
unread  = sum(self._unread_counts.get(m.cache_key, 0) for m in entry.members.values())
if self.selected_contact and self.selected_contact.cache_key in {m.cache_key for m in members}: unread = 0   # decisione 5
label = f"{chevron} {entry.display_name}" + (f" *{unread}" if unread else "")
```

Nessun emoji, nessun ✍️/💭 sull'header (decisione 5); i membri mantengono `_contact_label` (`events.py:447-463`) invariato.

### 2.6 Aggiornamenti mirati (badge/typing) sull'header

- **Badge aggregato**: il flush del polling aggiorna gli header via `_sync_row` nel fast-path — coperto senza codice nuovo. In `_select_contact` (azzera l'unread del membro a r. 466): aggiungere il refresh immediato della label header del gruppo (`_member_to_group` + `_group_widgets` + `_group_label`).
- **Typing**: `_update_typing_label` (`events.py:416-445`) resta **member-only** (decisione 5) → zero modifiche.
- **Cambio di group_key a runtime** (es. `@lid` → numero): cambia l'insieme delle righe → fallback progressivo (r. 293-296). Raro e corretto per costruzione.

---

## 3. Selezione / highlight / navigazione

| Percorso | Comportamento |
|---|---|
| Click/Enter su **membro** | `on_list_view_selected` → `_select_contact` invariato (`contacts.py:409-531`); apertura chat, open-or-create (`:354-381`), mark-read (`:457-466`) |
| Click/Enter su **header** | solo `_toggle_group` (decisione 3); nessuna apertura chat |
| `_select_contact`, membro visibile | highlight sulla riga membro (`:475-488`), invariato |
| `_select_contact`, membro in gruppo **collassato** (o da picker) | fallback: `index` = posizione header (guarded); **niente auto-espansione** |
| `_sync_contact_highlight` (`:140-152`) | invariata: una sola `-highlight` |
| Navigazione ↑/↓ | nativa ListView, già collaudata con righe `display=False` |
| Focus | `_select_contact` riporta focus all'input (invariato); `_toggle_group` non tocca il focus |

---

## 4. Filtro Ctrl+W

`action_cycle_protocol_filter` (`:340-350`) e `_apply_contact_filter` (`:302-338`) invariati. Regole di visibilità da §2.3 (decisione 7): gruppo senza membri del protocollo filtrato → header `display=False`; gruppo con il protocollo → header visibile + solo i membri di quel protocollo (se collassato, tutti i membri nascosti comunque). DOM completo preservato (invariante r. 96-105).

---

## 5. CSS (`tui/css.py`)

Dopo il blocco `#contact-list ListItem` (r. 35-37, padding attuale `1 1`):

```css
#contact-list ListItem.contact-group {
    text-style: bold;
    padding: 0 1;              /* header più compatto */
}
#contact-list ListItem.contact-member {
    padding: 1 1 1 3;          /* indentazione sotto l'header */
}
```

Nessuna regola colore nuova; le `-highlight` esistenti (r. 51-56) si applicano a entrambi i kind. Le classi `protocol-*` (r. 66-82) restano solo sui membri. Il chevron ▸/▾ dà lo stato anche agli screen reader.

---

## 6. Piano di test — rivalutazione con la decisione 1 (header per TUTTI)

Con header per tutti, `want_ids` raddoppia (~350 → ~700 righe). L'impatto resta contenuto grazie ai due vincoli di compatibilità (§2.3).

### 6.1 Test esistenti che SI ROMPONO e vanno aggiornati (7)

| Test | Riga | Perché rompe | Aggiornamento |
|---|---|---|---|
| `test_reorder_keeps_all_contacts_in_dom_with_filter` | `test_ui_protocol.py:336-355` | `len(items) == 3` → 6; set `_contact_id` include `person:*` | attese su righe membro (`_row_kind == "member"`); header gruppo WA-only nascosto sotto filtro signal |
| `test_filter_render_applies_to_view` | `:357-367` | `len == 1` → 2 (header+membro); `items[0]` è l'header | `len == 2`, `protocol-whatsapp` su `items[1]` |
| `test_render_in_place_when_composition_unchanged` | `:735-750` | `len == 2` → 4 | attese raddoppiate |
| `test_reorder_in_place_when_order_changes` | `:752-788` | sequenza `_contact_id` ora `[person:kB, ckB, person:kA, ckA]` | attese con header; invarianza oggetti preservata |
| `test_contact_list_renders` | `test_tui_integration.py:45-53` | labels includono header (`▾ Mario`, `📱 Mario`, …) | nuova attesa a 6 label |
| `test_select_contact` | `:57-69` | click su `children[0]` = header → toggle | click su `children[1]` (membro) |
| `test_chat_title_updates` | `:170-181` | idem | click su `children[1]` |

### 6.2 Test che PASSANO invariati

- `test_ui_protocol.py:122-334` (6 test selezione/highlight/visibilità con righe finte solo-membro), `:369-465` (filtro/titolo/classi/picker-gating), TestContactSorting (`:495-591`), TestContactListFlush (`:838-998`), badge `*N` (`:974-998`).
- `test_typing_indicator.py:442-502` (4 test: `_update_typing_label` member-only).
- `test_open_or_create.py` (11), `test_address_book.py` (82), `test_contact_picker.py` (46), `test_refresh_chat.py` (20), `test_tui_integration.py:285-297`.

### 6.3 Nuovi test (~18), in `test_ui_protocol.py` + nuovo `test_contact_grouping.py`

1. Proiezione: header per ogni contatto incluso single-member; chiavi `person:*` vs `cache_key` disgiunte; ordine gruppi per recency del default; ordine membri fisso signal→wa→tg anche contro recency; "Mamma Vod" → gruppo single-member con header.
2. `_group_label`: nessun emoji; chevron ▸/▾ coerente; badge somma multi-membro; soppressione badge quando il selezionato è nel gruppo.
3. Toggle: **default collassato** — all'avvio i membri sono `display=False` (solo header visibili), header visibile, conteggio DOM invariato, nessun `clear`; espansione (toggle) li mostra; ri-collasso li nasconde; toggle non muove il focus né chiama `_render_contact_list`.
4. Click/Enter su header → solo toggle (`_select_contact` mai chiamato); click su membro → `_select_contact`.
5. Filtro+gruppi: gruppo senza protocollo filtrato sparisce; gruppo misto → header visibile + solo membro filtrato; combinazione filtro × collasso.
6. Highlight: selezione con gruppo collassato → `index` sull'header; selezione da picker verso gruppo collassato → header evidenziato, niente auto-expand.
7. Badge header dopo flush e dopo `_select_contact` (somma + soppressione).
8. Reorder a blocco: nuovo messaggio su membro di gruppo multi → header+membri si spostano insieme, stessi oggetti.

**Bilancio: 7 aggiornati + ~18 nuovi → suite da 1080 a ~1098.**

---

## 7. Passi di implementazione ordinati

1. **`tui/app.py`** (r. 160-167): `_group_widgets`, `_member_to_group`, `_expanded_groups` (default vuoto = tutti collassati) (+ opzionale `_group_members`).
2. **`tui/contacts.py`** — il grosso del lavoro, in ordine:
   a. import da `contact_picker`: `group_by_person`, `entry_default_contact`, `_protocol_priority`;
   b. `_Row` + `_visible_rows()` (§1.3);
   c. `_group_label(entry)` (§2.5) e `_row_visible(row, visible_keys)` (§2.3);
   d. `_build_row_item(row)` estratto da r. 186-194;
   e. `_render_next_chunk` (r. 172-203) su righe + registrazione mappe;
   f. `_start_progressive_render` (r. 154-170): `_pending_rows`, clear delle 3 mappe (r. 169);
   g. `_render_contact_list` (r. 205-300): `want_ids` da proiezione, `_sync_row`, superset su righe;
   h. `_apply_contact_visibility` (r. 93-138): riscrittura con `_row_visible` + fallback header (§2.3), chiusura `_sync_contact_highlight` invariata;
   i. `_toggle_group(group_key)` (§2.4);
   j. `on_list_view_selected` (r. 511-531): dispatch gruppo in testa;
   k. `_select_contact` (r. 468-492): fallback highlight su header + refresh label header dopo mark-read.
3. **`ui_components.py`** (`ContactListView`, r. 98-138): binding `space` → toggle.
4. **`tui/css.py`**: le due regole di §5.
5. **`tui/events.py`**: **nessuna modifica**.
6. **Test**: aggiornare i 7 di §6.1, aggiungere i ~18 di §6.3; verifica `pytest` completo (1080 → ~1098).

**NON fare:**
- Non modificare `contact_picker.py`.
- Non introdurre un fast-path "subset": il collasso non rimuove righe dal DOM.
- Non aprire chat dall'header, non auto-espandere alla selezione.
- Non aggiungere emoji/typing/protocol-classi agli header.
- Non ordinare i membri per recency.
- Non persistere `_expanded_groups` (stato di sessione, default collassato all'avvio).
- Non rinominare/ri-keyare `_contact_widgets`.
- Non toccare `_filtered_contacts`, `_sort_contacts`, `contact_sort_key`, il flush di `polling.py`/`unread_reply.py`.

---

## 8. Rischio residuo / follow-up

- **Prestazioni**: con il **default collassato** la startup monta solo gli header (~350 righe, ~7 chunk) — più veloce dell'ipotesi a 700 righe; l'espansione mostra i membri via `display` (già nel DOM, istantanea). Se il boot risultasse comunque percepibile, `_render_chunk_size` (`app.py:166`) da 50 a 100 (follow-up misurato).
- **Churn di group_key** (risoluzione `@lid` → numero): rebuild progressivo, raro e corretto; coperto da un test.
- **Altezza righe**: padding header `0 1` compensa parzialmente lo scroll con ~700 righe; eventuale follow-up estetico.
- **Accessibilità**: stato via chevron testuale; gerarchia piatta linearizzabile; Enter/space togglano, Enter su membro apre.
- **Follow-up futuri (non Sprint 2)**: persistenza dei gruppi collassati; "collassa tutti/espandi tutti"; badge per-protocollo nell'header.

Il design è completo e senza decisioni aperte: l'implementatore può partire dal passo 7.1.
