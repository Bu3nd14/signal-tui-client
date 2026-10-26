"""Persisted-message queries and presentation (the ``messages.page`` domain).

SQLite read with cross-key read-union, intra/cross-key dedup, attachment and
caption normalisation, quote-thumbnail resolution and group sender resolution.
Also the edit/reaction row lookups and persistence. Moved verbatim from
``web/api.py``; no FastAPI/Starlette here.
"""

from __future__ import annotations

import logging
import mimetypes
import re
import sqlite3
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote as url_quote

from models import (
    MSG_TYPE_SYSTEM,
    PROTOCOL_WHATSAPP,
    attachment_display_name,
    contact_storage_keys,
    dedup_cross_key,
    is_caption_like,
    is_duplicate_media_identity,
    is_media_quote_placeholder_composite,
    is_whatsapp_synthetic_media_text,
    pick_survivor_and_merge,
)
from server.api import ApiError
from server.media import _IMAGE_EXTENSIONS, _VIDEO_EXTENSIONS, _attach_media_versions

logger = logging.getLogger(__name__)

#: Max length of an outgoing/edited message text.
MAX_TEXT_LENGTH = 64 * 1024
_MAX_MESSAGES_LIMIT = 2000
_MESSAGES_LIMIT_SLACK = 50


def _infer_attachment_type(attachment_id: str, content_type: str | None) -> str | None:
    if content_type and content_type.strip():
        return content_type
    path = attachment_id.split("?", 1)[0]
    suffix = Path(path).suffix.lower()
    if suffix not in _IMAGE_EXTENSIONS:
        return None
    guessed_type, _ = mimetypes.guess_type(path)
    if guessed_type and guessed_type.startswith("image/"):
        return guessed_type
    return f"image/{'jpeg' if suffix in {'.jpg', '.jpeg'} else suffix[1:]}"


def _is_whatsapp_synthetic_text(
    text: str | None, attachment_id: str | None = None
) -> bool:
    """Alias locale del predicato canonico ``models.is_whatsapp_synthetic_media_text``.

    Il predicato vive in ``models`` così ingest backend, dedup SQLite e web
    condividono una sola definizione.  *attachment_id* permette di riconoscere
    anche un'identità ``"Media: <id>"`` che non rientra nei pattern canonici.
    """
    return is_whatsapp_synthetic_media_text(text, attachment_id=attachment_id)


def _message_edit_id(row: sqlite3.Row | dict[str, Any]) -> str | None:
    if (
        not row["is_mine"]
        or row["msg_type"] != "text"
        or row["status"] in {"pending", "failed"}
    ):
        return None
    if row["protocol"] == "signal":
        return str(row["msg_id"] or row["timestamp"])
    return str(row["msg_id"]) if row["msg_id"] else None


def _aggregate_reactions(
    message_rows: list[sqlite3.Row], reaction_rows: list[dict[str, Any]]
) -> dict[int, list[dict[str, Any]]]:
    """Aggregate persisted reactions for each matching message row."""
    result: dict[int, list[dict[str, Any]]] = {}
    for message_row in message_rows:
        message_timestamp = int(message_row["timestamp"])
        message_ids = {str(message_timestamp)}
        if message_row["msg_id"] is not None:
            message_ids.add(str(message_row["msg_id"]))

        grouped: dict[str, dict[str, Any]] = {}
        for reaction_row in reaction_rows:
            target_msg_id = reaction_row.get("target_msg_id")
            matches_id = target_msg_id is not None and str(target_msg_id) in message_ids
            matches_timestamp = (
                reaction_row.get("target_timestamp") == message_timestamp
            )
            if not matches_id and not matches_timestamp:
                continue

            emoji = str(reaction_row["emoji"])
            reaction_timestamp = int(reaction_row["timestamp"])
            aggregate = grouped.setdefault(
                emoji,
                {
                    "emoji": emoji,
                    "count": 0,
                    "is_mine": False,
                    "authors": set(),
                    "first_timestamp": reaction_timestamp,
                },
            )
            aggregate["count"] += int(reaction_row.get("count") or 0)
            aggregate["is_mine"] = aggregate["is_mine"] or bool(
                reaction_row.get("is_mine")
            )
            author = str(reaction_row.get("author") or "").strip()
            if author:
                aggregate["authors"].add(author)
            aggregate["first_timestamp"] = min(
                aggregate["first_timestamp"], reaction_timestamp
            )

        if grouped:
            ordered = sorted(
                grouped.values(),
                key=lambda item: (-item["count"], item["first_timestamp"]),
            )
            result[int(message_row["id"])] = [
                {
                    "emoji": item["emoji"],
                    "count": item["count"],
                    "is_mine": item["is_mine"],
                    "authors": sorted(item["authors"]),
                }
                for item in ordered
            ]
    return result


def _row_message_identity(row: sqlite3.Row) -> dict[str, Any]:
    """Normalizza un row SQLite nella forma attesa da ``_cross_key_message_equivalent``.

    Il rowid ``id`` (chiave di ordinamento) NON è l'identità del messaggio:
    l'identità cross-key è ``msg_id`` ed è mappata sul campo ``id``.
    """
    return {
        "id": row["msg_id"],
        "is_mine": row["is_mine"],
        "text": row["text"],
        "timestamp": row["timestamp"],
        "msg_type": row["msg_type"],
        "attachment_id": row["attachment_id"],
    }


def _cross_key_dedup_rows(rows: list[sqlite3.Row], canonical: str) -> list[sqlite3.Row]:
    """Dedup read-union cross-key (due passate, regola condivisa in ``models``).

    Tiene integre le righe della chiave canonica; per le alias scarta i twin
    equivalenti (stesso ``msg_id``, attachment a parte, oppure tupla esatta).
    La dedup per chiave avviene DOPO aver raccolto tutte le canoniche, così
    un'alias con rowid minore del twin canonico non genera un duplicato.
    """
    kept = dedup_cross_key(
        rows,
        canonical_key=canonical,
        key_of=lambda row: row["contact_number"],
        normalize=_row_message_identity,
    )
    kept.sort(key=lambda r: (int(r["timestamp"]), int(r["id"])))
    return kept


def _real_caption(
    text: str | None,
    *,
    attachment_id: str | None,
    media_filename: str | None,
) -> str | None:
    """Classifica il testo di una riga come caption reale, o ``None``.

    Unica fonte di verità per la dedup intra-chiave web: scarta il vuoto,
    l'identità media sintetica WhatsApp (``"Media: <id>"``, ``attachment_id``
    in modalità non restrittiva come il resto del layer web) e il duplicato
    dell'identità (testo uguale al filename del media).  Tutto il resto è una
    caption reale.  È l'unica eccezione al ``text`` immutabile del merge.
    """
    stripped = (text or "").strip()
    if not stripped:
        return None
    if is_whatsapp_synthetic_media_text(stripped, attachment_id=attachment_id):
        return None
    if media_filename and is_duplicate_media_identity(stripped, media_filename):
        return None
    return stripped


def _basename(attachment_id: str | None) -> str:
    """Basename di un ``attachment_id``/URL senza query string né fragment."""
    return Path(str(attachment_id or "").split("?", 1)[0].split("#", 1)[0]).name


def _dedup_intra_key_rows(
    rows: list[sqlite3.Row],
) -> list[sqlite3.Row | dict[str, Any]]:
    """Dedup intra-chiave col merge condiviso di ``models``.

    Il gruppo fisico è ``(msg_id, attachment-compat, is_mine)``: stesso
    ``msg_id`` non vuoto, stesso ``is_mine`` e stesso ``attachment_id`` (OPPURE
    uno dei due assente).  Id uguale con due ``attachment_id`` distinti ed
    entrambi non vuoti sono allegati diversi (multi-allegato/batch) e restano
    separati; un ``is_mine`` diverso non è MAI fuso (R5).

    Flow unificata per media e no-media.  Il survivor è scelto con
    ``pick_survivor_and_merge(..., prefer_attachment=has_media)`` (l'allegato,
    se presente, non viene mai scartato).  Le caption reali del gruppo sono
    classificate da ``_real_caption``: se ne emergono >=2 distinte il gruppo NON
    viene fuso (anti-perdita, nessun "first-wins"); se ne emerge esattamente una
    e il survivor non la porta, viene trasferita sul suo ``text`` (unica
    eccezione all'immutabilità del text).  ``attachment_id``, ``msg_id``,
    ``batch_id``, ``batch_index``, ``is_mine``, ``read`` e ``id`` restano SEMPRE
    dal survivor.

    Per ogni gruppo fuso survivor e merge sono calcolati da
    ``models.pick_survivor_and_merge`` (status rank, ts reale, rowid; fill senza
    sovrascrittura, sender display, ``edited=max``); le righe singole restano
    l'oggetto originale.  In uscita l'ordine è ri-normalizzato su
    ``(timestamp, id)``.

    Nota reazioni (follow-up non bloccante): ``_aggregate_reactions`` aggancia
    le reazioni per rowid DOPO questa dedup, quindi una reazione registrata solo
    su ``target_timestamp`` del twin (id assente) non sopravvive.  Le reazioni
    con ``target_msg_id`` restano corrette perché il survivor condivide l'id.
    """
    groups: list[dict[str, Any]] = []
    for row in rows:
        msg_id = row["msg_id"]
        if not msg_id:
            groups.append({"rows": [row], "attachments": set()})
            continue
        attachment_id = row["attachment_id"]
        is_mine = bool(row["is_mine"])
        target = None
        for group in groups:
            if group.get("msg_id") != msg_id or group.get("is_mine") != is_mine:
                continue
            attachments = group["attachments"]
            if not attachment_id or not attachments or attachment_id in attachments:
                # Stesso allegato OPPURE uno dei due assente: compatibili.
                target = group
                break
        if target is None:
            target = {
                "msg_id": msg_id,
                "is_mine": is_mine,
                "rows": [],
                "attachments": set(),
            }
            groups.append(target)
        target["rows"].append(row)
        if attachment_id:
            target["attachments"].add(attachment_id)

    merged: list[sqlite3.Row | dict[str, Any]] = []
    for group in groups:
        group_rows = group["rows"]
        if len(group_rows) < 2:
            merged.extend(group_rows)
            continue

        has_media = any(row["attachment_id"] for row in group_rows)
        survivor_index, overrides = pick_survivor_and_merge(
            group_rows, prefer_attachment=has_media
        )
        survivor = dict(group_rows[survivor_index])
        survivor.update(overrides)

        attachment_id = survivor["attachment_id"]
        media_filename = (
            (survivor["attachment_info"] or _basename(attachment_id))
            if attachment_id
            else None
        )

        captions = {
            caption
            for row in group_rows
            if (
                caption := _real_caption(
                    row["text"],
                    attachment_id=attachment_id,
                    media_filename=media_filename,
                )
            )
            is not None
        }

        if len(captions) > 1:
            # Due caption reali distinte: due messaggi, nessuna fusione.
            merged.extend(group_rows)
            continue

        if (
            captions
            and _real_caption(
                survivor["text"],
                attachment_id=attachment_id,
                media_filename=media_filename,
            )
            is None
        ):
            survivor["text"] = next(iter(captions))

        merged.append(survivor)

    merged.sort(key=lambda row: (int(row["timestamp"] or 0), int(row["id"] or 0)))
    return merged


def _messages(
    protocol: str,
    contact_id: str,
    extra_keys: tuple[str, ...] = (),
    *,
    limit: int | None = None,
    since: int | None = None,
) -> list[dict[str, Any]]:
    """Messages of a chat, oldest first.

    ``since`` keeps only messages with ``timestamp >= since`` (the web cache's
    delta sync); ``limit`` keeps only the newest ``limit`` messages.
    """
    import protocols.db as backend
    from protocols.db import _DB_LOCK, _reactions_for_contact

    keys = [contact_id, *(key for key in extra_keys if key)]
    placeholders = ", ".join("?" for _ in keys)
    where_since = "AND timestamp >= ? " if since is not None else ""
    params: tuple[Any, ...] = (protocol, *keys) + (
        (since,) if since is not None else ()
    )
    if limit is not None:
        # Newest first in SQL, with slack for the rows the dedup drops; the
        # result is put back in chronological order and trimmed below.
        order = "ORDER BY timestamp DESC, id DESC LIMIT ?"
        params = (*params, limit + _MESSAGES_LIMIT_SLACK)
    else:
        order = "ORDER BY timestamp, id"
    with _DB_LOCK:
        try:
            connection = sqlite3.connect(backend.DB_FILE)
            connection.row_factory = sqlite3.Row
            try:
                rows = connection.execute(
                    "SELECT id, msg_id, text, is_mine, timestamp, contact_number, "
                    "sender, attachment_id, attachment_info, content_type, media_kind, "
                    "protocol, msg_type, "
                    "quote_text, quote_timestamp, quote_author, quote_attachment_id, "
                    "quote_content_type, quote_attachment_path, status, edited, read, "
                    "reply_to_message_id, batch_id, batch_index "
                    f"FROM messages WHERE protocol = ? "
                    f"AND contact_number IN ({placeholders}) "
                    f"{where_since}{order}",
                    params,
                ).fetchall()
            finally:
                connection.close()
        except sqlite3.Error:
            return []
        if limit is not None:
            rows.reverse()
        if len(keys) > 1:
            rows = _cross_key_dedup_rows(rows, contact_id)
        rows = _dedup_intra_key_rows(rows)
        if limit is not None:
            rows = rows[-limit:]

    messages = []
    for row in rows:
        attachment = None
        text = row["text"] or ""
        suppress_attachment_info_caption = False
        if row["attachment_id"]:
            attachment_id = row["attachment_id"]
            attachment_name = attachment_display_name(
                row["attachment_info"], attachment_id
            )
            attachment = {
                "attachment_id": attachment_id,
                "name": attachment_name,
                "type": (
                    row["content_type"]
                    or ("image/*" if row["msg_type"] == "image" else None)
                    or _infer_attachment_type(attachment_id, None)
                ),
                "media_kind": row["media_kind"],
            }
            logger.debug(
                "web messages proto=%s id=%s attachment_id=%s content_type=%s type=%s",
                row["protocol"],
                row["msg_id"] or str(row["id"]),
                attachment_id,
                row["content_type"],
                attachment["type"],
            )
            if (
                row["msg_type"] == "image"
                or (attachment["type"] or "").lower().startswith("image/")
            ) and text:
                # Caption: mantieni solo se reale; azzera placeholder media,
                # nome file e URL media WA ("Media: http://...").
                from models import is_media_quote_placeholder

                stripped = text.strip()
                info = str(row["attachment_info"] or "").strip()
                prefixed_info = bool(
                    info
                    and any(
                        stripped.casefold() == f"{label}: {info}".casefold()
                        for label in (
                            "immagine",
                            "image",
                            "media",
                            "video",
                            "audio",
                            "file",
                        )
                    )
                )
                if (
                    stripped == info
                    or is_media_quote_placeholder(text)
                    or prefixed_info
                    or (
                        protocol == "whatsapp"
                        and _is_whatsapp_synthetic_text(text, row["attachment_id"])
                    )
                ):
                    text = ""
            elif text and (
                str(row["media_kind"] or "").strip().lower()
                in {"video", "audio", "document", "sticker", "voice"}
                or str(row["msg_type"] or "").strip().lower()
                in {"attachment", "video", "audio", "document", "sticker", "voice"}
            ):
                from models import is_media_quote_placeholder

                stripped = text.strip()
                info = str(row["attachment_info"] or "").strip()
                prefixed_info = bool(info and stripped.startswith(f"{info}:"))
                if (
                    is_media_quote_placeholder(text)
                    or prefixed_info
                    or (
                        protocol == "whatsapp"
                        and _is_whatsapp_synthetic_text(text, row["attachment_id"])
                    )
                ):
                    text = ""
                    suppress_attachment_info_caption = prefixed_info
            elif protocol == "whatsapp" and text:
                from models import is_media_quote_placeholder

                if is_media_quote_placeholder(text) or _is_whatsapp_synthetic_text(
                    text, row["attachment_id"]
                ):
                    text = ""
            if (
                not text
                and row["attachment_info"]
                and not suppress_attachment_info_caption
            ):
                # Caption in attachment_info (es. invii WhatsApp che salvano la
                # caption qui, col text = URL media): usala se non è un nome file.
                info = str(row["attachment_info"] or "").strip()
                file_name = Path(str(row["attachment_id"] or "")).name
                if is_caption_like(info) and info != file_name:
                    text = info
        direction = "out" if row["is_mine"] else "in"
        messages.append(
            {
                "id": row["msg_id"] or str(row["id"]),
                "text": text,
                "direction": direction,
                "sender": row["sender"] or "",
                "timestamp": row["timestamp"],
                "attachment": attachment,
                "quote_text": row["quote_text"],
                "quote_timestamp": row["quote_timestamp"],
                "quote_author": row["quote_author"],
                "quote_attachment_id": row["quote_attachment_id"],
                "quote_content_type": row["quote_content_type"],
                "quote_thumb_url": _quote_thumb_url(row),
                "quote_media_placeholder": _quote_media_placeholder(row["quote_text"]),
                "status": (row["status"] or "sent") if direction == "out" else None,
                "read": bool(row["read"]),
                "edited": bool(row["edited"]),
                "edit_id": _message_edit_id(row),
                "batch_id": row["batch_id"],
                "batch_index": row["batch_index"],
            }
        )
        if row["msg_type"] == MSG_TYPE_SYSTEM:
            # Avviso di servizio (es. codice di sicurezza WhatsApp cambiato):
            # il client lo mostra centrato, fuori dalle bolle.
            messages[-1]["system"] = True

    # Le reazioni restano single-key (scelta di design): la read-union
    # cross-key copre i messaggi, non l'aggregazione reazioni.
    reactions_by_message = _aggregate_reactions(
        rows, _reactions_for_contact(protocol, contact_id)
    )
    for row, message in zip(rows, messages):
        reactions = reactions_by_message.get(int(row["id"]))
        if reactions:
            message["reactions"] = reactions
    return messages


def _quote_thumb_url(row: sqlite3.Row | dict[str, Any]) -> str | None:
    """URL miniatura della quote, calcolato dal server (cascata).

    1. ``quote_attachment_path`` sotto ``CACHE_DIR/quote-thumbs`` (thumb
       estratta) → ``/api/quote-media`` (endpoint dedicato anti-traversal);
    2. ``quote_attachment_path`` esistente e immagine/video (anche sotto la media
       root del protocollo, es. allegato originale Signal/Telegram/WhatsApp)
       → ``/api/media`` con il path;
    3. ``quote_attachment_id`` + content type media (o estensione nota per
       quote legacy senza content_type) → ``/api/media`` con l'id;
    altrimenti ``None`` (nessuna miniatura: placeholder testuale).
    """
    proto = str(row["protocol"])
    from protocols.db import CACHE_DIR

    thumbs_root = (Path(CACHE_DIR) / "quote-thumbs").resolve()
    image_extensions = (".jpg", ".jpeg", ".png", ".gif", ".webp")

    def _media_url(value: str) -> str:
        encoded = "/".join(url_quote(segment, safe="") for segment in value.split("/"))
        return f"api/media/{proto}/{encoded}?w=96"

    path = row["quote_attachment_path"]
    if path:
        resolved = Path(path).resolve()
        if resolved.parent == thumbs_root:
            return f"api/quote-media/{proto}/{row['id']}?w=96"
        content_type = (row["quote_content_type"] or "").lower()
        is_image = content_type.startswith("image/") or str(path).lower().endswith(
            image_extensions
        )
        is_video = content_type.startswith("video/") or resolved.suffix.lower() in (
            _VIDEO_EXTENSIONS
        )
        if (is_image or is_video) and resolved.is_file():
            return _media_url(str(path))
    quote_attachment_id = row["quote_attachment_id"]
    quote_content_type = (row["quote_content_type"] or "").lower()
    is_image = quote_content_type.startswith("image/") or (
        not quote_content_type
        and str(quote_attachment_id).lower().endswith(image_extensions)
    )
    is_video = quote_content_type.startswith("video/") or (
        not quote_content_type
        and Path(str(quote_attachment_id)).suffix.lower() in _VIDEO_EXTENSIONS
    )
    if quote_attachment_id and (is_image or is_video):
        return _media_url(str(quote_attachment_id))
    # 4. Fallback (reply inviate senza metadati quote persistiti): risolvi
    # il media quotato dalla stessa chat tramite quote_timestamp e usa il
    # suo attachment_id (il backend non persiste i metadati all'invio).
    quote_text = str(row["quote_text"] or "").lower()
    has_video_name = any(
        re.search(rf"{re.escape(extension)}\b", quote_text)
        for extension in _VIDEO_EXTENSIONS
    )
    has_image_name = bool(re.search(r"\.(?:jpe?g|png|gif|webp)\b", quote_text))
    media_kind = (
        "video"
        if quote_content_type.startswith("video/")
        or has_video_name
        or "🎬" in quote_text
        else "image"
        if quote_content_type.startswith("image/")
        or has_image_name
        or "🖼️" in quote_text
        else None
    )
    quoted = _quoted_media_attachment_id(
        proto, str(row["contact_number"]), row["quote_timestamp"], media_kind
    )
    if not quoted and media_kind in {"image", "video"}:
        # 5. Fallback per nome file: la quote testuale cita il file (es.
        # "IMG_1303.jpg — 🖼️ Immagine"); lo cerchiamo tra gli allegati della
        # chat quando il timestamp quotato non è stato persistito dal backend.
        quoted = _quoted_media_by_filename(
            proto,
            str(row["contact_number"]),
            row["quote_text"],
            row["timestamp"],
            media_kind,
        )
    if not quoted:
        # 6. Fallback per id messaggio quotato (Telegram/WhatsApp persistono
        # reply_to_message_id ma non sempre il timestamp quotato né l'allegato).
        quoted = _quoted_media_by_message_id(
            proto,
            str(row["contact_number"]),
            row["reply_to_message_id"],
            media_kind,
        )
    if quoted:
        return _media_url(quoted)
    return None


def _quoted_media_clause(kind: Literal["image", "video"] | None) -> str:
    image = (
        "content_type LIKE 'image/%' OR lower(attachment_id) LIKE '%.jpg' "
        "OR lower(attachment_id) LIKE '%.jpeg' OR lower(attachment_id) LIKE '%.png' "
        "OR lower(attachment_id) LIKE '%.gif' OR lower(attachment_id) LIKE '%.webp'"
    )
    video = "content_type LIKE 'video/%' OR " + " OR ".join(
        f"lower(attachment_id) LIKE '%{extension}'"
        for extension in sorted(_VIDEO_EXTENSIONS)
    )
    if kind == "image":
        return f"AND ({image})"
    if kind == "video":
        return f"AND ({video})"
    return f"AND ({image} OR {video})"


def _quoted_media_attachment_id(
    proto: str,
    contact_number: str,
    quote_ts: Any,
    kind: Literal["image", "video"] | None = None,
) -> str | None:
    """Attachment id del media quotato (stessa chat).

    Match PRIMA esatto sul timestamp: evita falsi positivi quando messaggi
    vicini (±2s) convivono (es. un'immagine appena inviata e la reply).
    La finestra ±2s viene usata solo se il timestamp esatto non esiste affatto
    (drift del protocollo).
    """
    if not quote_ts:
        return None
    import protocols.db as backend
    from protocols.db import _DB_LOCK

    media_clause = _quoted_media_clause(kind)
    try:
        with _DB_LOCK:
            connection = sqlite3.connect(backend.DB_FILE)
            try:
                row = connection.execute(
                    "SELECT attachment_id FROM messages "
                    "WHERE protocol = ? AND contact_number = ? "
                    "AND timestamp = ? AND attachment_id IS NOT NULL "
                    + media_clause
                    + " LIMIT 1",
                    (proto, contact_number, quote_ts),
                ).fetchone()
                if row is None:
                    exists = connection.execute(
                        "SELECT 1 FROM messages WHERE protocol = ? AND contact_number = ? "
                        "AND timestamp = ? LIMIT 1",
                        (proto, contact_number, quote_ts),
                    ).fetchone()
                    if exists:
                        # Il messaggio quotato esiste ma non è un media compatibile:
                        # niente miniatura (mai prendere messaggi vicini).
                        return None
                    row = connection.execute(
                        "SELECT attachment_id FROM messages "
                        "WHERE protocol = ? AND contact_number = ? "
                        "AND ABS(timestamp - ?) <= 2000 AND attachment_id IS NOT NULL "
                        + media_clause
                        + " ORDER BY ABS(timestamp - ?) LIMIT 1",
                        (proto, contact_number, quote_ts, quote_ts),
                    ).fetchone()
            finally:
                connection.close()
    except sqlite3.Error:
        return None
    if row is None or not row[0]:
        return None
    return str(row[0])


def _quoted_media_by_filename(
    proto: str,
    contact_number: str,
    quote_text: Any,
    quoter_ts: Any,
    kind: Literal["image", "video"] | None = None,
) -> str | None:
    """Attachment id del media quotato, risolto per nome file nella quote.

    Usato quando il backend non ha persistito ``quote_timestamp`` né metadati
    dell'allegato quotato (es. quote Signal a un'immagine): la ``quote_text``
    contiene il nome del file (``IMG_1303.jpg — 🖼️ Immagine``) che troviamo
    tra gli allegati della stessa chat, scegliendo quello più vicino temporalmente
    (solo prima del messaggio quotante, per non citare messaggi futuri).
    """
    if not quote_text:
        return None
    image_pattern = r"jpe?g|png|gif|webp"
    video_pattern = "|".join(
        extension.removeprefix(".") for extension in sorted(_VIDEO_EXTENSIONS)
    )
    extension_pattern = (
        image_pattern
        if kind == "image"
        else video_pattern
        if kind == "video"
        else f"{image_pattern}|{video_pattern}"
    )
    match = re.search(
        rf"([A-Za-z0-9][\w.\- ]*\.(?:{extension_pattern}))\b",
        str(quote_text),
        re.IGNORECASE,
    )
    if not match:
        return None
    filename = match.group(1).strip()
    if not filename or not quoter_ts:
        return None
    import protocols.db as backend
    from protocols.db import _DB_LOCK

    media_clause = _quoted_media_clause(kind)
    try:
        with _DB_LOCK:
            connection = sqlite3.connect(backend.DB_FILE)
            try:
                rows = connection.execute(
                    "SELECT attachment_id, timestamp FROM messages "
                    "WHERE protocol = ? AND contact_number = ? "
                    "AND attachment_id IS NOT NULL AND timestamp < ? "
                    "AND (attachment_info LIKE ? OR attachment_id LIKE ?) "
                    + media_clause
                    + " "
                    "ORDER BY timestamp DESC LIMIT 5",
                    (
                        proto,
                        contact_number,
                        int(quoter_ts),
                        f"%{filename}%",
                        f"%{filename}%",
                    ),
                ).fetchall()
            finally:
                connection.close()
    except sqlite3.Error:
        return None
    if not rows:
        return None
    # Più match: preferisci il più vicino al messaggio quotante.
    best = min(
        rows,
        key=lambda r: abs(int(r[1]) - int(quoter_ts)),
    )
    return str(best[0])


def _quoted_media_by_message_id(
    proto: str,
    contact_number: str,
    reply_to_message_id: Any,
    kind: Literal["image", "video"] | None = None,
) -> str | None:
    """Attachment id del media quotato per ``msg_id``.

    Telegram/WhatsApp persistono ``reply_to_message_id`` (= id del messaggio
    quotato, colonna ``msg_id``) ma non sempre il timestamp quotato: il match
    per id è preciso e copre anche i target non in cache all'arrivo.
    """
    if not reply_to_message_id:
        return None
    import protocols.db as backend
    from protocols.db import _DB_LOCK

    media_clause = _quoted_media_clause(kind)
    try:
        with _DB_LOCK:
            connection = sqlite3.connect(backend.DB_FILE)
            try:
                row = connection.execute(
                    "SELECT attachment_id FROM messages "
                    "WHERE protocol = ? AND contact_number = ? "
                    "AND attachment_id IS NOT NULL "
                    "AND (msg_id = ? OR msg_id LIKE '%_' || ?) "
                    + media_clause
                    + " LIMIT 1",
                    (
                        proto,
                        contact_number,
                        str(reply_to_message_id),
                        str(reply_to_message_id),
                    ),
                ).fetchone()
            finally:
                connection.close()
    except sqlite3.Error:
        return None
    if row is None or not row[0]:
        return None
    return str(row[0])


def _quote_media_placeholder(text: Any) -> bool:
    """True se ``text`` è un placeholder media tipizzato (o forma composta).

    Riusa il predicato della TUI così la web UI nasconde il placeholder quando
    la miniatura lo sostituisce (una caption reale resta visibile).
    """
    return is_media_quote_placeholder_composite(str(text or ""))


def _quoted_sender(proto: str, contact_id: str, quote_ts: Any) -> str | None:
    """Sender del messaggio quotato nella stessa chat (match esatto su timestamp)."""
    if not quote_ts:
        return None
    import protocols.db as backend
    from protocols.db import _DB_LOCK

    try:
        with _DB_LOCK:
            connection = sqlite3.connect(backend.DB_FILE)
            try:
                row = connection.execute(
                    "SELECT sender FROM messages "
                    "WHERE protocol = ? AND contact_number = ? AND timestamp = ? "
                    "LIMIT 1",
                    (proto, contact_id, quote_ts),
                ).fetchone()
            finally:
                connection.close()
    except sqlite3.Error:
        return None
    if row is None or not row[0]:
        return None
    return str(row[0])


def _group_sender_resolver(manager: Any, proto: str):
    """Risolvi il sender (JID/numero) al nome della rubrica, come la TUI.

    Mappa una volta la rubrica aggregata (id/phone/lid → display_name), poi per
    ogni sender: nome diretto, @lid→nome (alias rubrica), @lid→phone cache del
    backend → nome, @lid/numero locale → nome, @c.us → numero → nome.  Il
    fallback resta un numero leggibile (mai il JID grezzo con ``@lid``/``@c.us``).
    """
    phone_to_name: dict[str, str] = {}
    id_to_name: dict[str, str] = {}
    lid_to_name: dict[str, str] = {}
    try:
        book = manager.list_address_book_sync(protocols={proto}, force=False)
    except Exception:  # noqa: BLE001 — rubrica best-effort
        book = []
    for contact in book:
        name = str(contact.display_name or "")
        if not name:
            continue
        cid = str(contact.id or "")
        phone = str(contact.phone or "")
        extras = contact.extras if isinstance(contact.extras, dict) else {}
        if cid:
            id_to_name[cid] = name
            # A merged active chat keeps its ``@lid`` as id: that IS the alias a
            # group participant shows up with.
            if cid.endswith("@lid"):
                lid_to_name[cid] = name
        if phone:
            phone_to_name[phone] = name
        lid = str(extras.get("lid") or "")
        if lid:
            lid_to_name[lid] = name

    backend = None
    try:
        backend = manager.get(proto)
    except Exception:  # noqa: BLE001 — backend opzionale
        backend = None

    def resolve(sender: str) -> str:
        if not sender:
            return sender
        if sender in id_to_name:
            return id_to_name[sender]
        if sender in lid_to_name:
            return lid_to_name[sender]
        local = sender.split("@", 1)[0]

        if sender.endswith("@lid"):
            # 1) @lid → phone via cache backend (solo memoria), poi phone → nome.
            phone = ""
            try:
                phone = str(backend._jid_to_phone(sender) or "")
            except Exception:  # noqa: BLE001 — backend vecchi/mock senza il metodo
                phone = ""
            if phone and phone in phone_to_name:
                return phone_to_name[phone]
            # 2) Rubrica WAHA che espone la riga @lid: numero = cifre del lid.
            if local in phone_to_name:
                return phone_to_name[local]
            # 3) Rubrica dove l'id è il numero reale: ``<lid>@c.us``.
            if f"{local}@c.us" in id_to_name:
                return id_to_name[f"{local}@c.us"]
            # 4) Ultima spiaggia: il numero risolto (meglio del lid grezzo).
            if phone:
                return phone
        if sender.endswith("@c.us") and local in phone_to_name:
            return phone_to_name[local]
        if sender in phone_to_name:
            return phone_to_name[sender]
        if sender.endswith(("@lid", "@c.us")):
            # Fallback leggibile: numero senza @lid/@c.us (mai il JID grezzo).
            return local
        return sender

    return resolve


def _resolve_chat(manager: Any, protocol: str, contact_id: str) -> tuple[Any, Any, str]:
    """``(backend, contact, canonical_id)`` of a chat as the core knows it.

    A WhatsApp ghost id (``@c.us``) resolves to the active ``@lid`` contact;
    with no backend or no such contact the id is returned unchanged.
    """
    get_backend = getattr(manager, "get", None)
    backend = get_backend(protocol) if callable(get_backend) else None
    find_contact = getattr(backend, "find_contact", None)
    contact = find_contact(contact_id) if callable(find_contact) else None
    return backend, contact, (contact.id if contact is not None else contact_id)


def _refresh_history(
    backend: Any, protocol: str, canonical_id: str, limit: int
) -> bool:
    """Download the remote history of a chat into the backend (best effort).

    Returns ``True`` when the backend fetched it; a backend without
    ``fetch_history`` or a failing fetch (logged, swallowed) gives ``False``.
    """
    fetch = getattr(backend, "fetch_history", None)
    if fetch is None or not canonical_id:
        return False
    try:
        refreshed = fetch(canonical_id, limit)
    except Exception:
        logger.debug(
            "%s history refresh failed: %s", protocol, canonical_id, exc_info=True
        )
        return False
    logger.debug(
        "%s history refresh: %s -> %s",
        protocol,
        canonical_id,
        len(refreshed) if hasattr(refreshed, "__len__") else "?",
    )
    return True


def _union_cache_rows(
    cache: dict[str, list[dict[str, Any]]], contact: Any
) -> list[dict[str, Any]]:
    """The in-memory rows of *contact* across all its storage keys, deduped.

    WhatsApp history can sit under the ``@c.us`` and the ``@lid`` key of one
    chat; the rows are tagged with their key and collapsed with the same
    cross-key dedup the persisted read-union uses (canonical key first).
    """
    tagged = [
        (storage_key, row)
        for storage_key in contact_storage_keys(contact)
        for row in list(cache.get(storage_key, []))
    ]
    if not tagged:
        return []
    kept = dedup_cross_key(
        tagged,
        canonical_key=contact.id,
        key_of=lambda pair: pair[0],
        normalize=lambda pair: pair[1],
    )
    return [row for _key, row in kept]


def messages_window(
    manager: Any,
    protocol: str,
    contact_id: str,
    *,
    refresh: bool = False,
    refresh_limit: int = 50,
) -> tuple[list[dict[str, Any]], bool]:
    """One chat's canonical rows from the core's in-memory cache (TUI shape).

    The rows are the backend cache entries (``is_mine``, ``msg_type``, flat
    attachment/quote fields, delivery ``status``, ``read``) including those not
    persisted yet, unioned over the WhatsApp storage keys.  With *refresh* the
    remote history is downloaded first; the second item says whether that
    happened.  Blocking when refreshing: run in a worker thread.
    """
    backend, contact, canonical_id = _resolve_chat(manager, protocol, contact_id)
    if backend is None:
        return [], False
    refreshed = (
        _refresh_history(backend, protocol, canonical_id, refresh_limit)
        if refresh
        else False
    )
    cache = getattr(backend, "cache", None)
    if contact is None or not isinstance(cache, dict):
        rows = list(cache.get(canonical_id, [])) if isinstance(cache, dict) else []
    else:
        rows = _union_cache_rows(cache, contact)
    return [dict(row) for row in rows], refreshed


def messages_snapshot(manager: Any, protocol: str) -> dict[str, list[dict[str, Any]]]:
    """Every chat's canonical rows of one protocol, keyed by contact id.

    What a frontend mirrors when a backend becomes ready.  A WhatsApp chat
    appears once, under its canonical id, with its storage-key union deduped;
    cache keys no known contact owns are kept as they are.
    """
    get_backend = getattr(manager, "get", None)
    backend = get_backend(protocol) if callable(get_backend) else None
    cache = getattr(backend, "cache", None)
    if not isinstance(cache, dict):
        return {}
    result: dict[str, list[dict[str, Any]]] = {}
    if protocol == PROTOCOL_WHATSAPP:
        key_owner: dict[str, Any] = {}
        for contact in list(getattr(backend, "contacts", [])):
            for storage_key in contact_storage_keys(contact):
                key_owner.setdefault(storage_key, contact)
        processed: set[str] = set()
        for cid, rows in list(cache.items()):
            owner = key_owner.get(cid)
            if owner is None:
                result[cid] = [dict(row) for row in list(rows)]
            elif owner.id not in processed:
                processed.add(owner.id)
                result[owner.id] = [
                    dict(row) for row in _union_cache_rows(cache, owner)
                ]
    else:
        for cid, rows in list(cache.items()):
            result[cid] = [dict(row) for row in list(rows)]
    return result


def messages_page(
    manager: Any,
    protocol: str,
    contact_id: str,
    *,
    refresh: bool = False,
    limit: int | None = None,
    since: int | None = None,
) -> list[dict[str, Any]]:
    """One chat's messages for presentation (the ``messages.page`` operation).

    Canonicalizes *contact_id* first (a WhatsApp ghost ``@c.us`` id opens the
    history of the active ``@lid`` chat; the read-union still merges the DB
    history split between the two keys), refreshes the remote history when
    *refresh* is set or the protocol is Telegram (errors are logged and
    swallowed), reads and dedups the rows, attaches media versions and
    resolves group senders / quote authors.  Blocking: run in a worker thread.
    """
    backend, contact, canonical_id = _resolve_chat(manager, protocol, contact_id)
    if refresh or protocol == "telegram":
        _refresh_history(backend, protocol, canonical_id, 20)
    extra_keys = tuple(contact_storage_keys(contact)[1:]) if contact is not None else ()
    # Read from the canonical key: with a ghost id the extra keys are the ghost
    # itself, so reading by ``contact_id`` would skip the @lid history.
    result = _messages(
        protocol, canonical_id, extra_keys=extra_keys, limit=limit, since=since
    )
    _attach_media_versions(manager, protocol, result)
    is_group = contact_id.endswith("@g.us")
    if not is_group:
        try:
            for known in manager.list_contacts():
                if str(known.id) == contact_id and known.protocol == protocol:
                    is_group = bool(known.extras.get("is_group"))
                    break
        except (AttributeError, TypeError):
            is_group = False
    if is_group:
        resolver = _group_sender_resolver(manager, protocol)
        for message in result:
            message["is_group"] = True
            message["sender"] = resolver(message.get("sender") or "")
            qa = message.get("quote_author") or ""
            resolved_qa = resolver(qa)
            if qa and resolved_qa.endswith(("@lid", "@c.us")):
                # L'autore della quote non risolto: il messaggio quotato
                # nella stessa chat ha il sender (spesso già un nome,
                # es. pushname) — lo usiamo come autore.
                quoted_sender = _quoted_sender(
                    protocol, contact_id, message.get("quote_timestamp")
                )
                if quoted_sender:
                    resolved_qa = resolver(quoted_sender)
            message["quote_author"] = resolved_qa
    else:
        # Chat 1:1: l'autore della quote è sempre il contatto (o noi) —
        # ridondante, e il valore grezzo potrebbe essere un @lid.
        for message in result:
            message["quote_author"] = ""
    return result


def _message_row_for_edit(
    protocol: str,
    contact_id: str,
    message_id: str,
    extra_keys: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    import protocols.db as backend
    from protocols.db import _DB_LOCK

    keys = [contact_id, *(key for key in extra_keys if key)]
    placeholders = ", ".join("?" for _ in keys)
    with _DB_LOCK:
        try:
            connection = sqlite3.connect(backend.DB_FILE)
            connection.row_factory = sqlite3.Row
            try:
                row = connection.execute(
                    "SELECT id, msg_id, text, is_mine, timestamp, protocol, msg_type, "
                    "status FROM messages "
                    f"WHERE protocol = ? AND contact_number IN ({placeholders}) "
                    "AND (msg_id = ? OR (? = 'signal' AND msg_id IS NULL "
                    "AND timestamp = CAST(? AS INTEGER))) "
                    "ORDER BY CASE WHEN msg_id = ? THEN 0 ELSE 1 END LIMIT 1",
                    (
                        protocol,
                        *keys,
                        message_id,
                        protocol,
                        message_id,
                        message_id,
                    ),
                ).fetchone()
            finally:
                connection.close()
        except sqlite3.Error:
            logger.exception(
                "Web edit lookup database error: protocol=%s contact=%s",
                protocol,
                contact_id,
            )
            raise ApiError("internal", "Database error") from None
    return dict(row) if row else None


def _message_row_for_reaction(
    protocol: str, contact_id: str, message_id: str
) -> dict[str, Any] | None:
    import protocols.db as backend
    from protocols.db import _DB_LOCK

    with _DB_LOCK:
        try:
            connection = sqlite3.connect(backend.DB_FILE)
            connection.row_factory = sqlite3.Row
            try:
                row = connection.execute(
                    "SELECT id, msg_id, is_mine, timestamp, protocol FROM messages "
                    "WHERE protocol = ? AND contact_number = ? AND "
                    "(msg_id = ? "
                    "OR (? = 'signal' AND timestamp = CAST(? AS INTEGER)) "
                    "OR (? = 'signal' AND id = CAST(? AS INTEGER))) "
                    "ORDER BY CASE WHEN msg_id = ? THEN 0 ELSE 1 END LIMIT 1",
                    (
                        protocol,
                        contact_id,
                        message_id,
                        protocol,
                        message_id,
                        protocol,
                        message_id,
                        message_id,
                    ),
                ).fetchone()
            finally:
                connection.close()
        except sqlite3.Error:
            logger.exception(
                "Web reaction lookup database error: protocol=%s contact=%s",
                protocol,
                contact_id,
            )
            raise ApiError("internal", "Database error") from None
    return dict(row) if row else None


def _persist_message_edit(
    protocol: str, contact_id: str, message_id: str, new_text: str
) -> int:
    import protocols.db as backend
    from protocols.db import _DB_LOCK

    with _DB_LOCK:
        connection = sqlite3.connect(backend.DB_FILE)
        try:
            cursor = connection.execute(
                "UPDATE messages SET text = ?, edited = 1 "
                "WHERE protocol = ? AND contact_number = ? "
                "AND (msg_id = ? OR (? = 'signal' AND msg_id IS NULL "
                "AND timestamp = CAST(? AS INTEGER)))",
                (
                    new_text,
                    protocol,
                    contact_id,
                    message_id,
                    protocol,
                    message_id,
                ),
            )
            connection.commit()
            return cursor.rowcount
        finally:
            connection.close()


def _telegram_reaction_snapshot(
    contact_id: str, message_id: str, reaction_emoji: str
) -> list[dict[str, Any]]:
    message = next(
        (
            item
            for item in _messages("telegram", contact_id)
            if str(item["id"]) == message_id
        ),
        {},
    )
    snapshot: list[dict[str, Any]] = []
    for reaction in message.get("reactions", []):
        count = int(reaction.get("count") or 0)
        authors = [
            author
            for author in reaction.get("authors", [])
            if author not in {"You", "Tu"}
        ]
        if reaction.get("is_mine"):
            count -= 1
        if count > 0:
            snapshot.append(
                {
                    "emoji": str(reaction.get("emoji") or ""),
                    "count": count,
                    "is_mine": False,
                    "authors": authors,
                }
            )

    selected = next(
        (item for item in snapshot if item["emoji"] == reaction_emoji), None
    )
    if selected is None:
        selected = {
            "emoji": reaction_emoji,
            "count": 0,
            "is_mine": False,
            "authors": [],
        }
        snapshot.append(selected)
    selected["count"] += 1
    selected["is_mine"] = True
    selected["authors"].append("You")
    return snapshot
