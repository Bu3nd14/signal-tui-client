"""Design v3 FIX C — ``_dedup_messages_by_id`` fonde quote/media nel survivor.

Copre l'item (h): coppia (sent + placeholder, read + empty) → una sola riga con
placeholder e status read; idempotenza; partizione divergente NON fusa; e
nessuna regressione sui batch multi-allegato (attachment_id distinto).
"""

from __future__ import annotations

import sqlite3

from protocols import db as db_mod

CONTACT = "42"
PROTOCOL = "signal"


def _pair(*, rowid_first: bool = True):
    # sent row carries the placeholder + quote/media metadata, empty elsewhere.
    db_mod._add_message_to_cache(
        CONTACT,
        "answer",
        True,
        "You",
        1_000,
        protocol=PROTOCOL,
        msg_id="7",
        status="sent",
        quote_text="voice.ogg — 🎵 Audio",
        quote_author="Mario",
        quote_attachment_id="voice.ogg",
        quote_content_type="audio/ogg",
        content_type="image/png",
        media_kind="image",
    )
    db_mod._add_message_to_cache(
        CONTACT,
        "answer",
        True,
        "You",
        1_001,
        protocol=PROTOCOL,
        msg_id="7",
        status="read",
    )


def _rows():
    conn = sqlite3.connect(db_mod.DB_FILE)
    conn.row_factory = sqlite3.Row
    try:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM messages WHERE protocol = ? AND contact_number = ?",
                (PROTOCOL, CONTACT),
            ).fetchall()
        ]
    finally:
        conn.close()


def test_sent_placeholder_and_read_empty_merge_into_read_survivor():
    _pair()

    assert db_mod._dedup_messages_by_id() == 1

    rows = _rows()
    assert len(rows) == 1
    survivor = rows[0]
    assert survivor["status"] == "read"
    assert survivor["quote_text"] == "voice.ogg — 🎵 Audio"
    assert survivor["quote_author"] == "Mario"
    assert survivor["quote_attachment_id"] == "voice.ogg"
    assert survivor["quote_content_type"] == "audio/ogg"
    assert survivor["content_type"] == "image/png"
    assert survivor["media_kind"] == "image"
    # id/timestamp/status del survivor mai toccati dalla fusione (il survivor
    # è la riga read, ts=1001: rank più alto, poi rowid più basso è irrilevante).
    assert survivor["timestamp"] == 1_001
    assert survivor["msg_id"] == "7"


def test_dedup_merge_is_idempotent():
    _pair()

    assert db_mod._dedup_messages_by_id() == 1
    assert db_mod._dedup_messages_by_id() == 0
    assert len(_rows()) == 1


def test_dedup_merges_edited_as_max():
    _pair()
    conn = sqlite3.connect(db_mod.DB_FILE)
    try:
        conn.execute(
            "UPDATE messages SET edited = 1 WHERE msg_id = '7' AND status = 'sent'"
        )
        conn.commit()
    finally:
        conn.close()

    assert db_mod._dedup_messages_by_id() == 1
    assert _rows()[0]["edited"] == 1


def test_dedup_never_overwrites_existing_survivor_values():
    # read survivor already owns a real caption: it must win over the sent row.
    db_mod._add_message_to_cache(
        CONTACT,
        "answer",
        True,
        "You",
        2_000,
        protocol=PROTOCOL,
        msg_id="8",
        status="read",
        quote_text="caption reale",
    )
    db_mod._add_message_to_cache(
        CONTACT,
        "answer",
        True,
        "You",
        2_001,
        protocol=PROTOCOL,
        msg_id="8",
        status="sent",
        quote_text="fallback — 🎵 Audio",
    )

    assert db_mod._dedup_messages_by_id() == 1
    assert _rows()[0]["quote_text"] == "caption reale"


def test_divergent_partition_is_not_merged():
    db_mod._add_message_to_cache(
        CONTACT,
        "same text",
        False,
        "Mario",
        1_000,
        protocol=PROTOCOL,
        msg_id="9",
    )
    # Span beyond the echo window: an incorrectly shared id, never merged.
    db_mod._add_message_to_cache(
        CONTACT,
        "same text",
        False,
        "Mario",
        1_000 + db_mod._ECHO_MATCH_WINDOW_MS + 1,
        protocol=PROTOCOL,
        msg_id="9",
    )

    assert db_mod._dedup_messages_by_id() == 0
    assert len(_rows()) == 2


def test_multi_attachment_batch_rows_are_not_collapsed():
    # Same msg_id/text but distinct attachment slots: distinct rows.
    for attachment_id in ("attachment-A", "attachment-B"):
        db_mod._add_message_to_cache(
            CONTACT,
            "",
            True,
            "You",
            3_000,
            protocol=PROTOCOL,
            msg_id="10",
            status="sent",
            attachment_id=attachment_id,
            media_kind="image",
            content_type="image/png",
        )

    assert db_mod._dedup_messages_by_id() == 0
    assert len(_rows()) == 2


def test_same_status_tie_break_merges_from_higher_rowid_into_lower():
    # Lower rowid survives (empty quote); higher rowid carries the placeholder.
    db_mod._add_message_to_cache(
        CONTACT,
        "answer",
        True,
        "You",
        4_000,
        protocol=PROTOCOL,
        msg_id="11",
        status="sent",
    )
    db_mod._add_message_to_cache(
        CONTACT,
        "answer",
        True,
        "You",
        4_001,
        protocol=PROTOCOL,
        msg_id="11",
        status="sent",
        quote_text="voice.ogg — 🎵 Audio",
    )

    assert db_mod._dedup_messages_by_id() == 1
    rows = _rows()
    assert len(rows) == 1
    assert rows[0]["id"] == 1  # lower rowid survived
    assert rows[0]["quote_text"] == "voice.ogg — 🎵 Audio"
