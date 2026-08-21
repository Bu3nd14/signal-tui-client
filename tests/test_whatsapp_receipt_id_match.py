"""Regression tests for bug #39 (WhatsApp sent bubbles stuck at pending/sent).

WAHA 2026.8.1 (WEBJS) reports the same message with different id shapes:

- DB/cache (``sendText`` ``_serialized``): ``true_{jid}_{hex}`` (DM) or
  ``true_{jid@g.us}_{hex}_{participant}`` (group);
- webhook ``message.ack`` ``id``: just ``{hex}`` (no participant).

``process_receipt`` used to compare the raw strings, so the receipt never
matched the cache entry and the bubble never advanced.  This file covers the
shared ``canonical_msg_id`` normalizer, the canonical matching in
``process_receipt``, its id-less uniqueness fallback, and the canonical id
already carried by ``_event_from_ack``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import backend as backend_mod
from backends.whatsapp import WhatsAppBackend, _event_from_ack
from backends.whatsapp_events import canonical_msg_id


def _make_backend(api_url: str = "http://api.test") -> WhatsAppBackend:
    backend = WhatsAppBackend(api_url=api_url, media_dir="")
    backend._rest = MagicMock()
    return backend


# ─── canonical_msg_id ─────────────────────────────────────────────────────────


class TestCanonicalMsgId:
    """🔑 ``canonical_msg_id`` riduce ogni forma di id allo stesso token."""

    def test_dm_serialized(self):
        assert (
            canonical_msg_id("true_189025889575055@lid_3A268CF00E4ECCEA4474")
            == "3A268CF00E4ECCEA4474"
        )

    def test_group_serialized_with_participant(self):
        assert (
            canonical_msg_id("true_123456789@g.us_3EB0D9A373425F46E2A5F6_987654321@lid")
            == "3EB0D9A373425F46E2A5F6"
        )

    def test_s_whatsapp_net_jid(self):
        assert (
            canonical_msg_id("true_391234567890@s.whatsapp.net_3A268CF00E4ECCEA4474")
            == "3A268CF00E4ECCEA4474"
        )

    def test_plain_hex_uppercased(self):
        assert canonical_msg_id("3eb0d9a373425f46e2a5f6") == "3EB0D9A373425F46E2A5F6"

    def test_plain_hex_unchanged_when_already_upper(self):
        assert canonical_msg_id("3EB0D9A373425F46E2A5F6") == "3EB0D9A373425F46E2A5F6"

    def test_flat_opaque_ids_kept(self):
        for raw in ("msg_abc", "m1", "m", "msg-1", "outgoing-parent", "BAYES-123"):
            assert canonical_msg_id(raw) == raw, raw

    def test_unknown_serialized_shape_kept(self):
        # A synthetic serialized-looking id with no hex segment must not guess.
        assert (
            canonical_msg_id("true_reverse_189025889575055@lid")
            == "true_reverse_189025889575055@lid"
        )

    def test_empty_and_none(self):
        assert canonical_msg_id(None) == ""
        assert canonical_msg_id("") == ""
        assert canonical_msg_id("   ") == ""

    def test_dm_and_hex_are_equivalent(self):
        assert canonical_msg_id("true_189025889575055@lid_3A268CF00E4ECCEA4474") == (
            canonical_msg_id("3A268CF00E4ECCEA4474")
        )

    def test_group_and_hex_are_equivalent(self):
        assert canonical_msg_id(
            "true_123456789@g.us_3EB0D9A373425F46E2A5F6_987654321@lid"
        ) == canonical_msg_id("3EB0D9A373425F46E2A5F6")


# ─── process_receipt canonical matching ───────────────────────────────────────


class TestProcessReceiptCanonical:
    """📥 ``process_receipt`` matcha per id canonico, non per stringa grezza."""

    def test_dm_serialized_cache_matches_hex_receipt(self):
        backend = _make_backend()
        backend.cache = {
            "1@lid": [
                {
                    "id": "true_189025889575055@lid_3A268CF00E4ECCEA4474",
                    "is_mine": True,
                    "status": "sent",
                    "timestamp": 1000,
                    "text": "hello",
                }
            ]
        }
        with (
            patch.object(backend_mod, "_update_message_status_by_id") as mock_persist,
            patch.object(backend_mod, "_update_message_status"),
        ):
            updated = backend.process_receipt(
                {"message_ids": ["3A268CF00E4ECCEA4474"], "is_read": True}
            )
        assert [u["id"] for u in updated] == [
            "true_189025889575055@lid_3A268CF00E4ECCEA4474"
        ]
        assert backend.cache["1@lid"][0]["status"] == "read"
        mock_persist.assert_called_once_with(
            "true_189025889575055@lid_3A268CF00E4ECCEA4474",
            "read",
            protocol="whatsapp",
            contact_number="1@lid",
        )

    def test_group_serialized_cache_matches_hex_receipt(self):
        backend = _make_backend()
        backend.cache = {
            "1@g.us": [
                {
                    "id": "true_123456789@g.us_3EB0D9A373425F46E2A5F6_987654321@lid",
                    "is_mine": True,
                    "status": "sent",
                    "timestamp": 2000,
                    "text": "group msg",
                }
            ]
        }
        with (
            patch.object(backend_mod, "_update_message_status_by_id") as mock_persist,
            patch.object(backend_mod, "_update_message_status"),
        ):
            updated = backend.process_receipt(
                {"message_ids": ["3EB0D9A373425F46E2A5F6"], "is_read": False}
            )
        assert len(updated) == 1
        assert backend.cache["1@g.us"][0]["status"] == "delivered"
        mock_persist.assert_called_once()

    def test_receipt_carries_serialized_id_also_matches(self):
        backend = _make_backend()
        backend.cache = {
            "1@lid": [
                {
                    "id": "true_189025889575055@lid_3A268CF00E4ECCEA4474",
                    "is_mine": True,
                    "status": "sent",
                    "timestamp": 1000,
                    "text": "hello",
                }
            ]
        }
        with patch.object(backend_mod, "_update_message_status_by_id"):
            updated = backend.process_receipt(
                {
                    "message_ids": ["true_189025889575055@lid_3A268CF00E4ECCEA4474"],
                    "is_read": True,
                }
            )
        assert len(updated) == 1

    def test_rank_guard_still_respected_with_canonical_match(self):
        backend = _make_backend()
        backend.cache = {
            "1@lid": [
                {
                    "id": "true_189025889575055@lid_3A268CF00E4ECCEA4474",
                    "is_mine": True,
                    "status": "read",
                    "timestamp": 1000,
                    "text": "hello",
                }
            ]
        }
        assert (
            backend.process_receipt(
                {"message_ids": ["3A268CF00E4ECCEA4474"], "is_read": False}
            )
            == []
        )
        assert backend.cache["1@lid"][0]["status"] == "read"


class TestProcessReceiptIdlessFallback:
    """🎯 Fallback: entry ottimistica senza id matchata per unicità."""

    def test_single_idless_sent_entry_is_upgraded(self):
        backend = _make_backend()
        backend.cache = {
            "1@c.us": [
                {
                    "id": None,
                    "is_mine": True,
                    "status": "sent",
                    "timestamp": 3000,
                    "text": "racing",
                }
            ]
        }
        with (
            patch.object(backend_mod, "_update_message_status_by_id") as mock_by_id,
            patch.object(backend_mod, "_update_message_status") as mock_by_ts,
        ):
            updated = backend.process_receipt(
                {"message_ids": ["HEXID"], "is_read": True}
            )
        assert updated == [
            {
                "id": "",
                "timestamp": 3000,
                "status": "read",
                "text": "racing",
                "is_mine": True,
            }
        ]
        assert backend.cache["1@c.us"][0]["status"] == "read"
        # Id-less entries cannot persist by id: persist by timestamp/text instead.
        mock_by_id.assert_not_called()
        mock_by_ts.assert_called_once_with(
            3000, "read", "whatsapp", "1@c.us", text="racing"
        )

    def test_multiple_idless_sent_entries_not_matched(self):
        backend = _make_backend()
        backend.cache = {
            "1@c.us": [
                {
                    "id": None,
                    "is_mine": True,
                    "status": "sent",
                    "timestamp": 3000,
                    "text": "a",
                },
                {
                    "id": None,
                    "is_mine": True,
                    "status": "sent",
                    "timestamp": 3001,
                    "text": "b",
                },
            ]
        }
        assert (
            backend.process_receipt({"message_ids": ["HEXID"], "is_read": True}) == []
        )
        assert backend.cache["1@c.us"][0]["status"] == "sent"
        assert backend.cache["1@c.us"][1]["status"] == "sent"

    def test_idless_non_sent_entry_not_matched(self):
        backend = _make_backend()
        backend.cache = {
            "1@c.us": [
                {
                    "id": None,
                    "is_mine": True,
                    "status": "delivered",
                    "timestamp": 3000,
                    "text": "already delivered",
                }
            ]
        }
        assert (
            backend.process_receipt({"message_ids": ["HEXID"], "is_read": True}) == []
        )

    def test_no_match_logs_diagnostic_warning(self):
        backend = _make_backend()
        backend.cache = {"1@c.us": []}
        with (
            patch.object(backend_mod, "_update_message_status_by_id"),
            patch.object(backend_mod, "_update_message_status"),
            patch("backends.whatsapp.logger.warning") as mock_warn,
        ):
            backend.process_receipt({"message_ids": ["HEXID"], "is_read": True})
        mock_warn.assert_called_once()


# ─── _event_from_ack canonicalizes the receipt id ─────────────────────────────


class TestEventFromAckCanonical:
    """📤 ``_event_from_ack`` mette l'id canonico nel payload del receipt."""

    def test_ack_serialized_id_is_canonicalized(self):
        ev = _event_from_ack(
            {
                "id": "true_189025889575055@lid_3A268CF00E4ECCEA4474",
                "to": "1@lid",
                "fromMe": True,
                "status": 3,
            }
        )
        assert ev is not None
        assert ev.payload == {
            "message_ids": ["3A268CF00E4ECCEA4474"],
            "is_read": True,
        }

    def test_ack_flat_id_kept(self):
        ev = _event_from_ack({"id": "m1", "to": "1@c.us", "fromMe": True, "status": 2})
        assert ev is not None
        assert ev.payload == {"message_ids": ["m1"], "is_read": False}
