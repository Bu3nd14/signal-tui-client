"""Message editing flow: Alt+click / Alt+e request, optimistic submit, rollback."""

import logging

from models import PROTOCOL_SIGNAL
from ui_components import MessageTextArea, MessageWidget

logger = logging.getLogger(__name__)


class EditMessageMixin:
    def on_message_widget_edit_requested(self, event: MessageWidget.EditRequested) -> None:
        """Handle ``EditRequested`` (Alt+click / Alt+e on an own text message)."""
        if self._download_mode:
            return
        if not event.is_mine:
            self._status("❌ You can only edit your own messages", 0)
            return
        if event.status in ("pending", "failed"):
            self._status("❌ Message not sent yet — cannot edit", 0)
            return
        contact = self.selected_contact
        if contact is None:
            return
        cache_key = contact.cache_key
        entry = next(
            (
                m
                for m in self._cache.get(cache_key, [])
                if m.get("is_mine") and int(m.get("timestamp") or 0) == int(event.timestamp)
            ),
            None,
        )
        if entry is None:
            self._status("❌ Message not found in cache", 0)
            return
        if entry.get("msg_type", "text") != "text":
            self._status("❌ Only text messages can be edited", 0)
            return
        protocol = contact.protocol
        message_id = entry.get("id") or event.message_id
        if protocol == PROTOCOL_SIGNAL:
            message_id = entry.get("id") or str(int(event.timestamp))
            if not entry.get("id"):
                self._status(
                    "⚠️ ID server non noto — la modifica potrebbe non propagarsi", 5
                )
        elif not message_id:
            self._status("❌ Server message ID unavailable — reopen the chat", 0)
            return
        # Mutua esclusione reply/edit:
        self._cancel_reply()
        self._cancel_edit()
        widget = next(
            (
                c
                for c in self.chat_log.children
                if isinstance(c, MessageWidget)
                and c._msg_timestamp == event.timestamp
                and c._msg_text == event.text
            ),
            None,
        )
        self._editing_message = {
            "protocol": protocol,
            "contact_id": contact.id,
            "cache_key": cache_key,
            "timestamp": int(event.timestamp),
            "message_id": str(message_id),
            "old_text": entry.get("text", ""),
            "_widget": widget,
        }
        if widget is not None:
            widget.set_selected(True)
        ta = self.query_one("#message-input", MessageTextArea)
        ta.text = entry.get("text", "")
        try:
            ta.move_cursor(ta.document.end)
        except Exception:
            logger.debug("cursor-to-end failed", exc_info=True)
        ta.focus()
        self._update_reply_bar()

    def _cancel_edit(self) -> None:
        """Deselect the edit widget and clear the editing state."""
        editing = getattr(self, "_editing_message", None)
        if editing is not None:
            w = editing.get("_widget")
            if w is not None:
                try:
                    w.set_selected(False)
                except Exception:
                    logger.debug("deselect edit widget failed", exc_info=True)
        self._editing_message = None
        self._update_reply_bar()

    def _submit_edit(self, new_text: str) -> None:
        """Submit an edit: optimistic local apply + network worker (rollback on error)."""
        snap = self._editing_message
        old_text = snap["old_text"]
        if new_text == old_text:
            self._cancel_edit()
            return
        snap = {**self._editing_message, "new_text": new_text}
        self._apply_local_edit(snap, new_text)  # ottimistico
        self._cancel_edit()
        self.run_worker(
            lambda: self._edit_message_worker(snap, old_text, new_text),
            exclusive=False,
            thread=True,
        )

    def _apply_local_edit(self, snap: dict, new_text: str) -> None:
        """Ottimistico: cache UI + cache backend + DB + identity sets + widget."""
        entry = next(
            (
                m
                for m in self._cache.get(snap["cache_key"], [])
                if m.get("is_mine") and int(m.get("timestamp") or 0) == snap["timestamp"]
            ),
            None,
        )
        if entry is not None:
            entry["text"] = new_text
            entry["edited"] = True
        backend = self.manager.get(snap["protocol"])
        if backend is not None:
            backend.apply_edit(snap["contact_id"], snap["message_id"], new_text, is_mine=True)
        self._rewrite_message_identity(
            snap["protocol"], snap["cache_key"], snap["timestamp"],
            snap["old_text"], new_text, snap["message_id"],
        )
        w = snap.get("_widget")
        if w is not None and w.is_mounted:
            w.update_text(new_text)

    def _rewrite_message_identity(self, protocol, cache_key, ts, old_text,
                                  new_text, message_id=None) -> None:
        """L'identità (ts, text) cambia col testo: senza questa chirurgia
        ``_refresh_chat`` rimonterebbe il messaggio editato come NUOVO (duplicato)
        e la guardia ``_shown_in_log`` di ``_add_message`` non lo riconoscerebbe."""
        for s in (self._seen_message_ids, self._shown_in_log):
            s.discard((protocol, cache_key, int(ts), old_text))
            s.add((protocol, cache_key, int(ts), new_text))
            if message_id:
                s.discard((protocol, cache_key, str(message_id), old_text))
                s.add((protocol, cache_key, str(message_id), new_text))
        # _seen_timestamps NON si tocca: il timestamp non cambia mai.

    def _edit_message_worker(self, snap: dict, old_text: str, new_text: str) -> None:
        """Worker thread: invio edit al backend, rollback completo su errore."""
        backend = self.manager.get(snap["protocol"])
        if backend is None:
            self.call_from_thread(
                self._restore_local_edit, snap, old_text,
                f"no backend for {snap['protocol']}",
            )
            return
        try:
            ok = backend.edit_message_sync(snap["contact_id"], snap["message_id"], new_text)
        except Exception as e:  # noqa: BLE001
            self.call_from_thread(self._restore_local_edit, snap, old_text, str(e))
            return
        if not ok:
            self.call_from_thread(self._restore_local_edit, snap, old_text,
                                  "edit rejected by server")
        else:
            self.call_from_thread(self._status, "✏️ Message edited")

    def _restore_local_edit(self, snap: dict, old_text: str, error: str) -> None:
        """UI thread: ripristino completo del testo originale."""
        entry = next(
            (
                m
                for m in self._cache.get(snap["cache_key"], [])
                if m.get("is_mine") and int(m.get("timestamp") or 0) == snap["timestamp"]
            ),
            None,
        )
        if entry is not None:
            entry["text"] = old_text
            entry["edited"] = False
        backend = self.manager.get(snap["protocol"])
        if backend is not None:
            backend.apply_edit(snap["contact_id"], snap["message_id"], old_text, is_mine=True)
        self._rewrite_message_identity(
            snap["protocol"], snap["cache_key"], snap["timestamp"],
            snap.get("new_text", old_text), old_text,
            snap["message_id"],
        )
        w = snap.get("_widget")
        if w is not None and w.is_mounted:
            w.update_text(old_text, edited=False)
        self._status(f"❌ Edit failed: {error}", 0)
