# Handover Signal TUI Client — locale ↔ server Hetzner

Script: `scripts/tui_handover.sh` (alias: `signal-tui-go-server`, `signal-tui-go-local`, `signal-tui-status`).

Ciò che si sposta è il **server headless** (`megachat serve`: core + Web UI su `0.0.0.0:4242` + socket Unix), avviato come processo **staccato** (non più la TUI dentro tmux). Il processo che tiene il lock `/tmp/signal-tui.lock` è il server: "attivo" significa che quel pid è vivo.

Il client usa account Signal/WhatsApp/Telegram **condivisi** tra due macchine (desktop locale e server). La sessione WAHA (WhatsApp) e il daemon `signal-cli` **non devono essere attivi su due macchine contemporaneamente**: gli script centralizzano il passaggio pulito.

## Comandi

| Comando | Cosa fa |
|---|---|
| `signal-tui-go-server` | Spegne server/daemon/WAHA sul **locale** → accende WAHA+server sul **server** |
| `signal-tui-go-local` | Spegne server/daemon/WAHA sul **server** → accende WAHA+server sul **locale** |
| `signal-tui-status` | Mostra lo stato (server/daemon/WAHA) su **entrambe** le macchine |

Lo spegnimento invia SIGINT al pid del lock e **attende che il processo esca** (fino a 30 s); una vecchia sessione tmux `tui` (installazioni precedenti) viene ancora chiusa, ma non ne viene più avviata nessuna. Se il server non si ferma, l'handover si **interrompe** senza toccare il lock (mai due server attivi); lo stesso se il server remoto non si ferma o `ssh` fallisce durante `to-local`.

Equivalente Python: `python3 launcher.py handover {to-server|to-local|status}`. Il recupero d'emergenza sul server (`scripts/start_on_server.sh {start|stop|status}`, `launcher.py server ...`) avvia e ferma lo stesso `megachat serve`.

## Uso interattivo dopo l'handover

Sulla macchina di destinazione non c'è più `tmux attach -t tui`: lancia la TUI come client del server già in esecuzione:

```bash
cd /root/signal-tui-client   # sul server (sul locale: la directory del progetto)
.venv/bin/python megachat.py # si collega al server via socket Unix
```

**Detach** (`d`, Ctrl+C) chiude solo la TUI e lascia il server attivo (Web UI, sync); **Quit** (`q`, Ctrl+Q) esegue `server.shutdown` e lo ferma. Per spostare l'istanza usa sempre `signal-tui-go-*`, non Quit. Log del server: `/tmp/signal-tui.log` e `/tmp/megachat-serve.log`.

Gli alias sono installati in `.bashrc` sia sul locale sia sul server. Lo script va lanciato **dalla macchina locale** (dove sta la chiave SSH privata).

## Autenticazione — solo chiave SSH, mai password

L'handover usa esclusivamente la **chiave SSH ed25519**:

- **Chiave privata**: solo sulla macchina locale (`~/.ssh/id_ed25519`)
- **Chiave pubblica**: in `/root/.ssh/authorized_keys` sul server

Installazione (una tantum, dal locale):

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N ""
ssh-copy-id root@<IP_SERVER>
```

## Dopo snapshot / recreate del server (IP che cambia)

1. **Aggiorna l'IP** in `~/.config/signal-tui-handover.conf` sul locale:

   ```bash
   echo "HZ_HOST=<NUOVO_IP>" > ~/.config/signal-tui-handover.conf
   ```

   (In alternativa: `export HZ_HOST=<NUOVO_IP>` prima di lanciare lo script.)

2. **known_hosts** — se il server è stato ricreato:
   - Se l'**IP è nuovo** → nessun problema (lo script usa `StrictHostKeyChecking=accept-new`).
   - Se l'**IP è lo stesso** ma l'host key è cambiata (server ricreato) → SSH rifiuta la connessione con
     `REMOTE HOST IDENTIFICATION HAS CHANGED`. Rimuovi la vecchia riga prima di riconnetterti:

     ```bash
     ssh-keygen -R <IP_SERVER>
     ```

3. **Chiave pubblica** — se hai creato lo snapshot **prima** di installare la chiave, reinstallala:

   ```bash
   ssh-copy-id root@<NUOVO_IP>
   ```

   Se lo snapshot è successivo all'installazione, `authorized_keys` è già dentro lo snapshot e non serve rifare nulla.

4. Verifica con `signal-tui-status` prima di un handover.

## Nota sulla sincronizzazione della cache

I dati del client (`~/.local/share/signal-tui-client`, `~/.local/share/signal-cli`, `whatsapp-data/`) risiedono
separatamente sulle due macchine. Se vuoi che il server abbia i dati più recenti dopo un periodo di uso locale,
ri-sincronizzali via rsync **a client spento** (vedi trasferimento iniziale). I messaggi ricevuti mentre una
macchina è spenta vengono comunque recuperati all'avvio (Signal `on-connection`, WhatsApp `resync_history`,
Telegram `fetch_recent_history`).
