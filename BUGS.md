# Bug Report — Signal TUI Client

> **Stato:** Revisionato il 22/07/2026 — tutti i bug sono stati verificati contro il codice attuale.
> **Ordinamento:** Per impatto sull'utente finale (dal più grave al meno grave).

---

## 🔴 Critici (impatto diretto sull'esperienza utente)

### #1 — `_classify_attachments` processa solo il primo attachment (`signal_tui.py`, righe 473-493)



Il `for att in attachments` itera ma fa `return` al primo elemento che matcha.
Se ci sono più attachment (es. un'immagine + un video), solo il primo viene processato.
Inoltre il `return ("attachment", "📎 File", None)` finale (riga 493) è **dead code**
perché il loop ritorna sempre al primo giro.

**Impatto:** Media allegati persi — l'utente non vede attachment multipli.

---

### #6 — `_poll_worker` nessun backoff/gestione errori (`signal_tui.py`, righe 1081-1102)

Se la ricezione via RPC fallisce ripetutamente (es. daemon crash), il loop
continua a pollare ogni ~1 secondo senza backoff, riempiendo i log di errori.
L'eccezione viene catturata e loggata, ma non c'è alcun meccanismo di backoff
o notifica all'utente.

**Impatto:** CPU e log sprecati. L'utente non riceve feedback che il daemon non funziona.

---

## 🟡 Medi (funzionalità degradate)

### #5 — `_identify_contact_for_envelope` logica duplicata per `sent` (`signal_tui.py`, righe 437-466)

Controlla `sent` due volte:
1. Righe 439-449: primo blocco che cerca `dest`, `dest_number`, `dest_uuid`
2. Righe 460-464: secondo blocco che cerca solo `dest`

Il secondo controllo è ridondante e potrebbe matchare un contatto diverso dal primo.

**Impatto:** Messaggi assegnati al contatto sbagliato nella UI.

---

### #3 — `_add_message` per image non traccia timestamp in `_seen_timestamps` (`signal_tui.py`, riga 371)

Quando `msg_type == "image"`, la funzione chiama `_render_image_in_chat` e fa `return`.
Il chiamante (`_process_envelope` o `_load_messages_worker`) si aspetta
che il timestamp sia aggiunto a `_seen_timestamps`, ma per le image non lo fa.

**Nota:** Attualmente mitigato dal chiamante che aggiunge il timestamp prima di chiamare
`_add_message`, ma rimane un disallineamento: se in futuro si chiama `_add_message`
per un'immagine senza gestire il timestamp esternamente, il timestamp verrà perso.

**Impatto:** Potenziale duplicazione di immagini nella chat al refresh.

---

### #9 — `search_emoji` perde alias multipli (`emoji_picker.py`, riga 64)

La mappa `_EMOJI_TO_ALIAS` è popolata con l'**ultimo** alias incontrato per ogni
emoji. Se un emoji ha più alias (es. `😄` = `smile` e `happy`), solo l'ultimo
viene indicizzato. La ricerca potrebbe perdere match.

**Impatto:** Ricerca emoji incompleta — l'utente potrebbe non trovare l'emoji che cerca.

---

## 🟢 Minori (comportamenti subottimali ma non bloccanti)

### #2 — `_process_envelope` salva/ricarica cache ridondantemente (`signal_tui.py`, righe 620-622)

Dopo aver aggiunto un messaggio al dizionario `_cache`, chiama:
```python
_save_cache(self._cache)    # riga 620
_prune_cache()              # riga 621 — internamente fa _load_cache() + _write_cache()
self._cache = _load_cache() # riga 622 — ricarica tutto
```

`_prune_cache()` internamente (in `backend.py`) carica il file, lo pota e lo riscrive.
La sequenza è ridondante: si scrive su disco due volte invece di una.
Tuttavia, poiché `_poll_worker` è un singolo thread, non c'è rischio di race condition
o perdita dati. È solo un'inefficienza (due scritture invece di una).

**Impatto:** Minimo — due scritture su disco invece di una. Nessun impatto sull'utente.

---

### #10 — `on_input_changed` nella ricerca emoji non usa `search_emoji()` (`emoji_picker.py`, righe 347-374)


Invece di chiamare `search_emoji(query)` che è già definita, reimplementa la
ricerca in modo diverso, creando prima una lista di tutti gli emoji e poi
filtrando. Doppia implementazione = doppia manutenzione e possibili discrepanze.

**Impatto:** Manutenibilità ridotta. Nessun impatto immediato per l'utente.

---

### #4 — `_extract_message_data` quote dict vuoto (`signal_tui.py`, righe 509-510)

```python
quote = data_msg.get("quote", {})
quote_text = quote.get("text", "") if quote else None
```

Se `quote` è un dict vuoto `{}`, la condizione `if quote` è `False` (in Python
`bool({})` è `False`), quindi `quote_text` sarà `None`. Tuttavia, se `quote`
contiene altre chiavi ma non `"text"`, allora `quote.get("text", "")` ritornerà `""`
e verrà passato a `_add_message` come `quote_text=""`, creando un widget quote vuoto.

**Impatto:** In rari casi, potrebbe apparire un piccolo spazio vuoto nella chat.

---

### #7 — `_is_daemon_running` crea nuova istanza RPC ogni volta (`backend.py`, riga 84)

Crea un nuovo `SignalRPCClient()` invece di accettarne uno opzionale. Questo è
un problema perché se il daemon è stato appena avviato, il test potrebbe fallire
per una race condition.

**Impatto:** Falso negativo all'avvio del daemon, ritardando la connessione.

---

### #11 — `ImageModalScreen._render_image` non gestisce output vuoto di catimg (`ui_components.py`, righe 307-333)

Se `catimg` non produce output (es. file corrotto), `ansi_output` sarà vuoto e
`RichText.from_ansi("")` produce un `RichText` vuoto. Non causa crash ma mostra
una schermata modale vuota senza messaggio d'errore chiaro.

**Impatto:** Schermata modale vuota invece di un messaggio d'errore esplicativo.

---

### #12 — `ImageModalScreen._render_image` non gestisce `PermissionError` su attachment (`ui_components.py`, riga 289)

Se il file attachment non è leggibile (es. permessi 000), `catimg` fallirà.
L'eccezione viene catturata dal generico `except Exception` (riga 325), che mostra
un messaggio d'errore generico non chiaro per l'utente.

**Impatto:** Messaggio d'errore poco informativo.

---

### #8 — `_find_signal_cli` non gestisce `PermissionError` (`backend.py`, righe 62-70)

Se il file esiste ma non ha il permesso di esecuzione, viene ignorato silenziosamente.
Se la directory `bin/` non esiste, `iterdir()` solleva `FileNotFoundError` non gestito.
Se **tutti** i file mancano dei permessi di esecuzione, la funzione solleva
`FileNotFoundError` senza un messaggio chiaro.

**Impatto:** Crash all'avvio con messaggio poco chiaro in caso di setup errato.
