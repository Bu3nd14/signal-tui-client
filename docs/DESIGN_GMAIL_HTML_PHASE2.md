# Design — Fase 2 rendering HTML Gmail (allow-list estesa)

Data: 2026-10-27 · Base: Fase 1 (`docs/DESIGN_GMAIL_HTML_PHASE1.md`) + reader su foglio chiaro (PR #289).
Ambito: `server/gmail.py` (scanner, allow-list, validatori), `tests/test_gmail_html_phase2.py`.

> La pipeline della Fase 1 resta invariata (scanner lineare → nh3 → callback totale →
> invarianti finali). Cambia solo **cosa** l'allow-list accetta. Nessun `<style>`, nessuna
> classe/id, nessuna funzione oltre a `rgb/rgba/hsl/hsla`, nessun `url()`.

## 1. Motivazione

Elementi molto comuni nelle email venivano scartati, con un forte impatto sulla resa:
lo sfondo di `<body>`, il tag `<font>`, `max-width`, lo shorthand `background:#fff`, `%`/`em`
su `font-size`/`line-height`, `dir`/`direction` (RTL) e le proprietà del preheader nascosto.

## 2. Modifiche

### 2.1 `<body>` → `<div>` (scanner)

`_pre_scan_html` riscrive `<body …>` come `<div style="…">` e `</body>` come `</div>`
(`_render_body_tag`). `bgcolor`→`background-color` e `text`→`color` vengono anteposti allo
`style` esistente, che quindi vince. I valori devono corrispondere a
`#hex{3,6}` oppure a `[A-Za-z]{1,20}`, poi passano comunque per `_sanitize_style`.
Lo `style` aggiunto conta nel cap `_HTML_MAX_STYLE_ATTRS`. L'output non contiene più `body`,
quindi l'idempotenza resta.

### 2.2 Tag e attributi

| Tag | Attributo | Validazione |
|---|---|---|
| `font` (nuovo) | `color` | `#hex{3,6}` o colore con nome (`_NAMED_COLORS`) |
| `font` | `face` | 1–8 famiglie `[A-Za-z0-9][A-Za-z0-9 _-]{0,63}` separate da virgola |
| `font` | `size` | `[+-]?[1-7]` |
| `*` | `dir` | `ltr rtl auto` (`_GLOBAL_ATTR_VALIDATORS`) |
| `table/tr/td/th/col/colgroup` | `bgcolor` | ora accetta anche i colori con nome |

### 2.3 Proprietà CSS

| Proprietà | Valori |
|---|---|
| `max-width`, `max-height` | `none` · `<len>` 0–1600px · `%` 0–100 |
| `min-width`, `min-height` | come `width`/`height` (incluso `auto`) |
| `background` | **solo un `<color>`**, emesso come `background-color` (`_STYLE_ALIASES`) |
| `text-indent` | `<len>` ±256px · `%` ±100 |
| `list-style-type` | enum (`none disc circle square decimal …-roman …-alpha …-latin`) |
| `direction` | `ltr rtl` |
| `overflow` | `visible hidden auto` |
| `visibility` | `visible hidden` |
| `opacity` | numero 0–1 · `%` 0–100 |
| `font-size` | + `em` 0.5–2.5 · `%` 50–250 |
| `line-height` | + `em` 1–3 · `%` 100–300 |

## 3. Sicurezza

- **`background`**: qualsiasi valore diverso da un singolo colore (immagini, `url()`,
  `no-repeat`, `none`) scarta la dichiarazione. La matrice §8.1 della Fase 1 resta valida.
- **`em`/`%` sul font**: crescono con l'annidamento. Il limite 2.5× per livello è un
  compromesso accettato: il contenuto resta nel contenitore del reader, che ha `overflow`
  proprio. Le lunghezze di layout continuano a rifiutare `em/rem/ex/ch`.
- **`opacity`, `visibility`, `overflow:hidden`, `max-height:0`** possono nascondere
  contenuto, ma `display:none` era già ammesso: nessuna capacità nuova. Servono per il preheader.
- `position`, `z-index` e `inset` restano esclusi: il contenuto non può comunque uscire dal box.

## 4. Test

`tests/test_gmail_html_phase2.py`: ogni caso verifica idempotenza e
`_assert_document_safe`. Nella Fase 1 tre casi cambiano di proposito:
`background:red` e `font-size:100%` sono ora ammessi, `bgcolor="red"` ora è valido.
