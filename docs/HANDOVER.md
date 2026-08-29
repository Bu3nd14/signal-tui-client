# Handover Signal TUI Client — locale ↔ server Hetzner

Script: `scripts/tui_handover.sh` (alias: `signal-tui-go-server`, `signal-tui-go-local`, `signal-tui-status`).

Il client usa account Signal/WhatsApp/Telegram **condivisi** tra due macchine (desktop locale e server). La sessione WAHA (WhatsApp) e il daemon `signal-cli` **non devono essere attivi su due macchine contemporaneamente**: gli script centralizzano il passaggio pulito.

## Comandi

| Comando | Cosa fa |
|---|---|
| `signal-tui-go-server` | Spegne TUI/daemon/WAHA sul **locale** → accende TUI+WAHA sul **server** |
| `signal-tui-go-local` | Spegne TUI/daemon/WAHA sul **server** → accende TUI+WAHA sul **locale** |
| `signal-tui-status` | Mostra lo stato (TUI/daemon/WAHA) su **entrambe** le macchine |

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
