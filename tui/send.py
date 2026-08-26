"""Message sending (input submit + send worker)."""

import logging
import sys
import time
from pathlib import Path

from textual.widgets import Input

from backend import get_attachment_path
from emoji_picker import EmojiCompletionWidget
from emoji_picker import replace_emoji_aliases as _replace_emoji_aliases
from models import (
    PROTOCOL_SIGNAL,
    PROTOCOL_TELEGRAM,
    PROTOCOL_WHATSAPP,
    is_media_quote_placeholder,
)
from ui_components import MessageTextArea

logger = logging.getLogger(__name__)


def _quote_content_type(reply_data: dict) -> str | None:
    """Return the persisted quoted-attachment mime type, or ``None``.

    ``None`` is returned for legacy rows (pre-migration, ``content_type IS
    NULL``): the reply degrades to the V2 behaviour — no ``quoteAttachments``,
    so the quote stays correct and typed but without a thumbnail.  Deriving the
    mime from ``msg_type`` was considered and rejected (design §9): the mapping
    is unreliable (Signal video/audio are ``msg_type="attachment"`` → would
    yield ``application/octet-stream``) and guessing from the file extension is
    fragile, so the persisted value is the only trusted source.
    """
    return (reply_data.get("content_type") or "").strip() or None


def _resolve_emoji_replacer():
    """Return the alias replacer, patchable via ``signal_tui.replace_emoji_aliases``.

    Resolved lazily (instead of importing ``signal_tui`` at module level) to
    avoid a circular import: ``tui.send`` → ``signal_tui`` → ``tui.app``.
    Tests patch ``signal_tui.replace_emoji_aliases``, so we read it from the
    already-imported ``signal_tui`` module when available.
    """
    stui = sys.modules.get("signal_tui")
    if stui is not None:
        return stui.replace_emoji_aliases
    return _replace_emoji_aliases


class SendMixin:
    def on_message_text_area_submitted(self, event: MessageTextArea.Submitted) -> None:
        """Send a message when the user presses Enter.
        Also converts any :emoji: aliases in the message.
        If emoji completion is visible, insert the selected emoji instead."""
        # If emoji completion is visible, insert the selected emoji
        if self._is_completion_visible():
            self._insert_emoji_from_completion()
            return

        if not self.selected_contact:
            self._status("❌ Select a contact first!")
            return

        # Hide completion if visible
        try:
            completion = self.query_one("#emoji-completion", EmojiCompletionWidget)
            completion.hide_suggestions()
        except Exception as _e:
            logger.debug("Failed to hide emoji completion", exc_info=True)

        # Convert emoji aliases (e.g. :smile: → 😊)
        message = _resolve_emoji_replacer()(
            event.value.replace("\r\n", "\n").replace("\r", "\n").strip()
        )

        if not message:
            return

        if self._editing_message is not None:
            self._submit_edit(message)
            event.text_area.text = ""
            return

        contact = self.selected_contact
        contact_id = contact.id
        protocol = contact.protocol
        cache_key = contact.cache_key
        ts = int(time.time() * 1000)

        # Capture reply data before clearing it
        reply_data = self._reply_to
        quote_text = reply_data.get("text") if reply_data else None
        reply_to_message_id = reply_data.get("message_id") if reply_data else None

        # Telegram replies must use the original server message id.  A timestamp
        # is not a Telegram message id; refusing here prevents an optimistic
        # normal-message bubble from being created for an impossible reply.
        if reply_data and protocol == PROTOCOL_TELEGRAM:
            try:
                if (
                    isinstance(reply_to_message_id, bool)
                    or int(reply_to_message_id) <= 0
                ):
                    raise ValueError
            except (TypeError, ValueError):
                self._status(
                    "❌ Cannot reply: the original Telegram message ID is unavailable",
                    0,
                )
                return

        # WhatsApp quotes are applied server-side via the Baileys ``reply_to``
        # id only (WAHA ignores the ``quote_*`` params).  Without that id the
        # reply would be dropped or attached to the wrong target, so refuse
        # before creating an optimistic bubble.
        if reply_data and protocol == PROTOCOL_WHATSAPP and not reply_to_message_id:
            self._status(
                "❌ Cannot reply: the original WhatsApp message ID is unavailable",
                0,
            )
            return

        # Save to SQLite (incremental INSERT), protocol-aware.
        data = {
            "text": message,
            "is_mine": True,
            "sender": "You",
            "timestamp": ts,
            "quote_text": quote_text,
            "msg_type": "text",
            "attachment_info": None,
            "attachment_id": reply_data.get("attachment_id") if reply_data else None,
            "content_type": reply_data.get("content_type") if reply_data else None,
            "status": "pending",
            "protocol": protocol,
            "quote_timestamp": reply_data.get("timestamp") if reply_data else None,
            "quote_author": contact_id if reply_data else None,
            "reply_to_message_id": reply_to_message_id,
            # Quoted-media thumbnail metadata (display-only; wire #37 unchanged).
            "quote_attachment_id": (
                reply_data.get("quote_attachment_id") if reply_data else None
            ),
            "quote_attachment_path": (
                reply_data.get("quote_attachment_path") if reply_data else None
            ),
            "quote_content_type": (
                reply_data.get("quote_content_type") if reply_data else None
            ),
        }
        # Ingerisci l'ottimista nel backend CORRETTO del contatto (non hardcoded
        # su signal_backend): per WhatsApp deve finire nel WhatsAppBackend.cache,
        # altrimenti il polling dei messaggi non lo riconosce e ri-accoderebbe
        # l'echo -> messaggio mostrato DOPPIO (che si sistemava solo rientrando).
        ingest_backend = self.manager.get(protocol)
        if ingest_backend is None:
            ingest_backend = self.signal_backend
        added = ingest_backend.ingest_message(contact_id, data, ts, persist=False)
        logger.debug(
            "optimistic ingest: added=%s protocol=%s contact=%r ts=%s text=%r",
            added,
            protocol,
            contact_id,
            ts,
            message[:60],
        )

        # Update in-memory cache for UI
        if cache_key not in self._cache:
            self._cache[cache_key] = []
        self._cache[cache_key].append(
            {
                "text": message,
                "is_mine": True,
                "sender": "You",
                "timestamp": ts,
                "quote_text": quote_text,
                "msg_type": "text",
                "attachment_info": None,
                "attachment_id": data["attachment_id"],
                "content_type": data["content_type"],
                "read": True,
                "status": "pending",
                "quote_timestamp": data["quote_timestamp"],
                "quote_author": data["quote_author"],
                "reply_to_message_id": reply_to_message_id,
                "quote_attachment_id": data["quote_attachment_id"],
                "quote_attachment_path": data["quote_attachment_path"],
                "quote_content_type": data["quote_content_type"],
            }
        )

        self._promote_contact_after_send(contact, ts)

        # Show the message in the UI immediately (with quote if replying)

        self._add_message(
            message,
            is_mine=True,
            quote_text=quote_text,
            quote_attachment_id=(
                reply_data.get("quote_attachment_id") if reply_data else None
            ),
            quote_attachment_path=(
                reply_data.get("quote_attachment_path") if reply_data else None
            ),
            quote_content_type=(
                reply_data.get("quote_content_type") if reply_data else None
            ),
            timestamp=ts,
            sender="You",
            status="pending",
            message_id=None,
        )
        self._seen_timestamps.add((protocol, cache_key, ts))
        self._seen_message_ids.add((protocol, cache_key, int(ts), message))

        event.text_area.text = ""

        # Cancel the reply highlight
        self._cancel_reply()

        self.run_worker(
            lambda msg=message, ts=ts, rdata=reply_data, persist=((ingest_backend, contact_id, data, ts) if added else None), protocol=protocol, contact_id=contact_id: (
                self._send_message_worker(
                    msg,
                    ts,
                    rdata,
                    persist=persist,
                    protocol=protocol,
                    contact_id=contact_id,
                )
            ),
            exclusive=False,
            thread=True,
        )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Keep synthetic legacy submissions working without handling real Inputs."""
        if isinstance(event, Input.Submitted):
            return
        self.on_message_text_area_submitted(event)  # type: ignore[arg-type]

    def _send_message_worker(
        self,
        message: str,
        timestamp: int,
        reply_data: dict | None = None,
        persist: tuple | None = None,
        *,
        protocol: str,
        contact_id: str,
    ):
        """Send a message via the active backend's send path.

        Parameters
        ----------
        message:
            The message text to send.
        timestamp:
            The client-generated timestamp (ms) used as the message ID.
            Passed to the backend so that receipt timestamps match.
        reply_data:
            If provided, the message is sent as a quote/reply.
        persist:
            Optional ``(backend, contact_id, data, ts)`` payload.  When given,
            the optimistic row is persisted to SQLite here (worker thread),
            BEFORE the network send, so the echo always finds the row to
            upgrade via ``_update_message_id``.
        protocol:
            The protocol selected when the message was submitted.
        contact_id:
            The contact ID selected when the message was submitted.
        """
        if persist is not None:
            persist_backend, persist_contact_id, data, ts = persist
            persist_backend._persist_message(persist_contact_id, data, ts)
            if getattr(self, "_web_enabled", False):
                from web.bridge import push_event

                push_event(
                    {
                        "type": "message",
                        "payload": {
                            "id": data.get("id"),
                            "protocol": protocol,
                            "contact_id": persist_contact_id,
                            "timestamp": ts,
                        },
                    }
                )

        # Extract quote parameters from reply_data
        # quote_author MUST be a contact id, not a display name.
        # Use the contact selected at submission because we are replying to
        # the person we were chatting with.
        quote_timestamp = reply_data.get("timestamp") if reply_data else None
        quote_author = contact_id if reply_data else None
        quote_message = reply_data.get("text") if reply_data else None
        reply_to_message_id = reply_data.get("message_id") if reply_data else None

        # Wire-faithful quote body (R1/R2): a media reply carries the explicit
        # ``quote_wire_body`` key (real caption or None), so the Signal quote on
        # the wire uses that body (caption or "") — never the display placeholder
        # and never omitted.  Text replies never set the key, so their text is
        # used unchanged.  WA/TG ignore ``quote_message`` anyway.
        if reply_data is not None and "quote_wire_body" in reply_data:
            quote_message = reply_data["quote_wire_body"] or ""

        # Signal media reply → build the ``quoteAttachments`` descriptor so the
        # recipient renders the quoted thumbnail (bug #37, piano B).  Only for
        # Signal (WA/TG quote natively via ``reply_to``) and only for media
        # replies (the ``quote_wire_body`` key is present only there).
        quote_attachments: list[str] | None = None
        if (
            reply_data is not None
            and "quote_wire_body" in reply_data
            and protocol == PROTOCOL_SIGNAL
        ):
            content_type = _quote_content_type(reply_data)
            if content_type:
                attachment_id = reply_data.get("attachment_id")
                preview_path = (
                    get_attachment_path(attachment_id) if attachment_id else None
                )
                if preview_path is not None:
                    # signal-cli parses ``quoteAttachments`` with the regex
                    # ``([^:]+)(:([^:]+)(:(.+))?)?`` (G1=contentType,
                    # G3=filename, G5=previewFile): the filename is REQUIRED
                    # when the second block is present, so ``contentType::file``
                    # is invalid.  Use ``contentType:filename:previewFile`` with
                    # the basename of the local attachment as the (cosmetic)
                    # filename.
                    filename = Path(preview_path).name
                    quote_attachments = [f"{content_type}:{filename}:{preview_path}"]
                else:
                    # No local file → quoted attachment without thumbnail:
                    # ``contentType`` alone is a valid descriptor.
                    quote_attachments = [content_type]

        # Send synchronously through the submission contact's backend. This is
        # a sync call running in a worker thread; it is NOT an async coroutine
        # that needs awaiting, which would otherwise be silently dropped.
        backend = self.manager.get(protocol)
        if backend is None:
            self._transition_outgoing_status(
                protocol, contact_id, timestamp, message, "failed", ("pending",)
            )
            self.call_from_thread(
                self._status, f"❌ No backend for protocol: {protocol}", 0
            )
            return

        try:
            send_kwargs = {
                "quote_timestamp": quote_timestamp,
                "quote_author": quote_author,
                "quote_message": quote_message,
            }
            if reply_to_message_id is not None:
                send_kwargs["reply_to_message_id"] = reply_to_message_id
            if quote_attachments is not None:
                send_kwargs["quote_attachments"] = quote_attachments
            # Instrumentation: la durata di send_message_sync è il tempo in cui
            # la bolla resta "grigia" (pending).  Log a debug; loggato a warning
            # oltre la soglia (1000ms) per individuare backoff/degrado del backend.
            logger.debug(
                "worker send start: persist=%s protocol=%s contact=%r ts=%s text=%r",
                persist is not None,
                protocol,
                contact_id,
                timestamp,
                message[:60],
            )
            _t0 = time.perf_counter()
            result = backend.send_message_sync(contact_id, message, **send_kwargs)
            _elapsed_ms = (time.perf_counter() - _t0) * 1000.0
            if _elapsed_ms >= 1000.0:
                logger.warning(
                    "send_message_sync slow: protocol=%s contact=%s %.0f ms",
                    protocol,
                    contact_id,
                    _elapsed_ms,
                )
            else:
                logger.debug(
                    "send_message_sync took %.1f ms (protocol=%s)",
                    _elapsed_ms,
                    protocol,
                )
            self._transition_outgoing_status(
                protocol, contact_id, timestamp, message, "sent", ("pending",)
            )
            # For Telegram, WhatsApp and Signal, ingest the real server message
            # id to upgrade the optimistic entry (so the echo matches by id and
            # never rewrites the timestamp).  Signal's ingest has an upgrade
            # branch that attaches the id without touching the timestamp.
            if (
                protocol in (PROTOCOL_TELEGRAM, PROTOCOL_WHATSAPP, PROTOCOL_SIGNAL)
                and result
            ):
                ingest_backend = self.manager.get(protocol)
                if ingest_backend is not None:
                    ingest_backend.ingest_message(
                        contact_id,
                        {
                            "id": result,
                            "text": message,
                            "is_mine": True,
                            "sender": "You",
                            "timestamp": timestamp,
                            "quote_text": quote_message,
                            "quote_timestamp": quote_timestamp,
                            "quote_author": quote_author,
                            "reply_to_message_id": reply_to_message_id,
                        },
                        timestamp,
                    )
                    self._update_outgoing_message_id(
                        protocol, contact_id, timestamp, message, str(result)
                    )
        except Exception as e:
            # Strumentazione: l'eccezione del send va SOLO su _status (barra di
            # stato, che resta "permanente"); senza log è impossibile capire il
            # motivo del fallimento.  Loggiamo la causa e l'esito del passaggio
            # a failed.
            logger.exception(
                "send_message_sync raised (protocol=%r contact=%r ts=%s text=%r)",
                protocol,
                contact_id,
                timestamp,
                message[:60],
            )
            _failed_ok = self._transition_outgoing_status(
                protocol, contact_id, timestamp, message, "failed", ("pending",)
            )
            logger.warning(
                "send failed: transition to 'failed' ok=%s (protocol=%r contact=%r)",
                _failed_ok,
                protocol,
                contact_id,
            )
            self.call_from_thread(self._status, f"❌ Send error: {e}", 0)

    def _transition_outgoing_status(
        self,
        protocol: str,
        contact_id: str,
        timestamp: int,
        text: str,
        status: str,
        expected_statuses: tuple[str, ...],
    ) -> bool:
        """Atomically advance one optimistic message across every cache layer."""
        from backend import _update_message_status, _update_message_status_by_text
        from models import contact_cache_key

        if not _update_message_status(
            timestamp,
            status,
            protocol,
            contact_id,
            text=text,
            expected_statuses=expected_statuses,
        ):
            # Fallback: l'echo (spesso più veloce del worker) può aver sostituito
            # il timestamp ottimistico del client con quello del server, quindi il
            # match per timestamp fallisce.  Riprova sul testo (riga outgoing più
            # recente), con lo stesso expected-status e rank guard.
            updated = _update_message_status_by_text(
                text,
                status,
                protocol,
                contact_id,
                expected_statuses=expected_statuses,
            )
            if not updated:
                # Diagnosi: al momento del fallimento, quante righe corrispondono
                # per testo / per timestamp (is_mine) — capisce se la riga non è
                # ancora stata persistita (persist saltato / race con l'echo).
                rows_text = rows_ts = -1
                try:
                    import sqlite3

                    from backend import DB_FILE

                    conn = sqlite3.connect(DB_FILE)
                    try:
                        rows_text = conn.execute(
                            "SELECT COUNT(*) FROM messages WHERE protocol=? "
                            "AND contact_number=? AND text=? AND is_mine=1",
                            (protocol, contact_id, text),
                        ).fetchone()[0]
                        rows_ts = conn.execute(
                            "SELECT COUNT(*) FROM messages WHERE protocol=? "
                            "AND contact_number=? AND timestamp=? AND is_mine=1",
                            (protocol, contact_id, timestamp),
                        ).fetchone()[0]
                    finally:
                        conn.close()
                except Exception as _dbg:
                    logger.debug("diagnostic row count failed", exc_info=_dbg)
                logger.warning(
                    "Outgoing status transition failed "
                    "(protocol=%r, contact_id=%r, ts=%r, text=%r, status=%r, "
                    "rows_by_text=%s, rows_by_ts=%s)",
                    protocol,
                    contact_id,
                    timestamp,
                    text[:80],
                    status,
                    rows_text,
                    rows_ts,
                )
                return False
        backend = self.manager.get(protocol)
        if backend is None:
            backend = self.signal_backend
        for msg in getattr(backend, "cache", {}).get(contact_id, []):
            if (
                msg.get("is_mine")
                and msg.get("text") == text
                and msg.get("status") in expected_statuses
            ):
                msg["status"] = status
        for msg in self._cache.get(contact_cache_key(protocol, contact_id), []):
            if (
                msg.get("is_mine")
                and msg.get("text") == text
                and msg.get("status") in expected_statuses
            ):
                msg["status"] = status
        self.call_from_thread(
            self._update_message_widgets_status,
            [{"timestamp": timestamp, "text": text, "status": status}],
        )
        return True

    def _update_outgoing_message_id(
        self,
        protocol: str,
        contact_id: str,
        timestamp: int,
        text: str,
        message_id: str,
    ) -> None:
        """Synchronize the real server id into the UI cache and widget.

        Used for Telegram and WhatsApp: their send paths return the
        server-assigned message id, which must be mirrored into the UI cache so
        a later receipt/echo can be matched by id (and never rewrite the
        optimistic timestamp).
        """
        from models import contact_cache_key

        for msg in self._cache.get(contact_cache_key(protocol, contact_id), []):
            if (
                msg.get("is_mine")
                and msg.get("timestamp") == timestamp
                and msg.get("text") == text
            ):
                msg["id"] = message_id
        self.call_from_thread(
            self._update_outgoing_message_widget_id, timestamp, text, message_id
        )

    def _update_outgoing_message_widget_id(
        self, timestamp: int, text: str, message_id: str
    ) -> None:
        """Update the mounted optimistic bubble on Textual's UI thread."""
        try:
            for child in self.chat_log.children:
                if (
                    getattr(child, "_msg_is_mine", False)
                    and getattr(child, "_msg_timestamp", None) == timestamp
                    and getattr(child, "_msg_text", None) == text
                ):
                    child._message_id = message_id
        except Exception:
            logger.debug("Unable to update optimistic message widget id", exc_info=True)

    def _retry_failed_message(self, timestamp: int, text: str) -> None:
        """Retry a failed optimistic message without creating another row or bubble."""
        contact = self.selected_contact
        if contact is None:
            logger.warning(
                "retry aborted: selected_contact is None (ts=%s text=%r)",
                timestamp,
                text[:40],
            )
            return
        cache_entries = self._cache.get(contact.cache_key, [])
        message = next(
            (
                item
                for item in cache_entries
                if item.get("is_mine")
                and item.get("timestamp") == timestamp
                and item.get("text") == text
                and item.get("status") == "failed"
            ),
            None,
        )
        if message is None:
            logger.warning(
                "retry aborted: entry not found in UI cache (contact=%r cache_key=%r "
                "n_entries=%d ts=%s text=%r; statuses=%r)",
                contact.id,
                contact.cache_key,
                len(cache_entries),
                timestamp,
                text[:40],
                [m.get("status") for m in cache_entries if m.get("is_mine")][:10],
            )
            return
        if (
            contact.protocol == PROTOCOL_TELEGRAM
            and message.get("reply_to_message_id") is None
            and (
                message.get("quote_timestamp") is not None
                or message.get("quote_author") is not None
            )
        ):
            self.call_from_thread(
                self._status,
                "❌ Cannot retry a Telegram reply; original message ID is unavailable",
                0,
            )
            return
        if message.get("quote_text") and message.get("quote_timestamp") is None:
            self.call_from_thread(
                self._status,
                "❌ Cannot retry a reply after reload; quote metadata is unavailable",
                0,
            )
            return
        if not self._transition_outgoing_status(
            contact.protocol, contact.id, timestamp, text, "pending", ("failed",)
        ):
            logger.warning(
                "retry aborted: transition failed→pending rejected "
                "(protocol=%r contact=%r ts=%s text=%r)",
                contact.protocol,
                contact.id,
                timestamp,
                text[:40],
            )
            return
        logger.debug(
            "retry started: protocol=%r contact=%r ts=%s text=%r",
            contact.protocol,
            contact.id,
            timestamp,
            text[:40],
        )
        reply_data = None
        if message.get("quote_text"):
            reply_data = {
                "text": message["quote_text"],
                "timestamp": message["quote_timestamp"],
            }
            if message.get("reply_to_message_id") is not None:
                reply_data["message_id"] = message["reply_to_message_id"]
            # A media reply carries the quoted attachment metadata on its own
            # row (persisted at submit time).  Reconstruct it so the retry
            # rebuilds ``quoteAttachments`` exactly like the live send.  The
            # media marker is the presence of that metadata — NOT the display
            # placeholder — so a captioned media reply (whose persisted
            # ``quote_text`` is the real caption) is reconstructed too.
            is_media_reply = (
                message.get("content_type") is not None
                or message.get("attachment_id") is not None
            )
            if is_media_reply:
                reply_data["content_type"] = message.get("content_type")
                reply_data["attachment_id"] = message.get("attachment_id")
                # R2-bis: a media reply without caption has only the display
                # placeholder persisted; reconstruct a faithful empty body so
                # the Signal quote is never the placeholder.  For captioned
                # media the wire body is the caption itself (``quote_text``).
                if is_media_quote_placeholder(message["quote_text"]):
                    reply_data["quote_wire_body"] = ""
                else:
                    reply_data["quote_wire_body"] = message["quote_text"]
            elif is_media_quote_placeholder(message["quote_text"]):
                # Legacy captionless media row (pre-piano-B, no metadata):
                # keep the wire body empty (R2-bis) but there is nothing to
                # rebuild the thumbnail from → V2 behaviour.
                reply_data["quote_wire_body"] = ""
        self.run_worker(
            lambda: self._send_message_worker(
                text,
                timestamp,
                reply_data,
                protocol=contact.protocol,
                contact_id=contact.id,
            ),
            exclusive=False,
            thread=True,
        )
