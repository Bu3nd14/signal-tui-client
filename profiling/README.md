# Profiling Tools for Signal TUI Client

Questa cartella contiene strumenti per profilare l'applicazione Signal TUI Client
in termini di **CPU**, **RAM** e **I/O**.

## 📦 Installazione dipendenze

```bash
cd /home/rob/signal-tui-client
source .venv/bin/activate
pip install -r profiling/requirements.txt
```

Per strace (I/O profiling):
```bash
sudo apt install strace
```

> **⚠️ IMPORTANTE:** Attiva sempre il `.venv` prima di eseguire gli script:
> ```bash
> source .venv/bin/activate
> ```
> Gli script usano automaticamente il Python del `.venv` per lanciare l'app,
> quindi funzionano anche senza attivarlo. Ma se vuoi eseguire gli script
> di analisi (`analyze_*.py`), devi avere il `.venv` attivo.

## 🚀 Ordine di esecuzione consigliato

### 1. Monitoraggio risorse di base (CPU, RAM, I/O nel tempo)

```bash
# Monitora per 2 minuti, campionando ogni 2 secondi
python profiling/monitor_resources.py --duration 120

# Analizza i risultati
python profiling/analyze_resources.py
```

**Output:**
- `profiling/output/resources.csv` — dati grezzi (timestamp, CPU%, RSS, VMS, I/O rate)
- `profiling/output/resources_report.txt` — report con min/max/avg per ogni metrica

### 2. Flamegraph CPU (py-spy) — **strumento principale per CPU**

> **Perché py-spy e non cProfile?** L'app usa `run_worker(thread=True)` per
> tutto il lavoro reale (polling, startup, load messages, send). cProfile
> profila solo il thread principale, quindi non vede il lavoro dei thread.
> py-spy campiona **tutti i thread** e produce un flamegraph interattivo.

```bash
# Campiona per 2 minuti
./profiling/run_pyspy.sh 120
```

**Output:**
- `profiling/output/flamegraph.svg` — flamegraph interattivo (apri nel browser)

> **Nota su ptrace_scope:** su alcuni sistemi Linux (`kernel.yama.ptrace_scope=1`),
> py-spy potrebbe non riuscire ad attacharsi al processo. In quel caso:
> ```bash
> sudo sysctl kernel.yama.ptrace_scope=0
> ```
> oppure esegui lo script con `sudo`.

### 3. Profiling RAM (tracemalloc)

```bash
# Profila per 2 minuti
python profiling/profile_memory.py --duration 120

# Con più dettaglio (top 30 allocazioni)
python profiling/profile_memory.py --duration 300 --top 30
```

**Output:**
- `profiling/output/memory_report.txt` — Top allocazioni per dimensione e per conteggio,
  crescita di memoria (rilevamento leak), allocazioni specifiche del progetto

> **Nota:** tracemalloc traccia solo le allocazioni Python (heap). La memoria
> nativa (JVM di signal-cli, buffer di Textual, immagini catimg) non viene tracciata.

### 4. Profiling I/O (strace)

```bash
# Traccia per 2 minuti
./profiling/run_strace.sh 120
```

**Output:**
- `profiling/output/strace.log` — trace completo delle syscall
- `profiling/output/strace_summary.txt` — riepilogo: file più aperti, conteggio accessi al file cache

## 📊 Come interpretare i risultati

### CPU (flamegraph)
- **Larghezza delle barre**: proporzionale al tempo CPU speso in quella funzione.
- **Stack verticale**: mostra la catena di chiamate (funzione → chiamante → chiamante).
- **Cerca barre larghe** in `_poll_worker`, `_process_envelope`, `_save_cache`,
  `_update_unread_badges`, `mount`, `refresh_layout`.

### RAM
- **Top allocazioni per dimensione**: dove viene allocata più memoria.
  Es: `json.load` del file cache, widget Textual montati.
- **Crescita di memoria**: confronta il baseline (prima di `app.run()`) con il
  finale (dopo `app.run()`). Se la crescita supera 50 MB, c'è un possibile leak.

### I/O
- **File più aperti**: `messages.json` dovrebbe essere il file più accessato.
  Se viene letto/scritto decine di volte al minuto, è un problema.
- **Syscall summary**: mostra il tempo totale speso in read/write.

## 🔍 Punti critici noti

### ✅ Già ottimizzati (branch `perf/optimize-cache-io`)

1. **Cache riscritta interamente** — ogni messaggio causava `_save_cache` +
   `_prune_cache` + `_load_cache` (3 accessi disco completi).
   **Fix**: debounce a 5 messaggi (`_maybe_flush_cache`), flush forzato su
   azione utente (`_flush_cache`).
2. **Ricostruzione lista contatti** — `_update_unread_badges` ricostruiva
   l'intera lista per ogni messaggio non letto (O(N×M)).
   **Fix**: aggiornamento incrementale per singolo contatto (O(M)).

### ⚠️ Da verificare con i dati

3. **Polling aggressivo**: `_poll_worker` fa una chiamata HTTP ogni secondo.
   Verifica il tempo CPU speso in `receive()` e `urlopen`.
4. **Scansione lineare contatti**: `_identify_contact_for_envelope` fa O(n) per envelope.
   Verifica il tempo cumulativo.
5. **JSON con indent**: `json.dump(..., indent=2)` produce file più grandi e
   serializzazione più lenta. Verifica il tempo in `json.dump`.
6. **Timer Textual**: il flamegraph mostra ~53% del tempo nel timer di Textual.
   Verifica se il polling a 1s causa refresh eccessivi del layout.

## 📈 Risultati ottenuti (dal profiling py-spy)

Il flamegraph generato con py-spy ha rivelato:

| Area | % tempo CPU | Note |
|------|------------|------|
| Timer Textual | ~53% | Ciclo di eventi, refresh periodici |
| Refresh layout | ~22% | Ricostruzione layout dopo ogni messaggio |
| Polling | ~5.8% | `_poll_worker` → `receive` → `_process_envelope` |
| Immagini | ~5.3% | Rendering immagini via catimg |

**Ottimizzazioni applicate** (commit `26e29e6` e `3965636` sul branch `perf/optimize-cache-io`):
- Debounce del salvataggio cache (da 3-4 accessi disco/messaggio a 1 ogni 5)
- Aggiornamento incrementale degli unread badges (da O(N×M) a O(M))

## 🗑️ Pulizia output

```bash
rm -rf profiling/output/*
```

## 📝 Note

- Gli script **non modificano il codice dell'app**. Sono wrapper esterni.
- Gli script usano automaticamente il Python del `.venv` per lanciare l'app,
  quindi funzionano anche se eseguiti con `python3` di sistema.
- Gli script terminano l'app con **SIGINT** (Ctrl+C) per permettere a Textual
  di uscire pulitamente e ripristinare il terminale. Non usare SIGTERM.
- Durante la profilazione, **usa l'app normalmente** (invia/ricevi messaggi,
  cambia contatti, apri chat) per ottenere dati realistici.
- Per risultati più accurati, esegui ogni profiler per almeno 2-3 minuti.
