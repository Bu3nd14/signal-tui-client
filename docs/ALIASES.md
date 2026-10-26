# Alias shell della Web UI

Alias di shell (bash/zsh/ash) per avviare la **Web UI** del Signal TUI Client e gestirne il ciclo di
vita: una sessione **foreground** interattiva, un server **background** headless (`launcher.py server
start` → `megachat serve`, senza TUI, che esporta il token Bearer nella shell) e comandi di **stop** e
**status**.

Il server web ascolta su `0.0.0.0:4242` (porta di default `4242`). Il **token Bearer** è salvato in
`config.json` nella chiave `web.token`; il server lo accetta anche dalla variabile d'ambiente
`SIGNAL_TUI_WEB_TOKEN`.

## 1. Gli alias

| Alias | Cosa fa | Quando usarlo |
|---|---|---|
| `web-signal-tui` | Avvia la TUI con la Web UI in **foreground** su `0.0.0.0:4242` | Sessione interattiva: vuoi vedere output e log della TUI direttamente nel terminale |
| `web-signal-tui-bg` | Scalda la cache GPG del segreto Gmail (prompt della passphrase solo se fredda), poi avvia il server headless delegando a `launcher.py server start --no-token` (che lancia `megachat serve` — core + Web UI, senza TUI — e **WAHA**) come **processo staccato** (background) ed **esporta il token** nella shell corrente come `SIGNAL_TUI_WEB_TOKEN` | Fast cycle: pronto subito per `curl` e per il login nella Web UI |
| `web-signal-tui-stop` | Ferma il server background con SIGINT pulito (chiude anche un'eventuale vecchia sessione tmux `tui`) e rimuove `/tmp/signal-tui.lock`; **WAHA non viene fermata** | Arrestare il server avviato con `web-signal-tui-bg` senza interrompere WhatsApp |
| `web-signal-tui-status` | Mostra lo stato di server, daemon signal-cli, WAHA e Web UI (`launcher.py server status`) | Verificare rapidamente cosa è attivo |

> Handover locale ↔ server (`signal-tui-go-server`, `signal-tui-go-local`, `signal-tui-status`): sposta questo stesso server headless tra le due macchine, vedi [HANDOVER.md](HANDOVER.md). Dopo lo spostamento, per l'uso interattivo lancia `python3 megachat.py` sulla macchina di destinazione (si collega al server in esecuzione; Detach lo lascia attivo, Quit lo ferma).

## 2. Fast cycle (background + token in shell)

`web-signal-tui-bg` avvia il server headless + Web UI in **background** delegando a
`launcher.py server start --no-token` (che avvia anche WAHA) ed **esporta**
`SIGNAL_TUI_WEB_TOKEN` nella shell corrente. `--no-token` evita che il launcher stampi il token
due volte: l'alias lo stampa già alla fine. Se il server è già vivo (pid del lock attivo) non viene riavviato: l'alias si limita a
esportare e stampare il token. `web-signal-tui-stop` ferma solo il server headless: **WAHA resta
attiva** di proposito.

Il token letto da `config.json` viene anche stampato in console, quindi si può:

- **incollare nel login della Web UI** — il comando stampa il token (es. `Server bg attivo — token: …`);
- **usarlo subito con `curl`** contro l'API autenticata, senza recuperarlo a mano da `config.json`:

```bash
web-signal-tui-bg
# Server bg attivo — token: <token>

curl -H "Authorization: Bearer $SIGNAL_TUI_WEB_TOKEN" http://127.0.0.1:4242/api/contacts
```

Gli endpoint REST vivono sotto `/api` e richiedono l'header `Authorization: Bearer <token>`;
`GET /api/contacts` è l'endpoint di esempio per verificare subito che tutto funzioni.

## 3. Blocco alias completo

```bash
# ─── Signal TUI Client: web reader + background via megachat serve ───────
# Web su 0.0.0.0:4242. Token Bearer: config.json (web.token) o SIGNAL_TUI_WEB_TOKEN.
# web-signal-tui-bg stampa ed esporta il token (fast cycle: curl + login Web UI).
# Gmail TPM: la shell/TUI deve avere il gruppo tss (dopo usermod serve re-login).
SIGNAL_TUI_DIR="$HOME/signal-tui-client"
alias web-signal-tui='( cd "$SIGNAL_TUI_DIR" && .venv/bin/python -m signal_tui --web --web-port 4242 --web-host 0.0.0.0 )'
_signal_tui_web_bg() {
    if [ -x "$SIGNAL_TUI_DIR/.venv/bin/python" ] && [ -f "$SIGNAL_TUI_DIR/scripts/gmail_warm_cache.py" ]; then
        "$SIGNAL_TUI_DIR/.venv/bin/python" "$SIGNAL_TUI_DIR/scripts/gmail_warm_cache.py" || true
    fi
    local pid="" lock="${MEGACHAT_LOCK_FILE:-/tmp/signal-tui.lock}"
    [ -f "$lock" ] && pid="$(cat "$lock")"
    if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
        "$SIGNAL_TUI_DIR/.venv/bin/python" "$SIGNAL_TUI_DIR/launcher.py" server start --no-token || return 1
    fi
    SIGNAL_TUI_WEB_TOKEN="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["web"]["token"])' "$SIGNAL_TUI_DIR/config.json")" || return 1
    export SIGNAL_TUI_WEB_TOKEN
    echo "Server bg attivo — token: $SIGNAL_TUI_WEB_TOKEN"
}
_signal_tui_web_stop() {
    # WAHA non viene fermata di proposito: resta attiva per i messaggi.
    local lock="${MEGACHAT_LOCK_FILE:-/tmp/signal-tui.lock}" pid=""
    [ -f "$lock" ] && pid="$(cat "$lock" 2>/dev/null)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        kill -INT "$pid" 2>/dev/null || true
        for i in $(seq 1 60); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.5
        done
        if kill -0 "$pid" 2>/dev/null; then
            echo "Server (pid $pid) ancora attivo dopo 30s: lock lasciato." >&2
            return 1
        fi
    fi
    tmux kill-session -t tui 2>/dev/null || true
    # Rimuove il lock solo se punta ancora al processo appena fermato (o è orfano).
    if [ -f "$lock" ] && [ "$(cat "$lock" 2>/dev/null)" = "$pid" ]; then
        rm -f "$lock"
    fi
    echo "Server bg fermato."
}
_signal_tui_web_status() { "$SIGNAL_TUI_DIR/.venv/bin/python" "$SIGNAL_TUI_DIR/launcher.py" server status; }
alias web-signal-tui-bg='_signal_tui_web_bg'
alias web-signal-tui-stop='_signal_tui_web_stop'
alias web-signal-tui-status='_signal_tui_web_status'
alias signal-tui-bg='_signal_tui_web_bg'
alias signal-tui-stop='_signal_tui_web_stop'
```

> **Nota:** `SIGNAL_TUI_DIR` va adattato alla propria directory di installazione
> (es. `~/signal-tui-client`). Lo script `install.sh` lo scrive automaticamente con il **path reale
> del progetto** (vedi [Auto-installazione](#5-auto-installazione)).

### Installazione manuale

1. Incolla il blocco sopra in `~/.bashrc` (bash), `~/.zshrc` (zsh) oppure `~/.ashrc` (ash — assicurati
   che `$ENV` punti lì).
2. Ricarica il file o riapri la shell:

```bash
source ~/.bashrc    # bash
# oppure
source ~/.zshrc     # zsh
```

## 4. Compatibilità shell

**Sarebbero diversi se la shell non fosse bash?** Sì: cambia il file rc da modificare e, per alcune
shell, il blocco non è proprio utilizzabile.

| Shell | File rc | Compatibilità |
|---|---|---|
| **bash** | `~/.bashrc` | Supportata nativamente; sintassi `alias` + `export` + `$(...)` + `&&` usata così com'è |
| **zsh** | `~/.zshrc` | **Compatibile**: alias, `export`, `$(...)` e `&&` sono costrutti standard (POSIX/ksh) supportati da zsh; cambia **solo** il file rc. zsh supporta gli stessi alias |
| **ash** (BusyBox, Alpine) | `~/.ashrc` | **Compatibile**: il blocco è POSIX (nessun `[[`, nessuna funzione bash-specifica), quindi funziona così com'è; **attenzione**: ash legge gli alias da `~/.ashrc` solo se la variabile `$ENV` punta lì (tipico default su Alpine via `/etc/profile`) — altrimenti vanno sourcati a mano |
| **fish** | `~/.config/fish/config.fish` | **Non compatibile**: fish non usa `alias nome='...'` né `export VAR=...`; richiederebbe `function`/`abbr` e `set -gx` |
| **dash / sh** (POSIX minimale) | — | **Non supportati**: l'auto-installazione (`install.sh`) è uno script bash e rileva solo bash/zsh/ash; per le altre shell avvisa e salta |

## 5. Auto-installazione

`./install.sh --aliases` aggiunge il blocco degli alias al file rc della shell rilevata
(bash → `~/.bashrc`, zsh → `~/.zshrc`, ash → `~/.ashrc`) **con il path reale del progetto**:

```bash
./install.sh --aliases
```

- **Idempotente**: il blocco è delimitato dai marcatori `# ── BEGIN signal-tui aliases ──` /
  `# ── END signal-tui aliases ──`; se è già presente viene **sostituito**, non duplicato.
- La shell è rilevata da `$SHELL`; se non è bash/zsh/ash, lo script **avvisa e salta** (es. fish).
- Per ash lo script avvisa anche se `$ENV` non punta a `~/.ashrc`, dato che è quella variabile
  (non il nome del file) a determinare cosa ash sorge all'avvio di una shell interattiva.
- Gli alias vengono installati anche al termine di un'installazione completa (`./install.sh`),
  ma solo se la shell è supportata.
