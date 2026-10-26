"""
Standardized data models for the multi-protocol TUI client.

These dataclasses decouple the Textual UI from any specific chat protocol
(Signal, WhatsApp, ...).  Every backend (``ChatBackend``) converts its
protocol-specific data into these neutral objects, so the UI only ever deals
with ``ChatContact``, ``ChatMessage`` and ``ChatEvent``.

No Textual dependency.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ─── Protocol identifiers ────────────────────────────────────────────────────

PROTOCOL_SIGNAL = "signal"
PROTOCOL_WHATSAPP = "whatsapp"
PROTOCOL_TELEGRAM = "telegram"

#: Device name registered with signal-cli when the user does not choose one.
DEFAULT_SIGNAL_DEVICE_NAME = "Signal-TUI-Client"

#: Image thumbnail size limits (terminal lines / columns) used when the server
#: does not say otherwise (``server.config`` carries the configured values).
DEFAULT_THUMBNAIL_MAX_LINES = 12
DEFAULT_THUMBNAIL_MAX_COLS = 60

#: ``msg_type`` of a service notice rendered centred, outside any bubble
#: (e.g. WhatsApp "your security code with X changed").
MSG_TYPE_SYSTEM = "system"

#: Human-friendly emoji shown next to a contact of a given protocol.
PROTOCOL_EMOJI: dict[str, str] = {
    PROTOCOL_SIGNAL: "📱",
    PROTOCOL_WHATSAPP: "💬",
    PROTOCOL_TELEGRAM: "📨",
}


def protocol_emoji(protocol: str) -> str:
    """Return the emoji used to visually tag a contact of *protocol*."""
    return PROTOCOL_EMOJI.get(protocol, "💬")


#: Human-friendly protocol names shown on grouped member rows.
PROTOCOL_NAMES: dict[str, str] = {
    PROTOCOL_SIGNAL: "Signal",
    PROTOCOL_WHATSAPP: "WhatsApp",
    PROTOCOL_TELEGRAM: "Telegram",
}


def protocol_name(protocol: str) -> str:
    """Return the human-friendly name for *protocol* (fallback: the raw string)."""
    return PROTOCOL_NAMES.get(protocol, protocol)


def contact_cache_key(protocol: str, contact_id: str) -> str:
    """Build a unique cache key for a contact across protocols.

    The same phone number could theoretically exist on both Signal and
    WhatsApp, so the key must be namespaced by protocol.
    """
    return f"{protocol}:{contact_id}"


def contact_storage_keys(contact: Any) -> list[str]:
    """Chiavi di storage (= contact id lato DB) per un contatto WhatsApp.

    Un doppio JID dello stesso telefono (``@c.us`` attivo + ``@lid``, o il
    ghost rubrica ``@c.us``) produce storico DB diviso su due ``contact_number``.
    Questa mappa identifica TUTTE le chiavi equivalenti partendo dal contatto
    canonico, così read/send/UI possono operare in union cross-key.

    Pure: nessuna dipendenza dal backend, nessuna I/O.  Per i protocolli non
    WhatsApp ritorna la sola chiave del contatto.  L'ordine è preservato: la
    chiave canonica è sempre la prima.
    """
    if contact.protocol != PROTOCOL_WHATSAPP:
        return [contact.id]
    cid = str(contact.id or "")
    if not cid or "@" not in cid or cid.endswith("@g.us"):
        return [cid]
    keys = [cid]
    lid = str(contact.extras.get("lid") or "")
    phone = str(contact.extras.get("phone") or "")
    if cid.endswith("@lid"):
        if phone:
            keys.append(f"{phone}@c.us")
    else:  # @c.us / @s.whatsapp.net
        if lid and lid.endswith("@lid"):
            keys.append(lid)
        if phone and f"{phone}@c.us" not in keys and not cid.endswith("@c.us"):
            keys.append(f"{phone}@c.us")
    deduped: list[str] = []
    for key in keys:
        if key not in deduped:
            deduped.append(key)
    return deduped


def _cross_key_message_equivalent(a: Any, b: Any) -> bool:
    """True se due messaggi (dict-like con ``id``) sono lo stesso messaggio fisico.

    Usato dalla read-union cross-key WhatsApp.  Se l'id (``msg_id`` nei row
    SQLite, ``id`` nei dict TUI) è presente e uguale, i due record sono lo
    stesso messaggio **sempre**, anche con ``attachment_id`` diverso: un twin
    ``@c.us``/``@lid`` può registrare lo stesso allegato come URL WAHA da una
    parte e file locale ``sent-*`` dall'altra.  In assenza di id coincide la
    tupla esatta ``(is_mine, text, timestamp, msg_type, attachment_id)``
    (nessuna finestra temporale).

    L'eccezione multi-allegato (id uguale, attachment distinti = allegati
    diversi) NON si applica cross-key: i multi-allegato Signal/WhatsApp
    condividono la stessa ``contact_number`` e restano nella chiave canonica,
    che non viene mai deduplicata.
    """
    aid, bid = a.get("id"), b.get("id")
    if aid and bid and aid == bid:
        return True
    return (
        bool(a.get("is_mine")) == bool(b.get("is_mine"))
        and (a.get("text") or "") == (b.get("text") or "")
        and int(a.get("timestamp") or 0) == int(b.get("timestamp") or 0)
        and (a.get("msg_type") or "") == (b.get("msg_type") or "")
        and (a.get("attachment_id") or None) == (b.get("attachment_id") or None)
    )


def dedup_cross_key(
    records: list[Any],
    canonical_key: str,
    key_of: Callable[[Any], str],
    normalize: Callable[[Any], Any],
) -> list[Any]:
    """Dedup cross-key in due passate (pure, ordine preservato).

    1. le righe con ``key_of(r) == canonical_key`` sono tenute INTEGRE
       (multi-allegato della stessa chat inclusi);
    2. per ogni riga alias (``key_of(r) != canonical_key``) la si scarta se il
       suo ``normalize(r)`` è equivalente (``_cross_key_message_equivalent``)
       a uno dei normalize già tenuti, altrimenti la si appende.

    Con meno di 2 chiavi distinte è un no-op (nessuna dedup single-key).
    """
    if not records:
        return records
    if len({key_of(r) for r in records}) < 2:
        return records
    kept = [r for r in records if key_of(r) == canonical_key]
    kept_norm = [normalize(r) for r in kept]
    for r in records:
        if key_of(r) == canonical_key:
            continue
        norm = normalize(r)
        if any(_cross_key_message_equivalent(norm, other) for other in kept_norm):
            continue
        kept.append(r)
        kept_norm.append(norm)
    return kept


# ─── Media kinds ──────────────────────────────────────────────────────────────

MEDIA_KIND_VALUES = frozenset(
    {"image", "gif", "video", "voice", "audio", "document", "sticker"}
)


def media_kind_from_mime(
    mime: str | None, *, is_gif: bool = False, is_voice: bool = False
) -> str | None:
    """Map a mime type and protocol hints to a normalized media kind."""
    normalized = (mime or "").lower().split(";", 1)[0].strip()
    if not normalized:
        return None
    if normalized == "image/gif" or is_gif:
        return "gif"
    if normalized.startswith("image/"):
        return "image"
    if normalized.startswith("video/"):
        return "video"
    if normalized.startswith("audio/"):
        return "voice" if is_voice else "audio"
    return "document"


def msg_type_for_media_kind(kind: str) -> str:
    """Return the backwards-compatible message type for a media kind."""
    if kind in ("image", "gif"):
        return "image"
    if kind == "sticker":
        return "sticker"
    return "attachment"


# ─── Media quote placeholders ────────────────────────────────────────────────

#: Canonical typed placeholders for a quoted media message (bug #37).  When a
#: message quoting a media carries no real caption, the backends synthesize a
#: ``quote_text`` from this mapping so the quote bubble is still rendered.
#: These are display-only values: they must never travel on the wire as a
#: Signal ``quoteMessage`` (see ``is_media_quote_placeholder``).
MEDIA_QUOTE_PLACEHOLDERS: dict[str, str] = {
    "image": "🖼️ Immagine",
    "sticker": "🎨 Sticker",
    "attachment": "📎 File",
    "audio": "🎵 Audio",
    "video": "🎬 Video",
}

#: Media kind → ``media_quote_placeholder`` key.  Totale su
#: ``MEDIA_KIND_VALUES``: ogni kind ha un segnaposto canonico.
_KIND_TO_PLACEHOLDER: dict[str, str] = {
    "image": "image",
    "gif": "image",
    "video": "video",
    "voice": "audio",
    "audio": "audio",
    "document": "attachment",
    "sticker": "sticker",
}

_IMAGE_BASE64_PREFIXES = ("/9j/", "iVBORw0KGgo", "R0lGOD", "UklGR")


def embedded_media_quote_placeholder(value: object) -> str | None:
    """Return a safe placeholder when *value* contains inline media data."""
    if not isinstance(value, str):
        return None
    compact = "".join(value.split())
    lower = compact.lower()
    if lower.startswith("data:") and ";base64," in lower[:128]:
        mime = lower[5:].split(";", 1)[0]
        if mime.startswith("image/"):
            return MEDIA_QUOTE_PLACEHOLDERS["image"]
        if mime.startswith("video/"):
            return MEDIA_QUOTE_PLACEHOLDERS["video"]
        if mime.startswith("audio/"):
            return MEDIA_QUOTE_PLACEHOLDERS["audio"]
        return MEDIA_QUOTE_PLACEHOLDERS["attachment"]
    if compact.startswith(_IMAGE_BASE64_PREFIXES):
        return MEDIA_QUOTE_PLACEHOLDERS["image"]
    if len(compact) >= 128 and re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", compact):
        return MEDIA_QUOTE_PLACEHOLDERS["attachment"]
    return None


def media_quote_placeholder(msg_type: str, detail: str | None = None) -> str:
    """Return a human-readable label for a quoted media message.

    ``detail`` is the real user caption/filename when available; it takes
    priority over the typed placeholder derived from ``msg_type``.  Unknown
    message types degrade to the generic "📎 File" placeholder.
    """
    if detail:
        return detail
    return MEDIA_QUOTE_PLACEHOLDERS.get(
        msg_type, MEDIA_QUOTE_PLACEHOLDERS["attachment"]
    )


def media_quote_display(
    content_type: str | None, *, filename: str | None = None
) -> str | None:
    """Costruisce il ``quote_text`` display di un media quotato.

    Il mime seleziona il segnaposto tipizzato via ``media_kind_from_mime``;
    senza mime (o con kind non mappato) non c'è placeholder.  Il filename, se
    presente, è anteposto come ``"<filename> — <placeholder>"`` (em dash).
    """
    kind = media_kind_from_mime(content_type)
    if not kind or kind not in _KIND_TO_PLACEHOLDER:
        return None
    placeholder = media_quote_placeholder(_KIND_TO_PLACEHOLDER[kind])
    return f"{filename} — {placeholder}" if filename else placeholder


def parse_quote_attachment_descriptor(
    descriptor: str | None,
) -> tuple[str | None, str | None]:
    """Estrae ``(content_type, filename)`` da un descriptor Signal.

    Formato ``"content_type[:filename[:path]]"``.  Il path può contenere ``:``,
    quindi si usano DUE ``partition`` (ct | filename | path): tutto ciò che
    segue il secondo ``:`` resta nel path e viene ignorato.  Un descriptor
    vuoto (o ``None``) ritorna ``(None, None)``.
    """
    if not descriptor:
        return (None, None)
    content_type, sep, rest = (descriptor or "").partition(":")
    if not sep:
        return (content_type.strip() or None, None)
    filename, _sep2, _path = rest.partition(":")
    return (content_type.strip() or None, filename.strip() or None)


def is_media_quote_placeholder(text: str | None) -> bool:
    """Return True if *text* is exactly one of the canonical media placeholders.

    The predicate recognises only the 5 canonical strings (never the composed
    ``"filename — placeholder"`` form, which exists only on the display path).
    Used by the retry path to reconstruct the wire-faithful ``quote_wire_body``
    when a media reply is retried after reload.
    """
    return text is not None and text in MEDIA_QUOTE_PLACEHOLDERS.values()


def is_media_quote_placeholder_composite(text: str | None) -> bool:
    """Return True if *text* is a media placeholder or its composed form.

    The composed form ``"filename — placeholder"`` is display-only (built by
    ``_signal_quote_text``); the exact ``is_media_quote_placeholder`` remains the
    retry/wire predicate.  Used by ``QuoteWidget`` to hide the textual
    placeholder when a native thumbnail replaces it (a real caption stays
    visible).
    """
    if not text:
        return False
    if text in MEDIA_QUOTE_PLACEHOLDERS.values():
        return True
    return any(text.endswith(f" — {p}") for p in MEDIA_QUOTE_PLACEHOLDERS.values())


_CAPTION_FILENAME_RE = re.compile(r"^[^\s/\\]+\.[a-z0-9]{1,5}$", re.IGNORECASE)
_CAPTION_FILE_EXTENSIONS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".heic",
    ".heif",
    ".bmp",
    ".tif",
    ".tiff",
    ".avif",
    ".svg",
    ".mp4",
    ".mov",
    ".mkv",
    ".webm",
    ".mp3",
    ".oga",
    ".ogg",
    ".opus",
    ".aac",
    ".m4a",
    ".wav",
    ".flac",
    ".pdf",
    ".doc",
    ".docx",
)
_MEDIA_LABELS = {
    "photo",
    "image",
    "immagine",
    "video",
    "audio",
    "document",
    "documento",
    "file",
    "allegato",
    "sticker",
    "🖼️ image",
    "🖼️ immagine",
    "🖼️ photo",
    "🎬 video",
    "🎵 audio",
    "🎨 sticker",
    "📎 file",
}


def is_caption_like(value: str | None) -> bool:
    """Return whether *value* looks like a user caption rather than media metadata."""
    stripped = (value or "").strip()
    if not stripped or is_media_quote_placeholder_composite(stripped):
        return False
    lowered = stripped.lower()
    if (
        lowered in _MEDIA_LABELS
        or lowered.endswith(_CAPTION_FILE_EXTENSIONS)
        or _CAPTION_FILENAME_RE.fullmatch(stripped)
    ):
        return False
    if re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+(?:\s*;.*)?", lowered):
        return False
    return not (
        lowered.startswith("media:") and re.fullmatch(r"\S+", stripped[6:].strip())
    )


# ─── WhatsApp synthetic media identity ───────────────────────────────────────

#: Forme di identità media sintetica generate dal backend WhatsApp nel campo
#: ``text`` (``"Media: <id>"``): URL WAHA, id ``false_...@...``/``true_...@...``,
#: oppure coppia ``chat:message``.  Il predicato è volutamente RISTRETTO: un
#: singolo token qualsiasi NON è un'identità (una caption utente può iniziare
#: con ``Media:``).  L'id esplicito (``attachment_id``) è l'unica altra via per
#: riconoscere un'identità che non rientra nei pattern.
_WHATSAPP_MEDIA_IDENTITY = (
    r"https?://\S+"
    r"|(?:false|true)_[^\s@]*@[^\s@]+"
    r"|\d+:\d+"
)


def is_whatsapp_synthetic_media_text(
    text: str,
    *,
    attachment_id: str | None = None,
    require_attachment_match: bool = False,
) -> bool:
    """True se *text* è un'identità media WhatsApp sintetica (``Media: <id>``),
    non una caption utente.

    L'identità è riconosciuta se coincide con *attachment_id* (o col suo
    basename) oppure, con ``require_attachment_match=False`` (default: web,
    migrate, legacy), se rientra nei pattern canonici (URL WAHA, id serializzato
    ``true_``/``false_``, coppia ``chat:message``).  ``"Media: bella foto"``
    (caption multi-parola) NON è sintetica; ``"Media: ciao"`` non è riconosciuta
    senza un ``attachment_id`` esplicito che combaci.

    Con ``require_attachment_match=True`` (dedup incoming, path A) l'unica via è
    la coincidenza con *attachment_id*: un URL media che non è l'identità del
    proprio allegato resta una caption utente e non viene mai cancellato.
    """
    stripped = (text or "").strip()
    if not stripped.lower().startswith("media:"):
        return False
    media_identity = stripped[len("media:") :].strip()
    if not media_identity:
        return False
    if attachment_id:
        aid = str(attachment_id)
        if media_identity in (aid, Path(aid).name):
            return True
    if require_attachment_match:
        return False
    return bool(re.fullmatch(_WHATSAPP_MEDIA_IDENTITY, media_identity, re.IGNORECASE))


def is_duplicate_media_identity(text: str | None, info: str | None) -> bool:
    """True se *text* è solo il filename del media (duplicato dell'identità).

    Usato dalla fusione incoming: il ``text`` di un twin che ripete esattamente
    il filename in ``attachment_info`` è ridondante e può essere scartato.  Una
    caption reale (``is_caption_like``) o un URL incollato non matchano mai,
    quindi nessuna caption utente viene persa.
    """
    stripped = (text or "").strip()
    if not stripped or is_caption_like(stripped):
        return False
    info_stripped = (info or "").strip()
    if not info_stripped:
        return False
    return stripped == info_stripped


#: Un ``attachment_info`` composto solo da un mime non è un nome file.
_MIME_ONLY_RE = re.compile(r"^[a-z0-9.+-]+/[a-z0-9.+-]+(?:\s*;.*)?$", re.IGNORECASE)


def _is_http_url(value: str) -> bool:
    """True se *value* inizia con ``http://`` o ``https://``."""
    return value.startswith(("http://", "https://"))


def _attachment_basename(attachment_id: str | None) -> str:
    """Basename di un ``attachment_id``/URL, senza query string né fragment."""
    value = str(attachment_id or "").split("?", 1)[0].split("#", 1)[0]
    return Path(value).name


def attachment_display_name(
    attachment_info: str | None, attachment_id: str | None
) -> str | None:
    """Nome file visualizzato di un allegato, o ``None``.

    Un ``attachment_info`` che è un nome file reale (non URL, non solo mime,
    non caption utente) vince.  Altrimenti il nome si ricava dal basename
    dell'``attachment_id`` (query/fragment rimossi); infine, se ancora assente,
    si ricade sull'``info`` originale.  Usato da web e TUI per mostrare il
    filename e non un URL/mime/caption.
    """
    info = (attachment_info or "").strip()
    if (
        info
        and not _is_http_url(info)
        and not _MIME_ONLY_RE.fullmatch(info)
        and not is_caption_like(info)
    ):
        return info
    if attachment_id:
        basename = _attachment_basename(attachment_id)
        if basename:
            return basename
    return info or None


def is_sent_mirror_attachment_id(value: str | None) -> bool:
    """True se *value* è un attachment_id mirror client-side ``sent-*`` (basename)."""
    if not value:
        return False
    return Path(str(value)).name.lower().startswith("sent-")


# ─── Delivery status & twin merge helpers ────────────────────────────────────

#: Rank di delivery status condiviso.  Le query SQL di ``protocols.db``
#: replicano lo stesso mapping in un ``CASE`` per non alterarne il testo; questa
#: costante è la fonte per il codice Python (fusione dei twin DB/web).
_STATUS_RANK = {"pending": 0, "failed": 0, "sent": 1, "delivered": 2, "read": 3}

#: Campi che ``pick_survivor_and_merge`` completa sul survivor solo se assenti
#: (None/""), mai sovrascrivendo.  Lista canonica condivisa da DB, web e TUI.
MERGE_FILL_FIELDS: tuple[str, ...] = (
    "content_type",
    "media_kind",
    "quote_text",
    "quote_timestamp",
    "quote_author",
    "reply_to_message_id",
    "quote_attachment_id",
    "quote_attachment_path",
    "quote_content_type",
    "attachment_info",
)

#: Campi che il merge non tocca MAI (identità fisica e slot immutabili).
_MERGE_IMMUTABLE_FIELDS = frozenset(
    {
        "id",
        "msg_id",
        "attachment_id",
        "contact_number",
        "protocol",
        "is_mine",
        "read",
        "batch_id",
        "batch_index",
        "text",
    }
)


def _status_rank(status: str | None) -> int:
    """Rank numerico di uno status (NULL/sconosciuto → 0)."""
    return _STATUS_RANK.get((status or "").lower(), 0)


def _prefer_display_sender(a: str | None, b: str | None) -> str | None:
    """Sceglie il ``sender`` più leggibile fra due righe gemelle.

    Un sender WhatsApp può essere un JID raw (``...@lid``/``@c.us``/
    ``@s.whatsapp.net``) oppure un nome risolto (senza ``@``).  Vince il primo
    valore non vuoto che non è un JID; se entrambi sono raw/vuoti si tiene *a*,
    così nessun dato presente viene mai perso.
    """

    def _is_display(value: str | None) -> bool:
        return bool(value) and "@" not in str(value)

    if _is_display(a):
        return a
    if _is_display(b):
        return b
    return a or b


def _row_field(row: Any, field: str, default: Any = None) -> Any:
    """Legge *field* da un mapping o da una ``sqlite3.Row`` (pure)."""
    getter = getattr(row, "get", None)
    if getter is not None:
        return getter(field, default)
    try:
        return row[field]
    except (KeyError, IndexError, TypeError):
        return default


def _as_int(value: Any) -> int:
    """Converte *value* in int, trattando None/non numerici come 0."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _rowid_order(value: Any) -> tuple[int, int, str]:
    """Chiave di ordinamento crescente per ``id``/rowid eterogenei."""
    if value is None:
        return (2, 0, "")
    if isinstance(value, bool):
        return (1, int(value), "")
    if isinstance(value, int):
        return (0, value, "")
    try:
        return (0, int(value), "")
    except (TypeError, ValueError):
        return (1, 0, str(value))


def _survivor_sort_key(row: Any) -> tuple[int, int, int, tuple[int, int, str]]:
    """Chiave (ascendente) di selezione: status rank, ts reale, rowid.

    Il ``timestamp`` reale (≠None/≠0) batte sempre 0/None; a parità si usa
    l'``id``/rowid crescente per un esito deterministico.
    """
    status = _status_rank(_row_field(row, "status"))
    timestamp = _as_int(_row_field(row, "timestamp"))
    real_timestamp = 1 if timestamp else 0
    return (
        -status,
        -real_timestamp,
        -timestamp,
        _rowid_order(_row_field(row, "id")),
    )


def pick_survivor_and_merge(
    rows: list[Any], *, prefer_attachment: bool = False
) -> tuple[int, dict[str, Any]]:
    """Sceglie il survivor di un gruppo di twin e ne calcola il merge (pure).

    Input: sequenza di mapping omogenei già raggruppati dal chiamante per
    identità fisica.  L'input non viene mutato.  Ritorna ``(indice, overrides)``:
    l'indice del survivor nell'input e il dict dei soli campi da applicare al
    survivor.

    Selezione survivor (chiave ``_survivor_sort_key``): status rank DESC, poi
    ``timestamp`` reale DESC (un ts reale batte 0/None), poi ``id``/rowid ASC.

    Con ``prefer_attachment=True`` il POOL dei candidati survivor è ristretto
    alle righe con ``attachment_id`` non vuoto (per non scartare l'allegato
    quando un twin testo porta la caption: la caption verrà poi trasferita dal
    layer web).  Se nessuna riga ha allegato ricade sul pool intero.  Fill,
    timestamp, status, sender e ``edited`` restano calcolati sull'intero gruppo
    e ``text``/``attachment_id`` restano immutabili.

    Nota R1 (sfumatura nota, non fixata): la chiave mette lo ``status`` PRIMA
    del ``timestamp``.  Un twin a ``ts=0`` con rank di status superiore diventa
    quindi survivor; ``overrides["timestamp"]`` ne corregge il timestamp col ts
    reale dell'altro twin, ma ``read`` resta quello del survivor (immutabile).
    Rischio noto di falso "non letto"; non viene risolto qui.

    Overrides: i ``MERGE_FILL_FIELDS`` sono completati solo se il survivor è
    None/"" e un'altra riga ha valore; ``timestamp`` adotta il primo ts reale
    scartato se il survivor è 0/None; ``status`` è riempito solo se vuoto;
    ``sender`` è il fold di ``_prefer_display_sender`` (survivor per primo) e
    ``edited`` è il massimo.  I campi ``_MERGE_IMMUTABLE_FIELDS`` (identità
    fisica, ``text``, batch slot, ...) non compaiono MAI negli overrides.
    """
    materialized = list(rows)
    if not materialized:
        raise ValueError("pick_survivor_and_merge richiede almeno una riga")
    survivor_pool: list[int] | range = range(len(materialized))
    if prefer_attachment:
        with_attachment = [
            index
            for index, row in enumerate(materialized)
            if _row_field(row, "attachment_id")
        ]
        if with_attachment:
            survivor_pool = with_attachment
    survivor_index = min(
        survivor_pool,
        key=lambda index: _survivor_sort_key(materialized[index]),
    )
    survivor = materialized[survivor_index]
    others = [row for index, row in enumerate(materialized) if index != survivor_index]

    def _missing(value: Any) -> bool:
        return value is None or value == ""

    overrides: dict[str, Any] = {}
    for name in MERGE_FILL_FIELDS:
        value = _row_field(survivor, name)
        if _missing(value):
            for other in others:
                candidate = _row_field(other, name)
                if not _missing(candidate):
                    value = candidate
                    break
        overrides[name] = value

    timestamp = _as_int(_row_field(survivor, "timestamp"))
    if not timestamp:
        for other in others:
            candidate = _as_int(_row_field(other, "timestamp"))
            if candidate:
                timestamp = candidate
                break
        if timestamp:
            overrides["timestamp"] = timestamp

    status = _row_field(survivor, "status")
    if _missing(status):
        for other in others:
            candidate = _row_field(other, "status")
            if not _missing(candidate):
                status = candidate
                break
        if not _missing(status):
            overrides["status"] = status

    sender = _row_field(survivor, "sender")
    for other in others:
        sender = _prefer_display_sender(sender, _row_field(other, "sender"))
    overrides["sender"] = sender

    overrides["edited"] = max(
        (_as_int(_row_field(row, "edited")) for row in materialized), default=0
    )
    return survivor_index, overrides


# ─── Data models ─────────────────────────────────────────────────────────────


@dataclass
class ChatContact:
    """A contact in a chat protocol.

    Attributes
    ----------
    id:
        Protocol-scoped unique identifier (e.g. a phone number for Signal,
        a JID for WhatsApp).
    display_name:
        The name shown in the UI (falls back to ``id`` when unknown).
    protocol:
        One of the ``PROTOCOL_*`` constants.
    extras:
        Protocol-specific metadata (e.g. Signal ACI, WhatsApp profile pic,
        normalized ``phone`` number, address-book provenance markers).
    """

    id: str
    display_name: str
    protocol: str
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def cache_key(self) -> str:
        """Unique key for this contact across all protocols."""
        return contact_cache_key(self.protocol, self.id)

    @property
    def phone(self) -> str:
        """Normalized phone number (E.164 without ``+``), or ``""`` if unknown.

        Stored in ``extras`` by the address-book backends; used as the
        cross-backend grouping key and for search.
        """
        return str(self.extras.get("phone", "") or "")

    @property
    def last_message_ts(self) -> int:
        """Timestamp (ms) of the last known message, or ``0`` if none.

        Stored in ``extras`` by the UI / backends whenever a new message is
        ingested or when the timestamp is recovered from the SQLite cache or
        the WhatsApp ``/chats`` payload.  Used for "most recent first" sorting.
        """
        return int(self.extras.get("last_message_ts", 0) or 0)

    @last_message_ts.setter
    def last_message_ts(self, value: int) -> None:
        self.extras["last_message_ts"] = int(value or 0)


@dataclass
class ChatMessage:
    """A single message in a chat, normalized across protocols.

    Attributes
    ----------
    id:
        Protocol-scoped unique message identifier (e.g. a timestamp for
        Signal, a message ID for WhatsApp).
    contact_id:
        The ``ChatContact.id`` this message belongs to.
    protocol:
        One of the ``PROTOCOL_*`` constants.
    text:
        The message body (may be empty for pure media messages).
    is_mine:
        Whether this message was sent by the current user.
    sender:
        Display name / id of the sender.
    timestamp:
        Unix timestamp in milliseconds.
    quote_text:
        Text of the message being replied to, if any.
    msg_type:
        ``"text"``, ``"image"``, ``"sticker"``, ``"attachment"``.
    attachment_info:
        Additional attachment details (filename, sticker emoji, ...).
    attachment_id:
        Backend-specific attachment id for resolving the file on disk.
    status:
        Delivery status for sent messages: ``"pending"``, ``"failed"``,
        ``"sent"``, ``"delivered"``, ``"read"``.
    """

    id: str
    contact_id: str
    protocol: str
    text: str
    is_mine: bool
    sender: str
    timestamp: int
    quote_text: str | None = None
    msg_type: str = "text"
    attachment_info: str | None = None
    attachment_id: str | None = None
    content_type: str | None = None
    status: str = "sent"
    reply_to_message_id: str | None = None
    # Quoted-media thumbnail metadata (DESIGN_QUOTE_THUMBNAIL, additive).
    # ``quote_attachment_id``/``quote_content_type`` are backend-produced and
    # persisted; ``quote_attachment_path`` is UI-derived (resolved lazily via
    # ``get_attachment_path``) and is NOT persisted.
    quote_attachment_id: str | None = None
    quote_attachment_path: str | None = None
    quote_content_type: str | None = None
    media_kind: str | None = None


@dataclass
class ChatEvent:
    """A normalized event emitted by a backend.

    ``type`` is one of:

    - ``"message"``: a new message (payload is a ``ChatMessage`` dict).
    - ``"message_edit"``: un messaggio esistente è stato modificato.
      payload: ``{"edit_message_id": str, "text": str, "timestamp": int (ts ORIGINALE),
      "edit_timestamp": int|None, "is_mine": bool, "sender": str,
      "contact": ChatContact|None, "msg_type": "text"}``.
    - ``"reaction_update"``: aggiornamento reazioni a un messaggio.
      payload: ``{"target_message_id": str|None, "target_timestamp": int|None,
      "mode": "delta"|"snapshot", "emoji": str, "is_remove": bool,
      "author": str, "author_key": str, "is_mine": bool,
      "snapshot": [{"emoji": str, "count": int, "is_mine": bool,
      "authors": list[str]}]|None, "timestamp": int,
      "contact": ChatContact|None}``.
    - ``"typing"``: a typing indicator (payload: ``{"action": "STARTED"|"STOPPED"}``).
    - ``"receipt"``: a delivery/read receipt (payload: list of updated messages).
    - ``"contact_update"``: contact metadata changed (payload: ``ChatContact`` dict).
    - ``"sent-mirror"``: lightweight notification of a batch send mirror.
    - ``"lid_warmup_done"``: the WhatsApp lid resolver run finished (no payload).
    """

    type: str
    protocol: str
    contact_id: str
    payload: dict[str, Any] = field(default_factory=dict)
