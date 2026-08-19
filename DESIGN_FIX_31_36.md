# Design di fix — BUG #31 (allineamento foto da cache) + BUG #36 (caption foto)

> Redatto dall'architetto (input), trascritto su file dall'orchestratore.
> Riferimenti: `BUGS.md` #31 (righe 162-181) e #36 (righe 184-214).

---

## 1. Analisi di impatto

### 1.1 Percorsi di rendering delle immagini (oggi)

Esistono **due soli punti** in cui nasce un `ImageWidget`:

| Percorso | Funzione | File:righe | Quando viene usato |
|---|---|---|---|
| **Live** | `_add_message` → `_render_image_in_chat` | `tui/chat_view.py:117-125`, `203-250` | Evento live (`_handle_message_event` in `tui/events.py:142-154`), `_refresh_chat` (`chat_view.py:719-730`), `_load_all_messages` (`chat_view.py:639-650`) |
| **Cache** | `_build_message_widgets` | `tui/chat_view.py:567-577` | `_mount_window` dentro `_render_chat_window` (`chat_view.py:456`), cioè apertura chat (fase 1), re-render dopo `fetch_history` WhatsApp (fase 2, `chat_view.py:373`), e quindi **storico** per tutti i protocolli |

### 1.2 BUG #31 — stato attuale

- **Live**: `_render_image_in_chat` assegna `widget.classes = "msg-right" if is_mine else "msg-left"` in entrambi i rami (riga 230 senza `attachment_id`, riga 240 con). `.msg-right` in `tui/css.py:98-102` applica `text-align: right` + `color: $success`.
- **Cache**: `_build_message_widgets` (righe 571-577) crea `ImageWidget(attachment_path=None, attachment_id=..., fallback_text=...)` e lo appende **senza assegnare classi**. Uno `Static` senza classi resta allineato a sinistra con colore `$text` → foto inviata indistinguibile da una ricevuta.
- Impatto: tutti i protocolli, ma solo nel ramo cache/storico. `_load_all_messages` e `_refresh_chat` non sono affetti (passano da `_add_message`).

### 1.3 BUG #36 — dove vive la caption oggi, per protocollo

**Signal** (`backends/signal.py`):
- `_classify_attachments` (righe 467-490): legge `att["caption"]` (riga 476); per immagini `attachment_info = caption or f"Image: {fname}" or "🖼️ Image"` (riga 479).
- `_build_msg_dicts` (righe 501-537): il **primo** attachment riceve `text = dataMessage.message` (il body, che per Signal è la caption canonica, righe 513-514); gli altri ricevono testo sintetico `f"{label}: {att_id}"` (righe 521-524).
- Quindi la caption Signal è in **`text`** (body, primo attachment) oppure in **`attachment_info`** (caption per-attachment).

**WhatsApp** (`backends/whatsapp_events.py`):
- `caption = raw.get("caption")` (riga 162); `attachment_info = caption or media.caption or filename or mime or "Media"` nei tre rami (184-186, 205-215, 233-239); anche nel ramo ack di `backends/whatsapp.py:240-246`.
- `text` è **sempre sintetico** per i media: `f"Media: {media_identity}"` (riga 296).
- Quindi la caption WhatsApp è in **`attachment_info`**, ma solo quando esiste; altrimenti `attachment_info` è un'etichetta tecnica (filename, mime, `"Media"`, `"imageMessage (…)"`).

**Telegram** (`backends/telegram.py`):
- `text = msg.text or ""` (riga 711): Telethon mette la caption media in `msg.text` → la caption è in **`text`**.
- Ma `msg.photo` imposta `attachment_info = "🖼️ Photo"` hardcoded (riga 733), che ha sempre precedenza.

**Punti di perdita della caption in UI**:
- `_add_message` (riga 120): per `msg_type == "image"` chiama `_render_image_in_chat(attachment_info=attachment_info or text)` e **ritorna** — il `text` non diventa mai una bolla. Per Telegram, siccome `attachment_info="🖼️ Photo"` è truthy, la caption in `text` non appare nemmeno nel placeholder.
- `_finish_attachment_resolve` (righe 265-276): a risoluzione avvenuta sovrascrive il placeholder con `[🖼️ Image: {path.name} — Click Enter to View]` → la caption (che era nel placeholder live) sparisce per le foto **ricevute**.
- `_build_message_widgets` (righe 570-575): placeholder `[🖼️ {attachment_info or text or "Image"}]` → la caption appare solo dentro le quadre; per Telegram produce `[🖼️ 🖼️ Photo]` (doppia emoji).
- `ImageModalScreen` (`ui_components.py:454-548`): mostra solo l'immagine.

### 1.4 Cosa deve cambiare (sintesi)

- **#31**: una riga in `_build_message_widgets`: assegnare `msg-right`/`msg-left` all'`ImageWidget` in base a `is_mine`.
- **#36**: una **caption bubble** (`MessageWidget` con lo stesso allineamento/status/sender della foto) montata subito dopo l'`ImageWidget` in entrambi i percorsi, alimentata da un resolver centralizzato `_image_caption(...)` che distingue caption reale da etichette tecniche; più un fix mirato al backend Telegram (niente hardcode quando c'è caption) e la normalizzazione anti doppia-emoji del placeholder.

---

## 2. Design del fix

### 2.1 BUG #31 — allineamento/colore foto da cache

**Intervento unico** in `tui/chat_view.py`, `_build_message_widgets`, ramo `msg_type == "image"` (righe 567-577):

```python
widget = ImageWidget(
    attachment_path=None,
    attachment_id=attachment_id or "",
    fallback_text=...,
)
widget.classes = "msg-right" if is_mine else "msg-left"   # ← FIX #31
widgets.append(widget)
```

Identico alla logica live (`chat_view.py:230, 240`). Nessuna altra modifica: il CSS esiste già.

### 2.2 BUG #36 — caption come bolla di testo dedicata

#### 2.2.1 Decisione di design: resolver UI-side, niente nuovi campi dati

| Opzione | Descrizione | Esito |
|---|---|---|
| **A. Nuovo campo `caption` end-to-end** | `ChatMessage.caption` + colonna SQLite + popolamento nei 3 backend | **Scartata**: la dedup di `ingest_message` scarta i duplicati → le righe già in DB non verrebbero mai arricchite con la caption (storico scoperto); blast radius grande. |
| **B. Resolver euristico centralizzato in UI** | Funzione pura in `chat_view.py` che ricava la caption da `text`/`attachment_info`/`attachment_id`/`protocol` esistenti | **Scelta**: un solo file nuovo di logica, copre live+cache+storico+righe legacy senza migrazione, nessun cambio di schema né di `ChatEvent`. |
| **C. Caption dentro `ImageModalScreen`** | Mostrare la caption nella modale | **Scartata come portante e non implementata**: la bolla sotto il placeholder soddisfa il requisito; la modale richiederebbe traghettare la caption attraverso `ImageWidget` → `ImageClicked` → `on_image_widget_image_clicked` (`tui/download.py:102-133`). |

La caption bubble è un **`MessageWidget`** standard: eredita click-to-reply, focus, status styling e colorazione sender nei gruppi.

#### 2.2.2 Il resolver: `_image_caption()` in `tui/chat_view.py`

Aggiungere in cima a `tui/chat_view.py` (accanto a `_media_display_text`, riga 33) tre funzioni pure:

```python
import re  # aggiungere in testa al file

_TECHNICAL_LABELS = frozenset({"🖼️ Image", "🖼️ Photo", "Media", "Image", "Photo"})
_TECHNICAL_PREFIXES = ("Image: ", "Video: ", "Audio: ")   # fallback di backends/signal.py:479-485
_MIME_RE = re.compile(r"^[\w.-]+/[\w.+-]+$")               # "image/jpeg"
_MEDIA_KEY_RE = re.compile(r"^(image|video|audio|document|sticker)Message( \(.+\))?$")  # fallback WA nested
_MEDIA_EXT_RE = re.compile(
    r"\.(jpe?g|png|gif|webp|bmp|tiff?|heic|heif|mp4|mov|mkv|webm|avi|mp3|ogg|opus|aac|m4a|wav|pdf)$",
    re.IGNORECASE,
)

def _is_technical_media_label(label: str) -> bool:
    """True se `label` è un'etichetta tecnica (filename/mime/fallback), non una caption."""
    s = (label or "").strip()
    if not s:
        return True
    if s in _TECHNICAL_LABELS:
        return True
    if s.startswith(_TECHNICAL_PREFIXES):
        return True
    if _MIME_RE.match(s):
        return True
    if _MEDIA_KEY_RE.match(s):
        return True
    if s.startswith(("http://", "https://")):
        return True
    if " " not in s and _MEDIA_EXT_RE.search(s):   # bare filename "photo.jpg"
        return True
    return False

def _is_synthetic_media_text(text: str, attachment_info: str | None, attachment_id: str | None) -> bool:
    """True se `text` è un'identità sintetica generata dal backend, non una caption."""
    t = (text or "").strip()
    if not t:
        return True
    if t.startswith("Media: "):                    # WhatsApp, whatsapp_events.py:296
        return True
    info = (attachment_info or "").strip()
    att = str(attachment_id or "").strip()
    if info and t == info:                          # Signal: echo del label senza id
        return True
    if info and att and t == f"{info}: {att}":      # Signal: f"{label}: {att_id}"
        return True
    return False

def _image_caption(text, attachment_info, attachment_id, protocol) -> str | None:
    """Caption reale di una foto, o None. Regole per protocollo (deterministiche):

    - Telegram: la caption è `msg.text` (attachment_info è un'etichetta statica).
    - Signal: la caption è il body `dataMessage.message` (in `text` sul primo
      attachment); se assente/sintetico, la caption per-attachment in `attachment_info`.
    - WhatsApp: la caption è in `attachment_info` (il `text` è sempre sintetico).
    """
    t = (text or "").strip()
    info = (attachment_info or "").strip()
    if protocol == PROTOCOL_TELEGRAM:               # estendere import da models
        return t or None
    if protocol == PROTOCOL_SIGNAL:
        if t and not _is_synthetic_media_text(text, attachment_info, attachment_id):
            return t
        # altrimenti ricade su attachment_info qui sotto
    if info and not _is_technical_media_label(info):
        return info
    return None
```

Nota import: `chat_view.py` importa già `PROTOCOL_SIGNAL` da `models` (righe 13-15); estendere con `PROTOCOL_TELEGRAM`.

**Copertura dei casi reali**:

| Caso | Dati | Risultato |
|---|---|---|
| Signal, foto + body "guarda!" | `text="guarda!"`, `info="Image: photo.jpg"` | body non sintetico → caption `"guarda!"` ✓ |
| Signal, foto + per-att caption | `text="nice: att-1"` (sintetico), `info="nice"` | caption `"nice"` ✓ |
| Signal, foto senza caption | `text="🖼️ Image: att-1"`, `info="🖼️ Image"` | entrambi tecnici → `None` ✓ |
| WhatsApp, foto + caption | `text="Media: https://…"`, `info="Guarda!"` | caption `"Guarda!"` ✓ |
| WhatsApp, foto senza caption | `info="photo.jpg"` / `"image/jpeg"` / `"Media"` | tecnico → `None` ✓ |
| Telegram, foto + caption | `text="che bello"`, `info="Photo"` (dopo fix backend) | caption `"che bello"` ✓ |
| Telegram, foto senza caption | `text=""`, `info="Photo"` | `None` ✓ |

Caso limite noto e accettato: una caption utente identica a un bare filename (`"photo.jpg"`) viene classificata tecnica e non mostrata come bolla (resta nel placeholder). Documentarlo nel docstring.

#### 2.2.3 Rendering della caption bubble — percorso LIVE

In `_add_message` (`tui/chat_view.py:116-125`), ramo `msg_type == "image"`:

```python
if msg_type == "image":
    caption = _image_caption(text, attachment_info, attachment_id, protocol)
    info_for_placeholder = attachment_info or text
    if caption and (info_for_placeholder or "").strip() == caption:
        info_for_placeholder = None          # la caption vive nella bolla: placeholder generico
    self._render_image_in_chat(
        attachment_id=attachment_id,
        attachment_info=info_for_placeholder or "Photo",
        is_mine=is_mine,
        chat_log=chat_log,
        protocol=protocol,
    )
    if caption:
        is_group = bool(
            self.selected_contact and self.selected_contact.id.endswith("@g.us")
        )
        caption_widget = self._make_message_widget(
            text=caption,
            is_mine=is_mine,
            timestamp=timestamp,
            sender=sender,
            status=status,
            protocol=protocol or "",
            is_group=is_group,
            message_id=message_id,
        )
        chat_log.mount(caption_widget)
        chat_log.scroll_end(animate=False)
    return
```

Punti chiave:
- `_make_message_widget` (righe 165-201) è già lo statico corretto: applica `msg-right`/`msg-left`, sender_color goldenrod per gruppi `@g.us`, accent di protocollo e status style.
- `message_id` propagato: `_update_message_widgets_status` (`tui/events.py:235-277`) aggiorna lo status anche della bolla caption per le foto inviate (by-id, nessun conflitto).
- Il dedup di `_add_message` (righe 96-107) registra una sola identità: la bolla caption è montata dentro la stessa chiamata → nessun doppio mount su `_refresh_chat`.
- **Non toccare** `_render_image_in_chat` né `_finish_attachment_resolve`: il placeholder che dopo la risoluzione mostra `path.name` resta com'è (la caption vive nella bolla); i test T3a–f di `tests/test_image_async_download.py` restano verdi.

#### 2.2.4 Rendering della caption bubble — percorso CACHE/STORICO

In `_build_message_widgets` (`tui/chat_view.py:567-577`), ramo `msg_type == "image"`:

```python
if msg_type == "image":
    caption = _image_caption(text, attachment_info, attachment_id, protocol)
    display = attachment_info or text or "Photo"
    if caption and display.strip() == caption:
        display = "Photo"                       # niente caption duplicata nel placeholder
    if not display.startswith("🖼️"):            # fix doppia emoji "[🖼️ 🖼️ Photo]"
        display = f"🖼️ {display}"
    image_widget = ImageWidget(
        attachment_path=None,
        attachment_id=attachment_id or "",
        fallback_text=f"[{display}]",
    )
    image_widget.classes = "msg-right" if is_mine else "msg-left"   # FIX #31
    widgets.append(image_widget)
    if caption:
        widgets.append(
            self._make_message_widget(
                text=caption,
                is_mine=is_mine,
                timestamp=ts,
                sender=sender,
                status=status,
                protocol=protocol,
                is_group=is_group,
                message_id=message_id,
            )
        )
```

Nota: rimuovere l'import locale `from ui_components import ImageWidget` (riga 568) — è già importato in testa al modulo (riga 17).

#### 2.2.5 Fix backend Telegram

`backends/telegram.py`, `_message_to_chat_event`, righe 731-733:

```python
if msg.photo:
    msg_type = "image"
    attachment_info = text or "Photo"   # era: "🖼️ Photo" hardcoded
```

- Con caption: `attachment_info` = caption → il placeholder live diventa `[🖼️ Image: <caption> — loading…]` (ma `_add_message` lo neutralizza a `"Photo"` perché `info == caption`) e da cache `[🖼️ Photo]` + bolla caption.
- Senza caption: `"Photo"` → placeholder `[🖼️ Photo]` (niente doppia emoji).
- `document`/`video`/ecc. non cambiano.

Nessun cambio a `backends/signal.py` e `backends/whatsapp_events.py`: la caption è già trasportata da `attachment_info`/`text`; la distinzione caption/etichetta è demandata al resolver UI (per design, §2.2.1).

---

## 3. Vincoli di compatibilità

1. **Struttura dati**: nessun campo nuovo in `ChatEvent`/`ChatMessage`/message dict. ✔
2. **Schema SQLite**: **nessuna migrazione**; `_SCHEMA_VERSION` resta `2`. Le righe legacy già in DB rendono la caption immediatamente (resolver UI-side). ✔
3. **`ChatEvent` payload**: invariato; `_handle_message_event` e il mirror in UI-cache non richiedono modifiche.
4. **Dedup invariato**: nessuna modifica a `text` né nuove righe → `ingest_message`/`_message_already_cached`, `_seen_message_ids`/`_shown_in_log` e i test di dedup esistenti restano validi.
5. **`_render_image_in_chat` / `_finish_attachment_resolve` / `ImageWidget` / `ImageModalScreen`**: contratti invariati → `tests/test_image_async_download.py`, `tests/test_ui_components.py`, `tests/test_download_mode.py` restano verdi senza modifiche.
6. **Receipts**: la bolla caption è un `MessageWidget` con `message_id`: partecipa a `_update_message_widgets_status`; l'indicizzazione by-id è primaria.
7. **Test esistenti da aggiornare (unico)**: `tests/test_telegram.py:164-184` — `("photo", "image", "🖼️ Photo")` → `("photo", "image", "Photo")`. Il file stray `Telegram/test_telegram_backend.py:250` asserisce solo `msg_type == "image"` → nessuna modifica.
8. **Sender gruppi**: la bolla caption usa `_make_message_widget(is_group=…)`, coerente con la logica goldenrod esistente.

---

## 4. Piano di test

### 4.1 Test esistenti da toccare

| File | Azione |
|---|---|
| `tests/test_telegram.py` (164-184) | Aggiornare il parametro atteso: `("photo", "image", "🖼️ Photo")` → `("photo", "image", "Photo")`. |
| Tutti gli altri | Rieseguire per regression: `test_image_async_download.py`, `test_refresh_chat.py`, `test_ui_components.py`, `test_download_mode.py`, `test_whatsapp_backend.py`, `test_backends.py`, `test_telegram.py`, `Telegram/test_telegram_backend.py`. |

### 4.2 Nuovi test — file consigliato: `tests/test_image_caption.py`

Usare gli stessi helper/fake già presenti in `tests/test_refresh_chat.py` (`_FakeChatLog`, `_make_image_message`) e in `tests/test_image_async_download.py` (`_make_app` con `BackendManager` e `run_worker` mockato).

**Suite `TestCacheImageAlignment` (BUG #31):**
1. `test_cached_sent_image_is_msg_right` — `_build_message_widgets("whatsapp", False, {msg_type:"image", is_mine:True, …})` → primo widget `ImageWidget` con `has_class("msg-right")`, non `msg-left`.
2. `test_cached_received_image_is_msg_left` — `is_mine:False` → `has_class("msg-left")`.
3. Parametrizzare sui tre protocolli (`"signal"`, `"whatsapp"`, `"telegram"`).

**Suite `TestImageCaptionResolver` (unit puri su `_image_caption`):**
4. Signal: body reale → caption; `text` sintetico `"🖼️ Image: att-1"` → `None`; per-attachment caption in `attachment_info` con `text` sintetico → caption.
5. WhatsApp: `attachment_info="Guarda!"` → caption; `attachment_info` ∈ {`"image/jpeg"`, `"photo.jpg"`, `"Media"`, `"imageMessage (ABCD…)"`, `"Image: photo.jpg"`} → `None`; `text="Media: https://…"` mai caption.
6. Telegram: `text="che bello"` → caption indipendentemente da `attachment_info`; `text=""` → `None`.

**Suite `TestCaptionBubbleLive` (BUG #36, percorso live → `_add_message`):**
7. `test_live_received_image_with_caption_shows_bubble` — `_add_message(text="Media: https://x", msg_type="image", attachment_info="Guarda!", attachment_id="u", is_mine=False, protocol="whatsapp")` → children `[ImageWidget, MessageWidget]`; `MessageWidget._msg_text == "Guarda!"` e `has_class("msg-left")`.
8. `test_live_sent_image_with_caption_bubble_msg_right` — `is_mine=True` → entrambi `msg-right`.
9. `test_live_image_without_caption_shows_no_bubble` — `attachment_info="image/jpeg"` → un solo `ImageWidget`.
10. `test_live_telegram_photo_caption_in_text` — `text="che bello"`, `attachment_info="Photo"`, `protocol="telegram"` → bolla `"che bello"`; placeholder generico `"Photo"` (asserire su `ImageWidget.render()`).

**Suite `TestCaptionBubbleCache` (BUG #36, percorso cache/storico → `_build_message_widgets`):**
11. `test_cached_image_with_caption_shows_bubble` — msg WhatsApp con `attachment_info="Guarda!"` → `[ImageWidget, MessageWidget("Guarda!")]`; placeholder `"[🖼️ Photo]"` (non ripete la caption).
12. `test_cached_image_without_caption_single_widget` — `attachment_info="photo.jpg"` → solo `ImageWidget`.
13. `test_cached_telegram_photo_no_double_emoji` — `attachment_info="Photo"` (o legacy `"🖼️ Photo"`), `text=""` → `fallback_text == "[🖼️ Photo]"` (una sola emoji); con `text="didascalia"` → bolla `"didascalia"` + placeholder generico.
14. `test_cached_signal_body_caption` — `protocol="signal"`, `text="guarda!"`, `attachment_info="Image: photo.jpg"` → bolla `"guarda!"`.

**Test backend Telegram da aggiungere a `tests/test_telegram.py`:**
15. `test_message_photo_with_caption_uses_text_as_info` — `_message_to_chat_event(_message(photo=object(), text="che bello"))` → `attachment_info == "che bello"`; senza testo → `"Photo"`.

**Nota fixture**: `tests/conftest.py:97-117` (`sample_envelope_image` con caption "Guarda!") — verificare se usato; se sì, assicurarsi che le asserzioni esistenti su `attachment_info` restino valide (il fix non tocca `backends/signal.py`).

---

## 5. Passi di implementazione ordinati

1. **`tui/chat_view.py` — helper** (top-level, dopo `_media_display_text`, riga ~40):
   - aggiungere `import re` (se assente) e `PROTOCOL_TELEGRAM` all'import da `models` (righe 13-15);
   - aggiungere `_TECHNICAL_LABELS`, `_TECHNICAL_PREFIXES`, `_MIME_RE`, `_MEDIA_KEY_RE`, `_MEDIA_EXT_RE`, `_is_technical_media_label()`, `_is_synthetic_media_text()`, `_image_caption()` come da §2.2.2.
2. **`tui/chat_view.py` — `_build_message_widgets`** (righe 567-577): applicare il blocco di §2.2.4 (fix #31 + caption bubble cache + anti doppia-emoji). Rimuovere l'import ridondante di `ImageWidget` (riga 568).
3. **`tui/chat_view.py` — `_add_message`** (righe 116-125): applicare il blocco di §2.2.3 (caption bubble live + placeholder genericizzato quando coincide con la caption). **Non** toccare `_render_image_in_chat` e `_finish_attachment_resolve`.
4. **`backends/telegram.py` — `_message_to_chat_event`** (righe 731-733): `attachment_info = text or "Photo"`.
5. **Test**: aggiornare `tests/test_telegram.py:167`; creare `tests/test_image_caption.py` con i 15 casi di §4.2.
6. **Verifica**: `make check` (lint + suite completa); poi smoke manuale: (a) foto inviata WhatsApp → riaprire la chat → allineata a destra e `$success`; (b) foto ricevuta con caption su Signal/WA/TG → bolla caption sotto il placeholder, live e dopo reload.

**Cosa NON fare** (vincoli): niente nuove colonne SQLite né bump di `_SCHEMA_VERSION`; niente campi nuovi in `ChatEvent`/`ChatMessage`; niente modifiche a `backends/signal.py`, `backends/whatsapp_events.py`, `backends/whatsapp.py`, `backend/db.py`, `tui/events.py`, `ui_components.py`, `tui/css.py`; niente caption nella modale (alternativa C scartata, §2.2.1).
