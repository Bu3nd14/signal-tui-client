"""
Fase 7 — Riconciliazione ``_merge_backend_cache`` edit-aware (unit).

Verifica che ``_merge_backend_cache`` riconosca un messaggio MODIFICATO come lo
STESSO messaggio (il match per ``id`` ora precede il confronto sul testo in
entrambe le direzioni, incoming inclusi) e che l'aggiornamento del testo
avvenga in-place (``existing["text"]``, ``existing["edited"] = True``)
riportando ``True`` SOLO quando serve un remount della finestra.  Nessun test
modifica il codice di produzione: documentiamo la semantica attuale.

Approccio scelto (e motivazione)
--------------------------------
``_merge_backend_cache`` è un metodo di ``ChatViewMixin`` che tocca soltanto
``self._cache``, ``contact.id`` / ``contact.cache_key`` e ``backend.cache``.
Per isolarlo senza lanciare l'intera TUI (né ``App.run_test()``) costruiamo un
piccolo *harness* che mixa ``ChatViewMixin`` con il solo attributo ``_cache``;
il contatto è un vero ``ChatContact`` (dataclass di produzione) e il backend è
un oggetto minimale con ``.cache`` keyed by ``contact.id``.  Questo evita ogni
I/O, env o worker reale e replica lo spirito di ``test_refresh_chat.py`` (che
istanzia ``SignalTUI`` direttamente senza eseguirla).

Per il solo test che esercita ``_load_messages_worker`` (caso "cache cambiata
→ remount") usiamo un vero ``SignalTUI`` istanziato direttamente — come fa
``TestLoadWorkerStaleness`` in ``test_refresh_chat.py`` — con un contatto
WhatsApp e un backend fake dotato di ``fetch_history``, patchano
``_render_chat_window`` per contare i remount senza toccare il DOM.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from models import (
    PROTOCOL_SIGNAL,
    PROTOCOL_WHATSAPP,
    ChatContact,
)
from tui.chat_view import ChatViewMixin

# ─── Harness ──────────────────────────────────────────────────────────────────


class _MergeHarness(ChatViewMixin):
    """Minimal object exposing only the ``_cache`` used by ``_merge_backend_cache``."""

    def __init__(self, cache: dict | None = None) -> None:
        self._cache = cache if cache is not None else {}


class _FakeBackend:
    """Backend fake: espone solo ``.cache`` keyed by the raw contact id."""

    def __init__(self, contact_id: str, msgs: list[dict]) -> None:
        self.cache: dict[str, list[dict]] = {contact_id: msgs}


def _msg(
    text: str,
    *,
    is_mine: bool = False,
    ts: int = 1000,
    msg_type: str = "text",
    status: str | None = None,
    mid: str | None = None,
) -> dict:
    """Costruisce un message-dict coerente con la cache UI (come ``_make_message``)."""
    m = {
        "text": text,
        "is_mine": is_mine,
        "sender": "You" if is_mine else "Mario",
        "timestamp": ts,
        "msg_type": msg_type,
        "status": status if status is not None else ("sent" if is_mine else "read"),
        "read": is_mine,
    }
    if mid is not None:
        m["id"] = mid
    return m


def _merge(
    existing: list[dict],
    incoming: list[dict],
    *,
    protocol: str = PROTOCOL_SIGNAL,
) -> tuple[_MergeHarness, ChatContact, bool]:
    """Esegue ``_merge_backend_cache`` su un harness isolato e ritorna lo stato."""
    contact = ChatContact(id="+391234567890", display_name="Mario", protocol=protocol)
    harness = _MergeHarness({contact.cache_key: existing})
    backend = _FakeBackend(contact.id, incoming)
    changed = harness._merge_backend_cache(contact, backend)
    return harness, contact, changed


# ─── Edit-aware: same id, text changed ────────────────────────────────────────


class TestEditAwareMerge:
    def test_incoming_edit_same_id_updates_text_and_marks_edited(self):
        """Incoming con stesso id e testo diverso → riconosciuto come esistente,
        testo aggiornato in-place, ``edited=True`` e ritorno ``True``."""
        harness, contact, changed = _merge(
            existing=[_msg("old", is_mine=False, ts=1000, mid="m1")],
            incoming=[_msg("new", is_mine=False, ts=1000, mid="m1")],
        )

        entries = harness._cache[contact.cache_key]
        assert changed is True
        assert len(entries) == 1  # nessun duplicato aggiunto
        entry = entries[0]
        assert entry["text"] == "new"
        assert entry["edited"] is True
        # id e timestamp NON vengono toccati dall'edit.
        assert entry["id"] == "m1"
        assert entry["timestamp"] == 1000

    def test_incoming_same_id_same_text_is_noop(self):
        """Incoming con stesso id e testo IDENTICO → nessun update, ritorno False."""
        harness, contact, changed = _merge(
            existing=[_msg("same", is_mine=False, ts=1000, mid="m1")],
            incoming=[_msg("same", is_mine=False, ts=1000, mid="m1")],
        )

        entry = harness._cache[contact.cache_key][0]
        assert changed is False
        assert len(harness._cache[contact.cache_key]) == 1
        assert entry["text"] == "same"
        assert "edited" not in entry  # nessuna scrittura superflua

    def test_outgoing_edit_same_id_updates_text(self):
        """Outgoing con stesso id e testo diverso → edit del NOSTRO messaggio."""
        harness, contact, changed = _merge(
            existing=[_msg("old", is_mine=True, ts=1000, mid="m1", status="sent")],
            incoming=[_msg("new", is_mine=True, ts=1000, mid="m1", status="sent")],
        )

        entry = harness._cache[contact.cache_key][0]
        assert changed is True
        assert len(harness._cache[contact.cache_key]) == 1
        assert entry["text"] == "new"
        assert entry["edited"] is True
        assert entry["status"] == "sent"  # lo status non è toccato dall'edit

    def test_incoming_different_id_and_text_is_new_message_even_with_close_ts(self):
        """Incoming con id DIVERSO e testo diverso ma ts vicino (entro ±5s) →
        il fallback testo+finestra NON scatta (testo diverso): è un NUOVO
        messaggio, non un edit.  Documenta la semantica attuale (nessuna modifica)."""
        harness, contact, changed = _merge(
            existing=[_msg("old", is_mine=False, ts=1000, mid="m1")],
            incoming=[_msg("new", is_mine=False, ts=1001, mid="m2")],
        )

        entries = harness._cache[contact.cache_key]
        assert changed is True  # added
        assert len(entries) == 2
        texts = [e["text"] for e in entries]
        assert sorted(texts) == ["new", "old"]
        # Il messaggio pre-esistente NON è stato toccato.
        original = next(e for e in entries if e["id"] == "m1")
        assert original["text"] == "old"
        assert "edited" not in original

    def test_incoming_same_text_different_id_within_window_is_deduped(self):
        """Incoming con testo UGUALE, id diverso ma ts entro ±5s → il fallback
        testo+finestra lo riconosce come ESISTENTE (no-op, nessun duplicato).
        Contrasto col caso precedente: qui è il TESTO uguale a guidare il match."""
        harness, contact, changed = _merge(
            existing=[_msg("same", is_mine=False, ts=1000, mid="m1")],
            incoming=[_msg("same", is_mine=False, ts=1002, mid="m2")],
        )

        assert changed is False
        assert len(harness._cache[contact.cache_key]) == 1
        assert harness._cache[contact.cache_key][0]["text"] == "same"


def test_merge_ui_two_identical_outgoing_stay_two():
    """Confirmed outgoing messages with different ids must not merge by text/time."""
    harness, contact, changed = _merge(
        existing=[],
        incoming=[
            _msg("OK", is_mine=True, ts=1000, mid="A"),
            _msg("OK", is_mine=True, ts=121_000, mid="B"),
        ],
        protocol=PROTOCOL_WHATSAPP,
    )

    entries = harness._cache[contact.cache_key]
    assert changed is True
    assert len(entries) == 2
    assert [entry["id"] for entry in entries] == ["A", "B"]


# ─── Status-only upgrade ──────────────────────────────────────────────────────


class TestStatusUpgrade:
    def test_status_only_upgrade_returns_false_but_updates_status(self):
        """Upgrade di solo status (testo uguale, rank maggiore) → ritorno False
        (nessun remount superfluo) ma status aggiornato."""
        harness, contact, changed = _merge(
            existing=[_msg("hi", is_mine=True, ts=1000, mid="m1", status="sent")],
            incoming=[_msg("hi", is_mine=True, ts=1000, mid="m1", status="delivered")],
        )

        entry = harness._cache[contact.cache_key][0]
        assert changed is False
        assert entry["status"] == "delivered"
        assert entry["text"] == "hi"
        assert "edited" not in entry

    def test_status_never_downgrades(self):
        """Mai un downgrade read → sent: lo status esistente resta."""
        harness, contact, changed = _merge(
            existing=[_msg("hi", is_mine=True, ts=1000, mid="m1", status="read")],
            incoming=[_msg("hi", is_mine=True, ts=1000, mid="m1", status="sent")],
        )

        entry = harness._cache[contact.cache_key][0]
        assert changed is False
        assert entry["status"] == "read"


# ─── Media guard ──────────────────────────────────────────────────────────────


class TestMediaGuard:
    def test_media_message_text_not_updated_on_edit(self):
        """Messaggio media (msg_type != 'text') con stesso id e testo diverso →
        il testo NON viene aggiornato (guard ``msg_type == "text"``).

        Stesso ``attachment_id`` (un edit di caption dello stesso media), quindi
        la riconciliazione è sullo STESSO messaggio e la guard media evita
        l'aggiornamento del testo.
        """
        existing = {
            "id": "m1",
            "text": "Media: old-media-id",
            "is_mine": False,
            "sender": "Mario",
            "timestamp": 1000,
            "msg_type": "image",
            "attachment_info": "photo.jpg",
            "attachment_id": "same-media-id",
            "status": "read",
            "read": False,
        }
        incoming = {
            "id": "m1",
            "text": "Media: new-media-id",
            "is_mine": False,
            "sender": "Mario",
            "timestamp": 1000,
            "msg_type": "image",
            "attachment_info": "photo.jpg",
            "attachment_id": "same-media-id",
            "status": "read",
            "read": False,
        }

        harness, contact, changed = _merge(existing=[existing], incoming=[incoming])

        entry = harness._cache[contact.cache_key][0]
        assert changed is False
        assert len(harness._cache[contact.cache_key]) == 1
        assert entry["text"] == "Media: old-media-id"  # NON aggiornato
        assert "edited" not in entry

    def test_distinct_attachments_same_id_are_kept_both(self):
        """Due media con stesso ``id`` ma ``attachment_id`` diversi (allegati
        multipli di un messaggio Signal) NON collidono: entrambi restano."""
        first = {
            "id": "1787648916285",
            "text": "Image: IMG_0115.jpg",
            "is_mine": True,
            "sender": "You",
            "timestamp": 1787648916285,
            "msg_type": "image",
            "attachment_info": "Image: IMG_0115.jpg",
            "attachment_id": "att-0115",
            "status": "sent",
            "read": True,
        }
        second = {
            "id": "1787648916285",
            "text": "Image: IMG_0114.jpg",
            "is_mine": True,
            "sender": "You",
            "timestamp": 1787648916285,
            "msg_type": "image",
            "attachment_info": "Image: IMG_0114.jpg",
            "attachment_id": "att-0114",
            "status": "sent",
            "read": True,
        }

        harness, contact, changed = _merge(existing=[first], incoming=[second])

        entries = harness._cache[contact.cache_key]
        assert changed is True
        assert len(entries) == 2
        ids = {e["attachment_id"] for e in entries}
        assert ids == {"att-0115", "att-0114"}

    def test_same_attachment_same_id_still_deduped(self):
        """Stesso id E stesso attachment_id → un solo messaggio (redelivery)."""
        existing = {
            "id": "m1",
            "text": "Media: url",
            "is_mine": False,
            "sender": "Mario",
            "timestamp": 1000,
            "msg_type": "image",
            "attachment_info": "photo.jpg",
            "attachment_id": "att-1",
            "status": "read",
            "read": False,
        }

        harness, contact, changed = _merge(existing=[existing], incoming=[existing])

        assert changed is False
        assert len(harness._cache[contact.cache_key]) == 1

    def test_ack_echo_without_attachment_id_still_deduped_by_id(self):
        """L'ack-echo (#36) — stesso id ma senza attachment_id e testo=caption —
        resta deduplicato per id contro il media uscente."""
        media = {
            "id": "true_189025889575055",
            "text": "Media: https://wa.to/img/abc123.jpg",
            "is_mine": True,
            "sender": "You",
            "timestamp": 1700000000,
            "msg_type": "image",
            "attachment_info": "Yes, nice",
            "attachment_id": "https://wa.to/img/abc123.jpg",
            "status": "sent",
            "read": True,
        }
        ack = {
            "id": "true_189025889575055",
            "text": "Yes, nice",
            "is_mine": True,
            "sender": "You",
            "timestamp": 1700000001,
            "msg_type": "text",
            "attachment_info": None,
            "attachment_id": None,
            "status": "sent",
            "read": True,
        }

        harness, contact, changed = _merge(existing=[media], incoming=[ack])

        entries = harness._cache[contact.cache_key]
        assert changed is False
        assert len(entries) == 1
        assert entries[0]["msg_type"] == "image"


# ─── _load_messages_worker remount ────────────────────────────────────────────


class TestLoadWorkerRemount:
    def test_load_messages_worker_remounts_window_when_cache_changed(self):
        """Con cache "cambiata" (edit riconciliato → ``True``), la fase 2 di
        ``_load_messages_worker`` rimonta la finestra (seconda ``_render_chat_window``)."""
        from signal_tui import SignalTUI

        app = SignalTUI()
        contact = ChatContact(
            id="1@c.us", display_name="Mario", protocol=PROTOCOL_WHATSAPP
        )
        app.selected_contact = contact
        app._cache = {
            contact.cache_key: [_msg("old", is_mine=False, ts=1000, mid="m1")]
        }
        app._chat_reload_token = 1
        app._loaded_all = False

        backend = _FakeBackend(contact.id, [])  # vuota all'inizio
        app.manager = MagicMock()
        app.manager.get.return_value = backend

        new_msg = _msg("new", is_mine=False, ts=1000, mid="m1")

        def fetch_history(cid, limit=50):
            backend.cache[contact.id] = [new_msg]

        backend.fetch_history = fetch_history

        with patch.object(app, "_render_chat_window", return_value=True) as render:
            app._load_messages_worker()

        # Fase 1 (pending) + Fase 2 (l'edit ha cambiato la cache → remount).
        assert render.call_count == 2
        # La seconda render NON è quella "pending" iniziale.
        assert render.call_args_list[1].kwargs.get("pending_fetch") is False
        # L'edit è stato riconciliato nella cache UI.
        entry = app._cache[contact.cache_key][0]
        assert entry["text"] == "new"
        assert entry["edited"] is True
