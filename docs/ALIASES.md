# Alias shell della Web UI

Alias di shell (bash/zsh) per avviare la **Web UI** del Signal TUI Client e gestirne il ciclo di
vita: una sessione **foreground** interattiva, una sessione **background** in tmux (che esporta il
token Bearer nella shell) e un comando di **stop** pulito.

Il server web ascolta su `0.0.0.0:4242` (porta di default `4242`). Il **token Bearer** è salvato in
`config.json` nella chiave `web.token`; il server lo accetta anche dalla variabile d'ambiente
`SIGNAL_TUI_WEB_TOKEN`.

## 1. I tre alias

| Alias | Cosa fa | Quando usarlo |
|---|---|---|
| `web-signal-tui` | Avvia la TUI con la Web UI in **foreground** su `0.0.0.0:4242` | Sessione interattiva: vuoi vedere output e log della TUI direttamente nel terminale |
| `web-signal-tui-bg` | Avvia la TUI + Web UI in una **sessione tmux staccata** (background) ed **esporta il token** nella shell corrente come `SIGNAL_TUI_WEB_TOKEN` | Fast cycle: pronto subito per `curl` e per il login nella Web UI |
| `web-signal-tui-stop` | Chiude la sessione tmux in modo pulito e rimuove `/tmp/signal-tui.lock` | Arrestare la sessione background avviata con `web-signal-tui-bg` |

## 2. Fast cycle (background + token in shell)

`web-signal-tui-bg` avvia la TUI + Web UI in **background** (tmux) ed **esporta**
`SIGNAL_TUI_WEB_TOKEN` nella shell corrente. Il token letto da `config.json` viene anche stampato in
console, quindi si può:

- **incollare nel login della Web UI** — il comando stampa il token (es. `TUI bg avviata — token: …`);
- **usarlo subito con `curl`** contro l'API autenticata, senza recuperarlo a mano da `config.json`:

```bash
web-signal-tui-bg
# TUI bg avviata — token: <token>

curl -H "Authorization: Bearer $SIGNAL_TUI_WEB_TOKEN" http://127.0.0.1:4242/api/contacts
```

Gli endpoint REST vivono sotto `/api` e richiedono l'header `Authorization: Bearer <token>`;
`GET /api/contacts` è l'endpoint di esempio per verificare subito che tutto funzioni.

## 3. Blocco alias completo

```bash
# ─── Signal TUI Client: web reader + background via tmux ─────────────────
# Web su 0.0.0.0:4242. Token Bearer: config.json (web.token) o SIGNAL_TUI_WEB_TOKEN.
# web-signal-tui-bg esporta il token nella shell (fast cycle: curl + login Web UI).
SIGNAL_TUI_DIR="~/signal-tui-client"
alias web-signal-tui='( cd "$SIGNAL_TUI_DIR" && .venv/bin/python -m signal_tui --web --web-port 4242 --web-host 0.0.0.0 )'
alias web-signal-tui-bg='tmux new-session -d -s tui "cd $SIGNAL_TUI_DIR && .venv/bin/python -m signal_tui --web --web-port 4242 --web-host 0.0.0.0" && export SIGNAL_TUI_WEB_TOKEN="$(python3 -c "import json; print(json.load(open(\"$SIGNAL_TUI_DIR/config.json\"))[\"web\"][\"token\"])")" && echo "TUI bg avviata — token: $SIGNAL_TUI_WEB_TOKEN"'
alias web-signal-tui-stop='[ -f /tmp/signal-tui.lock ] && kill -INT "$(cat /tmp/signal-tui.lock)" 2>/dev/null; for i in $(seq 1 12); do [ ! -f /tmp/signal-tui.lock ] && break; sleep 0.5; done; tmux kill-session -t tui 2>/dev/null; sleep 0.5; [ -f /tmp/signal-tui.lock ] && rm -f /tmp/signal-tui.lock'
```

> **Nota:** `SIGNAL_TUI_DIR` va adattato alla propria directory di installazione
> (es. `~/signal-tui-client`). Lo script `install.sh` lo scrive automaticamente con il **path reale
> del progetto** (vedi [Auto-installazione](#5-auto-installazione)).

### Installazione manuale

1. Incolla il blocco sopra in `~/.bashrc` (bash) oppure in `~/.zshrc` (zsh).
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
| **fish** | `~/.config/fish/config.fish` | **Non compatibile**: fish non usa `alias nome='...'` né `export VAR=...`; richiederebbe `function`/`abbr` e `set -gx` |
| **dash / sh** (POSIX minimale) | — | **Non supportati**: l'auto-installazione (`install.sh`) è uno script bash e rileva solo bash/zsh; per le altre shell avvisa e salta |

## 5. Auto-installazione

`./install.sh --aliases` aggiunge il blocco degli alias al file rc della shell rilevata
(bash → `~/.bashrc`, zsh → `~/.zshrc`) **con il path reale del progetto**:

```bash
./install.sh --aliases
```

- **Idempotente**: il blocco è delimitato dai marcatori `# ── BEGIN signal-tui aliases ──` /
  `# ── END signal-tui aliases ──`; se è già presente viene **sostituito**, non duplicato.
- La shell è rilevata da `$SHELL`; se non è bash/zsh, lo script **avvisa e salta** (es. fish).
- Gli alias vengono installati anche al termine di un'installazione completa (`./install.sh`),
  ma solo se la shell è supportata.
