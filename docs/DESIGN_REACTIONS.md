# DESIGN — Reazioni (emoji reactions) nella web UI

> **Stato:** EMBRIONE — prima bozza dell'orchestratore basata su ricognizione del codice e del DB reale. **Da espandere dall'architetto** (design completo: modello dati, parse per protocollo, API, UI, live, piano test).
> Data: 28/08/2026.

## 1. Obiettivo
Visualizzare nella web UI le **reazioni emoji** ai messaggi (es. 👍 su un proprio messaggio), per i tre backend (Signal, WhatsApp, Telegram), con aggiornamento live.

## 2. Stato attuale (verificato)

### Codice
- **Nessun handling di reazioni** nell'app: `grep -rni "reaction"` su backends/models/tui → nessun match nel codice applicativo (solo librerie: Telethon espone le reazioni ma il backend non le usa).

### DB reale
- Esistono righe `text=''` e `attachment_id IS NULL` — **candidate reazioni / eventi non gestiti** (le "mezze bolle" osservate in web UI):
  - Signal: **5** / 676
  - WhatsApp: **39** / 1928
  - Telegram: **8** / 367

### Comportamento osservato
- Quando qualcuno reagisce a un nostro messaggio, l'evento viene ingerito come **messaggio vuoto** → la web UI renderizza una **bolla vuota** ("mezza bolla").

## 3. Cosa servirebbe (per protocollo)

| Pezzo | Dettaglio |
|---|---|
| **Parse Signal** | `dataMessage.reaction` (emoji + message_id target) → mappare nel modello |
| **Parse WhatsApp** | Webhook WAHA: formato reazione → emoji + target message id (da verificare) |
| **Parse Telegram** | Telethon `updateReactions` (già esposto dalla libreria) |
| **Persistenza DB** | Rappresentazione reazioni (colonna `reaction` o tabella dedicata) riferita al messaggio target; oggi le reazioni inquinano come righe vuote |
| **API** | `reactions: [{emoji, author}]` nel payload del messaggio (`/api/messages`) |
| **UI** | Badge emoji sotto il messaggio + aggiornamento live (nuovo tipo evento WS, pattern `receipt`/`message_edit` esistente) |
| **Pulizia legacy** | Righe vuote già in DB (mezze bolle) — decidere migrazione/bonifica |
| **Filtro ingest** | Non produrre più righe vuote quando è una reaction (bloccare alla fonte) |

## 4. Stima complessiva
~5 task (paragonabile alla feature "stato consegna/lettura + edit").
- Backend parse per protocollo: la parte grossa (Signal, WA, TG).
- DB + API: medio.
- UI + live: piccolo (pattern già pronti: tick, receipt WS, message_edit).

## 5. Note / rischi
- Prima della visualizzazione conviene **bloccare la produzione di righe vuote** (filtro ingest).
- Le reazioni a messaggi nostri (is_mine) e altrui vanno distinte.
- Modello: reazioni multiple per messaggio, aggregazione per emoji.
- Da definire: rettifica/reaction change/remove (arrivano eventi di aggiornamento/rimozione).
- La TUI è la fonte di verità per il comportamento; verificare cosa fa oggi con gli eventi reaction.

## 6. Da espandere (architetto)
- Design modello dati (schema SQL) e ciclo di vita (add/change/remove).
- Parse dettagliato per ciascun protocollo (file:riga, formati reali).
- Contratto API (payload, WS event types).
- UI (rendering, posizionamento, responsive, a11y).
- Piano test (Python + JS slice + E2E).
- Ordine di implementazione.
