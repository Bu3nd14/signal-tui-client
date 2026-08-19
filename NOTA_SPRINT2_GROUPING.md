# NOTA — Sprint 2: raggruppamento contatti nella lista principale

**Data nota:** 2026-08-19
**Stato:** NON avviato. Da procedere domani (2026-08-20) con il design.

## Promemoria per l'orchestratore

Quando si passerà il task di design all'architetto per lo **Sprint 2** (raggruppamento
per contatto nella lista principale, `tui/contacts.py`), chiedergli una **SESSIONE
INTERATTIVA con l'utente** per decidere il punto critico di rendering (punto 7).

## Punto da decidere con l'utente (in sessione interattiva)

Come implementare l'expand/collapse a due livelli senza rompere i fast-path di
`_render_contact_list` (render progressivo a chunk, `_apply_contact_visibility`,
`_sync_contact_highlight`, `_contact_widgets` — oggi tutto assume "1 ListItem = 1 contatto"):

- **Opzione A — Righe flat**: ListItem(gruppo) + ListItem(sottovoce) consecutivi,
  espansione = inserire/rimuovere righe figlie, diff in-place per chiave
  (`person:<key>` / `member:<protocol>:<id>`). Massima continuità col rendering attuale.
- **Opzione B — Nesting vero**: widget dentro widget. Più pulito visivamente ma
  stravolge fast-path, visibility e selezione (molto più lavoro e rischio).

Decisione dell'architetto da portare in sessione: motivazione tecnica + impatto su
render progressivo, filtro Ctrl+W, highlight e sui 869 test esistenti.

## Contesto già noto (de-risked)

- Mappatura persona per numero: già implementata (feature Ctrl+S rubrica, PR #20):
  `normalize_phone`, `group_by_person`/`PickerEntry` in `contact_picker.py`, `contact_sort_key`.
- `list_address_book()` + merge chat attive sui 3 backend: già fatto (backends/).
- 172 match reali, 0 falsi positivi; 1 solo contatto TG senza numero ("Mamma Vod").
- Stima architetto precedente: M (3–4,5 gg) per la sola parte UI raggruppata.
- Aree: UI expand/collapse+navigazione ~50%, modello gruppo+badge aggregato ~30%,
  mappatura identità ~20% (de-risked).
- Baseline test: 869 passed (`.venv-test`).

## Da NON fare ora

- Non avviare il design completo né l'implementazione prima della sessione interattiva.
- Non toccare il branch/PR (la feature Ctrl+S è già mergiata su master).
