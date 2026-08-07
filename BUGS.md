# Bug Report — Signal TUI Client

> **Stato:** Revisionato il 07/08/2026 — verificato con ruff v0.16.1 e analisi manuale del codice.
> **Ordinamento:** Per impatto sull'utente finale (dal più grave al meno grave).
> **Nota:** I bug #2, #14, #15 (pattern `_save_cache/_prune_cache/_load_cache` su JSON) sono stati **rimossi** — il passaggio a SQLite li ha resi obsoleti.

---

## 🔴 Critici (impatto diretto sull'esperienza utente)

### #1 — `_classify_attachments` processa solo il primo attachment (`backends/signal.py`, righe 350-369)

Il `for att in attachments` itera ma fa `return` al primo elemento che matcha.
Se ci sono più attachment (es. un'immagine + un video), solo il primo viene processato.
Inoltre il `return ("attachment", "📎 File", None)` finale (riga 369) è **dead code**
perché il loop ritorna sempre al primo giro. Confermato da ruff (B007).

**Impatto:** Media allegati persi — l'utente non vede attachment multipli.

---

### #6 — `_poll_worker` nessun backoff/gestione errori (`signal_tui.py`, righe 1768-1800)

Se la ricezione RPC/SSE fallisce ripetutamente (es. daemon crash), il loop
continua a pollare senza backoff, riempiendo i log di errori.
L'eccezione viene catturata e loggata, ma non c'è alcun meccanismo di backoff
o notifica all'utente.

**Impatto:** CPU e log sprecati. L'utente non riceve feedback che il daemon non funziona.

---

## 🟡 Medi (funzionalità degradate)

### #5 — `_identify_contact_for_envelope` logica duplicata per `sent` (`backends/signal.py`, righe 312-343)

Controlla `sent` due volte:
1. Righe 316-325: primo blocco che cerca `dest`, `dest_number`, `dest_uuid`
2. Righe 337-341: secondo blocco che cerca solo `dest`

Il secondo controllo è ridondante e potrebbe matchare un contatto diverso dal primo.

**Impatto:** Messaggi assegnati al contatto sbagliato nella UI.

---

### #3 — `_add_message` per image non traccia timestamp in `_seen_timestamps` (`signal_tui.py`, righe 517-520)

Quando `msg_type == "image"`, la funzione chiama `_render_image_in_chat` e fa `return`.
Il chiamante si aspetta che il timestamp sia aggiunto a `_seen_timestamps`, ma per le image non lo fa.

**Nota:** Attualmente mitigato dal chiamante che aggiunge il timestamp prima di chiamare
`_add_message`, ma rimane un disallineamento: se in futuro si chiama `_add_message`
per un'immagine senza gestire il timestamp esternamente, il timestamp verrà perso.

**Impatto:** Potenziale duplicazione di immagini nella chat al refresh.

---

### #9 — `search_emoji` perde alias multipli (`emoji_picker.py`, riga 45)

La mappa `_EMOJI_TO_ALIAS` è popolata con l'**ultimo** alias incontrato per ogni
emoji. Se un emoji ha più alias (es. `😄` = `smile` e `happy`), solo l'ultimo
viene indicizzato. La ricerca potrebbe perdere match.

**Impatto:** Ricerca emoji incompleta — l'utente potrebbe non trovare l'emoji che cerca.

---

## 🟢 Minori (comportamenti subottimali ma non bloccanti)

### #10 — `on_input_changed` nella ricerca emoji non usa `search_emoji()` (`emoji_picker.py`, righe 347-374)

Invece di chiamare `search_emoji(query)` che è già definita, reimplementa la
ricerca in modo diverso, creando prima una lista di tutti gli emoji e poi
filtrando. Doppia implementazione = doppia manutenzione e possibili discrepanze.

**Impatto:** Manutenibilità ridotta. Nessun impatto immediato per l'utente.

---

### #4 — `_extract_message_data` quote dict vuoto (`backends/signal.py`, righe 412-413)

```python
quote = sent.get("quote", {})
quote_text = quote.get("text", "") if quote else None
```

Se `quote` è un dict vuoto `{}`, la condizione `if quote` è `False` (in Python
`bool({})` è `False`), quindi `quote_text` sarà `None`. Tuttavia, se `quote`
contiene altre chiavi ma non `"text"`, allora `quote.get("text", "")` ritornerà `""`
e verrà passato come `quote_text=""`, creando un widget quote vuoto.

**Impatto:** In rari casi, potrebbe apparire un piccolo spazio vuoto nella chat.

---

### #7 — `_is_daemon_running` crea nuova istanza RPC ogni volta (`backend.py`, righe 86-93)

Crea un nuovo `SignalRPCClient()` invece di accettarne uno opzionale. Questo è
un problema perché se il daemon è stato appena avviato, il test potrebbe fallire
per una race condition.

**Impatto:** Falso negativo all'avvio del daemon, ritardando la connessione.

---

### #11 — `ImageModalScreen._render_image` non gestisce output vuoto di catimg (`ui_components.py`, riga 387)

Se `catimg` non produce output (es. file corrotto), `ansi_output` sarà vuoto e
`RichText.from_ansi("")` produce un `RichText` vuoto. Non causa crash ma mostra
una schermata modale vuota senza messaggio d'errore chiaro.

**Impatto:** Schermata modale vuota invece di un messaggio d'errore esplicativo.

---

### #12 — `ImageModalScreen._render_image` non gestisce `PermissionError` su attachment (`ui_components.py`, riga 340)

Se il file attachment non è leggibile (es. permessi 000), `catimg` fallirà.
L'eccezione viene catturata dal generico `except Exception` (riga 409), che mostra
un messaggio d'errore generico non chiaro per l'utente.

**Impatto:** Messaggio d'errore poco informativo.

---

### #8 — `_find_signal_cli` non gestisce `PermissionError` (`backend.py`, righe 67-75)

Se il file esiste ma non ha il permesso di esecuzione, viene ignorato silenziosamente.
Se la directory `bin/` non esiste, `iterdir()` solleva `FileNotFoundError` non gestito.
Se **tutti** i file mancano dei permessi di esecuzione, la funzione solleva
`FileNotFoundError` senza un messaggio chiaro.

**Impatto:** Crash all'avvio con messaggio poco chiaro in caso di setup errato.

---

### #16 — `_parse_contacts_from_output` parsing fragile (`backends/signal.py`, righe 177-195) ✅ RISOLTO

**Fix:** sostituito `line.split()` con regex `_RE_CONTACT_LINE` che usa named groups. Ora
gestisce correttamente nomi con spazi (es. "Mario Rossi"). Commit: (vedi git log).

---

### #18 — `_clean_download_dir` race condition potenziale (`backend.py`, righe 898-911)

Se due download vengono serviti in rapida successione, `_clean_download_dir()`
cancella il file del download precedente prima che l'utente abbia finito di scaricarlo.
Il cleanup cancella *tutti* i file nella directory temporanea invece di solo quelli vecchi.

**Impatto:** L'utente potrebbe cliccare un link di download e trovare un 404 perché
il file è già stato cancellato da un download successivo.

---

### #19 (nuovo) — `_prune_cache` ha variabile `cutoff` inutilizzata (`backend.py`, riga 359)

```python
now_ms = int(time.time() * 1000)
cutoff = now_ms - CACHE_RETENTION_DAYS * 24 * 60 * 60 * 1000
```

La variabile `cutoff` è calcolata ma mai usata — la potatura è passata da time-based
a count-based (200 messaggi per contatto). Il calcolo e la costante `CACHE_RETENTION_DAYS`
sono residui della vecchia logica. Rilevato da ruff (F841).

**Impatto:** Dead code — `CACHE_RETENTION_DAYS` non ha più effetto. La retention è
solo count-based (200 messaggi/contatto).

---

### #20 (nuovo) — `subprocess.run` senza `check` esplicito (`backend.py`, riga 98; `backends/signal.py`, riga 166)

```python
result = subprocess.run([...], capture_output=True, text=True)
```

Manca `check=False` esplicito. Se il processo fallisce, il comportamento dipende
dal chiamante che controlla `result.returncode` — ma senza `check` il default è
silenzioso. Rilevato da ruff (PLW1510).

**Impatto:** Basso — i chiamanti già gestiscono `returncode`, ma il codice è
ambiguo per un futuro maintainer.

---

## 🗑️ Bug rimossi (obsolescenza confermata)

| # | Descrizione | Motivo |
|---|-------------|--------|
| #2 | `_process_envelope` salva/ricarica cache ridondantemente | Funzione non esiste più — envelope parsing in `backends/signal.py` usa SQLite |
| #14 | `on_list_view_selected` salva/ricarica cache ridondantemente | Pattern `_save_cache`/`_load_cache` rimosso con SQLite |
| #15 | `on_input_submitted` salva/ricarica cache ridondantemente | Pattern `_save_cache`/`_load_cache` rimosso con SQLite |
