# Design — Fase 5 rendering HTML Gmail (testo citato comprimibile)

Data: 2026-10-27 · Base: PR #294.
Ambito: `server/gmail.py` (`_quote_marker`, allow-list), `web/static/gmail.js` (`collapseQuotes`),
`web/static/gmail.css`, `tests/test_gmail_html_phase5.py`.

## 1. Motivazione

Nelle risposte la cronologia citata domina il reader. Gmail la nasconde dietro un pulsante "•••".
Il sanitizer toglie classi e id, quindi il client da solo non può riconoscere le citazioni.

## 2. Riconoscimento (server)

`_pre_scan_html` aggiunge `data-quote` ai contenitori di citazione, prima che classi e id spariscano:

| Client | Elemento | Valore |
|---|---|---|
| Gmail | `div` con classe `gmail_quote` | `block` |
| Yahoo | `div` con classe `yahoo_quoted` | `block` |
| Apple Mail, Thunderbird | `blockquote type="cite"` | `block` |
| Outlook | `div#divRplyFwdMsg`, `div#appendonsend` | `after` |

- `block`: l'elemento è la citazione. `after`: la citazione è l'elemento e tutto ciò che lo segue
  allo stesso livello (Outlook mette l'intestazione "Da:… Inviato:…" e poi il testo originale come
  fratelli).
- L'attributo è in allow-list solo su `div`/`blockquote`, con valori `block|after`. Un mittente
  può impostarlo da sé, ma l'unico effetto è comprimere il proprio testo. Un elemento che lo ha
  già non viene marcato di nuovo, quindi l'idempotenza resta. Con l'inlining della Fase 3 le classi
  sono ancora presenti alla scansione del documento inlined.

## 3. Compressione (client)

`collapseQuotes` gira in `renderReader` dopo l'inserimento dell'HTML:
- si considerano solo i marcatori più esterni: le citazioni annidate si comprimono con quella che
  le contiene;
- ogni gruppo è nascosto (`hidden`) dietro un pulsante `.gmail-quote-toggle` "•••", con
  `aria-expanded` e un'etichetta "Mostra/Nascondi testo citato";
- **niente compressione se la citazione è tutto il messaggio**, cioè se fuori dai gruppi non resta
  testo (inoltri senza commento): altrimenti si vedrebbe una mail vuota.

## 4. Copertura sulle mail reali

Ultime 200 mail (sola lettura): 34 risposte o inoltri riconoscibili dall'oggetto. In **22**
messaggi la citazione è stata riconosciuta: 16 `div` Gmail, 7 `blockquote` Apple/Thunderbird e
3 marcatori Outlook.

## 5. Limiti noti

- Citazioni in testo semplice (`> `) e quelle di client che non usano questi marcatori non vengono
  compresse.
- Outlook desktop senza `divRplyFwdMsg` (separatore solo con `border-top`) non viene riconosciuto.

## 6. Test

`tests/test_gmail_html_phase5.py`:
- server: marcatori per ciascun client (anche con maiuscole), elementi che non vanno marcati,
  valori impostati dal mittente validati, marcatore unico anche con l'inlining;
- client, in Node con le funzioni reali di `gmail.js`: citazione Gmail con citazione annidata,
  gruppo "after" di Outlook, mail che è solo citazione o senza citazioni.
