"""Live (wire) integration tests for bug #37 — quote of a media message.

Implements the empirical verification gate §10.2 of
``DESIGN_QUOTE_MEDIA_37_V2.md`` (E1–E7).  These tests talk to the REAL
backends (signal-cli daemon, WAHA REST, Telethon) and send REAL messages to the
test contact **"Roberto BMW"**, present on all three protocols.

They are ALWAYS skipped unless ``LIVE_TESTS=1`` is set.  The manual ingress
test (E4) is additionally skipped unless ``LIVE_MANUAL=1``.

Setup required:
  - Signal:   signal-cli daemon running + ``SIGNAL_USER_NUMBER`` configured.
  - WhatsApp: WAHA reachable (``WHATSAPP_API_URL`` / local ``:3005``), session
              WORKING and ``WHATSAPP_API_KEY`` configured.
  - Telegram: ``TELEGRAM_API_ID`` / ``TELEGRAM_API_HASH`` configured and the
              session file authorized.
  - "Roberto BMW" present on all three protocols, or override the target ids
    via ``LIVE_TARGET_SIGNAL`` / ``LIVE_TARGET_WHATSAPP`` /
    ``LIVE_TARGET_TELEGRAM``.

Run:
  LIVE_TESTS=1 .venv-test/bin/python -m pytest tests/test_live_quote_media.py -v
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backends.signal import SignalBackend
from backends.telegram import TelegramBackend
from backends.whatsapp import WhatsAppBackend
from backends.whatsapp_events import _event_from_message
from models import (
    MEDIA_QUOTE_PLACEHOLDERS,
    PROTOCOL_SIGNAL,
    PROTOCOL_TELEGRAM,
    PROTOCOL_WHATSAPP,
    ChatContact,
    is_media_quote_placeholder,
    media_quote_placeholder,
)

LIVE = os.environ.get("LIVE_TESTS") == "1"
LIVE_MANUAL = os.environ.get("LIVE_MANUAL") == "1"

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not LIVE, reason="live test: set LIVE_TESTS=1 to run"),
]


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _unique_marker(case: str) -> str:
    """Return a unique, recognisable marker prefixed to every live message."""
    return f"[live-test #37] {case} {int(time.time() * 1000)}"


def _resolve_contact(protocol: str, env_var: str, contacts) -> ChatContact | None:
    """Resolve "Roberto BMW" for a protocol, or ``None``.

    ``LIVE_TARGET_*`` (explicit id: phone for Signal, JID for WhatsApp, numeric
    id for Telegram) wins; otherwise the address book is filtered by a
    case-insensitive ``display_name`` containing both "roberto" and "bmw".
    """
    override = os.environ.get(env_var, "").strip()
    if override:
        return ChatContact(id=override, display_name=override, protocol=protocol)
    for c in contacts:
        name = (c.display_name or "").lower()
        if "roberto" in name and "bmw" in name:
            return c
    return None


def _latest_image(msgs, incoming_only: bool = False) -> dict | None:
    """Return the most recent ``msg_type == "image"`` message dict, or None."""
    candidates = [
        m
        for m in msgs
        if m.get("msg_type") == "image" and (not incoming_only or not m.get("is_mine"))
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda m: int(m.get("timestamp") or 0))


def _latest_text(msgs, incoming_only: bool = False) -> dict | None:
    """Return the most recent non-empty text message dict, or None."""
    candidates = [
        m
        for m in msgs
        if m.get("msg_type") == "text"
        and (m.get("text") or "").strip()
        and (not incoming_only or not m.get("is_mine"))
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda m: int(m.get("timestamp") or 0))


def _signal_image_by_caption(msgs):
    """Split incoming Signal images into (captionless, captioned, caption).

    Reuses ``_image_caption`` — the exact UI source of the wire-faithful caption
    — so E1/E2 assert against the same value the TUI would send on the wire.
    """
    from tui.chat_view import _image_caption

    captionless = captioned = caption_text = None
    ordered = sorted(msgs, key=lambda m: int(m.get("timestamp") or 0), reverse=True)
    for m in ordered:
        if m.get("is_mine") or m.get("msg_type") != "image":
            continue
        cap = _image_caption(
            m.get("text", ""),
            m.get("attachment_info"),
            m.get("attachment_id"),
            PROTOCOL_SIGNAL,
        )
        if cap:
            if captioned is None:
                captioned, caption_text = m, cap
        elif captionless is None:
            captionless = m
    return captionless, captioned, caption_text


def _wa_latest_image_id(raw_msgs) -> str | None:
    """Return the Baileys id of the most recent WAHA image message, or None.

    Reuses the tested ``_event_from_message`` parser so all WAHA media shapes
    (top-level attachments, nested ``*Message``, flat ``hasMedia``) are
    recognised.  Returns the ``id`` (Baileys id) used as ``reply_to``.
    """
    best: tuple[int, str] | None = None
    for raw in raw_msgs:
        if not isinstance(raw, dict):
            continue
        for ev in _event_from_message(raw, None):
            if ev.payload.get("msg_type") != "image":
                continue
            mid = ev.payload.get("id")
            if not mid:
                continue
            ts = int(ev.payload.get("timestamp") or 0)
            if best is None or ts > best[0]:
                best = (ts, str(mid))
    return best[1] if best else None


def _tg_fetch_contact(
    backend: TelegramBackend, contact_id: str, limit: int = 30
) -> None:
    """Fetch the recent history of one Telegram contact (test-only helper).

    Mirrors ``fetch_recent_history`` but scoped to a single contact so the live
    test does not iterate every dialog.
    """

    async def _fetch() -> None:
        eid = int(contact_id)
        entity = await backend._client.get_input_entity(eid)
        messages = await backend._client.get_messages(entity, limit=limit)
        for msg in messages:
            if msg is None or not getattr(msg, "date", None):
                continue
            evt = backend._message_to_chat_event(msg)
            if evt is None:
                continue
            backend.ingest_message(
                contact_id, evt.payload, evt.payload.get("timestamp", 0)
            )

    future = asyncio.run_coroutine_threadsafe(_fetch(), backend._loop)
    future.result(timeout=120)


# ─── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def signal_live():
    """Connect Signal (daemon already running), resolve contact, wrap send."""
    from backend import _is_daemon_running, _require_user_number

    try:
        user_number = _require_user_number()
    except RuntimeError as exc:
        pytest.skip(f"Signal non configurato: {exc}")
    if not _is_daemon_running():
        pytest.skip("signal-cli daemon non in esecuzione (avvialo e rilancia)")

    backend = SignalBackend(user_number=user_number)
    backend._use_daemon = True
    backend.cache = backend._load_protocol_cache()
    backend._load_contacts_rpc()
    contact = _resolve_contact(
        PROTOCOL_SIGNAL,
        "LIVE_TARGET_SIGNAL",
        backend.list_address_book_sync(force=True),
    )
    if contact is None:
        pytest.skip(
            'contatto "Roberto BMW" non trovato su Signal '
            "(override: LIVE_TARGET_SIGNAL=<numero>)"
        )

    captures: list[dict] = []
    original = backend._rpc.send_message

    def _wrap(*args, **kwargs):
        captures.append(
            {
                "message": kwargs.get("message") or (args[0] if args else None),
                "recipient": kwargs.get("recipient")
                or (args[1] if len(args) > 1 else None),
                "quote_timestamp": kwargs.get("quote_timestamp"),
                "quote_author": kwargs.get("quote_author"),
                "quote_message": kwargs.get("quote_message"),
            }
        )
        return original(*args, **kwargs)

    backend._rpc.send_message = _wrap
    try:
        yield backend, contact, captures
    finally:
        backend._rpc.send_message = original


@pytest.fixture
def whatsapp_live():
    """Connect WhatsApp (light), resolve contact, wrap REST send."""
    backend = WhatsAppBackend()
    if backend._rest is None:
        pytest.skip("WhatsApp API non configurata (WHATSAPP_API_URL)")
    backend._load_contacts()
    contact = _resolve_contact(
        PROTOCOL_WHATSAPP,
        "LIVE_TARGET_WHATSAPP",
        backend.list_address_book_sync(force=True),
    )
    if contact is None:
        pytest.skip(
            'contatto "Roberto BMW" non trovato su WhatsApp '
            "(override: LIVE_TARGET_WHATSAPP=<jid>)"
        )

    captures: list[dict] = []
    original = backend._rest.send_message

    def _wrap(to, text, **kwargs):
        captures.append(
            {
                "to": to,
                "text": text,
                "reply_to": kwargs.get("reply_to_message_id"),
                "quote_timestamp": kwargs.get("quote_timestamp"),
                "quote_author": kwargs.get("quote_author"),
                "quote_message": kwargs.get("quote_message"),
            }
        )
        return original(to, text, **kwargs)

    backend._rest.send_message = _wrap
    try:
        yield backend, contact, captures
    finally:
        backend._rest.send_message = original


@pytest.fixture
def telegram_live():
    """Connect Telegram, resolve contact, wrap Telethon send."""
    backend = TelegramBackend()
    if backend._api_id == 0 or not backend._api_hash:
        pytest.skip("Telegram non configurato (TELEGRAM_API_ID/TELEGRAM_API_HASH)")
    backend._connect_sync()
    if not backend._connected:
        pytest.skip("Telegram non connesso (sessione non autorizzata)")
    contact = _resolve_contact(
        PROTOCOL_TELEGRAM,
        "LIVE_TARGET_TELEGRAM",
        backend.list_address_book_sync(force=True),
    )
    if contact is None:
        pytest.skip(
            'contatto "Roberto BMW" non trovato su Telegram '
            "(override: LIVE_TARGET_TELEGRAM=<id numerico>)"
        )

    captures: list[dict] = []
    original = backend._client.send_message

    async def _wrap(entity, message, **kwargs):
        captures.append(
            {
                "entity": entity,
                "message": message,
                "reply_to": kwargs.get("reply_to"),
            }
        )
        return await original(entity, message, **kwargs)

    backend._client.send_message = _wrap
    try:
        yield backend, contact, captures
    finally:
        backend._client.send_message = original


# ─── E1–E3, E7 (Signal) ──────────────────────────────────────────────────────


def test_e1_signal_quote_media_no_caption_wire_empty(signal_live):
    """E1 — quote di un'immagine Signal SENZA caption → ``quoteMessage == ""``.

    Criterio §10.2 E1: mai il segnaposto (F2), mai omesso (F3); params loggati
    con ``quoteMessage == ""`` e ``quoteTimestamp/quoteAuthor`` coerenti.
    """
    backend, contact, captures = signal_live
    captionless, _, _ = _signal_image_by_caption(backend.cache.get(contact.id, []))
    if captionless is None:
        pytest.skip(
            "nessuna immagine senza caption da Roberto BMW su Signal: "
            "manda un'immagine e rilancia"
        )
    media_ts = int(captionless.get("timestamp") or 0)
    text = _unique_marker("E1 media senza caption")

    result = backend.send_message_sync(
        contact.id,
        text,
        quote_timestamp=media_ts,
        quote_author=contact.id,
        quote_message="",
    )

    assert result is not None  # nessuna eccezione dal daemon
    assert captures, "nessuna chiamata intercettata sul filo"
    call = captures[-1]
    assert call["quote_message"] == ""
    assert call["quote_timestamp"] == media_ts
    assert call["quote_author"] == contact.id
    assert call["message"] == text


def test_e2_signal_quote_media_with_caption_wire_is_caption(signal_live):
    """E2 — quote di un'immagine Signal CON caption → ``quoteMessage == caption``.

    Criterio §10.2 E2: quote ricevuta con immagine; ``quoteMessage == caption``.
    """
    backend, contact, captures = signal_live
    _, captioned, caption = _signal_image_by_caption(backend.cache.get(contact.id, []))
    if captioned is None:
        pytest.skip(
            "nessuna immagine con caption da Roberto BMW su Signal: "
            "manda un'immagine con caption e rilancia"
        )
    media_ts = int(captioned.get("timestamp") or 0)
    text = _unique_marker("E2 media con caption")

    backend.send_message_sync(
        contact.id,
        text,
        quote_timestamp=media_ts,
        quote_author=contact.id,
        quote_message=caption,
    )

    assert captures, "nessuna chiamata intercettata sul filo"
    assert captures[-1]["quote_message"] == caption


def test_e3_signal_text_reply_unchanged(signal_live):
    """E3 — reply a un TESTO Signal → ``quoteMessage == testo`` (regressione).

    Criterio §10.2 E3: la quote testuale resta invariata.
    """
    backend, contact, captures = signal_live
    target = _latest_text(backend.cache.get(contact.id, []), incoming_only=True)
    if target is None:
        pytest.skip(
            "nessun messaggio di testo da Roberto BMW su Signal: "
            "manda un messaggio di testo e rilancia"
        )
    media_ts = int(target.get("timestamp") or 0)
    quote = (target.get("text") or "").strip()
    text = _unique_marker("E3 reply a testo")

    backend.send_message_sync(
        contact.id,
        text,
        quote_timestamp=media_ts,
        quote_author=contact.id,
        quote_message=quote,
    )

    assert captures, "nessuna chiamata intercettata sul filo"
    assert captures[-1]["quote_message"] == quote


def test_e7_signal_retry_after_failure_wire_empty(signal_live):
    """E7 — retry post-riavvio di reply media → ``quoteMessage == ""`` (buco #3).

    Riproduce la normalizzazione di ``_retry_failed_message`` (§6.2 R2-bis): una
    reply media fallita ha il solo segnaposto persistito in ``quote_text``; al
    retry la chiave ``quote_wire_body`` viene ricostruita a ``""`` e il worker
    la mappa a ``quote_message == ""`` (mai il segnaposto sul filo).
    """
    backend, contact, captures = signal_live
    captionless, _, _ = _signal_image_by_caption(backend.cache.get(contact.id, []))
    if captionless is None:
        pytest.skip(
            "nessuna immagine senza caption da Roberto BMW su Signal: "
            "manda un'immagine e rilancia"
        )
    media_ts = int(captionless.get("timestamp") or 0)

    # Persistito dopo il primo tentativo fallito (solo display, non filo).
    persisted_quote_text = media_quote_placeholder("image")
    assert is_media_quote_placeholder(persisted_quote_text)
    reply_data = {"text": persisted_quote_text, "timestamp": media_ts}
    if is_media_quote_placeholder(persisted_quote_text):
        reply_data["quote_wire_body"] = ""
    # Regola R1/R2 del worker: chiave presente → body fedele (o "").
    quote_message = reply_data["quote_wire_body"] or ""
    assert quote_message == ""

    text = _unique_marker("E7 retry")
    backend.send_message_sync(
        contact.id,
        text,
        quote_timestamp=media_ts,
        quote_author=contact.id,
        quote_message=quote_message,
    )

    assert captures, "nessuna chiamata intercettata sul filo"
    assert captures[-1]["quote_message"] == ""


# ─── E5 (WhatsApp), E6 (Telegram) ────────────────────────────────────────────


def test_e5_whatsapp_quote_photo_reply_to_baileys_id(whatsapp_live):
    """E5 — reply a una foto WhatsApp → ``reply_to`` = id Baileys reale.

    Criterio §10.2 E5: il destinatario vede la quote nativa; sul filo viaggia
    solo ``reply_to`` = id Baileys del messaggio quotato (i ``quote_*`` sono
    ignorati da WAHA).
    """
    backend, contact, captures = whatsapp_live
    raw = backend._rest.list_messages(contact.id, limit=50)
    media_id = _wa_latest_image_id(raw)
    if media_id is None:
        pytest.skip(
            "nessuna foto da Roberto BMW su WhatsApp: manda una foto e rilancia"
        )
    text = _unique_marker("E5 reply a foto")

    backend.send_message_sync(
        contact.id,
        text,
        quote_timestamp=int(time.time() * 1000),
        quote_author=contact.id,
        quote_message="",
        reply_to_message_id=media_id,
    )

    assert captures, "nessuna chiamata intercettata sul filo"
    assert captures[-1]["reply_to"] == media_id


def test_e6_telegram_quote_photo_reply_to_numeric_id(telegram_live):
    """E6 — reply a una foto Telegram → ``reply_to`` = id numerico reale.

    Criterio §10.2 E6: il destinatario vede la quote nativa; sul filo viaggia
    solo ``reply_to`` = id numerico del messaggio quotato.
    """
    backend, contact, captures = telegram_live
    media = _latest_image(backend.cache.get(contact.id, []), incoming_only=True)
    if media is None:
        _tg_fetch_contact(backend, contact.id, limit=30)
        media = _latest_image(backend.cache.get(contact.id, []), incoming_only=True)
    if media is None:
        pytest.skip(
            "nessuna foto da Roberto BMW su Telegram: manda una foto e rilancia"
        )
    media_id = str(media.get("id") or "")
    if not media_id:
        pytest.skip("foto Telegram senza id numerico")
    text = _unique_marker("E6 reply a foto")

    backend.send_message_sync(
        contact.id,
        text,
        quote_message="",
        reply_to_message_id=media_id,
    )

    assert captures, "nessuna chiamata intercettata sul filo"
    assert captures[-1]["reply_to"] == int(media_id)


# ─── E4 (manual) ─────────────────────────────────────────────────────────────


@pytest.mark.skipif(
    not LIVE_MANUAL, reason="test manuale E4: set LIVE_MANUAL=1 per eseguirlo"
)
def test_e4_ingest_manual(signal_live):
    """E4 (MANUALE) — ingresso: la bolla quote media arriva col segnaposto.

    Procedura (dal client ufficiale di "Roberto BMW"):
      1. quota un'immagine/sticker/video/audio/documento (con e senza caption)
         verso questo account Signal;
      2. lascia girare questo test: fa poll di ``_rpc.receive`` fino a 120 s e
         asserisce che il ``quote_text`` dell'evento ricevuto sia uno dei
         segnaposto tipizzati (``MEDIA_QUOTE_PLACEHOLDERS``).

    Criterio §10.2 E4: la bolla ▎ mostra il segnaposto corretto, anche dopo
    ricarica/riavvio (qui copriamo solo l'ingresso live; la persistenza/cache
    è già coperta dai test unitari di §10.1).
    """
    backend, _contact, _captures = signal_live
    deadline = time.time() + 120
    seen: set[str] = set()
    while time.time() < deadline:
        try:
            raw = backend._rpc.receive()
        except Exception as _e:  # noqa: BLE001 — receive() può fallire se il daemon riparte
            raw = []
        for env in raw or []:
            if not isinstance(env, dict):
                continue
            envelope = env.get("envelope", env)
            for ev in backend.envelope_to_event(envelope or {}):
                if ev is None or ev.type != "message":
                    continue
                qt = ev.payload.get("quote_text")
                if qt in MEDIA_QUOTE_PLACEHOLDERS.values():
                    seen.add(qt)
        if seen:
            break
        time.sleep(2)
    assert seen, (
        "nessun evento con quote media ricevuto: dal client di Roberto BMW "
        "quota un'immagine/sticker/video e rilancia"
    )
