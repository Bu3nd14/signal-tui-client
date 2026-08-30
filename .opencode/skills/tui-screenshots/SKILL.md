---
name: tui-screenshots
description: Cattura screenshot grafici di app TUI (Textual/kitty) in headless. Usa quando serve produrre screenshot del client Signal TUI (signal_tui.py) per README/bug report, sia della vista normale (lista contatti, chat, address book, modal) sia delle immagini native hi-res kitty graphics protocol. Trigger: screenshot TUI, kitty screenshot, Xvfb screenshot, cattura schermata terminale, native images kitty, screenshot README.
---

# Screenshot di app TUI (Textual) in headless — kitty + Xvfb

Workflow collaudato per catturare screenshot **grafici reali** di app Textual (in
particolare `signal-tui-client/signal_tui.py`) in ambiente headless, inclusi il
**rendering nativo kitty graphics protocol** (miniature hi-res nelle chat).

## Perché questo workflow

Un'app Textual gira in un terminale. Per fotografarla servono:
1. Un **display X virtuale** (Xvfb) dove il terminale possa disegnare.
2. Un **window manager minimale** (openbox) — senza WM la finestra kitty non
   riceve gli expose e il contenuto NON viene ripresentato nel framebuffer.
3. **kitty** come terminale (necessario per il rendering TGP hi-res).
4. **xdotool** per attivare/focalizzare la finestra prima della cattura.
5. **ImageMagick `import`** per catturare la finestra specifica.

**Strumenti richiesti** (installare con sudo se mancano):
`xvfb Xvfb`, `openbox`, `xdotool`, `imagemagick` (fornisce `import`/`convert`),
`kitty` (≥0.20; 0.32 testata OK).

## Lezioni chiave (imparate sul campo)

- **Il testo Textual SI presenta** su Xvfb+openbox+kitty. Se lo screenshot sembra
  "vuoto" controlla con il conteggio dei **pixel chiari** (sotto), NON solo i
  colori: il tema scuro (colore di sfondo ~`(30,30,30)`) domina il conteggio
  colori ma il contenuto c'è.
- Cattura con `import -window <WID>` (la finestra kitty specifica), **non**
  `-window root` (che su Xvfb non compone le finestre figlie in modo affidabile).
- **NON serve** un compositor (xcompmgr/picom): openbox basta.
- **NON è** colpa della sync output di Textual (CSI 2026): lasciala attiva.
- kitty 0.32 e 0.48 si comportano allo stesso modo per gli screenshot.
- Il client Signal TUI è **single-instance**: prima di lanciarlo dentro kitty,
  ferma eventuali altre istanze (tmux web, ecc.) e rimuovi `/tmp/signal-tui.lock`.
- Per l'attesa del boot usa il **log pulito**: `rm -f /tmp/signal-tui.log` prima
  di lanciare, poi fai poll su `grep -q "Backend signal ready"`.

## Navigazione TUI (sequenza tasti corretta)

Per aprire una chat specifica con immagini native:

1. `Ctrl+S` → apre il contact search (picker).
2. Digita il nome/numero (es. `Roberto Work`).
3. `Tab` → sposta il focus dall'input alla lista risultati.
4. `Down` (1 volta) → evidenzia il primo risultato. **Obbligatorio**: senza Down
   la ListView non ha indice e l'Enter non seleziona.
5. `Enter` → seleziona il contatto. Se il contatto è multi-backend (Signal +
   WhatsApp + Telegram) si apre il sottodialogo **"Scegli backend"** con il più
   recente pre-selezionato.
6. `Enter` di nuovo → apre la chat di quel backend (per Roberto Work: Signal).
7. Con il focus nella chat, `Enter` su una miniatura apre il **modal hi-res**.

I tasti vanno inviati via `kitty @ --to unix:$SOCK send-key --match id:1 ...`
(il socket è creato con `--listen-on unix:$SOCK` al lancio di kitty).

## Script completo

```bash
#!/bin/bash
# shot_tui.sh <display> <out-prefix> [nome-contatto]
# Esempio: shot_tui.sh 106 /tmp/shot "Roberto Work"
set -u
DISP="$1"; PREFIX="$2"; TARGET="${3:-Roberto Work}"
PROJ="${SIGNAL_TUI_DIR:-/home/rob/signal-tui-client}"
export DISPLAY=":$DISP"
SOCK=/tmp/k${DISP}.sock
CLIENT_LOG=/tmp/signal-tui.log

# 0. cleanup processi + lock single-instance
pkill -9 -f "kitt[y].*k${DISP}"; pkill -9 -f "Xvfb :${DISP}"
pkill -9 -f "openbo[x].*:${DISP}"; pkill -9 -f "signal_tui"
sleep 1; rm -f /tmp/signal-tui.lock "$SOCK" "$CLIENT_LOG" "/tmp/.X11-unix/X${DISP}"

# 1. Xvfb + openbox
Xvfb ":$DISP" -screen 0 1280x800x24 -ac >/tmp/xvfb-${DISP}.log 2>&1 & XPID=$!
sleep 2
openbox >/tmp/ob-${DISP}.log 2>&1 & OPID=$!
sleep 2

# 2. kitty con il client TUI
kitty --listen-on "unix:$SOCK" -o allow_remote_control=yes \
      -o remember_window_size=no -o initial_window_width=1200 -o initial_window_height=750 \
      -o font_size=13 --title "shot-${DISP}" \
      -- sh -c "cd $PROJ && exec .venv/bin/python signal_tui.py" >/tmp/kitty-${DISP}.log 2>&1 & KPID=$!

# 3. attendi socket kitty + boot client (log pulito)
for i in $(seq 1 30); do [ -S "$SOCK" ] && { echo "socket ${i}s"; break; }; sleep 1; done
for i in $(seq 1 90); do
  grep -q "Backend signal ready" "$CLIENT_LOG" 2>/dev/null && { echo "boot ${i}s"; break; }
  sleep 1
done
sleep 8  # respiro per il render dei contatti

# 4. focus finestra kitty
WID=$(xdotool search --name "shot-${DISP}" 2>/dev/null | head -1)
echo "WID=$WID"
xdotool windowactivate "$WID" 2>/dev/null; xdotool windowfocus "$WID" 2>/dev/null
sleep 2

KCTL="kitty @ --to unix:$SOCK"
shot() { import -window "$WID" "$1" 2>/dev/null; }

# 5. screenshot TUI iniziale
shot "${PREFIX}-tui.png"

# 6. naviga alla chat target
$KCTL send-key --match id:1 ctrl+s 2>/dev/null; sleep 3
$KCTL send-text --match id:1 "$TARGET" 2>/dev/null; sleep 3
$KCTL send-key --match id:1 tab 2>/dev/null; sleep 2
$KCTL send-key --match id:1 down 2>/dev/null; sleep 1
shot "${PREFIX}-addressbook.png"
$KCTL send-key --match id:1 enter 2>/dev/null; sleep 3   # pick contact (o Scegli backend)
$KCTL send-key --match id:1 enter 2>/dev/null; sleep 12  # conferma backend / apre chat
shot "${PREFIX}-native-images.png"
$KCTL send-key --match id:1 enter 2>/dev/null; sleep 6   # modal hi-res (su miniatura)
shot "${PREFIX}-modal.png"

echo "=== get-text (diagnostica) ==="
$KCTL get-text --match id:1 2>/dev/null | head -8
echo "=== FILE ==="
ls -la ${PREFIX}-*.png 2>/dev/null
kill -9 $XPID $OPID $KPID 2>/dev/null
echo DONE
```

## Verifica qualità screenshot (niente occhio umano richiesto)

L'orchestratore non può "vedere" le immagini: valuta in modo programmatico.

```python
from PIL import Image
from collections import Counter
im = Image.open(path).convert("RGB")
w, h = im.size
light = sum(
    1 for y in range(0, h, 2) for x in range(0, w, 2)
    if sum(im.getpixel((x, y))) > 300
)
print("colors=", len(Counter(im.getdata())), "light_px(sampled)=", light)
```

- `light_px` **> 20.000** su ~1200x750 → contenuto ricco (testo + UI) ✅
- `light_px` **> 5.000** → almeno picker/modal visibile.
- `colors` > 10.000 → probabile presenza di **immagini native hi-res** nella chat.
- Se `light_px` è basso (~1.000) ma il tema è scuro, NON concludere "vuoto":
  ricontrolla con una soglia più bassa (es. >100) e con `get-text`.

## Invio degli screenshot via Signal (se richiesto dall'utente)

1. Ferma l'istanza kitty/TUI (rilascia il lock single-instance):
   `rm -f /tmp/signal-tui.lock` (dopo aver killato i processi).
2. Riavvia la web UI in tmux:
   `tmux new-session -d -s tui "cd $PROJ && .venv/bin/python -m signal_tui --web --web-port 4242 --web-host 0.0.0.0"`
3. Attendi `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:4242/api/contacts
   -H "Authorization: Bearer <token>"` → `200`.
4. Invio multipart (l'header `Origin` è OBBLIGATORIO, senza → 404):
   ```bash
   curl -s -X POST http://127.0.0.1:4242/api/send \
     -H "Authorization: Bearer <token>" \
     -H "Origin: http://127.0.0.1:4242" \
     -F "protocol=signal" -F "contact_id=<numero-destinatario>" \
     -F "text=descrizione" -F "file=@/tmp/shot.png;type=image/png"
   ```
   Risposta attesa: `{"ok":true}`.

## Note

- Se un contatto è multi-backend, il sottodialogo "Scegli backend" si apre solo
  dopo il primo Enter; un secondo Enter conferma il backend pre-selezionato
  (il più recente), che di solito è quello giusto.
- Il path del progetto è configurabile con `SIGNAL_TUI_DIR`.
- Per il picker come screenshot standalone (`-addressbook.png`) cattura subito
  dopo `Tab`/`Down`, prima dell'Enter.
