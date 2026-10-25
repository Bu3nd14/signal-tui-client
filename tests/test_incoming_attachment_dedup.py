"""Regression tests for the WhatsApp incoming double-attachment bug.

A WhatsApp incoming media (e.g. a voice note) may be persisted twice, both
rows sharing the same ``msg_id`` and ``attachment_id`` (``is_mine=0``):

* the synthetic identity row (``text = "Media: <id>"``, lower timestamp);
* the blank row (``text = ""``, higher timestamp), often with a different
  ``sender`` (raw JID vs resolved display name).

``_dedup_messages_by_id`` does not fuse them because it also partitions by
``text``.  This suite covers the boot fusion in
``protocols.db._dedup_incoming_attachment_mirrors`` invoked by
``run_startup_maintenance``.

Each test maps to a design point (1..12 in the approved plan).
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

from models import attachment_display_name
from protocols import db
from server.messages import _messages

CONTACT = "125593484054624@lid"
MSG_ID = "true_125593484054624@lid_ABC"
MEDIA_ID = "http://localhost:3000/api/files/default/true_125593484054624@lid_ABC.ogg"
MEDIA_ID_2 = "http://localhost:3000/api/files/default/true_125593484054624@lid_DEF.ogg"
TS_A = 1_700_000_000_000
TS_B = TS_A + 1_500


def _add(
    *,
    text: str,
    attachment_id: str = MEDIA_ID,
    ts: int,
    msg_id: str = MSG_ID,
    contact: str = CONTACT,
    protocol: str = "whatsapp",
    is_mine: bool = False,
    sender: str = "",
    status: str | None = None,
    msg_type: str = "attachment",
    content_type: str | None = "audio/ogg",
    media_kind: str | None = "voice",
    attachment_info: str | None = None,
    batch_id: str | None = None,
    batch_index: int | None = None,
    quote_text: str | None = None,
    quote_timestamp: int | None = None,
    quote_author: str | None = None,
    reply_to_message_id: str | None = None,
    quote_attachment_id: str | None = None,
    quote_attachment_path: str | None = None,
    quote_content_type: str | None = None,
):
    return db._add_message_to_cache(
        contact,
        text,
        is_mine,
        sender,
        ts,
        quote_text=quote_text,
        msg_type=msg_type,
        attachment_id=attachment_id,
        content_type=content_type,
        protocol=protocol,
        msg_id=msg_id,
        status=status,
        quote_timestamp=quote_timestamp,
        quote_author=quote_author,
        reply_to_message_id=reply_to_message_id,
        quote_attachment_id=quote_attachment_id,
        quote_attachment_path=quote_attachment_path,
        quote_content_type=quote_content_type,
        media_kind=media_kind,
        attachment_info=attachment_info,
        batch_id=batch_id,
        batch_index=batch_index,
    )


def _all_rows() -> list[dict]:
    with closing(sqlite3.connect(db.DB_FILE)) as conn, conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, text, sender, status, edited, timestamp, attachment_info, "
            "batch_id, batch_index, "
            "content_type, media_kind, quote_text, quote_author, quote_timestamp, "
            "reply_to_message_id, quote_attachment_id, quote_attachment_path, "
            "quote_content_type FROM messages ORDER BY rowid"
        ).fetchall()
    return [dict(row) for row in rows]


def _count() -> int:
    with closing(sqlite3.connect(db.DB_FILE)) as conn, conn:
        return conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]


# ─── 1. Synthetic + blank pair is fused, text becomes "" ─────────────────────


def test_boot_dedup_fuses_synthetic_and_blank_pair():
    _add(text=f"Media: {MEDIA_ID}", ts=TS_A, sender="125593484054624@lid")
    _add(text="", ts=TS_B, sender="Roberto Di Maggio")

    assert db._dedup_incoming_attachment_mirrors() == 1

    rows = _all_rows()
    assert len(rows) == 1
    assert rows[0]["text"] == ""


# ─── 2. Highest status wins, NULL never downgrades ───────────────────────────


def test_boot_dedup_keeps_highest_status_and_no_downgrade():
    _add(text="", ts=TS_A, status="read", sender="Roberto")
    _add(text=f"Media: {MEDIA_ID}", ts=TS_B, status=None, sender="125593484054624@lid")

    assert db._dedup_incoming_attachment_mirrors() == 1

    row = _all_rows()[0]
    assert row["text"] == ""
    assert row["status"] == "read"


# ─── 3. Idempotency ──────────────────────────────────────────────────────────


def test_boot_dedup_is_idempotent():
    _add(text=f"Media: {MEDIA_ID}", ts=TS_A)
    _add(text="", ts=TS_B)

    assert db._dedup_incoming_attachment_mirrors() == 1
    assert db._dedup_incoming_attachment_mirrors() == 0
    assert _count() == 1


# ─── 4. A real caption is never lost ─────────────────────────────────────────


def test_boot_dedup_skips_real_caption_pair():
    _add(text=f"Media: {MEDIA_ID}", ts=TS_A)
    _add(text="bella registrazione", ts=TS_B)

    assert db._dedup_incoming_attachment_mirrors() == 0
    assert _count() == 2


# ─── 5. Two synthetic rows are left intact ───────────────────────────────────


def test_boot_dedup_skips_two_synthetic_rows():
    _add(text=f"Media: {MEDIA_ID}", ts=TS_A)
    _add(text=f"Media: {MEDIA_ID}", ts=TS_B)

    assert db._dedup_incoming_attachment_mirrors() == 0
    assert _count() == 2


# ─── 6. Outside the echo window → no fusion ──────────────────────────────────


def test_boot_dedup_outside_window_not_fused():
    """La finestra di echo vale per due timestamp NON-zero: due ts reali
    divergenti oltre ``_ECHO_MATCH_WINDOW_MS`` non vengono fusi.  Le coppie
    con ts=0/NULL sono coperte dai test dedicati (diff calcolata solo sui ts
    reali)."""
    _add(text=f"Media: {MEDIA_ID}", ts=TS_A)
    _add(text="", ts=TS_A + db._ECHO_MATCH_WINDOW_MS + 1)

    assert db._dedup_incoming_attachment_mirrors() == 0
    assert _count() == 2


# ─── 7. Ambiguous three-row group is skipped ─────────────────────────────────


def test_boot_dedup_skips_ambiguous_three_row_group():
    _add(text=f"Media: {MEDIA_ID}", ts=TS_A)
    _add(text="", ts=TS_A + 10)
    _add(text="caption reale", ts=TS_A + 20)

    assert db._dedup_incoming_attachment_mirrors() == 0
    assert _count() == 3


# ─── 8. Same msg_id + distinct attachment_ids → multi-attachment kept ────────


def test_boot_dedup_skips_distinct_attachment_ids():
    _add(text=f"Media: {MEDIA_ID}", attachment_id=MEDIA_ID, ts=TS_A)
    _add(text=f"Media: {MEDIA_ID_2}", attachment_id=MEDIA_ID_2, ts=TS_B)

    assert db._dedup_incoming_attachment_mirrors() == 0
    assert _count() == 2


# ─── 9. batch slot of the survivor is preserved ──────────────────────────────


def test_boot_dedup_preserves_survivor_batch_slot():
    # Il survivor è quello col rank di status più alto (read > delivered):
    # la riga synth resta survivor e conserva il proprio slot batch, che
    # l'helper non tocca mai (intoccabile).
    _add(
        text=f"Media: {MEDIA_ID}",
        ts=TS_A,
        status="read",
        batch_id="batch-7",
        batch_index=0,
    )
    _add(text="", ts=TS_B, status="delivered", batch_id="batch-9", batch_index=1)

    assert db._dedup_incoming_attachment_mirrors() == 1

    row = _all_rows()[0]
    assert row["text"] == ""
    assert row["batch_id"] == "batch-7"
    assert row["batch_index"] == 0


# ─── 10. quote/media fields merged, edited = max ─────────────────────────────


def test_boot_dedup_merges_quote_fields_and_edited():
    # ``_init_db()`` normalizza a 'audio' una ``media_kind IS NULL`` su un
    # allegato ``.ogg``/``.oga`` (protocols/db.py ~riga 168): la prima riga
    # (survivor per rank di status: read > delivered) ha quindi
    # ``media_kind='audio'``, non NULL.  Il merge completa i campi mancanti
    # senza mai sovrascrivere, quindi conserva 'audio' e NON adotta il 'voice'
    # del twin.  La impostiamo esplicitamente per rendere l'aspettativa
    # deterministica.
    _add(
        text=f"Media: {MEDIA_ID}",
        ts=TS_A,
        status="read",
        content_type=None,
        media_kind="audio",
    )
    _add(
        text="",
        ts=TS_B,
        status="delivered",
        content_type="audio/ogg",
        media_kind="voice",
        quote_text="domanda",
        quote_timestamp=123,
        quote_author="Mario",
        reply_to_message_id="q1",
        quote_attachment_id="att-q",
        quote_attachment_path="/tmp/q.png",
        quote_content_type="image/png",
    )
    with closing(sqlite3.connect(db.DB_FILE)) as conn, conn:
        conn.execute("UPDATE messages SET edited = 1 WHERE text = ''")

    assert db._dedup_incoming_attachment_mirrors() == 1

    row = _all_rows()[0]
    assert row["text"] == ""
    assert row["content_type"] == "audio/ogg"
    assert row["media_kind"] == "audio"
    assert row["quote_text"] == "domanda"
    assert row["quote_author"] == "Mario"
    assert row["quote_timestamp"] == 123
    assert row["reply_to_message_id"] == "q1"
    assert row["quote_attachment_id"] == "att-q"
    assert row["quote_attachment_path"] == "/tmp/q.png"
    assert row["quote_content_type"] == "image/png"
    assert row["edited"] == 1


# ─── 11. Preferences display sender over raw JID ─────────────────────────────


def test_boot_dedup_prefers_display_sender():
    _add(text=f"Media: {MEDIA_ID}", ts=TS_A, sender="125593484054624@lid")
    _add(text="", ts=TS_B, sender="Roberto Di Maggio")

    assert db._dedup_incoming_attachment_mirrors() == 1
    assert _all_rows()[0]["sender"] == "Roberto Di Maggio"


# ─── 12. run_startup_maintenance invokes the new dedup ───────────────────────


def test_run_startup_maintenance_invokes_incoming_dedup(monkeypatch):
    _add(text=f"Media: {MEDIA_ID}", ts=TS_A)
    _add(text="", ts=TS_B)

    removed: list[int] = []
    real = db._dedup_incoming_attachment_mirrors

    def spy() -> int:
        result = real()
        removed.append(result)
        return result

    monkeypatch.setattr(db, "_dedup_incoming_attachment_mirrors", spy)

    db.run_startup_maintenance(force=True)
    db.run_startup_maintenance(force=True)

    assert removed == [1, 0]
    assert _count() == 1


# ─── 13. R2: finestra echo con wildcard ts=0/NULL ────────────────────────────


def test_boot_dedup_ts_zero_twin_fused():
    """(ts=0, ts reale) fondono: la finestra ignora i ts 0/NULL (diff 0) e il
    survivor è la riga reale."""
    _add(text=f"Media: {MEDIA_ID}", ts=0)
    _add(text="", ts=TS_A)

    assert db._dedup_incoming_attachment_mirrors() == 1

    row = _all_rows()[0]
    assert row["timestamp"] == TS_A
    assert row["text"] == ""


def test_boot_dedup_both_ts_zero_fused():
    """Entrambi ts=0: diff 0 -> fondono; il survivor è la prima riga (rowid)."""
    _add(text=f"Media: {MEDIA_ID}", ts=0)
    _add(text="", ts=0)

    assert db._dedup_incoming_attachment_mirrors() == 1

    rows = _all_rows()
    assert len(rows) == 1
    assert rows[0]["text"] == ""


def test_boot_dedup_survivor_prefers_real_timestamp():
    """Un ts reale batte un ts=0 anche se la riga a ts=0 ha rowid minore."""
    _add(text="", ts=0)
    _add(text=f"Media: {MEDIA_ID}", ts=TS_A)

    assert db._dedup_incoming_attachment_mirrors() == 1

    row = _all_rows()[0]
    assert row["timestamp"] == TS_A
    assert row["text"] == ""


# ─── 14. R3/R4: predicato di fusione path A ──────────────────────────────────


def test_boot_dedup_fuses_synth_and_filename_twin():
    """R3: il twin il cui ``text`` ripete il filename in ``attachment_info`` è
    una identità ridondante, non una caption: viene fuso e il text synth è
    azzerato."""
    _add(
        text=f"Media: {MEDIA_ID}",
        ts=TS_B,
        status="read",
        attachment_info="voice.ogg",
    )
    _add(
        text="voice.ogg",
        ts=TS_A,
        status="delivered",
        attachment_info="voice.ogg",
    )

    assert db._dedup_incoming_attachment_mirrors() == 1

    row = _all_rows()[0]
    assert row["text"] == ""
    assert row["attachment_info"] == "voice.ogg"


def test_boot_dedup_skips_synth_and_url_caption():
    """R3/71134: un URL incollato (caption-like) NON è un duplicato
    dell'identità: la coppia non viene fusa e la caption resta."""
    _add(text=f"Media: {MEDIA_ID}", ts=TS_A, attachment_info="voice.ogg")
    _add(
        text="https://example.com/pagina",
        ts=TS_B,
        attachment_info="voice.ogg",
    )

    assert db._dedup_incoming_attachment_mirrors() == 0
    assert _count() == 2


def test_boot_dedup_skips_media_url_caption_no_attachment_match():
    """R4: ``Media: <url diverso>`` non è l'identità del PROPRIO allegato
    (``require_attachment_match=True``): nessuna fusione."""
    _add(text=f"Media: {MEDIA_ID_2}", attachment_id=MEDIA_ID, ts=TS_A)
    _add(text="", attachment_id=MEDIA_ID, ts=TS_B)

    assert db._dedup_incoming_attachment_mirrors() == 0
    assert _count() == 2


# ─── 15. R6: attachment_info Completa il survivor ────────────────────────────


def test_boot_dedup_merges_attachment_info():
    """R6: l'``attachment_info`` del twin completa il survivor se mancante."""
    _add(
        text=f"Media: {MEDIA_ID}",
        ts=TS_A,
        status="delivered",
        attachment_info="voice.ogg",
    )
    _add(text="", ts=TS_B, status="read", attachment_info=None)

    assert db._dedup_incoming_attachment_mirrors() == 1

    row = _all_rows()[0]
    assert row["attachment_info"] == "voice.ogg"
    assert row["text"] == ""


# ─── N1/N2: dedup intra-chiave web (models.pick_survivor_and_merge) ─────────

WEB_CONTACT = "555"


def _web(
    text: str,
    *,
    msg_id: str,
    ts: int,
    attachment_id: str | None = None,
    attachment_info: str | None = None,
    msg_type: str = "attachment",
    content_type: str | None = None,
    media_kind: str | None = None,
    is_mine: bool = False,
    sender: str = "M",
    status: str | None = None,
    batch_id: str | None = None,
    batch_index: int | None = None,
) -> None:
    """Inserisce una riga per i test web su ``WEB_CONTACT``."""
    db._add_message_to_cache(
        WEB_CONTACT,
        text,
        is_mine,
        sender,
        ts,
        msg_type=msg_type,
        attachment_id=attachment_id,
        attachment_info=attachment_info,
        content_type=content_type,
        media_kind=media_kind,
        protocol="whatsapp",
        msg_id=msg_id,
        status=status,
        batch_id=batch_id,
        batch_index=batch_index,
    )


def test_web_intra_key_echo_caption_survivor_keeps_attachment():
    """N1: la caption di un twin senza allegato non fa scartare il media."""
    _web("Yes, nice", msg_id="cap-1", ts=1_000)
    _web(
        "",
        msg_id="cap-1",
        ts=1_500,
        msg_type="image",
        attachment_id="a.jpg",
        attachment_info="Yes, nice",
        content_type="image/jpeg",
    )

    result = _messages("whatsapp", WEB_CONTACT)

    assert len(result) == 1
    assert result[0]["attachment"] is not None
    assert result[0]["attachment"]["attachment_id"] == "a.jpg"
    assert result[0]["text"] == "Yes, nice"


def test_web_intra_key_caption_only_in_text_row_transferred():
    """N1: la caption reale vive solo nella riga senza allegato -> trasferita."""
    _web("ecco la foto", msg_id="cap-2", ts=1_000)
    _web(
        "",
        msg_id="cap-2",
        ts=1_500,
        msg_type="image",
        attachment_id="a.jpg",
        attachment_info="photo.jpg",
        content_type="image/jpeg",
    )

    result = _messages("whatsapp", WEB_CONTACT)

    assert len(result) == 1
    assert result[0]["text"] == "ecco la foto"
    assert result[0]["attachment"]["name"] == "photo.jpg"


def test_web_intra_key_two_no_attachment_divergent_texts_not_merged():
    """N2: due testi significativi diversi con lo stesso msg_id restano 2."""
    _web("hello", msg_id="div-1", ts=1_000)
    _web("world", msg_id="div-1", ts=1_500)

    assert len(_messages("whatsapp", WEB_CONTACT)) == 2


def test_web_intra_key_synthetic_vs_blank_no_attachment_merges():
    """N2: identità sintetica vs vuoto non sono testi divergenti -> 1 riga."""
    media_url = "http://localhost:3000/api/files/default/voice.ogg"
    _web(f"Media: {media_url}", msg_id="syn-1", ts=1_000)
    _web("", msg_id="syn-1", ts=1_500)

    result = _messages("whatsapp", WEB_CONTACT)

    assert len(result) == 1
    assert result[0]["text"] == ""


def test_web_intra_key_distinct_attachment_ids_stay_separate():
    """Multi-allegato: ``attachment_id`` distinti restano gruppi separati."""
    for index, attachment_id in enumerate(("a.jpg", "b.jpg")):
        _web(
            "",
            msg_id="multi-1",
            ts=1_000 + index,
            msg_type="image",
            attachment_id=attachment_id,
            content_type="image/jpeg",
        )

    assert len(_messages("whatsapp", WEB_CONTACT)) == 2


def test_web_intra_key_batch_slot_only_from_survivor():
    """Il merge non tocca gli slot batch: restano quelli del survivor."""
    _web(
        "",
        msg_id="batch-1",
        ts=1_000,
        msg_type="image",
        attachment_id="a.jpg",
        content_type="image/jpeg",
        status="read",
        batch_id="batch-7",
        batch_index=0,
    )
    _web(
        "",
        msg_id="batch-1",
        ts=1_500,
        msg_type="image",
        attachment_id="a.jpg",
        content_type="image/jpeg",
        status="delivered",
        batch_id="batch-9",
        batch_index=1,
    )

    result = _messages("whatsapp", WEB_CONTACT)

    assert len(result) == 1
    assert result[0]["batch_id"] == "batch-7"
    assert result[0]["batch_index"] == 0


# ─── N3/N4: nome allegato unico (URL/mime -> basename) ───────────────────────


def test_attachment_display_name_url_falls_back_to_basename():
    url = "http://localhost:3000/api/files/default/voice.ogg"
    assert attachment_display_name(url, url) == "voice.ogg"


def test_attachment_display_name_mime_falls_back_to_basename():
    assert (
        attachment_display_name("image/jpeg", "http://host/a/photo.jpg") == "photo.jpg"
    )


def test_attachment_display_name_keeps_plain_filename():
    assert (
        attachment_display_name("contratto.pdf", "http://host/a/doc.pdf")
        == "contratto.pdf"
    )


def test_web_synth_url_caption_shows_filename_not_url():
    """N3 end-to-end: un ``attachment_info`` URL non diventa il nome mostrato."""
    url = "http://localhost:3000/api/files/default/true_555@lid_ABC.ogg"
    _web(
        f"Media: {url}",
        msg_id="name-1",
        ts=1_000,
        attachment_id=url,
        attachment_info=url,
        content_type="audio/ogg",
        media_kind="voice",
    )

    result = _messages("whatsapp", WEB_CONTACT)

    assert len(result) == 1
    assert result[0]["attachment"]["name"] == "true_555@lid_ABC.ogg"
    assert result[0]["text"] == ""


# ─── DELTA-3: flow unificata caption/media (P1/P2) ────────────────────────────

_QUANTI_URL = "http://localhost:3000/api/files/default/true_555@lid_ABC.ogg"


def test_web_intra_key_caption_recovered_from_same_attachment_twin():
    """P1: twin con lo STESSO ``attachment_id`` dove uno porta la caption reale."""
    _web(
        f"Media: {_QUANTI_URL}",
        msg_id="quanti-1",
        ts=1_000,
        attachment_id=_QUANTI_URL,
        attachment_info=_QUANTI_URL,
        content_type="audio/ogg",
        media_kind="voice",
    )
    _web(
        "quanti",
        msg_id="quanti-1",
        ts=1_500,
        attachment_id=_QUANTI_URL,
        attachment_info=_QUANTI_URL,
        content_type="audio/ogg",
        media_kind="voice",
    )

    result = _messages("whatsapp", WEB_CONTACT)

    assert len(result) == 1
    assert result[0]["text"] == "quanti"
    assert result[0]["attachment"] is not None
    assert result[0]["attachment"]["attachment_id"] == _QUANTI_URL
    assert result[0]["attachment"]["name"] == "true_555@lid_ABC.ogg"


def test_web_intra_key_url_caption_recovered_from_same_attachment_twin():
    """P1: la caption URL incollata viene recuperata dal twin sintetico."""
    caption_url = "https://example.com/pagina"
    _web(
        caption_url,
        msg_id="urlcap-1",
        ts=1_000,
        attachment_id=_QUANTI_URL,
        attachment_info=_QUANTI_URL,
        msg_type="image",
        content_type="image/jpeg",
    )
    _web(
        f"Media: {_QUANTI_URL}",
        msg_id="urlcap-1",
        ts=1_500,
        attachment_id=_QUANTI_URL,
        attachment_info=_QUANTI_URL,
        msg_type="image",
        content_type="image/jpeg",
    )

    result = _messages("whatsapp", WEB_CONTACT)

    assert len(result) == 1
    assert result[0]["text"] == caption_url


def test_web_intra_key_divergent_captions_not_merged():
    """Guardia anti-perdita: due caption reali distinte non vengono fuse."""
    _web(
        f"Media: {_QUANTI_URL}",
        msg_id="divcap-1",
        ts=1_000,
        attachment_id=_QUANTI_URL,
        attachment_info=_QUANTI_URL,
        content_type="audio/ogg",
        media_kind="voice",
    )
    for text, ts in (("quanti", 1_500), ("grazie", 2_000)):
        _web(
            text,
            msg_id="divcap-1",
            ts=ts,
            attachment_id=_QUANTI_URL,
            attachment_info=_QUANTI_URL,
            content_type="audio/ogg",
            media_kind="voice",
        )

    assert len(_messages("whatsapp", WEB_CONTACT)) == 3


def test_web_intra_key_media_and_divergent_text_rows_not_merged():
    """Guardia anti-perdita anche con una riga media e due testi divergenti."""
    _web(
        "",
        msg_id="mixdiv-1",
        ts=1_000,
        msg_type="image",
        attachment_id="a.jpg",
        content_type="image/jpeg",
    )
    _web("hello", msg_id="mixdiv-1", ts=1_500)
    _web("world", msg_id="mixdiv-1", ts=2_000)

    assert len(_messages("whatsapp", WEB_CONTACT)) == 3


def test_web_intra_key_single_text_survives_read_blank_survivor():
    """P2: l'unico testo reale sopravvive al survivor vuoto con rank superiore."""
    _web(
        "important",
        msg_id="single-1",
        ts=1_000,
        is_mine=True,
        sender="You",
        status="sent",
    )
    _web(
        "",
        msg_id="single-1",
        ts=1_500,
        is_mine=True,
        sender="You",
        status="read",
    )

    result = _messages("whatsapp", WEB_CONTACT)

    assert len(result) == 1
    assert result[0]["text"] == "important"
    assert result[0]["status"] == "read"


def test_web_intra_key_caption_transfer_keeps_survivor_identity():
    """Il transfer della caption non tocca l'identità fisica del survivor."""
    _web(
        f"Media: {_QUANTI_URL}",
        msg_id="identity-1",
        ts=1_000,
        attachment_id=_QUANTI_URL,
        attachment_info=_QUANTI_URL,
        msg_type="image",
        content_type="image/jpeg",
        is_mine=True,
        sender="You",
        status="read",
        batch_id="b7",
        batch_index=0,
    )
    _web(
        "caption reale",
        msg_id="identity-1",
        ts=1_500,
        is_mine=True,
        sender="You",
        status="delivered",
        batch_id="b9",
        batch_index=1,
    )
    with closing(sqlite3.connect(db.DB_FILE)) as connection, connection:
        connection.execute("UPDATE messages SET read = 0 WHERE text = 'caption reale'")
        connection.execute(
            "UPDATE messages SET read = 1 WHERE attachment_id IS NOT NULL"
        )
        connection.commit()

    result = _messages("whatsapp", WEB_CONTACT)

    assert len(result) == 1
    message = result[0]
    assert message["attachment"]["attachment_id"] == _QUANTI_URL
    assert message["id"] == "identity-1"
    assert message["batch_id"] == "b7"
    assert message["batch_index"] == 0
    assert message["read"] is True
    assert message["text"] == "caption reale"


def test_web_intra_key_dedup_idempotent():
    """La dedup web non muta il DB: due letture danno lo stesso output."""
    _web(
        f"Media: {_QUANTI_URL}",
        msg_id="idem-1",
        ts=1_000,
        attachment_id=_QUANTI_URL,
        attachment_info=_QUANTI_URL,
        content_type="audio/ogg",
        media_kind="voice",
    )
    _web(
        "quanti",
        msg_id="idem-1",
        ts=1_500,
        attachment_id=_QUANTI_URL,
        attachment_info=_QUANTI_URL,
        content_type="audio/ogg",
        media_kind="voice",
    )

    assert _messages("whatsapp", WEB_CONTACT) == _messages("whatsapp", WEB_CONTACT)
