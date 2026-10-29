"""Device linking (Signal / WhatsApp / Telegram) as a core service.

The blocking and stateful parts of linking a new account live here, in the
core: the ``signal-cli link`` subprocess, the WAHA REST calls, the Telegram QR
login (Telethon) and the persistence of the linked Signal account.  A frontend
only renders what the ``link.*`` operations return (a QR payload, a progress
state) and sends the user's input back (device name, 2FA password).

The service is a *passive* state machine: it owns no timers.  The frontend
drives it: ``start`` once, then ``poll`` every couple of seconds (and gives up
after its own deadline, 5 minutes in the TUI).  A session abandoned without
``cancel`` (a frontend that vanished) is reaped after :data:`SESSION_MAX_AGE`
so a ``signal-cli`` subprocess never lingers.

Errors are plain exceptions (:class:`LinkError`, :class:`LinkUnavailable`);
``server.ops.link`` maps them to API errors.  This module must not import
Textual, FastAPI, ``tui`` or ``web``.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from models import DEFAULT_SIGNAL_DEVICE_NAME

logger = logging.getLogger(__name__)

#: The QR code is renewed after this many seconds (WhatsApp and Telegram
#: tokens expire after about a minute).
QR_REFRESH_AFTER_S = 60

#: A session no frontend polled or cancelled for this long is terminated.
SESSION_MAX_AGE = 900

#: WAHA session states in which a new QR can be requested without a reset.
_WA_SCAN_STATES = ("scan_qr", "scan_qr_code", "unpaired", "pending")


class LinkError(RuntimeError):
    """A link step failed; the message is shown to the user as it is."""


class LinkUnavailable(LinkError):
    """The protocol's backend is missing or not configured."""


class NoLinkInProgress(LinkError):
    """``poll`` for a protocol that has no started (or already ended) link."""


Qr = dict[str, str]


def _qr(kind: str, data: str) -> Qr:
    return {"kind": kind, "data": data}


def _qr_from_png(png: bytes) -> Qr:
    return _qr("png", base64.b64encode(png).decode("ascii"))


def _qr_from_waha(qr: str | bytes | None, missing: str) -> Qr:
    """WAHA answers a PNG (bytes) or the older text form (str)."""
    if isinstance(qr, bytes):
        return _qr_from_png(qr)
    if isinstance(qr, str):
        return _qr("text", qr)
    return _qr("text", missing)


def _qr_from_telegram(raw: str) -> Qr:
    """The Telegram backend reports ``INFO:``/``ERROR:`` strings or a URL."""
    if raw.startswith("INFO:"):
        return _qr("info", raw[5:])
    if raw.startswith("ERROR:"):
        return _qr("error", raw[7:])
    return _qr("url", raw)


def whatsapp_status_is_linked(status: dict) -> bool:
    """Require both WORKING state and an authenticated account identity."""
    state = str(status.get("status") or "").lower()
    return state == "working" and bool(status.get("me"))


# ─── Signal accounts (signal-cli) ──────────────────────────────────────────


def list_signal_accounts() -> list[str]:
    """Return canonical E.164 account numbers reported by signal-cli."""
    from protocols import rpc as signal_rpc

    result = subprocess.run(
        [str(signal_rpc.find_signal_cli()), "listAccounts"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("signal-cli could not list the linked accounts")
    return list(dict.fromkeys(re.findall(r"(?<!\d)\+\d{7,15}(?!\d)", result.stdout)))


def resolve_linked_signal_account(
    phone: str, previous_accounts: set[str] | None = None
) -> str:
    """Resolve the linked account without changing local configuration."""
    accounts = list_signal_accounts()
    new_accounts = set(accounts) - (previous_accounts or set())
    normalized_phone = re.sub(r"[\s()-]", "", phone)
    if len(new_accounts) == 1:
        account = new_accounts.pop()
    elif normalized_phone in accounts:
        account = normalized_phone
    elif len(accounts) == 1:
        account = accounts[0]
    elif not accounts:
        raise RuntimeError("signal-cli did not report a linked account")
    else:
        raise RuntimeError("multiple linked accounts found; select the account number")

    environment_account = os.environ.get("SIGNAL_USER_NUMBER")
    if environment_account and environment_account != account:
        raise RuntimeError(
            "SIGNAL_USER_NUMBER selects a different account; unset it and retry"
        )
    return account


def persist_signal_account(account: str) -> None:
    """Atomically save the linked account while preserving other settings."""
    from protocols import rpc as signal_rpc

    config_file = signal_rpc.PROJECT_DIR / "config.json"
    config: dict[str, object] = {}
    if config_file.exists():
        try:
            loaded = json.loads(config_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise RuntimeError("existing config.json is invalid or unreadable") from exc
        if not isinstance(loaded, dict):
            raise RuntimeError("existing config.json must contain a JSON object")
        config = loaded
    config["user_number"] = account

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=config_file.parent,
            prefix=".config.json.",
            delete=False,
        ) as temporary:
            json.dump(config, temporary, indent=2)
            temporary.write("\n")
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, config_file)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


# ─── Service ───────────────────────────────────────────────────────────────


@dataclass
class _Session:
    protocol: str
    phone: str = ""
    device_name: str = DEFAULT_SIGNAL_DEVICE_NAME
    created: float = field(default_factory=lambda: time.time())
    #: When the QR on screen was issued (it is renewed once it is old).
    qr_started: float = field(default_factory=lambda: time.time())
    #: Signal: the ``signal-cli link`` subprocess and the accounts that
    #: existed before it ran (to tell the new one apart).
    proc: subprocess.Popen | None = None
    accounts_before: set[str] = field(default_factory=set)
    #: Telegram: the 2FA request was already reported to the frontend.
    twofa_shown: bool = False


class LinkService:
    """Linking sessions, one per protocol, over the core's backends."""

    def __init__(self, manager: Callable[[], Any]) -> None:
        #: Callable returning the ``BackendManager`` (looked up on use).
        self._manager = manager
        self._lock = threading.Lock()
        self._sessions: dict[str, _Session] = {}

    def is_active(self, protocol: str) -> bool:
        """Whether a link flow is open for *protocol* (in memory, no I/O)."""
        with self._lock:
            return protocol in self._sessions

    # -- status -------------------------------------------------------------

    def status(self) -> dict[str, dict[str, Any]]:
        """Per protocol: configured (a link can start), account, linked.

        In-memory only: no daemon or WAHA call.
        """
        manager = self._manager()
        signal = manager.get("signal")
        account = str(getattr(signal, "user_number", "") or "")
        telegram = manager.get("telegram")
        return {
            "signal": {
                "available": signal is not None,
                "account": account,
                "linked": bool(account),
            },
            "whatsapp": {
                "available": manager.get("whatsapp") is not None,
                "account": "",
                "linked": None,
            },
            "telegram": {
                "available": telegram is not None,
                "account": "",
                "linked": (
                    None if telegram is None else not bool(telegram.needs_pairing)
                ),
            },
        }

    # -- sessions -----------------------------------------------------------

    def _reap(self) -> None:
        cutoff = time.time() - SESSION_MAX_AGE
        with self._lock:
            old = [s for s in self._sessions.values() if s.created < cutoff]
            for sess in old:
                del self._sessions[sess.protocol]
        for sess in old:
            logger.info("Reaping abandoned %s link session", sess.protocol)
            self._terminate(sess)

    @staticmethod
    def _terminate(sess: _Session) -> None:
        proc = sess.proc
        sess.proc = None
        if proc is not None and proc.poll() is None:
            logger.info("Killing signal-cli link subprocess (PID %s)", proc.pid)
            try:
                proc.terminate()
            except Exception:
                logger.debug("Failed to terminate link subprocess", exc_info=True)

    def _session(self, protocol: str) -> _Session | None:
        with self._lock:
            return self._sessions.get(protocol)

    def cancel(self, protocol: str) -> None:
        """Stop a link in progress (kills the ``signal-cli`` subprocess)."""
        with self._lock:
            sess = self._sessions.pop(protocol, None)
        if sess is not None:
            self._terminate(sess)

    def cancel_all(self) -> None:
        for protocol in list(self._sessions):
            self.cancel(protocol)

    # -- start --------------------------------------------------------------

    def start(
        self,
        protocol: str,
        phone: str = "",
        device_name: str = DEFAULT_SIGNAL_DEVICE_NAME,
    ) -> Qr:
        """Begin linking *protocol*; returns what to show.  Blocks (signal-cli
        startup, WAHA and Telegram requests): run it off the UI thread.

        A link already open for the protocol is cancelled first.
        """
        self._reap()
        sess = _Session(protocol, phone=phone, device_name=device_name)
        with self._lock:
            previous = self._sessions.get(protocol)
            self._sessions[protocol] = sess
        if previous is not None:
            self._terminate(previous)
        try:
            if protocol == "signal":
                return self._start_signal(sess)
            if protocol == "whatsapp":
                return self._start_whatsapp()
            if protocol == "telegram":
                return self._start_telegram()
            raise LinkError(f"Unknown protocol {protocol!r}")
        except BaseException:
            with self._lock:
                if self._sessions.get(protocol) is sess:
                    del self._sessions[protocol]
            self._terminate(sess)
            raise

    def _start_signal(self, sess: _Session) -> Qr:
        """Run ``signal-cli link`` and extract the ``sgnl://`` URL."""
        from protocols.rpc import find_signal_cli

        try:
            sess.accounts_before = set(list_signal_accounts())
        except Exception:
            sess.accounts_before = set()
            logger.debug("Could not snapshot Signal accounts", exc_info=True)
        args = [str(find_signal_cli()), "link", "-n", sess.device_name]
        logger.info("Starting signal-cli link: %s", args)
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        # Registered before the blocking read, so a cancel can end it.
        sess.proc = proc
        if self._session("signal") is not sess:  # cancelled while starting
            self._terminate(sess)
            raise LinkError("Link cancelled")

        link_found = None
        for line in iter(proc.stdout.readline, ""):  # type: ignore[union-attr]
            line = line.rstrip()
            match = re.search(r"((?:sgnl|signal)://link[^\s]*)", line)
            if match:
                link_found = match.group(1)
                break
            if "error" in line.lower() or "cannot" in line.lower():
                logger.warning("signal-cli reported an error during linking")

        if link_found:
            logger.info("Signal link URL found")
            return _qr("url", link_found)
        raise LinkError(
            "Could not find Signal link URL in signal-cli output. "
            "Is signal-cli configured correctly?"
        )

    def _wa_rest(self) -> Any:
        wa = self._manager().get("whatsapp")
        rest = getattr(wa, "_rest", None)
        if wa is None or rest is None:
            raise LinkUnavailable("WhatsApp backend not available")
        return rest

    def _start_whatsapp(self) -> Qr:
        """Current QR; restarts a dead session; "already linked" info."""
        rest = self._wa_rest()
        status = rest.get_session_status() or {}
        s = str(status.get("status") or "").lower()

        if whatsapp_status_is_linked(status):
            return _qr(
                "info", "WhatsApp is already linked and working ✅\nNo QR needed."
            )

        # If the session is in a dead/failed state, restart it
        if s in {"failed", "stopped", "stop", ""}:
            logger.info("WhatsApp session is %s, restarting for fresh QR...", s)
            qr = rest.get_fresh_pairing_qr(reset=True)
            if isinstance(qr, (bytes, str)):
                return _qr_from_waha(qr, "")
            raise LinkError(f"Failed to restart session (status was: {s})")

        # Try current QR, then fresh without reset
        qr = rest.get_pairing_qr()
        if isinstance(qr, (bytes, str)):
            return _qr_from_waha(qr, "")
        qr = rest.get_fresh_pairing_qr(reset=False)
        if isinstance(qr, (bytes, str)):
            return _qr_from_waha(qr, "")
        raise LinkError(
            f"No QR available (session status: {s or 'unknown'}). "
            "The session may already be linked."
        )

    def _telegram(self) -> Any:
        tb = self._manager().get("telegram")
        if tb is None:
            raise LinkUnavailable("Telegram backend not available")
        return tb

    def _start_telegram(self) -> Qr:
        qr = _qr_from_telegram(self._telegram().get_pairing_qr() or "ERROR: No QR data")
        if qr["kind"] == "error":
            raise LinkError(qr["data"])
        return qr

    # -- poll ---------------------------------------------------------------

    def poll(self, protocol: str) -> dict[str, Any]:
        """Progress of the started link (see ``LinkPollOut``).  May block
        (WAHA / Telegram requests): run it off the UI thread."""
        sess = self._session(protocol)
        if sess is None:
            raise NoLinkInProgress("No link in progress")
        if protocol == "signal":
            return self._poll_signal(sess)
        if protocol == "whatsapp":
            return self._poll_whatsapp(sess)
        return self._poll_telegram(sess)

    @staticmethod
    def _progress(
        state: str = "waiting", needs_2fa: bool = False, qr: Qr | None = None
    ) -> dict[str, Any]:
        return {"state": state, "needs_2fa": needs_2fa, "qr": qr}

    def _poll_signal(self, sess: _Session) -> dict[str, Any]:
        """Done when the ``signal-cli link`` subprocess exited successfully."""
        proc = sess.proc
        if proc is None:
            return self._progress()
        rc = proc.poll()
        if rc is None:
            return self._progress()  # still running
        return self._progress("finalize" if rc == 0 else "waiting")

    def _poll_whatsapp(self, sess: _Session) -> dict[str, Any]:
        """WAHA session status; renews the QR once it is old."""
        wa = self._manager().get("whatsapp")
        rest = getattr(wa, "_rest", None)
        if wa is None or rest is None:
            return self._progress()
        status = rest.get_session_status() or {}
        s = str(status.get("status") or "").lower()
        if whatsapp_status_is_linked(status):
            return self._progress("done")
        age = time.time() - sess.qr_started
        if age < QR_REFRESH_AFTER_S or s not in _WA_SCAN_STATES:
            return self._progress()
        logger.info("WhatsApp QR expired, refreshing...")
        try:
            qr = _qr_from_waha(
                rest.get_fresh_pairing_qr(reset=False), "ERROR: No fresh QR available"
            )
        except Exception:
            logger.exception("Failed to refresh WhatsApp QR")
            return self._progress()
        sess.qr_started = time.time()
        return self._progress(qr=qr)

    def _poll_telegram(self, sess: _Session) -> dict[str, Any]:
        """QR login completion, the 2FA request, and QR renewal."""
        tb = self._manager().get("telegram")
        if tb is None:
            return self._progress()
        # _connected is set to True by the background wait task
        if tb._connected:
            return self._progress("done")

        needs_2fa = False
        if getattr(tb, "_needs_2fa", False) and not sess.twofa_shown:
            sess.twofa_shown = True
            needs_2fa = True

        qr = None
        # Telegram QR tokens expire after about a minute
        if time.time() - sess.qr_started >= QR_REFRESH_AFTER_S:
            logger.info("Telegram QR expired, refreshing...")
            try:
                raw = tb.get_pairing_qr() or "ERROR: No QR data"
                sess.twofa_shown = False
                qr = _qr_from_telegram(raw)
                sess.qr_started = time.time()
            except Exception:
                logger.exception("Failed to refresh Telegram QR")
        return self._progress(needs_2fa=needs_2fa, qr=qr)

    # -- finish -------------------------------------------------------------

    def finalize_signal(self, phone: str = "") -> str:
        """After ``signal-cli link`` succeeded: find the new account, save it
        and activate it on the Signal backend.  Blocks (``signal-cli
        listAccounts``): run it off the UI thread."""
        sess = self._session("signal")
        account = resolve_linked_signal_account(
            phone, sess.accounts_before if sess is not None else set()
        )
        backend = self._manager().get("signal")
        current_account = getattr(backend, "user_number", "")
        if current_account and current_account != account:
            raise RuntimeError(
                "switching Signal accounts in an active profile is not supported"
            )
        persist_signal_account(account)
        if backend is not None:
            backend.user_number = account
        self.cancel("signal")
        return account

    def submit_2fa(self, password: str) -> bool:
        """Telegram: finish the login with the 2FA *password*.  Blocks."""
        return bool(self._telegram().complete_2fa(password))
