"""Test per il backfill ``quote_text`` delle reply media Signal (design v3).

Copre: dry-run readonly senza backup, apply con backup + idempotenza, skip
auto-quote / quoted-not-found / quoted-ambiguous, caption reale conservata,
segnaposto derivato per identità sintetiche, e le note red-team N1/N2/N3.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import migrate_quote_text_backfill as backfill
from protocols import db

CONTACT = "+391234567890"
TARGET_TS = 1_700_000_000_000
REPLY_TS = TARGET_TS + 1_000


def _bootstrap() -> Path:
    db._init_db()
    return db.DB_FILE


def _add(*, text, is_mine, sender, timestamp, **kwargs):
    db._add_message_to_cache(
        CONTACT,
        text,
        is_mine,
        sender,
        timestamp,
        **kwargs,
    )


def _add_quoted_pair(
    *,
    target_text,
    target_ts=TARGET_TS,
    target_kwargs=None,
    reply_ts=REPLY_TS,
):
    kwargs = {
        "protocol": "signal",
        "msg_type": "text",
    }
    kwargs.update(target_kwargs or {})
    _add(
        text=target_text,
        is_mine=False,
        sender="Mario",
        timestamp=target_ts,
        **kwargs,
    )
    _add(
        text="risposta",
        is_mine=True,
        sender="You",
        timestamp=reply_ts,
        protocol="signal",
        quote_timestamp=target_ts,
        quote_text="",
        status="sent",
    )


def _quote_text(reply_ts=REPLY_TS):
    import sqlite3

    with sqlite3.connect(db.DB_FILE) as conn:
        row = conn.execute(
            "SELECT quote_text FROM messages WHERE timestamp = ? AND is_mine = 1",
            (reply_ts,),
        ).fetchone()
    return row[0] if row else None


def test_dry_run_writes_nothing_and_creates_no_backup():
    path = _bootstrap()
    _add_quoted_pair(
        target_text="Audio: voice.ogg: att-1",
        target_kwargs={
            "msg_type": "attachment",
            "attachment_info": "Audio: voice.ogg",
            "attachment_id": "voice.ogg",
            "content_type": "audio/ogg",
            "media_kind": "voice",
        },
    )

    assert backfill.run(path, apply=False, contact=None, limit=None) == 0
    assert _quote_text() == ""
    assert list(path.parent.glob("*.bak*")) == []


def test_apply_creates_backup_and_is_idempotent():
    path = _bootstrap()
    _add_quoted_pair(
        target_text="Audio: voice.ogg: att-1",
        target_kwargs={
            "msg_type": "attachment",
            "attachment_info": "Audio: voice.ogg",
            "attachment_id": "voice.ogg",
            "content_type": "audio/ogg",
            "media_kind": "voice",
        },
    )

    assert backfill.run(path, apply=True, contact=None, limit=None) == 0
    assert _quote_text() == "voice.ogg — 🎵 Audio"
    backups = list(path.parent.glob("*.bak*"))
    assert len(backups) == 1

    # Rerun: nothing left to update.
    assert backfill.run(path, apply=True, contact=None, limit=None) == 0
    assert _quote_text() == "voice.ogg — 🎵 Audio"


def test_auto_quote_is_skipped():
    path = _bootstrap()
    _add(
        text="self",
        is_mine=True,
        sender="You",
        timestamp=TARGET_TS,
        protocol="signal",
        quote_timestamp=TARGET_TS,
        quote_text="",
        status="sent",
    )

    assert backfill.run(path, apply=True, contact=None, limit=None) == 0
    assert _quote_text(TARGET_TS) == ""


def test_real_caption_is_preserved_and_used_as_quote_text():
    path = _bootstrap()
    _add_quoted_pair(
        target_text="Che bella!",
        target_kwargs={
            "msg_type": "image",
            "attachment_id": "p.jpg",
            "content_type": "image/jpeg",
            "media_kind": "image",
        },
    )

    assert backfill.run(path, apply=True, contact=None, limit=None) == 0
    assert _quote_text() == "Che bella!"


def test_quoted_not_found_is_skipped():
    path = _bootstrap()
    _add(
        text="risposta",
        is_mine=True,
        sender="You",
        timestamp=REPLY_TS,
        protocol="signal",
        quote_timestamp=TARGET_TS,  # no message with this ts
        quote_text="",
        status="sent",
    )

    assert backfill.run(path, apply=True, contact=None, limit=None) == 0
    assert _quote_text() == ""


def test_quoted_ambiguous_is_skipped():
    path = _bootstrap()
    # Two distinct rows sharing the quoted timestamp.
    _add(
        text="a",
        is_mine=False,
        sender="Mario",
        timestamp=TARGET_TS,
        protocol="signal",
        msg_type="text",
    )
    _add(
        text="b",
        is_mine=False,
        sender="Mario",
        timestamp=TARGET_TS,
        protocol="signal",
        msg_type="text",
    )
    _add(
        text="risposta",
        is_mine=True,
        sender="You",
        timestamp=REPLY_TS,
        protocol="signal",
        quote_timestamp=TARGET_TS,
        quote_text="",
        status="sent",
    )

    assert backfill.run(path, apply=True, contact=None, limit=None) == 0
    assert _quote_text() == ""


def test_n1_synthetic_placeholder_without_content_type_is_skipped():
    """N1: identità sintetica ma ``content_type`` NULL → nessuna scrittura."""
    path = _bootstrap()
    _add_quoted_pair(
        target_text="🖼️ Immagine",
        target_kwargs={
            "msg_type": "image",
            "attachment_info": "🖼️ Immagine",
            "attachment_id": "x.png",
            "content_type": None,
            "media_kind": "image",
        },
    )

    assert backfill.run(path, apply=True, contact=None, limit=None) == 0
    assert _quote_text() == ""


def test_n2_real_caption_ending_with_placeholder_is_preserved():
    """N2: una caption reale che termina con " — <placeholder>" va conservata.

    Con ``content_type`` valorizzato il classificatore sintetico non deve
    scambiare la caption per un segnaposto e sovrascriverla.
    """
    path = _bootstrap()
    _add_quoted_pair(
        target_text="La mia canzone — 🎵 Audio",
        target_kwargs={
            "msg_type": "attachment",
            "attachment_id": "att-uuid",
            "content_type": "audio/ogg",
            "media_kind": "audio",
        },
    )

    assert backfill.run(path, apply=True, contact=None, limit=None) == 0
    assert _quote_text() == "La mia canzone — 🎵 Audio"


def test_raw_audio_identity_is_never_written_verbatim():
    """Un text sintetico ``"🎵 Audio: <id>"`` non deve finire grezzo nel quote_text."""
    path = _bootstrap()
    _add_quoted_pair(
        target_text="🎵 Audio: abc123",
        target_kwargs={
            "msg_type": "attachment",
            "attachment_id": "abc123",
            "content_type": "audio/ogg",
            "media_kind": "voice",
            # attachment_info legacy NULL: il classificatore deve comunque
            # riconoscere l'identità sintetica dal pattern del testo.
        },
    )

    assert backfill.run(path, apply=True, contact=None, limit=None) == 0
    result = _quote_text()
    assert result != "🎵 Audio: abc123"
    assert result == "abc123 — 🎵 Audio"


def test_contact_and_limit_filters_are_applied():
    path = _bootstrap()
    _add_quoted_pair(
        target_text="Audio: voice.ogg: att-1",
        target_kwargs={
            "msg_type": "attachment",
            "attachment_info": "Audio: voice.ogg",
            "attachment_id": "voice.ogg",
            "content_type": "audio/ogg",
            "media_kind": "voice",
        },
    )

    # A non-matching contact must update nothing.
    assert backfill.run(path, apply=True, contact="+39999", limit=None) == 0
    assert _quote_text() == ""


@pytest.mark.parametrize(
    ("descriptor", "expected"),
    [
        (None, (None, None)),
        ("", (None, None)),
        ("image/png", ("image/png", None)),
        ("image/png:photo.png", ("image/png", "photo.png")),
        (
            "image/png:photo.png:/tmp/ph:oto.png",
            ("image/png", "photo.png"),
        ),
        (
            "image/png:name with spaces.png:/tmp/x",
            ("image/png", "name with spaces.png"),
        ),
    ],
)
def test_n3_descriptor_parser_is_robust(descriptor, expected):
    """N3: descriptor vuoto/malformato non deve rompere il parsing."""
    from models import parse_quote_attachment_descriptor

    assert parse_quote_attachment_descriptor(descriptor) == expected


# ─── Cond.7/8 — identità audio sintetiche vs caption reali ───────────────────


def test_bug2_audio_identity_with_attachment_id_derives_placeholder():
    path = _bootstrap()
    _add_quoted_pair(
        target_text="🎵 Audio: abc123",
        target_kwargs={
            "msg_type": "attachment",
            "attachment_id": "abc123",
            "content_type": "audio/ogg",
            "media_kind": "voice",
        },
    )

    assert backfill.run(path, apply=True, contact=None, limit=None) == 0
    assert _quote_text() == "abc123 — 🎵 Audio"


def test_bug2_audio_identity_without_attachment_id_uses_fallback():
    path = _bootstrap()
    _add_quoted_pair(
        target_text="🎵 Audio: abc123",
        target_kwargs={
            "msg_type": "attachment",
            "attachment_id": None,
            "content_type": "audio/ogg",
            "media_kind": "voice",
        },
    )

    assert backfill.run(path, apply=True, contact=None, limit=None) == 0
    assert _quote_text() == "🎵 Audio"


def test_bug2_audio_caption_with_spaces_is_preserved_with_attachment_id():
    path = _bootstrap()
    _add_quoted_pair(
        target_text="🎵 Audio: la mia canzone",
        target_kwargs={
            "msg_type": "attachment",
            "attachment_id": "abc123",
            "content_type": "audio/ogg",
            "media_kind": "voice",
        },
    )

    assert backfill.run(path, apply=True, contact=None, limit=None) == 0
    assert _quote_text() == "🎵 Audio: la mia canzone"


def test_bug2_audio_caption_with_spaces_is_preserved_without_attachment_id():
    path = _bootstrap()
    _add_quoted_pair(
        target_text="🎵 Audio: la mia canzone",
        target_kwargs={
            "msg_type": "attachment",
            "attachment_id": None,
            "content_type": "audio/ogg",
            "media_kind": "voice",
        },
    )

    assert backfill.run(path, apply=True, contact=None, limit=None) == 0
    assert _quote_text() == "🎵 Audio: la mia canzone"


def test_legacy_per_component_audio_identity_is_synthetic():
    path = _bootstrap()
    _add_quoted_pair(
        target_text="Audio: rec.ogg: abc123",
        target_kwargs={
            "msg_type": "attachment",
            "attachment_info": "Audio: rec.ogg",
            "attachment_id": "abc123",
            "content_type": "audio/ogg",
            "media_kind": "voice",
        },
    )

    assert backfill.run(path, apply=True, contact=None, limit=None) == 0
    assert _quote_text() == "abc123 — 🎵 Audio"


def test_63745_like_auto_quote_is_skipped():
    path = _bootstrap()
    _add(
        text="self 63745",
        is_mine=True,
        sender="You",
        timestamp=TARGET_TS,
        protocol="signal",
        msg_id="63745",
        quote_timestamp=TARGET_TS,
        quote_text="",
        status="sent",
    )

    assert backfill.run(path, apply=True, contact=None, limit=None) == 0
    assert _quote_text(TARGET_TS) == ""
