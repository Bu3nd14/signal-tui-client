"""Unit test dell'helper puro ``models.pick_survivor_and_merge``.

Copre la regola condivisa di selezione del survivor (status rank → timestamp
reale → rowid), il fill senza sovrascrittura, ``edited=max``, il fold del
``sender``, il divieto di sovrascrivere i campi intoccabili e il determinismo
su input disordinato.
"""

from __future__ import annotations

import pytest

from models import (
    _MERGE_IMMUTABLE_FIELDS,
    MERGE_FILL_FIELDS,
    pick_survivor_and_merge,
)

OVERLAPPING_OVERRIDES = set(MERGE_FILL_FIELDS) | {
    "timestamp",
    "status",
    "sender",
    "edited",
}


def _row(**overrides) -> dict:
    """Riga minima omogenea per l'helper (mapping puro, non mutato)."""
    row = {
        "id": 1,
        "msg_id": "m1",
        "text": "",
        "is_mine": False,
        "sender": "",
        "timestamp": 0,
        "status": "read",
        "edited": 0,
        "content_type": None,
        "media_kind": None,
        "quote_text": None,
        "quote_timestamp": None,
        "quote_author": None,
        "reply_to_message_id": None,
        "quote_attachment_id": None,
        "quote_attachment_path": None,
        "quote_content_type": None,
        "attachment_info": None,
        "attachment_id": "att-1",
        "contact_number": "c",
        "protocol": "whatsapp",
        "read": 0,
        "batch_id": None,
        "batch_index": None,
    }
    row.update(overrides)
    return row


def test_survivor_prefers_status_rank_over_timestamp():
    rows = [
        _row(id=1, status="sent", timestamp=999),
        _row(id=2, status="read", timestamp=1),
    ]
    index, _ = pick_survivor_and_merge(rows)
    assert index == 1


def test_survivor_real_timestamp_beats_zero():
    rows = [
        _row(id=1, status="read", timestamp=0),
        _row(id=2, status="read", timestamp=1234),
    ]
    index, _ = pick_survivor_and_merge(rows)
    assert index == 1


def test_survivor_rowid_tie_breaker_is_ascending():
    rows = [
        _row(id=7, status="read", timestamp=50),
        _row(id=3, status="read", timestamp=50),
    ]
    index, _ = pick_survivor_and_merge(rows)
    assert index == 1


def test_fill_completes_missing_without_overwriting():
    rows = [
        _row(
            id=1,
            content_type="audio/ogg",
            media_kind="audio",
            quote_text="keep",
            timestamp=10,
        ),
        _row(
            id=2,
            content_type="video/mp4",
            media_kind="voice",
            quote_text="other",
            quote_author="Mario",
            attachment_info="f.pdf",
        ),
    ]
    index, overrides = pick_survivor_and_merge(rows)

    assert index == 0
    assert overrides["content_type"] == "audio/ogg"
    assert overrides["media_kind"] == "audio"
    assert overrides["quote_text"] == "keep"
    assert overrides["quote_author"] == "Mario"
    assert overrides["attachment_info"] == "f.pdf"
    assert "timestamp" not in overrides


def test_timestamp_filled_only_when_survivor_zero():
    rows = [
        _row(id=1, status="read", timestamp=0),
        _row(id=2, status="sent", timestamp=888),
    ]
    index, overrides = pick_survivor_and_merge(rows)

    assert index == 0
    assert overrides["timestamp"] == 888


def test_edited_is_max_and_sender_folds_display_names():
    rows = [
        _row(id=1, edited=0, sender="123593484054624@lid"),
        _row(id=2, edited=1, sender="Roberto Di Maggio"),
    ]
    index, overrides = pick_survivor_and_merge(rows)

    assert index == 0
    assert overrides["edited"] == 1
    assert overrides["sender"] == "Roberto Di Maggio"


def test_sender_fold_keeps_survivor_display_on_tie():
    rows = [_row(id=1, sender="Alice"), _row(id=2, sender="Bob")]
    _, overrides = pick_survivor_and_merge(rows)
    assert overrides["sender"] == "Alice"


def test_sender_fold_keeps_first_raw_when_no_display():
    rows = [_row(id=1, sender="1@lid"), _row(id=2, sender="2@lid")]
    _, overrides = pick_survivor_and_merge(rows)
    assert overrides["sender"] == "1@lid"


def test_immutable_fields_never_in_overrides():
    rows = [
        _row(
            id=1,
            text="A",
            msg_id="m1",
            attachment_id="a",
            read=0,
            batch_id="b1",
            batch_index=0,
        ),
        _row(
            id=2,
            text="B",
            msg_id="m2",
            attachment_id="a2",
            read=1,
            batch_id="b2",
            batch_index=1,
        ),
    ]
    _, overrides = pick_survivor_and_merge(rows)

    assert _MERGE_IMMUTABLE_FIELDS.isdisjoint(overrides)
    assert set(overrides) <= OVERLAPPING_OVERRIDES


def test_input_is_not_mutated():
    rows = [
        _row(id=1, sender="1@lid", content_type=None),
        _row(id=2, sender="Roberto", content_type="audio/ogg"),
    ]
    snapshot = [dict(row) for row in rows]

    pick_survivor_and_merge(rows)

    assert rows == snapshot


def test_deterministic_on_shuffled_input():
    base = [
        _row(id=1, status="sent", timestamp=100, sender="1@lid"),
        _row(
            id=2,
            status="read",
            timestamp=0,
            sender="Roberto",
            content_type="audio/ogg",
        ),
        _row(id=3, status="read", timestamp=200, sender="2@lid", quote_text="q"),
    ]
    index_a, overrides_a = pick_survivor_and_merge(base)
    index_b, overrides_b = pick_survivor_and_merge([base[2], base[0], base[1]])

    assert base[index_a]["id"] == 3
    assert [base[2], base[0], base[1]][index_b]["id"] == 3
    assert overrides_a == overrides_b


def test_empty_input_raises():
    with pytest.raises(ValueError):
        pick_survivor_and_merge([])


# ─── prefer_attachment (N1) ──────────────────────────────────────────────────


def test_prefer_attachment_restricts_survivor_to_media_row():
    """Il pool del survivor si restringe alle righe con allegato, ma fill,
    timestamp e ``edited`` restano calcolati sull'intero gruppo."""
    rows = [
        _row(
            id=1,
            text="una caption",
            attachment_id="",
            status="read",
            timestamp=100,
            quote_text="domanda",
        ),
        _row(
            id=2,
            text="",
            attachment_id="a.jpg",
            status="sent",
            timestamp=50,
            quote_author="Mario",
        ),
    ]

    index, overrides = pick_survivor_and_merge(rows, prefer_attachment=True)

    assert index == 1
    assert rows[index]["attachment_id"] == "a.jpg"
    # fill dal gruppo intero, senza sovrascrittura
    assert overrides["quote_text"] == "domanda"
    assert overrides["quote_author"] == "Mario"
    assert "text" not in overrides
    assert "attachment_id" not in overrides
    assert "read" not in overrides


def test_prefer_attachment_noop_when_all_media_or_all_text():
    """Se tutte le righe hanno allegato (o nessuna) il pool coincide col gruppo:
    l'esito è identico al default."""
    all_media = [
        _row(id=1, attachment_id="a.jpg", status="sent", timestamp=10),
        _row(id=2, attachment_id="b.jpg", status="read", timestamp=20),
    ]
    assert (
        pick_survivor_and_merge(all_media, prefer_attachment=True)[0]
        == (pick_survivor_and_merge(all_media)[0])
    )

    all_text = [
        _row(id=1, text="x", attachment_id="", status="sent", timestamp=10),
        _row(id=2, text="", attachment_id="", status="read", timestamp=20),
    ]
    assert (
        pick_survivor_and_merge(all_text, prefer_attachment=True)[0]
        == (pick_survivor_and_merge(all_text)[0])
    )


def test_prefer_attachment_default_false_keeps_legacy_order():
    """Il default resta invariato: vince il rank di status, non l'allegato."""
    rows = [
        _row(id=1, text="caption", attachment_id="", status="read", timestamp=100),
        _row(id=2, text="", attachment_id="a.jpg", status="sent", timestamp=50),
    ]

    assert pick_survivor_and_merge(rows)[0] == 0
    assert pick_survivor_and_merge(rows, prefer_attachment=False)[0] == 0


def test_survivor_divergent_status_with_ts_zero_twin():
    """R1: lo status precede il timestamp.  Un twin a ts=0 con rank superiore
    diventa survivor; l'override ne corregge il timestamp, ma ``read`` resta
    quello del twin (immutabile) — rischio falso-unread noto, non fixato."""
    rows = [
        _row(id=1, status="sent", timestamp=5000, read=1),
        _row(id=2, status="read", timestamp=0, read=0),
    ]

    index, overrides = pick_survivor_and_merge(rows)

    assert index == 1
    assert overrides["timestamp"] == 5000
    assert "read" not in overrides
