"""
Synchronous REST client for the Baileys-based WhatsApp HTTP API.

Endpoints follow the generic ``whatsapp-http-api`` contract; the base URL is
provided by ``backends.config.get_whatsapp_api_url``.  Configuration values
(session name, API key) are read through ``backends.whatsapp`` so that tests
can patch them on that module.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from pathlib import Path

import backends.whatsapp as _wa

logger = logging.getLogger(__name__)

_DATA_URL_RE = re.compile(r"data:[^;,\s]+;base64,[A-Za-z0-9+/=_-]+", re.IGNORECASE)
_MEDIA_FILE_EXTENSIONS = (
    ".jpeg",
    ".jpg",
    ".png",
    ".webp",
    ".gif",
    ".mp4",
    ".mp3",
    ".pdf",
    ".opus",
    ".ogg",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".txt",
    ".zip",
)


class WhatsAppRESTClient:
    """Minimal synchronous JSON client for the Baileys-based HTTP API.

    Endpoints follow the generic ``whatsapp-http-api`` contract; the base URL
    is provided by ``backends.config.get_whatsapp_api_url``.
    """

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.session_name = _wa.get_whatsapp_session_name()
        self.api_key = _wa.get_whatsapp_api_key()
        # HTTP status of the most recent _request (0 if never attempted).
        self.last_status: int = 0
        self.last_error: str | None = None

    @staticmethod
    def _response_error(raw: bytes) -> str:
        text = raw.decode("utf-8", errors="replace").strip()
        text = _DATA_URL_RE.sub("[redacted data URL]", text)
        try:
            body = json.loads(text)
        except json.JSONDecodeError:
            return text[:500] or "empty response"
        if isinstance(body, dict):
            detail = body.get("message") or body.get("error") or body.get("detail")
            if detail is None and isinstance(body.get("exception"), dict):
                detail = body["exception"].get("message")
            if isinstance(detail, (str, int, float)):
                return str(detail)[:500]
            if isinstance(detail, list):
                return "; ".join(str(item) for item in detail)[:500]
        return text[:500] or "empty response"

    def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        timeout: int = 30,
        log_level: int = logging.ERROR,
    ) -> dict | None:
        """Execute an HTTP request and return the JSON body.

        When a WAHA API key is configured, it is sent as the ``X-Api-Key``
        header (WAHA returns ``401`` for unauthenticated calls).  Returns
        ``None`` on any transport/HTTP error (callers treat it as an
        unavailable service, matching Signal's error-tolerant style).
        """
        url = f"{self.base_url}{path}"
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        req = urllib.request.Request(
            url,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                self.last_status = getattr(resp, "status", 200)
                self.last_error = None
                raw = resp.read().decode("utf-8")
                if not raw:
                    return {}
                return json.loads(raw)
        except urllib.error.HTTPError as err:
            status = err.code
            detail = self._response_error(err.read())
            self.last_status = status
            self.last_error = detail
            log_detail = " ".join(str(detail).split())[:300]
            logger.log(
                log_level,
                "WAHA request failed: method=%s path=%s status=%s detail=%s",
                method,
                path,
                status,
                log_detail,
            )
            return None
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            detail = str(exc)[:500]
            self.last_status = 0
            self.last_error = detail
            log_detail = " ".join(str(detail).split())[:300]
            logger.log(
                log_level,
                "WAHA request failed: method=%s path=%s status=0 detail=%s",
                method,
                path,
                log_detail,
            )
            return None

    def _request_raw(self, method: str, path: str, timeout: int = 30) -> bytes | None:
        """Execute an HTTP request and return the raw (possibly binary) body.

        Used for endpoints WAHA serves as binary (e.g. the pairing QR as a PNG
        image).  Returns ``None`` on transport/HTTP error.
        """
        headers = {"Accept": "*/*"}
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                self.last_status = getattr(resp, "status", 200)
                return resp.read()
        except urllib.error.HTTPError as err:
            self.last_status = err.code
            return None
        except (urllib.error.URLError, OSError):
            self.last_status = 0
            return None

    # ── Sessions / pairing ────────────────────────────────────────────

    def create_session(self) -> dict | None:
        """Create the session and return the response (may include a QR)."""
        return self._request(
            "POST",
            "/api/sessions",
            {"name": self.session_name, "session": self.session_name},
        )

    def get_session_status(self) -> dict | None:
        """Return the current session status dict, or ``None`` on error."""
        return self._request("GET", f"/api/sessions/{self.session_name}")

    def update_session_config(self, config: dict) -> dict | None:
        """Update the config of an existing session via PUT /api/sessions/{name}.

        Used to register the WAHA push webhook(s) on the session itself (the
        WAHA_WEBHOOK_URL env var alone does not make WAHA emit events).  The
        config payload maps to SessionUpdateRequest, e.g.
        {webhooks: [{url: ..., events: [message, message.ack]}]}.  Returns the updated
        session dict, or None on error (callers treat it as best-effort).
        """
        return self._request("PUT", f"/api/sessions/{self.session_name}", config)

    def start_session(self) -> dict | None:
        """Create (if needed) and start the session via WAHA ``/api/sessions/start``."""
        return self._request(
            "POST",
            "/api/sessions/start",
            {
                "name": self.session_name,
            },
        )

    def reset_session(self, logout: bool = True) -> dict | None:
        """Force a clean pairing state so the next QR is guaranteed fresh and valid.

        A stale/expired QR is the most common reason WhatsApp answers *"can't link
        a new device right now"*.  We tear down the old session (logout/stop it)
        and let ``get_fresh_pairing_qr()`` start a brand-new one.  Uses WAHA's
        ``/api/sessions/logout`` (keeps the session object but invalidates the
        linked device), falling back to ``/api/sessions/stop``.
        """
        if logout:
            result = self._request(
                "POST", "/api/sessions/logout", {"name": self.session_name}
            )
            if result is not None or self.last_status in (200, 201, 204):
                return result
        return self._request("POST", "/api/sessions/stop", {"name": self.session_name})

    def get_fresh_pairing_qr(self, reset: bool = True) -> str | bytes | None:
        """Return a freshly-generated, valid pairing QR for immediate scanning.

        Always tears down any existing session first (so the QR is brand-new and
        not a stale/expired token) then asks WAHA to start a new session and
        returns its current QR (PNG bytes or text).  Returns ``None`` on failure.
        """
        if reset:
            self.reset_session()
        self.start_session()
        return self.get_session_qr()

    def get_pairing_qr(self) -> str | bytes | None:
        """Return the current pairing QR, or ``None`` if not available.

        WAHA exposes the QR as a binary PNG under ``/api/{session}/auth/qr``
        (current versions) or as text under ``/api/sessions/{session}/qr``
        (older/NOWEB versions).  This returns whatever QR WAHA currently has; use
        ``get_fresh_pairing_qr()`` for a guaranteed-fresh one.
        """
        qr = self.get_session_qr()
        if qr:
            return qr
        # A STOPPED session has no QR — ask WAHA to start it first.
        self.start_session()
        return self.get_session_qr()

    def get_session_qr(self) -> str | bytes | None:
        """Return the QR (PNG bytes or text string), or ``None``.

        Tries the current WAHA binary-PNG endpoint first, then falls back to the
        older textual endpoint.
        """
        # Current WAHA: PNG image under /api/{session}/auth/qr.
        png = self._request_raw("GET", f"/api/{self.session_name}/auth/qr")
        if png:
            return png
        # Older WAHA: JSON/text under /api/sessions/{session}/qr.
        result = self._request("GET", f"/api/sessions/{self.session_name}/qr")
        if not result:
            return None
        # WAHA returns the QR under various keys depending on version/format.
        for key in ("qr", "code", "pairingCode", "data"):
            val = result.get(key)
            if isinstance(val, str) and val:
                return val
        data = result.get("data")
        if isinstance(data, dict):
            for key in ("qr", "code", "pairingCode"):
                val = data.get(key)
                if isinstance(val, str) and val:
                    return val
        return None

    # ── Contacts ──────────────────────────────────────────────────────

    def list_contacts(self) -> list[dict]:
        """Return the raw contacts list (may contain nested ``data``).

        Tries the classic ``GET /api/contacts?session=...`` first.  On the
        current WAHA "core" builds that endpoint is broken (it may return 500
        with a ``TypeError`` in ``ContactsController``) — in that case we fall
        back to ``GET /api/{session}/chats``, which returns the session's chats
        (aka the active contacts) with ``id._serialized`` + ``name``.
        """
        timeout = 5
        chats = self._request(
            "GET",
            f"/api/{self.session_name}/chats",
            timeout=timeout,
        )
        if not chats or not isinstance(chats, list):
            return None if chats is None else []
        if not chats:
            return []  # API alive, no contacts yet
        out: list[dict] = []
        for chat in chats:
            cid = chat.get("id")
            if isinstance(cid, dict):
                cid = cid.get("_serialized") or cid.get("id")
            name = chat.get("name") or chat.get("pushName") or chat.get("notifyName")
            # Timestamp (seconds) of the last message/activity.  On WAHA
            # ``/api/{session}/chats`` the field is ``timestamp`` (epoch s) and
            # optionally ``lastMessage`` (object with ``t``).  Best-effort, 0
            # if absent so the contact simply sorts as "no messages yet".
            last_ts = 0
            raw_t = chat.get("timestamp") or chat.get("t")
            if isinstance(raw_t, (int, float)):
                last_ts = int(raw_t * 1000) if raw_t < 10**12 else int(raw_t)
            else:
                lm = chat.get("lastMessage") or chat.get("last_message")
                if isinstance(lm, dict):
                    ts = lm.get("t") or lm.get("timestamp")
                    if isinstance(ts, (int, float)):
                        last_ts = int(ts * 1000) if ts < 10**12 else int(ts)
            if cid:
                out.append(
                    {
                        "id": cid,
                        "name": name or cid,
                        "isGroup": bool(chat.get("isGroup")),
                        "last_ts": last_ts,
                        "unread": int(chat.get("unreadCount") or 0),
                    }
                )
        return out

    @staticmethod
    def _unwrap_contacts(result) -> list[dict]:
        """Normalize a contacts REST response into a flat list of dicts."""
        if not result:
            return []
        if isinstance(result, list):
            return result
        data = result.get("data") or result.get("contacts") or []
        return data if isinstance(data, list) else []

    def list_all_contacts(self) -> list[dict] | None:
        """Return the full address book via ``GET /api/contacts/all``.

        The response is a flat list of raw WAHA contact dicts (``id`` may be a
        plain string or a ``{"_serialized": ...}`` object, plus ``name`` and
        ``pushname``), normalized through ``_unwrap_contacts``.  Returns ``None``
        on transport/HTTP error (the existing ``_request`` contract).
        """
        result = self._request(
            "GET",
            f"/api/contacts/all?session={self.session_name}",
            timeout=10,
        )
        if result is None:
            return None
        return self._unwrap_contacts(result)

    def resolve_contact(self, jid: str) -> dict | None:
        """Resolve a JID via ``GET /api/{session}/contacts/{jid}`` (timeout 5).

        The path is percent-encoded (``quote(jid, safe="")``, same pattern as
        ``download_media``) so ``@lid``/``@c.us`` don't become URL userinfo.
        Used to map a ``@lid`` chat to its ``@c.us`` number.
        """
        from urllib.parse import quote

        encoded = quote(jid, safe="")
        return self._request(
            "GET",
            f"/api/{self.session_name}/contacts/{encoded}",
            timeout=5,
        )

    def check_number_exists(self, phone_digits: str) -> bool | None:
        """Best-effort check ``GET /api/contacts/check-exists`` (timeout 5).

        Tolerates both ``{"exists": bool}`` and ``{"numberExists": bool}``
        response shapes.  Returns ``None`` when the endpoint is absent/errors
        (best-effort contract, §5).
        """
        result = self._request(
            "GET",
            f"/api/contacts/check-exists?phone={phone_digits}"
            f"&session={self.session_name}",
            timeout=5,
        )
        if not isinstance(result, dict):
            return None
        if "exists" in result:
            return bool(result.get("exists"))
        if "numberExists" in result:
            return bool(result.get("numberExists"))
        return None

    # ── Messaging ─────────────────────────────────────────────────────

    def send_message(
        self,
        to: str,
        text: str,
        quote_timestamp: int | None = None,
        quote_author: str | None = None,
        quote_message: str | None = None,
        reply_to_message_id: str | None = None,
    ) -> dict | None:
        """Send a message via WAHA ``/api/sendText``.

        Returns the API response or ``None`` on error.
        """
        payload = {
            "session": self.session_name,
            "chatId": to,
            "text": text,
        }
        if reply_to_message_id is not None:
            payload["reply_to"] = reply_to_message_id
        return self._request("POST", "/api/sendText", payload)

    def send_image(
        self,
        chat_id: str,
        file_path: Path,
        caption: str | None = None,
        reply_to_message_id: str | None = None,
        mime_type: str | None = None,
        filename: str | None = None,
    ) -> dict | None:
        """Send a local image via WAHA ``/api/sendImage``."""
        return self._send_media(
            "/api/sendImage",
            chat_id,
            file_path,
            caption=caption,
            reply_to_message_id=reply_to_message_id,
            mime_type=mime_type,
            default_mime_type="image/jpeg",
            filename=filename,
        )

    def send_video(
        self,
        chat_id: str,
        file_path: Path,
        caption: str | None = None,
        reply_to_message_id: str | None = None,
        mime_type: str | None = None,
        filename: str | None = None,
    ) -> dict | None:
        """Send a local video via WAHA ``/api/sendVideo``."""
        return self._send_media(
            "/api/sendVideo",
            chat_id,
            file_path,
            caption=caption,
            reply_to_message_id=reply_to_message_id,
            mime_type=mime_type,
            default_mime_type="video/mp4",
            filename=filename,
        )

    def send_file(
        self,
        chat_id: str,
        file_path: Path,
        caption: str | None = None,
        reply_to_message_id: str | None = None,
        mime_type: str | None = None,
        filename: str | None = None,
    ) -> dict | None:
        """Send a local file via WAHA ``/api/sendFile``."""
        return self._send_media(
            "/api/sendFile",
            chat_id,
            file_path,
            caption=caption,
            reply_to_message_id=reply_to_message_id,
            mime_type=mime_type,
            default_mime_type="application/octet-stream",
            filename=filename,
        )

    def _send_media(
        self,
        endpoint: str,
        chat_id: str,
        file_path: Path,
        *,
        caption: str | None,
        reply_to_message_id: str | None,
        mime_type: str | None,
        default_mime_type: str,
        filename: str | None,
    ) -> dict | None:
        import base64
        import mimetypes

        path = Path(file_path)
        detected_type = (
            mime_type or mimetypes.guess_type(path.name)[0] or default_mime_type
        )
        payload = {
            "session": self.session_name,
            "chatId": chat_id,
            "file": {
                "mimetype": detected_type,
                "filename": filename or path.name,
                "data": base64.b64encode(path.read_bytes()).decode("ascii"),
            },
            "caption": caption or "",
        }
        if reply_to_message_id is not None:
            payload["reply_to"] = reply_to_message_id
        return self._request("POST", endpoint, payload)

    def edit_message(self, chat_id: str, message_id: str, text: str) -> dict | None:
        """WAHA ``PUT /api/{session}/chats/{chatId}/messages/{messageId}``.

        Body ``EditMessageRequest``: ``{"text": ..., "linkPreview": true}``.
        I path segment sono percent-encoded (pattern di ``resolve_contact``):
        gli id Baileys contengono ``@``/``=`` che romperebbero l'URL.
        Ritorna il messaggio aggiornato, ``None`` su errore (contratto _request).
        """
        from urllib.parse import quote

        return self._request(
            "PUT",
            f"/api/{self.session_name}/chats/{quote(chat_id, safe='')}"
            f"/messages/{quote(message_id, safe='')}",
            {"text": text, "linkPreview": True},
        )

    def get_message_media(self, chat_id: str, message_id: str) -> dict | None:
        """WAHA ``GET`` for one message with ``downloadMedia=true``.

        Forces media download and returns the complete message, including its
        media URL and mimetype.  Returns ``None`` on error, per ``_request``.
        """
        from urllib.parse import quote

        result = self._request(
            "GET",
            f"/api/{self.session_name}/chats/{quote(chat_id, safe='')}"
            f"/messages/{quote(message_id, safe='')}?downloadMedia=true",
        )
        return result if isinstance(result, dict) else None

    def list_messages(self, chat_id: str, limit: int = 1) -> list[dict]:
        """Fetch recent messages of a chat via ``GET /api/messages``.

        WAHA returns a list of message objects (``body`` holds the text, ``from``
        the JID, ``fromMe`` a bool, ``timestamp`` in seconds).  Returns ``[]`` on
        any error so callers can treat a slow/unreachable API as non-fatal.

        Usato SOLO per il caricamento dello storico di una chat all'apertura
        (``fetch_history``) — NON più per un polling periodico: la ricezione live
        arriva via webhook (Push).  Gira in un worker thread, quindi un timeout
        generoso non blocca la UI; WAHA può impiegare >10s a rispondere a
        ``/api/messages`` (sync lato telefono), e 3s facevano fallire il fetch
        silenziosamente (messaggi mancanti nella TUI).
        """
        result = self._request(
            "GET",
            f"/api/messages?session={self.session_name}&chatId={chat_id}&limit={int(limit)}",
            timeout=30,
        )
        if not isinstance(result, list):
            return []
        return result

    def mark_read(self, contact_id: str) -> dict | None:
        """Best-effort mark-read (WAHA Core may need a proxy/module).

        Returns ``None`` (treated as non-fatal) if the endpoint is unavailable.
        """
        return self._request(
            "POST",
            "/api/sendSeen",
            {
                "session": self.session_name,
                "chatId": contact_id,
            },
            log_level=logging.DEBUG,
        )

    def presence_subscribe(self, chat_id: str) -> dict | None:
        """Subscribe to presence updates for a chat (best-effort POST).

        WAHA only distributes a chat's ``presence.update`` events after
        ``POST /api/{session}/presence/{chatId}/subscribe``.  The JID is
        percent-encoded (same pattern as ``resolve_contact``) so ``@c.us`` /
        ``@g.us`` / ``@lid`` don't become URL userinfo.  Returns ``None`` on
        any error (best-effort contract).
        """
        from urllib.parse import quote

        encoded = quote(chat_id, safe="")
        return self._request(
            "POST",
            f"/api/{self.session_name}/presence/{encoded}/subscribe",
        )

    # ── Attachments ───────────────────────────────────────────────────

    def get_download_url(self, media_id: str) -> str | None:
        """Return a download URL for a media id (if the API exposes one)."""
        result = self._request("GET", f"/api/messages/{media_id}/download")
        if not result:
            return None
        return result.get("url") or (result.get("data") or {}).get("url")

    def download_media(self, media_id_or_url: str, timeout: int = 60) -> bytes | None:
        """Download a media file as raw bytes from WAHA.

        *media_id_or_url* can be either a plain message/media id or a full URL
        (WAHA's ``media.url`` field is often a direct HTTP link to the file).

        WhatsApp message IDs contain ``@`` (e.g. ``false_12345@lid_ABC``)
        which would be misinterpreted as URL userinfo.  Paths are always
        percent-encoded so ``@`` → ``%40``.

        Resolution order:
        1. If *media_id_or_url* looks like a URL (starts with ``http``) →
           rewrite container-internal ``localhost:3000`` to the real host port
           and fetch with API key auth; on failure, extract its media id.
        2. Try WAHA Core binary endpoint ``/api/{session}/{id}/download``.
        3. Fall back to legacy JSON endpoint → redirect URL → fetch.

        Returns the raw bytes on success, ``None`` on any error.
        """
        from urllib.parse import quote, unquote, urlparse

        # 0) Direct URL (WAHA media.url is often a full HTTP link).
        if media_id_or_url.startswith(("http://", "https://")):
            try:
                parsed = urlparse(media_id_or_url)
                # Rewrite container-internal port 3000 → real host port.
                # docker-compose maps 127.0.0.1:3005→3000, so localhost:3000
                # is unreachable from the host — must use the real base URL.
                host = parsed.netloc
                if "localhost:3000" in host or "127.0.0.1:3000" in host:
                    base_parsed = urlparse(self.base_url)
                    host = base_parsed.netloc  # e.g. localhost:3005
                safe_path = quote(unquote(parsed.path), safe="/")
                if parsed.query:
                    safe_path += "?" + parsed.query
                safe_url = parsed.scheme + "://" + host + safe_path
            except Exception as _e:
                logger.debug(
                    "Failed to rewrite media URL, using original", exc_info=True
                )
                safe_url = media_id_or_url
            try:
                headers = {"Accept": "*/*"}
                if self.api_key:
                    headers["X-Api-Key"] = self.api_key
                req = urllib.request.Request(safe_url, headers=headers, method="GET")
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    self.last_status = getattr(resp, "status", 200)
                    data = resp.read()
                    return data
            except (urllib.error.HTTPError, urllib.error.URLError, OSError):
                # URL non più servito da WAHA (es. media.url datato o storage
                # purgato): NON arrendersi — estrai l'id dal path e prova i
                # fallback per id sotto, che usano la forma canonica.
                logger.debug(
                    "WAHA media URL fetch failed, falling back to id endpoints: %s",
                    safe_url,
                )
                # Estrai l'id dal path ORIGINALE (non da safe_url, che è già
                # percent-encoded): l'id va ri-encodato una sola volta sotto.
                media_name = Path(unquote(urlparse(media_id_or_url).path)).name
                media_name_lower = media_name.lower()
                for extension in _MEDIA_FILE_EXTENSIONS:
                    if media_name_lower.endswith(extension):
                        media_name = media_name[: -len(extension)]
                        break
                media_id_or_url = media_name
        # 1) WAHA Core: direct binary endpoint — percent-encode the id
        #    so @lid / @c.us / @g.us don't become userinfo in the URL.
        encoded = quote(media_id_or_url, safe="")
        direct_url = f"/api/{self.session_name}/{encoded}/download"
        raw = self._request_raw(
            "GET",
            direct_url,
            timeout=timeout,
        )
        if raw:
            return raw
        # 2) Legacy: JSON endpoint that returns a redirect URL.
        download_url = self.get_download_url(media_id_or_url)
        if download_url:
            try:
                headers = {"Accept": "*/*"}
                req = urllib.request.Request(
                    download_url, headers=headers, method="GET"
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    self.last_status = getattr(resp, "status", 200)
                    data = resp.read()
                    return data
            except (urllib.error.HTTPError, urllib.error.URLError, OSError):
                return None
        # 2) WAHA Files API: /api/files/default/{id} — same endpoint used
        #    by media.url, but constructed from a bare message id.
        #    This rescues cached entries stored before media.url was captured
        #    and handles edge cases where media.url is absent.
        raw = self._request_raw("GET", f"/api/files/default/{encoded}", timeout=timeout)
        if raw:
            return raw
        return None

    def download_media_range(
        self,
        media_url: str,
        start: int | None,
        length: int,
        timeout: int = 10,
    ) -> bytes | None:
        """Fetch one byte range from a fresh WAHA media URL."""
        from urllib.parse import quote, unquote, urlparse

        if length <= 0 or not media_url.startswith(("http://", "https://")):
            return None
        try:
            parsed = urlparse(media_url)
            host = parsed.netloc
            if "localhost:3000" in host or "127.0.0.1:3000" in host:
                host = urlparse(self.base_url).netloc
            safe_path = quote(unquote(parsed.path), safe="/")
            if parsed.query:
                safe_path += "?" + parsed.query
            safe_url = parsed.scheme + "://" + host + safe_path
            range_value = (
                f"bytes=-{length}"
                if start is None
                else f"bytes={max(0, start)}-{max(0, start) + length - 1}"
            )
            headers = {"Accept": "*/*", "Range": range_value}
            if self.api_key:
                headers["X-Api-Key"] = self.api_key
            request = urllib.request.Request(safe_url, headers=headers, method="GET")
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = getattr(response, "status", None) or response.getcode()
                if status != 206 or not response.headers.get("Content-Range"):
                    return None
                return response.read(length)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
            logger.debug("WAHA media Range fetch failed", exc_info=True)
            return None
