# Checklist manuale — Immagini native su kitty

Validazione operativa della feature «immagini native inline (kitty graphics
protocol) + fallback catimg», da eseguire **su un terminale kitty reale**.
Durata stimata: **~10 minuti**.  Il rendering nativo non è verificabile in
headless/CI (`post_display_hook` non gira senza terminale): questa checklist è
la controparte manuale dei test automatici.

> Riferimenti: design completo in `documentation/design/DESIGN_NATIVE_IMAGES.md`
> (repo principale `signal-tui-client`); panoramica utente nel README, sezione
> «Immagini native inline».

---

## Prerequisiti

- [ ] **kitty reale ≥ 0.20** come terminale di lancio (sviluppo/test su 0.48) — verifica con `kitty --version`
- [ ] **Non dentro tmux/GNU screen**: `echo $TMUX` vuoto e `TERM` che non inizia per `screen` (dentro tmux la feature degrada sempre a catimg)
- [ ] **ssh va bene**: la checklist è valida anche su kitty via ssh
- [ ] **Account collegato** e raggiungibile (almeno Signal, daemon attivo)
- [ ] **2-3 chat con immagini**, di cui:
  - almeno una con immagini **già in cache locale** (aperte in passato);
  - almeno una con immagine **larga/panorama** (per il clipping orizzontale);
  - un mittente disponibile per inviarti immagini **nuove** durante il test.
- [ ] *(opzionale)* **Ghostty/iTerm2** o altro terminale non-kitty, per le voci di fallback — oppure basta `IMAGE_PROTOCOL=catimg`

## Preparazione (~1 minuto)

1. Nel terminale kitty: `echo $TERM` → deve stampare `xterm-kitty`.
2. Avvia il client dal worktree: alias **`signal-dev`**, oppure
   `.venv/bin/python signal_tui.py --debug` (se usi uno script wrapper tipo
   `./run.sh`, il procedimento non cambia).  Con `--debug` il log dettagliato
   va su `/tmp/signal-tui.log`.
3. **Verifica detection**:
   - comportamento atteso all'avvio: aprendo una chat con immagini compaiono
     **miniature native nitide** (non i placeholder `[🖼️ Image: …]`);
   - se le miniature non compaiono, riprova con override esplicito
     `IMAGE_PROTOCOL=kitty signal-dev`: se così funzionano, il problema è la
     detection; se restano artefatti/caratteri strani, il terminale non era
     kitty (⚠️ il forzaggio su un terminale non-kitty produce output corrotto:
     usalo solo come test diagnostico);
   - nel log cerca eventuali righe DEBUG di degrade («Image support detection
     failed…», «Cell-size … failed») — indicano fallback a catimg.
4. Ricorda: è consentita **una sola istanza** (lock `/tmp/signal-tui.lock`);
   se un'altra copia è attiva, chiudila prima di iniziare.

## Checklist

Compila le colonne ✓ / ✗ / note per ogni voce.  Numerazione **V1-V17** da usare
nelle segnalazioni.

### A · Rendering base

| # | Verifica | Passi | Risultato atteso | ✓/✗ | Note |
|---|---|---|---|---|---|
| V1 | Miniature all'avvio (chat da cache) | Apri una chat con immagini già in cache | Dopo un breve istante (risoluzione path in background) le immagini mostrano miniature ad alta risoluzione, non più il placeholder `[🖼️ Image: …]` | ☐ | |
| V2 | Miniature su messaggi nuovi | Fatti arrivare (o invia) una nuova immagine nella chat aperta | Appena scaricato il media appare la miniatura; il placeholder testo resta visibile sotto finché non è pronta (loading state), poi viene coperto | ☐ | |
| V3 | Clipping immagine larga/panorama | Apri la chat con il panorama e osserva la miniatura | L'immagine larga è limitata in larghezza (~60 colonne) e clippata ai bordi/padding della chat, senza sforare né coprire il bordo del pannello | ☐ | |

### B · Interazione (scroll, resize, zoom)

| # | Verifica | Passi | Risultato atteso | ✓/✗ | Note |
|---|---|---|---|---|---|
| V4 | Scroll veloce e lento | Scorri rapidamente su/giù con rotellina/PgUp-PgDn, poi ripeti lentamente | Le miniature seguono il testo **senza sfasamento** («due velocità»); niente immagini lasciate a metà schermo, niente flicker da ritrasmissione | ☐ | |
| V5 | Resize finestra | Allarga → stringe molto → ri-allarga la finestra | Entro pochi decimi di secondo le miniature si ri-allineano al nuovo layout; nessuna immagine fuori posto o sovrapposta al testo | ☐ | |
| V6 | Font-zoom kitty (**P1**) | Premi `Ctrl+Shift+=` (+zoom) poi `Ctrl+Shift+-` (-zoom); reset con `Ctrl+Shift+Backspace`; ripeti un paio di volte a ogni scala | L'altezza dei widget miniatura si ricalcola al cambio pixel/cella: allineamento perfetto col testo, niente sovrapposizioni o buchi vuoti | ☐ | |

### C · Robustezza input e layout

| # | Verifica | Passi | Risultato atteso | ✓/✗ | Note |
|---|---|---|---|---|---|
| V7 | Messaggi in arrivo mentre digiti (**C1**) | Col cursore nel box messaggio e qualche carattere già digitato, fai arrivare 2-3 immagini | Nessun testo spazzatura nell'input (le risposte `OK` di kitty sono silenziate, `q=2`); quello che stavi scrivendo resta intatto | ☐ | |
| V8 | Allineamento destro/sinistro (**P4**) | Apri una chat con immagini sia tue (`msg-right`) sia ricevute (`msg-left`) | Ogni miniatura è allineata alla colonna del proprio messaggio, dentro bordi/padding; nessuna immagine centrata «a caso» | ☐ | |

### D · Modal e screen stack

| # | Verifica | Passi | Risultato atteso | ✓/✗ | Note |
|---|---|---|---|---|---|
| V9 | Modal hi-res | Click (o `Enter`) su una miniatura | Si apre il modal fullscreen con l'immagine ad alta risoluzione centrata (cap 1600 px sul lato lungo); header/footer non coperti | ☐ | |
| V10 | Resize dentro il modal | Con il modal aperto ridimensiona la finestra | L'immagine si riposiziona/ri-scala centrata nel nuovo spazio, senza artefatti residui | ☐ | |
| V11 | Chiusura modal | Chiudi con `Esc`, poi riapri e chiudi con `q`, infine prova il click fuori immagine | Tutte e tre le chiusure funzionano; tornati in chat le miniature sono esattamente dove erano | ☐ | |
| V12 | Picker sopra le miniature (**C5**) | Con miniature visibili apri l'emoji picker (`Ctrl+E`) e poi il contact search (`Ctrl+S`); chiudi con `Esc` | Nessuna immagine visibile sopra/dietro i picker (gate sullo screen-stack); alla chiusura i placement della chat vengono ripristinati senza sfasamenti | ☐ | |

### E · Ciclo di vita

| # | Verifica | Passi | Risultato atteso | ✓/✗ | Note |
|---|---|---|---|---|---|
| V13 | Cambio chat | Da una chat con miniature passa a 2-3 altre chat (anche senza immagini) e torna indietro | Nessuna miniatura «orfana» della chat precedente rimasta a schermo; le miniature della chat riusata si ripiazzano correttamente | ☐ | |
| V14 | Uscita pulita | Esci con `Ctrl+C` (prova anche `Ctrl+Q` in una seconda sessione) | Ritorno al prompt pulito: nessun residuo di immagine sul terminale (cleanup globale `d=A` anche su SIGINT) | ☐ | |

### F · Fallback e modalità forzate

| # | Verifica | Passi | Risultato atteso | ✓/✗ | Note |
|---|---|---|---|---|---|
| V15 | Fallback catimg (altri terminali) | Da Ghostty/iTerm2/xterm (oppure `IMAGE_PROTOCOL=catimg` su kitty): apri una chat con immagini e un'immagine a fullscreen | Comportamento **identico a prima della feature**: placeholder `[🖼️ Image: nome — Click Enter to View]` inline + modal renderizzato con `catimg` | ☐ | |
| V16 | tmux / GNU screen | Dentro tmux (anche se il terminale sottostante è kitty) apri una chat con immagini | Solo catimg/placeholder, mai immagini native; nessun output corrotto | ☐ | |
| V17 | `IMAGE_PROTOCOL=off` | `IMAGE_PROTOCOL=off signal-dev`, apri una chat e clicca un'immagine | Nessun modal e nessuna miniatura nativa; il click mostra lo stato `🖼️ Image rendering is disabled`; placeholder invariato | ☐ | |

---

## Problemi noti / da segnalare

Mappatura degli id della review architetto citati nel codice e nel design
(`DESIGN_NATIVE_IMAGES.md`), con la voce di checklist che li copre:

| Id | Tema | Dove | Coperto da |
|---|---|---|---|
| **P1** | Cambio font-size di kitty → ricalcolo altezze widget nativi | `tui/app.py` (resize handler), `tests/test_kitty_renderer.py` | V6 |
| **P2** | Cell size misurato **prima** di `app.run()`; la query CSI `16 t` non gira mai dentro l'app (races sullo stdin) | `signal_tui.py`, `tui/images/cellsize.py` | V1/V6 (fallimenti appaiono nel log) |
| **P3** | Gate screen-stack: allo switch modal/picker vengono cancellati **solo** i placement della chat (`d=i` per-id, dati mantenuti) — mai quelli del modal | `tui/app.py` (`_native_sync_tick`) | V12 |
| **P4** | Allineamento miniature su messaggi `msg-right` vs `msg-left` | `tui/css.py`, `tui/chat_view.py` | V8 |
| **P5–P7** | Punti restanti della review architetto (vedi review di `DESIGN_NATIVE_IMAGES.md` nel repo principale) | — | usa gli id nelle segnalazioni |
| **B1** | Clipping orizzontale: propagazione `x_src`/`w_src` (source rectangle) | `tui/images/kitty_renderer.py` | V3/V5 |
| **C1** | `q=2` su tutti i comandi kitty: risposte `OK` sopprimate, niente tasti spuri nell'input | `tui/images/kitty_renderer.py`, design §3.1 | V7 |
| **C5** | Immagini sopra i picker/modal quando lo screen-stack cambia | `tui/app.py` | V12 |

### Come segnalare un problema

Apri una segnalazione includendo:

1. **id voce checklist** (V1-V17) e, se noto, **id review** (P1-P7 / B1 / C1-C5);
2. ambiente: `kitty --version`, `echo $TERM`, con/senza ssh, dentro/fuori tmux;
3. modalità effettiva: default (`auto`) o override `IMAGE_PROTOCOL` usato;
4. log `/tmp/signal-tui.log` dell'avvio in questione (richiede `--debug`,
   incluso di serie con `signal-dev`);
5. sequenza minima di passi per riprodurre il sintomo.
