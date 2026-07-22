# Bug Report — Signal TUI Client

## signal_tui.py

### Bug #1 — `_classify_attachments` loop sempre al primo match (linee 418-438)
Il `for att in attachments` itera ma fa `return` al primo elemento che matcha.
Se ci sono più attachment, solo il primo viene processato.
Inoltre il `return` finale dopo il loop (`return ("attachment", "📎 File", None)`)
non è mai raggiungibile perché il loop ritorna sempre al primo giro.

### Bug #2 — `_process_envelope` salva/ricarica cache ridondantemente (linee 549-551)
Dopo aver aggiunto un messaggio al dizionario `_cache`, chiama:
`_save_cache(self._cache)` → `_prune_cache()` → `self._cache = _load_cache()`.
Ma `_prune_cache()` internamente fa già `_load_cache()` e `_write_cache()`.
C'è una ridondanza che potrebbe causare perdita di dati se nel frattempo
arrivano altri messaggi.

### Bug #3 — `_add_message` per image non traccia timestamp in `_seen_timestamps` (linea 316)
Quando `msg_type == "image"`, la funzione chiama `_render_image_in_chat` e fa
`return`. Il chiamante (`_process_envelope` o `_load_messages_worker`) si aspetta
che il timestamp sia aggiunto a `_seen_timestamps`, ma per le image non lo fa.

### Bug #4 — `_extract_message_data` quote dict vuoto (linea 455)
`quote = data_msg.get("quote", {})` — se il campo "quote" esiste ma è un dict
vuoto `{}`, `quote.get("text", "")` ritorna `""` invece di `None`. Questo significa
che `quote_text` sarà una stringa vuota invece di `None`, e verrà passato a
`_add_message` come `quote_text=""` che creerà un widget quote vuoto.

### Bug #5 — `_identify_contact_for_envelope` logica duplicata per `sent` (linee 386-410)
Controlla `sent` due volte: una all'inizio (linee 386-394) e una alla fine
(linee 405-409). Il secondo controllo è ridondante.

### Bug #6 — `_poll_worker` nessun backoff/gestione errori (linea 995+)
Se la ricezione via RPC fallisce ripetutamente (es. daemon crash), il loop
continua a pollare ogni secondo senza backoff, riempiendo i log di errori.

---

## backend.py

### Bug #7 — `_is_daemon_running` crea nuova istanza RPC ogni volta (linea 80)
Crea un nuovo `SignalRPCClient()` invece di accettarne uno opzionale. Questo è
un problema perché se il daemon è stato appena avviato, il test potrebbe fallire
per un race condition.

### Bug #8 — `_find_signal_cli` non gestisce `PermissionError` (linee 58-66)
Se il file esiste ma non ha il permesso di esecuzione, solleva un'eccezione non
gestita. Inoltre, se la directory `bin/` non esiste, `iterdir()` solleva
`FileNotFoundError`.

---

## emoji_picker.py

### Bug #9 — `search_emoji` itera su `_EMOJI_TO_ALIAS.items()` ma la mappa ha solo 1 alias per emoji (linea 64)
La mappa `_EMOJI_TO_ALIAS` è popolata con l'ultimo alias incontrato per ogni
emoji. Se un emoji ha più alias, solo l'ultimo viene indicizzato. La ricerca
potrebbe perdere match.

### Bug #10 — `on_input_changed` nella ricerca emoji non usa `search_emoji()` (linee 342-369)
Invece di chiamare `search_emoji(query)` che è già definita, reimplementa la
ricerca in modo diverso, creando prima una lista di tutti gli emoji e poi
filtrando. Doppia implementazione = doppia manutenzione.

---

## ui_components.py

### Bug #11 — `ImageModalScreen._render_image` non gestisce output vuoto di catimg (linee 306-332)
Se `catimg` non produce output (es. file corrotto), `ansi_output` sarà vuoto e
`RichText.from_ansi("")` potrebbe causare comportamenti imprevisti.

### Bug #12 — `ImageModalScreen._render_image` non gestisce `PermissionError` su attachment (linea 289)
Se il file attachment non è leggibile, `catimg` fallirà con un errore non chiaro.
