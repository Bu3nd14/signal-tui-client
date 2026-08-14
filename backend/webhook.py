"""
WAHA webhook server (event-driven / push) for the Signal TUI Client.

Receives incoming-message POSTs from WAHA and forwards them to the WhatsApp
backend's ``handle_webhook``.  No Textual dependency.
"""

import http.server
import json
import logging
import os
import socketserver
import threading

logger = logging.getLogger(__name__)

# ─── WAHA webhook server (event-driven / push) ──────────────────────────────
# WAHA Core invia i messaggi in ingresso al client tramite webhook (POST a
# ``/webhook``) invece di un polling ``GET /api/messages``.  Questo server HTTP
# in thread daemon riceve quei pacchetti e li consegna al backend WhatsApp via
# ``handle_webhook``, che li normalizza e li accoda per la TUI.  Risponde subito
# ``200`` a WAHA per confermare la ricezione (evita retry e saturazione).

WEBHOOK_PORT = int(os.environ.get("CLIENT_WEBHOOK_PORT", "8088") or "8088")
_WEBHOOK_SERVER = None  # type: Optional[socketserver.TCPServer]


class _WebhookHTTPHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler that receives WAHA webhook POSTs on ``/webhook``.

    WAHA pushes ``{"event": "message", "session": "...", "payload": {...}}``.
    The handler extracts the JSON body, forwards it to the registered WhatsApp
    backend, and ALWAYS replies ``200`` (application/json) so WAHA doesn't
    retry.  Errors inside the handler are never fatal and never block the ack.
    """

    target = None  # set lazily by ensure_webhook_server()

    def do_POST(self):
        if not self.path.rstrip("/").endswith("/webhook"):
            return self._wh_response(404)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                data = json.loads(raw or b"{}")
            except (json.JSONDecodeError, ValueError):
                return self._wh_response(400)
        except Exception as _e:
            logger.debug("Failed to read webhook body", exc_info=True)
            return self._wh_response(400)
        # Forward to the WhatsApp backend (never raises into the handler).
        try:
            target = self.target
            if target is not None and isinstance(data, dict):
                target(data)
        except Exception as _e:
            logger.debug("Webhook target failed, still acking 200", exc_info=True)
        return self._wh_response(200)

    def _wh_response(self, code: int) -> None:
        body = json.dumps({"ok": code == 200}).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        except Exception as _e:
            logger.debug("Failed to send webhook response", exc_info=True)

    def log_message(self, format: str, *args) -> None:
        """Suppress default HTTP log output."""


def ensure_webhook_server(backend) -> int:
    """Start the persistent WAHA webhook server (idempotent) and bind *backend*.

    Registers ``backend.handle_webhook`` as the target for incoming webhook
    POSTs, starts the ``socketserver`` in a daemon thread if not running yet,
    and returns the client webhook port.  Safe to call multiple times (re-binds
    the current backend without restarting the socket).
    """
    global _WEBHOOK_SERVER

    if backend is not None and hasattr(backend, "handle_webhook"):
        _WebhookHTTPHandler.target = backend.handle_webhook

    if _WEBHOOK_SERVER is not None:
        return WEBHOOK_PORT

    socketserver.TCPServer.allow_reuse_address = True
    try:
        _WEBHOOK_SERVER = socketserver.TCPServer(
            ("0.0.0.0", WEBHOOK_PORT), _WebhookHTTPHandler
        )
    except OSError:
        # Porta già in uso o non bindabile: non blocchiamo l'avvio.  Il fallback
        # è che il webhook non venga consegnato (best-effort).
        return 0
    t = threading.Thread(target=_WEBHOOK_SERVER.serve_forever, daemon=True)
    t.start()
    return WEBHOOK_PORT
