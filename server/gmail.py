"""Synchronous IMAP/SMTP transport and MIME parsing for the Gmail integration.

Part of the core (bead mc-im6): it does not import any web framework; the
``gmail.*`` operations (``server/ops/gmail.py``) call it from worker threads.
IMAP connections are pooled; SMTP opens one connection per send.
"""

from __future__ import annotations

import base64
import concurrent.futures
import copy
import hashlib
import hmac
import imaplib
import logging
import math
import mimetypes
import os
import re
import secrets
import smtplib
import socket
import tempfile
import threading
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Iterable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.utils import formatdate, getaddresses, make_msgid, parsedate_to_datetime
from fractions import Fraction
from functools import lru_cache, partial
from html import unescape as _html_unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, unquote_to_bytes

from filename_utils import sanitize_filename
from protocols import config
from server.uploads import (
    GMAIL_MAX_ATTACHMENT_BYTES,
    GMAIL_MAX_ATTACHMENTS,
    GMAIL_MAX_TOTAL_ATTACHMENT_BYTES,
    cache_directory,
)

logger = logging.getLogger(__name__)

_MAX_TEXT_LENGTH = 64 * 1024
_MAX_ATTACHMENTS = GMAIL_MAX_ATTACHMENTS
_MAX_ATTACHMENT_BYTES = GMAIL_MAX_ATTACHMENT_BYTES
_MAX_TOTAL_ATTACHMENT_BYTES = GMAIL_MAX_TOTAL_ATTACHMENT_BYTES
_MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200
_MAX_BULK_DELETE = 200
_LIST_WINDOW = 200
_MAX_HTML_CHARS = 1_000_000
_HTML_MAX_TAGS = 8_000
# Inlined <style> CSS gives most elements a style attribute, so the count cap
# is generous; the real work bound is the total declaration budget (same
# tinycss2 work as the former 400 attributes x 32 declarations).
_HTML_MAX_STYLE_ATTRS = 2_000
_HTML_MAX_STYLE_DECLS_TOTAL = 12_800
_HTML_MAX_TEXT_CHARS = 1_000_000
_HTML_MAX_OUTPUT_CHARS = 1_000_000
_HTML_MAX_DEPTH = 128
_HTML_MAX_ATTRS_PER_TAG = 256
_HTML_MAX_ATTR_LEN = 16_384
_ATTR_VALUE_MAX_LEN = 2_048
_IMAGE_URL_MAX_LEN = 4_096
_STYLE_MAX_LEN = 1_024
_STYLE_SERIALIZED_MAX = 2_048
_STYLE_MAX_DECLS = 32
# Limits for inlining <style> blocks (css-inline): stylesheet size and rules.
_INLINE_CSS_MAX_CHARS = 65_536
_INLINE_CSS_MAX_RULES = 1_000
_GMAIL_LIST_CACHE_TTL_S = 15
_INBOX_CACHE_MAX_ENTRIES = 16
_LIST_FETCH_ITEM = (
    "(FLAGS BODY.PEEK[HEADER.FIELDS "
    "(FROM TO SUBJECT DATE CONTENT-TYPE CONTENT-DISPOSITION)])"
)
_PARTIAL_FETCH_ENABLED = True
_MAX_RENDER_PARTS = 8
_STRUCTURE_FETCH_ITEM = (
    "(BODYSTRUCTURE FLAGS BODY.PEEK[HEADER.FIELDS "
    "(FROM TO CC SUBJECT DATE MESSAGE-ID IN-REPLY-TO REFERENCES)])"
)

_IMAP_POOL_MAX_CONNECTIONS = 4
_IMAP_POOL_ACQUIRE_TIMEOUT_S = 2.0
_IMAP_POOL_MAX_IDLE_S = 600
_IMAP_POOL_NOOP_IDLE_S = 30
_IMAP_POOL_NOOP_TIMEOUT_S = 1.0
_IMAP_POOL_CLOSE_TIMEOUT_S = 0.5
_IMAP_POOL_CLOSE_TOTAL_S = 1.0
_IMAP_POOL_ENABLED = True
_POOL_FP_KEY = secrets.token_bytes(32)

# Conservative allow-list for rendered email HTML.  Remote images are
# rewritten to signed same-origin proxy URLs and loaded by default in the
# client (the user accepted tracking); inline CSS is re-serialized from a
# strict property/value allow-list, while <style> and classes are stripped.
# Only safe URL schemes survive; nh3 removes scripts and event handlers.
_ALLOWED_HTML_TAGS = frozenset(
    {
        "a",
        "abbr",
        "b",
        "blockquote",
        "br",
        "caption",
        "center",
        "cite",
        "code",
        "col",
        "colgroup",
        "dd",
        "del",
        "div",
        "dl",
        "dt",
        "em",
        "figcaption",
        "figure",
        "font",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
        "i",
        "img",
        "ins",
        "li",
        "mark",
        "ol",
        "p",
        "pre",
        "q",
        "s",
        "small",
        "span",
        "strike",
        "strong",
        "sub",
        "sup",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "u",
        "ul",
    }
)
_ALLOWED_HTML_ATTRIBUTES = {
    "*": {"dir", "style"},
    "a": {"href", "title"},
    "blockquote": {"data-quote"},
    "font": {"color", "face", "size"},
    "col": {"align", "bgcolor", "span", "valign", "width"},
    "colgroup": {"align", "bgcolor", "span", "valign", "width"},
    "div": {"data-quote"},
    "img": {"align", "alt", "data-src", "height", "width"},
    "li": {"value"},
    "ol": {"start"},
    "table": {
        "align",
        "bgcolor",
        "border",
        "cellpadding",
        "cellspacing",
        "height",
        "width",
    },
    "td": {
        "align",
        "bgcolor",
        "colspan",
        "height",
        "rowspan",
        "valign",
        "width",
    },
    "th": {
        "align",
        "bgcolor",
        "colspan",
        "height",
        "rowspan",
        "scope",
        "valign",
        "width",
    },
    "tr": {"align", "bgcolor", "height", "valign"},
}
_CLEAN_CONTENT_TAGS = {
    "base",
    "embed",
    "form",
    "head",
    "iframe",
    "link",
    "math",
    "meta",
    "noscript",
    "object",
    "script",
    "style",
    "svg",
    "template",
    "title",
}

_FOLDER_RE = re.compile(r"^[A-Za-z0-9._/\- ]+$")
_SIGNED_IMAGE_RE = re.compile(
    r"^/?gmail-image\?token=([A-Za-z0-9_-]+)\.([0-9a-f]{32})(?:&v=\d+)?$"
)
_SAFE_IMAGE_DATA_RE = re.compile(
    r"^data:image/(?:png|jpe?g|gif|webp);base64,", re.IGNORECASE
)
_IMAGE_PROXY_KEY = config.web_token().encode("utf-8") or secrets.token_bytes(32)
_IMAGE_PROXY_VERSION = 2
_UID_RE = re.compile(rb"\bUID\s+(\d+)")
_FLAGS_RE = re.compile(rb"FLAGS\s+\(([^)]*)\)")
_BODY_SECTION_RE = re.compile(rb"BODY\[([^\]]*)\]", re.IGNORECASE)
_PART_NUMBER_RE = re.compile(r"^[1-9][0-9]*(\.[1-9][0-9]*)*$")
_RFC2231_RE = re.compile(r"^([^']*)'([^']*)'(.*)$", re.DOTALL)
_RFC2231_CONT_RE = re.compile(r"^(.*)\*(\d+)(\*)?$", re.DOTALL)
_CHARSET_RE = re.compile(r"^[A-Za-z0-9._\-]+$")
_MIME_TOKEN_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+\-.^_`{|}~]+$")
_MAX_BODYSTRUCTURE_BYTES = 1024 * 1024
_MAX_BODYSTRUCTURE_DEPTH = 64
_MAX_BODYSTRUCTURE_TOKENS = 10_000
_MAX_BODYSTRUCTURE_TOKEN_BYTES = 64 * 1024


class GmailError(Exception):
    """Base class for Gmail transport/validation errors."""


class GmailNotConfigured(GmailError):
    """Raised when ``GMAIL_ADDRESS``/``GMAIL_APP_PASSWORD`` are missing."""


class GmailValidationError(GmailError):
    """Invalid client input; carries the HTTP status to return."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class GmailNotFound(GmailError):
    """Raised when a UID or attachment index does not exist."""


class GmailUpstreamError(GmailError):
    """Raised when IMAP/SMTP returns an unexpected response."""


@dataclass(frozen=True)
class GmailSettings:
    address: str
    password: str = field(repr=False)
    imap_host: str
    imap_port: int
    smtp_host: str
    smtp_port: int
    folder: str
    timeout_s: int


@dataclass(frozen=True)
class AttachmentDownload:
    filename: str
    content_type: str
    data: bytes


@dataclass(frozen=True)
class _InboxCacheEntry:
    uidvalidity: int
    messages: tuple[dict[str, Any], ...]
    expires_at: float


_InboxCacheKey = tuple[str, str]

_inbox_cache: OrderedDict[_InboxCacheKey, _InboxCacheEntry] = OrderedDict()
_inbox_epoch = 0
_inbox_cache_lock = threading.Lock()


def _now() -> float:
    """Indirection over ``time.monotonic`` so tests can freeze the clock."""
    return time.monotonic()


def _account_fingerprint(settings: GmailSettings) -> str:
    """Return an opaque account key: never log the address or the host."""
    raw = f"{settings.address}\0{settings.imap_host}\0{settings.imap_port}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class _PooledConn:
    conn_id: int
    conn: imaplib.IMAP4 | None
    fingerprint: str = field(repr=False)
    generation: int
    created_at: float
    last_used_at: float
    idle: bool
    discarded: bool


_pool_cond = threading.Condition(threading.Lock())
_pool_current_fingerprint: str | None = None
_pool_generation = 0
_pool_idle: list[_PooledConn] = []
_pool_active: dict[int, _PooledConn] = {}
_pool_conn_seq = 0


@lru_cache(maxsize=4)
def _pool_fingerprint_cached(
    address: str, host: str, port: int, timeout_s: float, password: str
) -> str:
    """Derive an opaque pool key with a password-appropriate KDF.

    This is not credential storage: the digest only separates pools when the
    account, endpoint, timeout, or credential changes.  PBKDF2 is used so the
    key derivation stays expensive even though the result is cached.
    """
    raw = f"{address}\0{host}\0{port}\0{timeout_s}\0{password}".encode()
    return hashlib.pbkdf2_hmac("sha256", raw, _POOL_FP_KEY, 120_000).hex()


def _pool_fingerprint(settings: GmailSettings) -> str:
    return _pool_fingerprint_cached(
        settings.address,
        settings.imap_host,
        settings.imap_port,
        settings.timeout_s,
        settings.password,
    )


def _next_conn_id_locked() -> int:
    global _pool_conn_seq
    _pool_conn_seq += 1
    return _pool_conn_seq


def _conn_socket(conn: imaplib.IMAP4 | None) -> socket.socket | None:
    if conn is None:
        return None
    try:
        return conn.sock
    except Exception:  # noqa: BLE001 - imaplib may raise on a dead socket
        return None


def _close_conn(conn: imaplib.IMAP4 | None) -> None:
    if conn is None:
        return
    sock = _conn_socket(conn)
    try:
        if sock is not None:
            try:
                sock.settimeout(_IMAP_POOL_CLOSE_TIMEOUT_S)
            except Exception:  # noqa: BLE001, S110 - best effort
                pass
        try:
            conn.logout()
        except Exception:  # noqa: BLE001 - best effort
            try:
                conn.shutdown()
            except Exception:  # noqa: BLE001, S110 - best effort
                pass
    finally:
        if sock is None:
            sock = _conn_socket(conn)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:  # noqa: BLE001, S110 - best effort
                pass
            try:
                sock.close()
            except Exception:  # noqa: BLE001, S110 - best effort
                pass


def _close_many(conns: Iterable[imaplib.IMAP4 | None]) -> None:
    pending = [conn for conn in conns if conn is not None]
    if not pending:
        return
    executor = ThreadPoolExecutor(max_workers=min(len(pending), 8))
    try:
        futures = [executor.submit(_close_conn, conn) for conn in pending]
        try:
            for _future in as_completed(futures, timeout=_IMAP_POOL_CLOSE_TOTAL_S):
                pass
        except (TimeoutError, concurrent.futures.TimeoutError):
            logger.debug("imap pool close reached deadline; abandoning sockets")
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _ensure_fingerprint_locked(fingerprint: str) -> list[imaplib.IMAP4]:
    global _pool_current_fingerprint, _pool_generation

    if _pool_current_fingerprint == fingerprint:
        return []
    to_close = list(_pool_idle)
    _pool_idle.clear()
    for pc in to_close:
        pc.idle = False
        pc.discarded = True
    for pc in _pool_active.values():
        pc.discarded = True
    _pool_generation += 1
    _pool_current_fingerprint = fingerprint
    _pool_cond.notify_all()
    return [pc.conn for pc in to_close if pc.conn is not None]


def _reap_idle_locked(now: float) -> list[imaplib.IMAP4]:
    if not _pool_idle:
        return []
    stale: list[_PooledConn] = []
    keep: list[_PooledConn] = []
    for pc in _pool_idle:
        if pc.discarded or now - pc.last_used_at > _IMAP_POOL_MAX_IDLE_S:
            pc.idle = False
            pc.discarded = True
            stale.append(pc)
        else:
            keep.append(pc)
    if not stale:
        return []
    _pool_idle[:] = keep
    return [pc.conn for pc in stale if pc.conn is not None]


def _teardown_locked() -> list[imaplib.IMAP4]:
    global _pool_current_fingerprint, _pool_generation

    to_close = list(_pool_idle)
    _pool_idle.clear()
    for pc in to_close:
        pc.idle = False
        pc.discarded = True
    for pc in _pool_active.values():
        pc.discarded = True
    _pool_generation += 1
    _pool_current_fingerprint = None
    _pool_cond.notify_all()
    return [pc.conn for pc in to_close if pc.conn is not None]


def _reset_imap_pool() -> None:
    with _pool_cond:
        to_close = _teardown_locked()
    _close_many(to_close)


def clear_imap_pool() -> None:
    _pool_fingerprint_cached.cache_clear()
    """Close every idle pooled connection and invalidate active ones."""
    _reset_imap_pool()


def close_imap_pool() -> None:
    """Shut the IMAP pool down; safe to call more than once."""
    _reset_imap_pool()


# -- temporary blobs ------------------------------------------------------------
#
# ``gmail.attachment`` / ``gmail.image`` hand adapters a local file (a blob), but
# the bytes come from the network, not from a cache the core already owns.  They
# are written here, 0600 in a 0700 directory, and deleted by whoever delivered
# them (the HTTP adapter does, right after sending); anything left behind is
# pruned by age on the next write and wiped when the core shuts down.

_BLOB_DIR_NAME = "gmail-blobs"
BLOB_MAX_AGE_S = 15 * 60


def blob_directory() -> Path:
    """``<cache dir>/gmail-blobs`` (``CACHE_DIR`` is read at call time)."""
    return cache_directory() / _BLOB_DIR_NAME


def prune_blobs(*, max_age_s: float = BLOB_MAX_AGE_S) -> None:
    """Delete temporary blobs older than *max_age_s* (``0`` wipes them all)."""
    directory = blob_directory()
    cutoff = time.time() - max_age_s
    try:
        entries = list(directory.iterdir())
    except OSError:
        return
    for path in entries:
        try:
            if (
                path.is_file()
                and not path.is_symlink()
                and (max_age_s <= 0 or path.stat().st_mtime < cutoff)
            ):
                path.unlink()
        except OSError:
            continue


def write_blob(data: bytes, suffix: str = "") -> Path:
    """Store *data* as a new private temporary blob; return its path."""
    directory = blob_directory()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    prune_blobs()
    with tempfile.NamedTemporaryFile(
        dir=directory, prefix="blob-", suffix=suffix, delete=False
    ) as handle:
        path = Path(handle.name)
        try:
            os.chmod(path, 0o600)
            handle.write(data)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
    return path


def close_resources() -> None:
    """Release what Gmail holds in the core: the IMAP pool and temporary blobs."""
    close_imap_pool()
    prune_blobs(max_age_s=0)


def _create_and_login(pc: _PooledConn, settings: GmailSettings) -> None:
    conn: imaplib.IMAP4 | None = None
    try:
        conn = imaplib.IMAP4_SSL(
            settings.imap_host,
            settings.imap_port,
            timeout=settings.timeout_s,
        )
        conn.login(settings.address, settings.password)
    except BaseException:
        with _pool_cond:
            _pool_active.pop(pc.conn_id, None)
            pc.idle = False
            pc.discarded = True
            _pool_cond.notify_all()
        _close_conn(conn)
        raise
    pc.conn = conn
    pc.last_used_at = _now()


def _needs_noop(pc: _PooledConn) -> bool:
    return _now() - pc.last_used_at > _IMAP_POOL_NOOP_IDLE_S


def _noop_or_reconnect(pc: _PooledConn, settings: GmailSettings) -> _PooledConn:
    conn = pc.conn
    sock = _conn_socket(conn)
    previous_timeout: float | None = None
    healthy = False
    try:
        if sock is not None:
            try:
                previous_timeout = sock.gettimeout()
                sock.settimeout(_IMAP_POOL_NOOP_TIMEOUT_S)
            except OSError:
                sock = None
        if conn is not None:
            status, _ = conn.noop()
            healthy = status == "OK"
    except Exception:  # noqa: BLE001 - any NOOP failure discards the connection
        healthy = False
    finally:
        if sock is not None:
            try:
                sock.settimeout(previous_timeout)
            except OSError:
                pass
    if healthy:
        pc.last_used_at = _now()
        return pc
    with _pool_cond:
        _pool_active.pop(pc.conn_id, None)
        pc.idle = False
        pc.discarded = True
        stale_conn = pc.conn
        pc.conn = None
        _pool_cond.notify_all()
    _close_conn(stale_conn)
    return _acquire_fresh(settings)


def _acquire_fresh(settings: GmailSettings) -> _PooledConn:
    fingerprint = _pool_fingerprint(settings)
    to_close: list[imaplib.IMAP4] = []
    try:
        with _pool_cond:
            to_close.extend(_ensure_fingerprint_locked(fingerprint))
            deadline = _now() + _IMAP_POOL_ACQUIRE_TIMEOUT_S
            while True:
                to_close.extend(_reap_idle_locked(_now()))
                if len(_pool_active) < _IMAP_POOL_MAX_CONNECTIONS:
                    now = _now()
                    pc = _PooledConn(
                        conn_id=_next_conn_id_locked(),
                        conn=None,
                        fingerprint=fingerprint,
                        generation=_pool_generation,
                        created_at=now,
                        last_used_at=now,
                        idle=False,
                        discarded=False,
                    )
                    _pool_active[pc.conn_id] = pc
                    break
                remaining = deadline - _now()
                if remaining <= 0:
                    raise GmailUpstreamError("busy")
                _pool_cond.wait(remaining)
        _close_many(to_close)
        to_close = []
        _create_and_login(pc, settings)
        return pc
    finally:
        _close_many(to_close)


def _imap_acquire(settings: GmailSettings) -> _PooledConn:
    fingerprint = _pool_fingerprint(settings)
    to_close: list[imaplib.IMAP4] = []
    try:
        with _pool_cond:
            to_close.extend(_ensure_fingerprint_locked(fingerprint))
            deadline = _now() + _IMAP_POOL_ACQUIRE_TIMEOUT_S
            while True:
                to_close.extend(_reap_idle_locked(_now()))
                if _pool_idle:
                    pc = _pool_idle.pop()
                    pc.idle = False
                    _pool_active[pc.conn_id] = pc
                    break
                if len(_pool_active) < _IMAP_POOL_MAX_CONNECTIONS:
                    now = _now()
                    pc = _PooledConn(
                        conn_id=_next_conn_id_locked(),
                        conn=None,
                        fingerprint=fingerprint,
                        generation=_pool_generation,
                        created_at=now,
                        last_used_at=now,
                        idle=False,
                        discarded=False,
                    )
                    _pool_active[pc.conn_id] = pc
                    break
                remaining = deadline - _now()
                if remaining <= 0:
                    raise GmailUpstreamError("busy")
                _pool_cond.wait(remaining)
        _close_many(to_close)
        to_close = []
        if pc.conn is None:
            _create_and_login(pc, settings)
        elif _needs_noop(pc):
            pc = _noop_or_reconnect(pc, settings)
        return pc
    finally:
        _close_many(to_close)


def _is_keep_exception(exc: BaseException) -> bool:
    return isinstance(exc, (GmailNotFound, GmailValidationError, GmailNotConfigured))


def _imap_release(pc: _PooledConn, discard: bool) -> None:
    conn: imaplib.IMAP4 | None = None
    reusable = False
    with _pool_cond:
        if (
            not discard
            and not pc.discarded
            and pc.conn is not None
            and pc.fingerprint == _pool_current_fingerprint
            and pc.generation == _pool_generation
        ):
            reusable = True
        _pool_active.pop(pc.conn_id, None)
        if reusable:
            pc.idle = True
            pc.last_used_at = _now()
            _pool_idle.append(pc)
        else:
            pc.idle = False
            pc.discarded = True
            conn = pc.conn
            pc.conn = None
        _pool_cond.notify_all()
    if not reusable:
        _close_conn(conn)


def _canonical_flags(flags: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(set(flags)))


def _freeze_message(message: dict[str, Any]) -> dict[str, Any]:
    # deepcopy also protects future nested summary fields from sharing state
    # between the cache and the response.
    frozen = copy.deepcopy(dict(message))
    frozen["flags"] = _canonical_flags(message.get("flags", ()))
    return frozen


def _thaw_message(message: dict[str, Any]) -> dict[str, Any]:
    thawed = copy.deepcopy(dict(message))
    thawed["flags"] = list(message.get("flags", ()))
    return thawed


def _add_flag(flags: Iterable[str], flag: str) -> list[str]:
    """Add *flag* case-insensitively, keeping the existing order stable."""
    current = list(flags)
    lowered = flag.lower()
    if any(existing.lower() == lowered for existing in current):
        return current
    index = 0
    while index < len(current) and current[index] <= flag:
        index += 1
    current.insert(index, flag)
    return current


def _mark_read_cached(folder_value: str, account_fingerprint: str, uid: int) -> None:
    """Advance the epoch and patch the current folder entry in place."""
    global _inbox_epoch
    key = (folder_value, account_fingerprint)
    with _inbox_cache_lock:
        _inbox_epoch += 1
        entry = _inbox_cache.get(key)
        if entry is None:
            return
        updated: list[dict[str, Any]] = []
        changed = False
        for message in entry.messages:
            if message.get("uid") == uid and not message.get("seen"):
                replacement = dict(message)
                replacement["flags"] = _canonical_flags(
                    _add_flag(message.get("flags", ()), "\\Seen")
                )
                replacement["seen"] = True
                updated.append(replacement)
                changed = True
            else:
                updated.append(message)
        if changed:
            _inbox_cache[key] = replace(entry, messages=tuple(updated))


def _inbox_cache_get_locked(key: _InboxCacheKey, now: float) -> _InboxCacheEntry | None:
    entry = _inbox_cache.get(key)
    if entry is None:
        return None
    if entry.expires_at <= now:
        del _inbox_cache[key]
        return None
    _inbox_cache.move_to_end(key)
    return entry


def _inbox_cache_put_locked(
    key: _InboxCacheKey, entry: _InboxCacheEntry, epoch0: int
) -> bool:
    if _inbox_epoch != epoch0:
        return False
    _inbox_cache[key] = entry
    _inbox_cache.move_to_end(key)
    while len(_inbox_cache) > _INBOX_CACHE_MAX_ENTRIES:
        _inbox_cache.popitem(last=False)
    return True


def _invalidate_inbox_all() -> None:
    global _inbox_epoch
    with _inbox_cache_lock:
        _inbox_epoch += 1
        _inbox_cache.clear()


def clear_inbox_cache() -> None:
    """Drop every cached inbox window (tests, teardown, explicit refresh)."""
    _invalidate_inbox_all()


def _invalidate_inbox(folder_value: str, account_fingerprint: str) -> None:
    """Drop one folder window and invalidate every in-flight list fetch."""
    global _inbox_epoch
    with _inbox_cache_lock:
        _inbox_epoch += 1
        _inbox_cache.pop((folder_value, account_fingerprint), None)


def _list_response(
    entry: _InboxCacheEntry,
    folder_value: str,
    start: int,
    page_size: int,
) -> dict[str, Any]:
    messages = [_thaw_message(message) for message in entry.messages]
    total = len(messages)
    return {
        "uidvalidity": entry.uidvalidity,
        "folder": folder_value,
        "offset": start,
        "limit": page_size,
        "window": _LIST_WINDOW,
        "total": total,
        "has_more": start + page_size < total,
        "messages": messages[start : start + page_size],
    }


def load_settings() -> GmailSettings:
    """Build the settings snapshot, raising if credentials are missing."""
    address = config.get_gmail_address()
    password = config.get_gmail_app_password()
    if not address or not password:
        raise GmailNotConfigured("Gmail non configurato")
    return GmailSettings(
        address=address,
        password=password,
        imap_host=config.get_gmail_imap_host(),
        imap_port=config.get_gmail_imap_port(),
        smtp_host=config.get_gmail_smtp_host(),
        smtp_port=config.get_gmail_smtp_port(),
        folder=config.get_gmail_folder(),
        timeout_s=config.get_gmail_timeout_s(),
    )


def status_snapshot() -> dict[str, Any]:
    """Return the configuration status without contacting the network."""
    configured = config.gmail_enabled()
    return {
        "configured": configured,
        "address": config.get_gmail_address() if configured else "",
        "folder": config.get_gmail_folder(),
        "connected": None,
        "limits": {
            "max_text_length": _MAX_TEXT_LENGTH,
            "max_attachments": _MAX_ATTACHMENTS,
            "max_attachment_bytes": _MAX_ATTACHMENT_BYTES,
            "max_total_attachment_bytes": _MAX_TOTAL_ATTACHMENT_BYTES,
            "max_download_bytes": _MAX_DOWNLOAD_BYTES,
            "max_messages_limit": _MAX_LIMIT,
        },
    }


def validate_folder(folder: str | None = None) -> str:
    """Return a safe mailbox name or raise :class:`GmailValidationError`."""
    if folder is not None and not isinstance(folder, str):
        raise GmailValidationError("Cartella non valida")
    candidate = (folder if folder is not None else config.get_gmail_folder()) or ""
    candidate = candidate.strip()
    if (
        not candidate
        or candidate in {".", ".."}
        or any(char in candidate for char in '*%"')
        or not _FOLDER_RE.fullmatch(candidate)
    ):
        raise GmailValidationError("Cartella non valida")
    return candidate


def validate_header_value(value: str, field: str = "header") -> str:
    """Reject CR/LF (header injection) and return the stripped value."""
    if "\r" in value or "\n" in value:
        raise GmailValidationError(f"Intestazione {field} non valida")
    return value.strip()


def _parse_addresses(raw: str, field: str) -> list[str]:
    value = validate_header_value(raw or "", field)
    if not value:
        return []
    addresses = [
        address.strip() for _, address in getaddresses([value]) if address.strip()
    ]
    if not addresses or any("@" not in address for address in addresses):
        raise GmailValidationError(f"Indirizzo {field} non valido")
    return addresses


def _validate_uid(uid: int | str, field: str = "uid") -> int:
    try:
        value = int(uid)
    except (TypeError, ValueError):
        raise GmailValidationError(f"{field} non valido") from None
    if value <= 0:
        raise GmailValidationError(f"{field} non valido")
    return value


def _validate_uids(uids: object) -> list[int]:
    """Validate a bulk UID list, preserving order and dropping duplicates."""
    if not isinstance(uids, list):
        raise GmailValidationError("Lista UID non valida")
    if not uids:
        raise GmailValidationError("Lista UID vuota")
    if len(uids) > _MAX_BULK_DELETE:
        raise GmailValidationError(f"Troppi UID (max {_MAX_BULK_DELETE})")
    validated: list[int] = []
    seen: set[int] = set()
    for uid in uids:
        if isinstance(uid, bool) or not isinstance(uid, int):
            raise GmailValidationError("UID non valido")
        value = _validate_uid(uid)
        if value not in seen:
            seen.add(value)
            validated.append(value)
    return validated


def _validate_limit(limit: int | None) -> int:
    if limit is None:
        return _DEFAULT_LIMIT
    try:
        value = int(limit)
    except (TypeError, ValueError):
        raise GmailValidationError("Limit non valido") from None
    if value < 1 or value > _MAX_LIMIT:
        raise GmailValidationError("Limit non valido")
    return value


def _validate_offset(offset: int | None) -> int:
    if offset is None:
        return 0
    try:
        value = int(offset)
    except (TypeError, ValueError):
        raise GmailValidationError("Offset non valido") from None
    if value < 0:
        raise GmailValidationError("Offset non valido")
    return value


def _validate_text(text: str) -> str:
    if not isinstance(text, str):
        raise GmailValidationError("Testo non valido")
    if len(text) > _MAX_TEXT_LENGTH:
        raise GmailValidationError("Testo troppo lungo", status_code=413)
    return text


def decode_mime_header(value: str | None) -> str:
    """Decode RFC 2047 headers, tolerating unknown charsets."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (LookupError, UnicodeDecodeError, ValueError):
        parts: list[str] = []
        for text, charset in decode_header(value):
            if isinstance(text, bytes):
                try:
                    parts.append(text.decode(charset or "utf-8", errors="replace"))
                except (LookupError, UnicodeDecodeError):
                    parts.append(text.decode("utf-8", errors="replace"))
            else:
                parts.append(text)
        return "".join(parts)


def _date_to_ts(value: str | None) -> int | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    try:
        return int(parsed.timestamp())
    except (OSError, OverflowError, ValueError):
        return None


class _HTMLTextExtractor(HTMLParser):
    _SKIP_TAGS = frozenset({"script", "style", "head"})
    _BLOCK_TAGS = frozenset(
        {
            "address",
            "article",
            "aside",
            "blockquote",
            "br",
            "dd",
            "div",
            "dl",
            "dt",
            "fieldset",
            "figcaption",
            "figure",
            "footer",
            "form",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "header",
            "hr",
            "li",
            "main",
            "nav",
            "ol",
            "p",
            "pre",
            "section",
            "table",
            "tbody",
            "td",
            "tfoot",
            "th",
            "thead",
            "tr",
            "ul",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "li":
            self._parts.append("\n- ")
        elif tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._SKIP_TAGS:
            return
        if not self._skip_depth and tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in raw.splitlines()]
        output: list[str] = []
        blank = 0
        for line in lines:
            if line:
                blank = 0
                output.append(line)
            else:
                blank += 1
                if blank == 1:
                    output.append("")
        return "\n".join(output).strip()


def html_to_text(html: str) -> str:
    """Convert an HTML fragment to plain text using only the stdlib parser."""
    if not html:
        return ""
    parser = _HTMLTextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        logger.debug("Malformed HTML part ignored", exc_info=True)
    return parser.text()


def _payload_text(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    charset = part.get_content_charset()
    try:
        return payload.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _is_attachment_part(part: Message) -> bool:
    return bool(part.get_filename()) or part.get_content_disposition() == "attachment"


@dataclass(frozen=True)
class _Bodystructure:
    maintype: str
    subtype: str
    params: tuple[tuple[str, str | None], ...] = ()
    content_id: str | None = None
    encoding: str | None = None
    octets: int | None = None
    children: tuple[_Bodystructure, ...] = ()
    disposition: str | None = None
    disposition_params: tuple[tuple[str, str | None], ...] = ()


@dataclass(frozen=True)
class _MimePart:
    walk_index: str
    maintype: str
    subtype: str
    params: tuple[tuple[str, str | None], ...]
    disposition: str | None
    disposition_params: tuple[tuple[str, str | None], ...]
    content_id: str | None
    encoding: str | None
    octets: int | None
    filename: str | None
    attachment_index: int | None = None
    has_content_id: bool = False

    @property
    def part_number(self) -> str:
        return self.walk_index

    @property
    def content_type(self) -> str:
        return f"{self.maintype}/{self.subtype}"


@dataclass(frozen=True)
class _PartMap:
    parts: tuple[_MimePart, ...]
    render_parts: tuple[_MimePart, ...]
    attachments: tuple[_MimePart, ...]


@dataclass(frozen=True)
class _StructureRecord:
    uid: int
    headers: bytes
    index: _PartMap


class _BodystructureParseError(Exception):
    """Internal tokenizer failure; always converted to a fallback."""


class _PartFetchError(Exception):
    """A logically completed partial fetch that must fall back to full."""


def _extract_bodystructure(meta: bytes) -> bytes | None:
    """Return the balanced ``BODYSTRUCTURE`` block, or ``None``.

    IMAP literals are deliberately rejected: a structure carrying ``{N}``
    cannot be parsed safely from the FETCH metadata, so callers fall back to
    the legacy full fetch.
    """
    if not isinstance(meta, (bytes, bytearray)):
        return None
    data = bytes(meta)
    if len(data) > _MAX_BODYSTRUCTURE_BYTES:
        return None
    match = re.search(rb"\bBODYSTRUCTURE\b", data, re.IGNORECASE)
    if match is None:
        return None
    start = match.end()
    while start < len(data) and data[start : start + 1].isspace():
        start += 1
    if start >= len(data) or data[start] != 0x28:
        return None
    depth = 0
    in_quote = False
    escaped = False
    for index in range(start, len(data)):
        char = data[index]
        if in_quote:
            if escaped:
                escaped = False
            elif char == 0x5C:
                escaped = True
            elif char == 0x22:
                in_quote = False
            continue
        if char == 0x22:
            in_quote = True
        elif char == 0x7B:
            end = index + 1
            while end < len(data) and 0x30 <= data[end] <= 0x39:
                end += 1
            if end > index + 1 and end < len(data) and data[end] == 0x7D:
                return None
        elif char == 0x28:
            depth += 1
        elif char == 0x29:
            depth -= 1
            if depth == 0:
                return data[start : index + 1]
            if depth < 0:
                return None
    return None


def _bodystructure_tokens(data: bytes) -> list[Any]:
    if not isinstance(data, (bytes, bytearray)):
        raise _BodystructureParseError
    data = bytes(data)
    if len(data) > _MAX_BODYSTRUCTURE_BYTES:
        raise _BodystructureParseError
    index = 0
    tokens = 0

    def skip_space() -> None:
        nonlocal index
        while index < len(data) and data[index] in b" \t\r\n":
            index += 1

    def parse_value(depth: int) -> Any:
        nonlocal index, tokens
        skip_space()
        if index >= len(data):
            raise _BodystructureParseError
        char = data[index]
        if char == 0x28:
            if depth >= _MAX_BODYSTRUCTURE_DEPTH:
                raise _BodystructureParseError
            index += 1
            result: list[Any] = []
            while True:
                skip_space()
                if index >= len(data):
                    raise _BodystructureParseError
                if data[index] == 0x29:
                    index += 1
                    return result
                result.append(parse_value(depth + 1))
        if char == 0x22:
            index += 1
            buffer = bytearray()
            while index < len(data):
                current = data[index]
                if current == 0x5C:
                    index += 1
                    if index >= len(data):
                        raise _BodystructureParseError
                    buffer.append(data[index])
                    index += 1
                elif current == 0x22:
                    index += 1
                    break
                else:
                    buffer.append(current)
                    index += 1
            else:
                raise _BodystructureParseError
            if len(buffer) > _MAX_BODYSTRUCTURE_TOKEN_BYTES:
                raise _BodystructureParseError
            tokens += 1
            if tokens > _MAX_BODYSTRUCTURE_TOKENS:
                raise _BodystructureParseError
            return buffer.decode("latin-1")
        if char == 0x7B:
            end = index + 1
            while end < len(data) and 0x30 <= data[end] <= 0x39:
                end += 1
            if end > index + 1 and end < len(data) and data[end] == 0x7D:
                raise _BodystructureParseError
        start = index
        while index < len(data) and data[index] not in b' \t\r\n()"':
            index += 1
        if start == index:
            raise _BodystructureParseError
        raw = data[start:index]
        if len(raw) > _MAX_BODYSTRUCTURE_TOKEN_BYTES:
            raise _BodystructureParseError
        tokens += 1
        if tokens > _MAX_BODYSTRUCTURE_TOKENS:
            raise _BodystructureParseError
        if raw.upper() == b"NIL":
            return None
        return raw.decode("latin-1")

    value = parse_value(0)
    skip_space()
    if index != len(data) or not isinstance(value, list) or not value:
        raise _BodystructureParseError
    return value


def _parse_pairs(value: Any) -> tuple[tuple[str, str | None], ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) % 2:
        raise _BodystructureParseError
    pairs: list[tuple[str, str | None]] = []
    for index in range(0, len(value), 2):
        key = value[index]
        item = value[index + 1]
        if not isinstance(key, str) or (item is not None and not isinstance(item, str)):
            raise _BodystructureParseError
        pairs.append((key, item))
    return tuple(pairs)


def _parse_disposition(
    value: Any,
) -> tuple[str | None, tuple[tuple[str, str | None], ...]]:
    if value is None:
        return None, ()
    if not isinstance(value, list) or not value or not isinstance(value[0], str):
        raise _BodystructureParseError
    params = _parse_pairs(value[1]) if len(value) > 1 else ()
    return value[0].lower(), params


def _parse_octets(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise _BodystructureParseError


def _bodystructure_node(value: Any) -> _Bodystructure:
    if not isinstance(value, list) or not value:
        raise _BodystructureParseError
    first = value[0]
    if isinstance(first, list):
        children: list[_Bodystructure] = []
        index = 0
        while index < len(value) and isinstance(value[index], list):
            children.append(_bodystructure_node(value[index]))
            index += 1
        if not children or index >= len(value) or not isinstance(value[index], str):
            raise _BodystructureParseError
        subtype = value[index].lower()
        index += 1
        params = _parse_pairs(value[index]) if index < len(value) else ()
        index += 1
        disposition, disposition_params = (None, ())
        if index < len(value):
            disposition, disposition_params = _parse_disposition(value[index])
        return _Bodystructure(
            maintype="multipart",
            subtype=subtype,
            params=params,
            children=tuple(children),
            disposition=disposition,
            disposition_params=disposition_params,
        )

    if not isinstance(first, str) or len(value) < 7:
        raise _BodystructureParseError
    if not isinstance(value[1], str):
        raise _BodystructureParseError
    maintype = first.lower()
    subtype = value[1].lower()
    params = _parse_pairs(value[2])
    content_id = value[3] if isinstance(value[3], str) else None
    encoding = value[5].lower() if isinstance(value[5], str) else None
    octets = _parse_octets(value[6])

    children: tuple[_Bodystructure, ...] = ()
    if maintype == "message" and subtype == "rfc822":
        if len(value) < 9:
            raise _BodystructureParseError
        children = (_bodystructure_node(value[8]),)
        extension = 10
    elif maintype == "text":
        extension = 8
    else:
        extension = 7

    disposition, disposition_params = (None, ())
    if extension + 1 < len(value):
        disposition, disposition_params = _parse_disposition(value[extension + 1])
    return _Bodystructure(
        maintype=maintype,
        subtype=subtype,
        params=params,
        content_id=content_id,
        encoding=encoding,
        octets=octets,
        children=children,
        disposition=disposition,
        disposition_params=disposition_params,
    )


def parse_bodystructure(blk: bytes) -> _Bodystructure | None:
    """Parse an IMAP BODYSTRUCTURE block without ``eval`` or recursion abuse."""
    try:
        return _bodystructure_node(_bodystructure_tokens(blk))
    except (_BodystructureParseError, ValueError, LookupError, RecursionError):
        return None


def _decode_param(value: str | None, *, encoded: bool = False) -> str:
    """Decode one RFC 2231 parameter value, then any RFC 2047 words.

    RFC 2231 syntax is only valid for starred parameters: a plain filename
    such as ``O'Brien's report.pdf`` must be preserved verbatim.
    """
    if not value:
        return ""
    text = value
    if encoded:
        charset: str | None = None
        match = _RFC2231_RE.match(text)
        if match is not None:
            charset = match.group(1) or None
            text = match.group(3)
        try:
            raw = unquote_to_bytes(text)
        except (UnicodeEncodeError, ValueError):
            return decode_mime_header(text)
        try:
            text = raw.decode(charset or "utf-8", errors="replace")
        except LookupError:
            text = raw.decode("utf-8", errors="replace")
    return decode_mime_header(text)


def _params_get(params: tuple[tuple[str, str | None], ...], name: str) -> str:
    """Return a parameter, combining RFC 2231 continuations when present."""
    lowered = name.lower()
    mapping = {key.lower(): value for key, value in params}
    numbered: list[tuple[int, bool, str | None]] = []
    for key, value in mapping.items():
        match = _RFC2231_CONT_RE.match(key)
        if match is not None and match.group(1) == lowered:
            numbered.append((int(match.group(2)), bool(match.group(3)), value))
    if numbered:
        numbered.sort()
        charset: str | None = None
        chunks = bytearray()
        for offset, (_, encoded, value) in enumerate(numbered):
            text = value or ""
            if offset == 0 and encoded:
                match = _RFC2231_RE.match(text)
                if match is not None:
                    charset = match.group(1) or None
                    text = match.group(3)
            if encoded:
                chunks.extend(unquote_to_bytes(text))
            else:
                chunks.extend(text.encode("latin-1", errors="replace"))
        try:
            decoded = bytes(chunks).decode(charset or "utf-8", errors="replace")
        except LookupError:
            decoded = bytes(chunks).decode("utf-8", errors="replace")
        return decode_mime_header(decoded)
    star_key = f"{lowered}*"
    if star_key in mapping:
        return _decode_param(mapping[star_key], encoded=True)
    return _decode_param(mapping.get(lowered))


def _part_filename(node: _Bodystructure) -> str | None:
    value = _params_get(node.disposition_params, "filename")
    if not value:
        value = _params_get(node.params, "name")
    return value or None


def _mime_part(node: _Bodystructure, walk_index: str) -> _MimePart:
    content_id = decode_mime_header(node.content_id).strip().strip("<>")
    return _MimePart(
        walk_index=walk_index,
        maintype=node.maintype,
        subtype=node.subtype,
        params=node.params,
        disposition=node.disposition,
        disposition_params=node.disposition_params,
        content_id=content_id or None,
        encoding=node.encoding,
        octets=node.octets,
        filename=_part_filename(node),
        has_content_id=bool(node.content_id),
    )


def _part_is_attachment(part: _MimePart) -> bool:
    return (
        bool(part.filename)
        or part.disposition == "attachment"
        or (part.has_content_id and part.maintype != "text")
    )


def _index_bodystructure(root: _Bodystructure) -> _PartMap:
    parts: list[_MimePart] = []
    render: list[_MimePart] = []
    attachments: list[_MimePart] = []

    def descend(node: _Bodystructure, walk_index: str) -> None:
        if node.maintype == "multipart":
            for position, child in enumerate(node.children, 1):
                descend(child, f"{walk_index}.{position}")
            return
        if node.maintype == "message" and node.subtype == "rfc822" and node.children:
            child = node.children[0]
            if child.maintype == "multipart":
                for position, nested in enumerate(child.children, 1):
                    descend(nested, f"{walk_index}.{position}")
            else:
                descend(child, f"{walk_index}.1")
            return
        part = _mime_part(node, walk_index)
        if _part_is_attachment(part):
            part = replace(part, attachment_index=len(attachments))
            attachments.append(part)
        elif part.maintype == "text" and part.subtype in {"plain", "html"}:
            render.append(part)
        parts.append(part)

    if root.maintype == "multipart":
        for position, child in enumerate(root.children, 1):
            descend(child, str(position))
    elif root.maintype == "message" and root.subtype == "rfc822" and root.children:
        child = root.children[0]
        if child.maintype == "multipart":
            for position, nested in enumerate(child.children, 1):
                descend(nested, f"1.{position}")
        else:
            descend(child, "1.1")
    else:
        descend(root, "1")
    return _PartMap(
        parts=tuple(parts),
        render_parts=tuple(render),
        attachments=tuple(attachments),
    )


def _encoded_size(part: Message) -> int | None:
    """Return the transfer-encoded wire size, or ``None`` when not decidable."""
    cte = (part.get("Content-Transfer-Encoding") or "").strip().lower()
    if cte in ("base64", "quoted-printable"):
        raw = part.get_payload(decode=False)
        if isinstance(raw, bytes):
            encoded = raw
        elif isinstance(raw, str):
            try:
                encoded = raw.encode("ascii")
            except UnicodeEncodeError:
                return None
        else:
            return None
        return len(encoded) + encoded.count(b"\n") - encoded.count(b"\r\n")
    if cte in ("", "7bit", "8bit", "binary"):
        payload = part.get_payload(decode=True)
        if payload is None:
            return None
        if isinstance(payload, str):
            return len(payload.encode("utf-8", errors="replace"))
        return len(payload)
    return None


def _b64url_encode(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> str:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8")


def sign_image_url(url: str) -> str:
    """Return an opaque, signed page-relative URL for a remote image."""
    payload = _b64url_encode(url)
    signature = hmac.new(
        _IMAGE_PROXY_KEY, payload.encode("ascii"), hashlib.sha256
    ).hexdigest()[:32]
    return f"gmail-image?token={payload}.{signature}&v={_IMAGE_PROXY_VERSION}"


def verify_image_token(token: str) -> str:
    """Return the signed image URL, or ``""`` when the token is invalid."""
    if not token or len(token) > 8192:
        return ""
    payload, separator, signature = token.partition(".")
    if not separator:
        return ""
    expected = hmac.new(
        _IMAGE_PROXY_KEY, payload.encode("ascii"), hashlib.sha256
    ).hexdigest()[:32]
    if not hmac.compare_digest(signature, expected):
        return ""
    try:
        return _b64url_decode(payload)
    except (ValueError, UnicodeDecodeError):
        return ""


# Inline (``cid:``) images are MIME parts of the message, served by the same
# signed proxy.  Their payload is not a URL but ``gmail-part:`` + expiry, uid,
# attachment index and folder.  Only the server signs it (remote URLs are
# signed only when they are http/https), and unlike remote images it gives
# access to mailbox content without the Bearer token, so it expires.
_PART_IMAGE_PREFIX = "gmail-part:"
_PART_IMAGE_TTL_S = 24 * 3600


def sign_part_image_url(
    folder: str, uid: int, index: int, *, now: float | None = None
) -> str:
    """Signed proxy URL for attachment *index* of message *uid* (expires)."""
    expires = int((time.time() if now is None else now) + _PART_IMAGE_TTL_S)
    return sign_image_url(f"{_PART_IMAGE_PREFIX}{expires}:{uid}:{index}:{folder}")


def parse_part_image(
    payload: str, *, now: float | None = None
) -> tuple[str, int, int] | None:
    """``(folder, uid, index)`` of a verified ``gmail-part:`` payload, if valid.

    Returns ``None`` for remote-image payloads, expired or malformed ones.
    """
    if not payload.startswith(_PART_IMAGE_PREFIX):
        return None
    fields = payload[len(_PART_IMAGE_PREFIX) :].split(":", 3)
    if len(fields) != 4:
        return None
    expires, uid, index, folder = fields
    if not (expires.isdigit() and uid.isdigit() and index.isdigit()):
        return None
    if int(expires) < (time.time() if now is None else now):
        return None
    try:
        folder = validate_folder(folder)
    except GmailValidationError:
        return None
    return folder, int(uid), int(index)


def _cid_image_urls(
    attachments: Iterable[dict[str, Any]], folder: str, uid: int
) -> dict[str, str]:
    """Signed proxy URL of every inline image, keyed by lowercase Content-ID."""
    urls: dict[str, str] = {}
    for attachment in attachments:
        content_id = str(attachment.get("content_id") or "").lower()
        if attachment.get("inline") and content_id:
            urls[content_id] = sign_part_image_url(folder, uid, attachment["index"])
    return urls


# Inline images are downloaded with the message itself (same IMAP FETCH as the
# text parts) and kept here, so the image proxy serves them without opening
# IMAP sessions of its own: one per image cost 0.5-0.8 s and, with the pool
# capped at 4 connections, delayed the next message opened.  Bounded by bytes
# and age; a miss (restart, eviction) falls back to fetch_attachment.
_INLINE_PREFETCH_PART_MAX = 2 * 1024 * 1024
_INLINE_PREFETCH_TOTAL_MAX = 6 * 1024 * 1024
_INLINE_CACHE_MAX_BYTES = 32 * 1024 * 1024
_INLINE_CACHE_TTL_S = 3600
_inline_cache: OrderedDict[tuple[str, int, int], tuple[float, bytes]] = OrderedDict()
_inline_cache_bytes = 0
_inline_cache_lock = threading.Lock()


def _inline_cache_put(folder: str, uid: int, index: int, data: bytes) -> None:
    global _inline_cache_bytes
    if not data or len(data) > _INLINE_PREFETCH_PART_MAX:
        return
    key = (folder, uid, index)
    with _inline_cache_lock:
        previous = _inline_cache.pop(key, None)
        if previous is not None:
            _inline_cache_bytes -= len(previous[1])
        _inline_cache[key] = (time.monotonic(), data)
        _inline_cache_bytes += len(data)
        while _inline_cache_bytes > _INLINE_CACHE_MAX_BYTES and _inline_cache:
            _, (_, evicted) = _inline_cache.popitem(last=False)
            _inline_cache_bytes -= len(evicted)


def cached_inline_image(folder: str, uid: int, index: int) -> bytes | None:
    """Bytes of a prefetched inline image, or ``None`` (missing or expired)."""
    global _inline_cache_bytes
    key = (folder, uid, index)
    with _inline_cache_lock:
        entry = _inline_cache.get(key)
        if entry is None:
            return None
        stored_at, data = entry
        if time.monotonic() - stored_at > _INLINE_CACHE_TTL_S:
            del _inline_cache[key]
            _inline_cache_bytes -= len(data)
            return None
        _inline_cache.move_to_end(key)
        return data


def clear_inline_image_cache() -> None:
    global _inline_cache_bytes
    with _inline_cache_lock:
        _inline_cache.clear()
        _inline_cache_bytes = 0


def _inline_image_candidates(index: _PartMap) -> list[_MimePart]:
    """Parts worth fetching with the text: small images that may be ``cid:``.

    Decided from BODYSTRUCTURE alone (before the HTML is known); only the
    ones the HTML really references are cached afterwards.
    """
    selected: list[_MimePart] = []
    total = 0
    for part in index.attachments:
        if (
            part.maintype != "image"
            or not part.content_id
            or (part.disposition or "").lower() == "attachment"
            or part.octets is None
            or part.octets > _INLINE_PREFETCH_PART_MAX
            or total + part.octets > _INLINE_PREFETCH_TOTAL_MAX
        ):
            continue
        selected.append(part)
        total += part.octets
    return selected


def _cache_inline_images(
    folder: str,
    uid: int,
    index: _PartMap,
    attachments: Iterable[dict[str, Any]],
    payloads: Mapping[str, bytes],
) -> None:
    """Cache the prefetched payloads of the images marked ``inline``."""
    for attachment in attachments:
        if not attachment.get("inline"):
            continue
        position = attachment["index"]
        part = index.attachments[position]
        payload = payloads.get(part.walk_index)
        if payload is None:
            continue
        try:
            data = _decode_fetched_bytes(part, payload)
        except _PartFetchError:
            continue
        _inline_cache_put(folder, uid, position, data)


def _find_tag_end(raw: str, start: int) -> int:
    """Return the index just past ``>``, honouring quoted attribute values."""
    quote = ""
    index = start
    length = len(raw)
    while index < length:
        char = raw[index]
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == ">":
            return index + 1
        index += 1
    return length


@dataclass(frozen=True)
class _ScannedAttribute:
    name: str
    lower_name: str
    value: str | None
    quote: str | None


def _parse_tag(
    raw: str, start: int
) -> tuple[str, list[_ScannedAttribute], int, bool] | None:
    """Parse one tag starting after ``<``; return name, attributes, end, slash."""
    length = len(raw)
    index = start
    name_start = index
    while index < length and (raw[index].isalnum() or raw[index] in ":-"):
        index += 1
    if index == name_start:
        return None
    name = raw[name_start:index].lower()
    attributes: list[_ScannedAttribute] = []
    self_closing = False
    while index < length:
        while index < length and raw[index] in " \t\n\r\f":
            index += 1
        if index >= length:
            return None
        if raw[index] == ">":
            return name, attributes, index + 1, self_closing
        if raw[index] == "/" and index + 1 < length and raw[index + 1] == ">":
            return name, attributes, index + 2, True
        attr_start = index
        while index < length and raw[index] not in " \t\n\r\f/>=":
            index += 1
        if index == attr_start:
            index += 1
            continue
        attr_name = raw[attr_start:index]
        while index < length and raw[index] in " \t\n\r\f":
            index += 1
        value: str | None = None
        quote: str | None = None
        if index < length and raw[index] == "=":
            index += 1
            while index < length and raw[index] in " \t\n\r\f":
                index += 1
            if index < length and raw[index] in "\"'":
                quote = raw[index]
                index += 1
                value_start = index
                value_end = raw.find(quote, index)
                if value_end == -1:
                    return None
                value = raw[value_start:value_end]
                index = value_end + 1
            else:
                value_start = index
                while index < length and raw[index] not in " \t\n\r\f>":
                    index += 1
                value = raw[value_start:index]
        attributes.append(
            _ScannedAttribute(
                name=attr_name,
                lower_name=attr_name.lower(),
                value=value,
                quote=quote,
            )
        )
    return None


def _find_raw_text_close(raw: str, start: int, name: str) -> int:
    index = start
    length = len(raw)
    while True:
        index = raw.find("</", index)
        if index == -1:
            return -1
        name_start = index + 2
        name_end = name_start
        while name_end < length and (raw[name_end].isalnum() or raw[name_end] in ":-"):
            name_end += 1
        if raw[name_start:name_end].lower() == name and (
            name_end >= length or raw[name_end] in " \t\n\r\f/>"
        ):
            return index
        index += 2


def _render_img_tag(attributes: list[_ScannedAttribute], self_closing: bool) -> str:
    has_data_src = any(attr.lower_name == "data-src" for attr in attributes)
    pieces = ["<img"]
    for attr in attributes:
        name = attr.name
        value = attr.value
        quote = attr.quote
        if attr.lower_name == "src" and not has_data_src:
            name = "data-src"
        if value is not None and quote is None:
            value = value.replace('"', "&quot;")
            quote = '"'
        pieces.append(" ")
        pieces.append(name)
        if value is not None:
            pieces.append("=")
            pieces.append(quote or "")
            pieces.append(value)
            pieces.append(quote or "")
    pieces.append("/>" if self_closing else ">")
    return "".join(pieces)


_BODY_COLOR_RE = re.compile(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?|[A-Za-z]{1,20}")


def _render_body_tag(attributes: list[_ScannedAttribute]) -> tuple[str, bool, int]:
    """Render ``<body>`` as a ``<div>`` that keeps the page colors.

    nh3 drops ``<body>`` with its attributes, losing the background around the
    email "card".  ``bgcolor``/``text`` become leading declarations of the
    div's ``style`` (an explicit body style still wins); the style sanitizer
    validates every value.  Returns the tag, whether a new ``style`` attribute
    was created and its declaration count (both count towards the caps).
    """
    declarations: list[str] = []
    existing = ""
    for attr in attributes:
        value = (attr.value or "").strip()
        if attr.lower_name == "style":
            existing = value.replace('"', "&quot;")
        elif attr.lower_name in {"bgcolor", "text"} and _BODY_COLOR_RE.fullmatch(value):
            prop = "background-color" if attr.lower_name == "bgcolor" else "color"
            declarations.append(f"{prop}:{value}")
    if existing:
        declarations.append(existing)
    if not declarations:
        return "<div>", False, 0
    style = ";".join(declarations)
    return f'<div style="{style}">', not existing, _style_declaration_count(style)


_QUOTE_BLOCK_CLASSES = frozenset({"gmail_quote", "yahoo_quoted"})
_QUOTE_AFTER_IDS = frozenset({"divrplyfwdmsg", "appendonsend"})


def _quote_marker(name: str, attributes: list[_ScannedAttribute]) -> str | None:
    """``data-quote`` value for the quoted-text containers of common clients.

    Classes and ids are stripped by the sanitizer, so the reader can only
    recognize quotes through this marker, added while the scanner still sees
    them.  ``block``: the element is the quote (Gmail ``div.gmail_quote``,
    Apple/Thunderbird ``blockquote type=cite``, Yahoo ``div.yahoo_quoted``).
    ``after``: the quote is the element and everything after it (Outlook's
    ``divRplyFwdMsg`` header, ``appendonsend``).
    """
    if name not in {"div", "blockquote"}:
        return None
    values = {attr.lower_name: (attr.value or "").strip() for attr in attributes}
    if "data-quote" in values:
        return None
    if name == "blockquote":
        return "block" if values.get("type", "").lower() == "cite" else None
    if _QUOTE_BLOCK_CLASSES & set(values.get("class", "").split()):
        return "block"
    if values.get("id", "").lower() in _QUOTE_AFTER_IDS:
        return "after"
    return None


_OPTIONAL_END_TAGS = frozenset(
    {
        "caption",
        "colgroup",
        "dd",
        "dt",
        "li",
        "optgroup",
        "option",
        "p",
        "rp",
        "rt",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
    }
)


def _style_declaration_count(value: str) -> int:
    """Upper bound of the declarations in a ``style`` value.

    Counted on the entity-decoded text (what tinycss2 parses), so sanitizing
    never increases it: the output is re-accepted (idempotence).
    """
    return sum(1 for part in _html_unescape(value).split(";") if part.strip())


def _pre_scan_html(raw: str, styles: list[str] | None = None) -> str:
    """Linear, quote-aware scan with complexity caps and image deferral.

    Any cap violation is fail-closed (``""``).  The scanner never backtracks
    and understands comments, CDATA, raw-text elements, void tags, unquoted
    attributes and ``<`` characters in text.  When ``styles`` is given, the
    contents of real ``<style>`` elements are appended to it.
    """
    length = len(raw)
    out: list[str] = []
    index = 0
    tag_count = 0
    style_count = 0
    style_decls = 0
    text_chars = 0
    depth = 0
    void_tags = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    }
    raw_text_tags = {"script", "style", "textarea", "title"}
    while index < length:
        if raw[index] != "<":
            end = raw.find("<", index)
            if end == -1:
                end = length
            text_chars += end - index
            if text_chars > _HTML_MAX_TEXT_CHARS:
                return ""
            out.append(raw[index:end])
            index = end
            continue
        if raw.startswith("<!--", index):
            end = raw.find("-->", index + 4)
            end = length if end == -1 else end + 3
            out.append(raw[index:end])
            index = end
            continue
        if raw.startswith("<![CDATA[", index):
            end = raw.find("]]>", index + 9)
            end = length if end == -1 else end + 3
            text_chars += max(0, end - index - 12)
            if text_chars > _HTML_MAX_TEXT_CHARS:
                return ""
            out.append(raw[index:end])
            index = end
            continue
        if raw.startswith("<!", index) or raw.startswith("<?", index):
            end = _find_tag_end(raw, index + 2)
            out.append(raw[index:end])
            index = end
            continue
        closing = raw.startswith("</", index)
        parsed = _parse_tag(raw, index + (2 if closing else 1))
        if parsed is None:
            text_chars += 1
            if text_chars > _HTML_MAX_TEXT_CHARS:
                return ""
            out.append("<")
            index += 1
            continue
        name, attributes, end, self_closing = parsed
        if not closing:
            if len(attributes) > _HTML_MAX_ATTRS_PER_TAG:
                return ""
            for attr in attributes:
                attr_length = len(attr.name)
                if attr.value is not None:
                    attr_length += 1 + len(attr.value)
                if attr_length > _HTML_MAX_ATTR_LEN:
                    return ""
                if attr.lower_name == "style":
                    style_count += 1
                    if style_count > _HTML_MAX_STYLE_ATTRS:
                        return ""
                    if name != "body":
                        style_decls += _style_declaration_count(attr.value or "")
                        if style_decls > _HTML_MAX_STYLE_DECLS_TOTAL:
                            return ""
            if name != "tbody":
                tag_count += 1
                if tag_count > _HTML_MAX_TAGS:
                    return ""
                if name not in void_tags and name not in _OPTIONAL_END_TAGS:
                    depth += 1
                    if depth > _HTML_MAX_DEPTH:
                        return ""
            if name == "img":
                has_data_src = any(attr.lower_name == "data-src" for attr in attributes)
                has_src = any(attr.lower_name == "src" for attr in attributes)
                if has_src and not has_data_src:
                    out.append(_render_img_tag(attributes, self_closing))
                else:
                    out.append(raw[index:end])
            elif (marker := _quote_marker(name, attributes)) is not None:
                tag = raw[index:end]
                cut = 1 + len(name)
                out.append(f'{tag[:cut]} data-quote="{marker}"{tag[cut:]}')
            elif name == "body":
                body_tag, added_style, body_decls = _render_body_tag(attributes)
                if added_style:
                    style_count += 1
                    if style_count > _HTML_MAX_STYLE_ATTRS:
                        return ""
                style_decls += body_decls
                if style_decls > _HTML_MAX_STYLE_DECLS_TOTAL:
                    return ""
                out.append(body_tag)
            else:
                out.append(raw[index:end])
            index = end
            if name in raw_text_tags:
                close_start = _find_raw_text_close(raw, index, name)
                if close_start == -1:
                    text_chars += length - index
                    if text_chars > _HTML_MAX_TEXT_CHARS:
                        return ""
                    out.append(raw[index:])
                    index = length
                else:
                    text_chars += close_start - index
                    if text_chars > _HTML_MAX_TEXT_CHARS:
                        return ""
                    if name == "style" and styles is not None:
                        styles.append(raw[index:close_start])
                    out.append(raw[index:close_start])
                    close_parsed = _parse_tag(raw, close_start + 2)
                    if close_parsed is None:
                        index = close_start
                    else:
                        _, _, close_end, _ = close_parsed
                        out.append(raw[close_start:close_end])
                        if name not in _OPTIONAL_END_TAGS and depth > 0:
                            depth -= 1
                        index = close_end
            continue
        out.append("</div>" if name == "body" else raw[index:end])
        if name not in void_tags and name not in _OPTIONAL_END_TAGS and depth > 0:
            depth -= 1
        index = end
    return "".join(out)


def _safe_image_source(value: str) -> bool:
    value = value.strip().lower()
    return value.startswith(("http://", "https://"))


def _filter_image_src(
    value: str, cid_urls: Mapping[str, str] | None = None
) -> str | None:
    value = value.strip()
    if value[:4].lower() == "cid:":
        # Inline image: only parts the caller mapped to a signed proxy URL.
        content_id = unquote(value[4:]).strip("<>").lower()
        return (cid_urls or {}).get(content_id)
    signed = _SIGNED_IMAGE_RE.fullmatch(value)
    if signed:
        token = f"{signed.group(1)}.{signed.group(2)}"
        # Always emit the page-relative form, even for a legacy "/gmail-image".
        return value.lstrip("/") if verify_image_token(token) else None
    if _SAFE_IMAGE_DATA_RE.match(value):
        return value
    if len(value) > _IMAGE_URL_MAX_LEN:
        return None
    if _safe_image_source(value):
        return sign_image_url(value)
    return None


_NAMED_COLORS = frozenset(
    {
        "aliceblue",
        "antiquewhite",
        "aqua",
        "aquamarine",
        "azure",
        "beige",
        "bisque",
        "black",
        "blanchedalmond",
        "blue",
        "blueviolet",
        "brown",
        "burlywood",
        "cadetblue",
        "chartreuse",
        "chocolate",
        "coral",
        "cornflowerblue",
        "cornsilk",
        "crimson",
        "cyan",
        "darkblue",
        "darkcyan",
        "darkgoldenrod",
        "darkgray",
        "darkgreen",
        "darkgrey",
        "darkkhaki",
        "darkmagenta",
        "darkolivegreen",
        "darkorange",
        "darkorchid",
        "darkred",
        "darksalmon",
        "darkseagreen",
        "darkslateblue",
        "darkslategray",
        "darkslategrey",
        "darkturquoise",
        "darkviolet",
        "deeppink",
        "deepskyblue",
        "dimgray",
        "dimgrey",
        "dodgerblue",
        "firebrick",
        "floralwhite",
        "forestgreen",
        "fuchsia",
        "gainsboro",
        "ghostwhite",
        "gold",
        "goldenrod",
        "gray",
        "green",
        "greenyellow",
        "grey",
        "honeydew",
        "hotpink",
        "indianred",
        "indigo",
        "ivory",
        "khaki",
        "lavender",
        "lavenderblush",
        "lawngreen",
        "lemonchiffon",
        "lightblue",
        "lightcoral",
        "lightcyan",
        "lightgoldenrodyellow",
        "lightgray",
        "lightgreen",
        "lightgrey",
        "lightpink",
        "lightsalmon",
        "lightseagreen",
        "lightskyblue",
        "lightslategray",
        "lightslategrey",
        "lightsteelblue",
        "lightyellow",
        "lime",
        "limegreen",
        "linen",
        "magenta",
        "maroon",
        "mediumaquamarine",
        "mediumblue",
        "mediumorchid",
        "mediumpurple",
        "mediumseagreen",
        "mediumslateblue",
        "mediumspringgreen",
        "mediumturquoise",
        "mediumvioletred",
        "midnightblue",
        "mintcream",
        "mistyrose",
        "moccasin",
        "navajowhite",
        "navy",
        "oldlace",
        "olive",
        "olivedrab",
        "orange",
        "orangered",
        "orchid",
        "palegoldenrod",
        "palegreen",
        "paleturquoise",
        "palevioletred",
        "papayawhip",
        "peachpuff",
        "peru",
        "pink",
        "plum",
        "powderblue",
        "purple",
        "rebeccapurple",
        "red",
        "rosybrown",
        "royalblue",
        "saddlebrown",
        "salmon",
        "sandybrown",
        "seagreen",
        "seashell",
        "sienna",
        "silver",
        "skyblue",
        "slateblue",
        "slategray",
        "slategrey",
        "snow",
        "springgreen",
        "steelblue",
        "tan",
        "teal",
        "thistle",
        "tomato",
        "transparent",
        "turquoise",
        "violet",
        "wheat",
        "white",
        "whitesmoke",
        "yellow",
        "yellowgreen",
    }
)
_GLOBAL_KEYWORDS = frozenset({"inherit", "initial", "unset", "revert"})
_COLOR_FUNCTIONS = frozenset({"rgb", "rgba", "hsl", "hsla"})
_ABSOLUTE_UNITS = {
    "px": Fraction(1),
    "pt": Fraction(4, 3),
    "pc": Fraction(16),
    "in": Fraction(96),
    "cm": Fraction(96, 1) / Fraction("2.54"),
    "mm": Fraction(96, 1) / Fraction("25.4"),
    "q": Fraction(96, 1) / Fraction("101.6"),
}
_REJECTED_TOKEN_TYPES = frozenset(
    {
        "at-keyword",
        "bad-string",
        "bad-url",
        "[] block",
        "{} block",
        "url",
    }
)
_REJECTED_LITERALS = frozenset({"!", ";", "<", ">", "{", "}", "[", "]", ":"})


def _text_is_safe(text: str) -> bool:
    if "<" in text or ">" in text:
        return False
    for char in text:
        code = ord(char)
        if code < 0x20:
            if char not in "\t\n\r\f":
                return False
        elif code < 0x7F:
            continue
        elif code < 0xA0:
            return False
        else:
            if 0xFDD0 <= code <= 0xFDEF or (code & 0xFFFE) == 0xFFFE:
                return False
            category = unicodedata.category(char)
            if category in {"Cc", "Cf", "Cs"}:
                return False
    return True


def _token_is_safe(token: Any) -> bool:
    token_type = token.type
    if token_type == "whitespace":
        return True
    if token_type in _REJECTED_TOKEN_TYPES:
        return False
    if token_type == "ident":
        return (
            token.lower_value not in _GLOBAL_KEYWORDS
            and not token.lower_value.startswith("--")
            and _text_is_safe(token.value)
        )
    if token_type in {"string", "hash"}:
        return _text_is_safe(token.value)
    if token_type == "literal":
        return token.value not in _REJECTED_LITERALS and _text_is_safe(token.value)
    if token_type in {"number", "percentage"}:
        return math.isfinite(token.value)
    if token_type == "dimension":
        return math.isfinite(token.value) and _text_is_safe(token.unit)
    if token_type == "function":
        return token.lower_name in _COLOR_FUNCTIONS and all(
            _token_is_safe(item) for item in token.arguments
        )
    return False


def _tokens_are_safe(tokens: list[Any]) -> bool:
    return all(_token_is_safe(token) for token in tokens)


def _strip_whitespace(tokens: list[Any]) -> list[Any]:
    start = 0
    end = len(tokens)
    while start < end and tokens[start].type == "whitespace":
        start += 1
    while end > start and tokens[end - 1].type == "whitespace":
        end -= 1
    return tokens[start:end]


def _split_whitespace(tokens: list[Any]) -> list[list[Any]]:
    return [[token] for token in tokens if token.type != "whitespace"]


def _split_literal(tokens: list[Any], literal: str) -> list[list[Any]]:
    parts: list[list[Any]] = []
    current: list[Any] = []
    for token in tokens:
        if token.type == "literal" and token.value == literal:
            parts.append(current)
            current = []
        else:
            current.append(token)
    parts.append(current)
    return parts


def _single_token(tokens: list[Any]) -> Any | None:
    stripped = _strip_whitespace(tokens)
    if len(stripped) != 1:
        return None
    return stripped[0]


def _serialize(token: Any) -> str:
    return token.serialize()


def _finite_number(token: Any) -> float | None:
    if token.type not in {"number", "percentage"}:
        return None
    value = token.value
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _dimension_px(token: Any) -> Fraction | None:
    if token.type != "dimension":
        return None
    factor = _ABSOLUTE_UNITS.get(token.unit.lower())
    if factor is None:
        return None
    value = token.value
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    try:
        number = Fraction(token.representation)
    except (ValueError, ZeroDivisionError):
        number = Fraction(value)
    return number * factor


def _length_value(
    token: Any,
    *,
    minimum: Fraction | int = 0,
    maximum: Fraction | int = 0,
    allow_percent: bool = False,
    percent_min: int = 0,
    percent_max: int = 100,
    allow_auto: bool = False,
) -> str | None:
    if token.type == "ident" and allow_auto and token.lower_value == "auto":
        return "auto"
    if token.type == "dimension":
        pixels = _dimension_px(token)
        if pixels is None or pixels < minimum or pixels > maximum:
            return None
        return _serialize(token)
    if token.type == "number":
        value = _finite_number(token)
        if value is None or value != 0:
            return None
        if not minimum <= 0 <= maximum:
            return None
        return _serialize(token)
    if token.type == "percentage" and allow_percent:
        value = _finite_number(token)
        if value is None or not percent_min <= value <= percent_max:
            return None
        return _serialize(token)
    return None


def _color_function(token: Any) -> str | None:
    parts = _split_literal(token.arguments, ",")
    name = token.lower_name
    expected = 4 if name in {"rgba", "hsla"} else 3
    if len(parts) != expected:
        return None
    values: list[str] = []
    for position, part in enumerate(parts):
        component = _single_token(part)
        if component is None:
            return None
        if position == 3:
            value = _finite_number(component)
            if value is None:
                return None
            if component.type == "percentage":
                if not 0 <= value <= 100:
                    return None
            elif component.type == "number":
                if not 0 <= value <= 1:
                    return None
            else:
                return None
            values.append(_serialize(component))
            continue
        if name in {"rgb", "rgba"}:
            value = _finite_number(component)
            if value is None:
                return None
            if component.type == "percentage":
                if not 0 <= value <= 100:
                    return None
            elif component.type == "number":
                if not 0 <= value <= 255:
                    return None
            else:
                return None
            values.append(_serialize(component))
            continue
        if position == 0:
            value = _finite_number(component)
            if value is None or component.type != "number":
                return None
            values.append(_serialize(component))
        else:
            value = _finite_number(component)
            if value is None or component.type != "percentage":
                return None
            if not 0 <= value <= 100:
                return None
            values.append(_serialize(component))
    return f"{name}({', '.join(values)})"


def _parse_color(token: Any) -> str | None:
    if token.type == "ident":
        if token.lower_value in _NAMED_COLORS:
            return _serialize(token)
        if token.lower_value == "currentcolor":
            return _serialize(token)
        return None
    if token.type == "hash":
        if re.fullmatch(
            r"(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})",
            token.value,
        ):
            return _serialize(token)
        return None
    if token.type == "function" and token.lower_name in _COLOR_FUNCTIONS:
        return _color_function(token)
    return None


def _validate_color(tokens: list[Any]) -> str | None:
    token = _single_token(tokens)
    if token is None:
        return None
    return _parse_color(token)


def _keyword_validator(choices: frozenset[str]) -> Any:
    def validate(tokens: list[Any]) -> str | None:
        token = _single_token(tokens)
        if token is None or token.type != "ident":
            return None
        if token.lower_value not in choices:
            return None
        return _serialize(token)

    return validate


def _validate_font_family(tokens: list[Any]) -> str | None:
    families = _split_literal(tokens, ",")
    if not 1 <= len(families) <= 8:
        return None
    rendered: list[str] = []
    for family in families:
        serialized_words: list[str] = []
        value_words: list[str] = []
        for token in family:
            if token.type == "whitespace":
                continue
            if token.type not in {"ident", "string"}:
                return None
            if not _text_is_safe(token.value):
                return None
            if len(token.value) > 128:
                return None
            serialized_words.append(_serialize(token))
            value_words.append(token.value)
        if not value_words:
            return None
        if len(" ".join(value_words)) > 128:
            return None
        rendered.append(" ".join(serialized_words))
    return ", ".join(rendered)


def _relative_value(
    token: Any, *, em_min: float, em_max: float, percent_min: int, percent_max: int
) -> str | None:
    """Bounded ``em``/``%`` value (font-relative; bounds limit nesting growth)."""
    value = _finite_number(token) if token.type == "percentage" else None
    if token.type == "dimension" and token.unit.lower() == "em":
        number = token.value
        if not isinstance(number, (int, float)) or not math.isfinite(number):
            return None
        return _serialize(token) if em_min <= number <= em_max else None
    if value is not None and percent_min <= value <= percent_max:
        return _serialize(token)
    return None


def _validate_font_size(tokens: list[Any]) -> str | None:
    token = _single_token(tokens)
    if token is None:
        return None
    if token.type == "percentage" or (
        token.type == "dimension" and token.unit.lower() == "em"
    ):
        return _relative_value(
            token, em_min=0.5, em_max=2.5, percent_min=50, percent_max=250
        )
    if token.type == "ident":
        allowed = {
            "xx-small",
            "x-small",
            "small",
            "medium",
            "large",
            "x-large",
            "xx-large",
        }
        return _serialize(token) if token.lower_value in allowed else None
    return _length_value(token, minimum=0, maximum=48)


def _validate_font_weight(tokens: list[Any]) -> str | None:
    token = _single_token(tokens)
    if token is None:
        return None
    if token.type == "ident":
        allowed = {"normal", "bold", "bolder", "lighter"}
        return _serialize(token) if token.lower_value in allowed else None
    if token.type == "number":
        value = _finite_number(token)
        if value is None or not 100 <= value <= 900:
            return None
        if token.int_value is None:
            return None
        return _serialize(token)
    return None


def _validate_line_height(tokens: list[Any]) -> str | None:
    token = _single_token(tokens)
    if token is None:
        return None
    if token.type == "ident" and token.lower_value == "normal":
        return "normal"
    if token.type == "number":
        value = _finite_number(token)
        if value is None or not 1 <= value <= 3:
            return None
        return _serialize(token)
    if token.type == "percentage" or (
        token.type == "dimension" and token.unit.lower() == "em"
    ):
        return _relative_value(
            token, em_min=1, em_max=3, percent_min=100, percent_max=300
        )
    return _length_value(token, minimum=1, maximum=200)


def _validate_letter_spacing(tokens: list[Any]) -> str | None:
    token = _single_token(tokens)
    if token is None:
        return None
    if token.type == "ident" and token.lower_value == "normal":
        return "normal"
    if token.type == "number":
        value = _finite_number(token)
        return _serialize(token) if value == 0 else None
    if token.type != "dimension":
        return None
    pixels = _dimension_px(token)
    if pixels is None or abs(pixels) > 8:
        return None
    return _serialize(token)


def _validate_vertical_align(tokens: list[Any]) -> str | None:
    token = _single_token(tokens)
    if token is None:
        return None
    if token.type == "ident":
        allowed = {
            "baseline",
            "top",
            "middle",
            "bottom",
            "sub",
            "super",
            "text-top",
            "text-bottom",
        }
        return _serialize(token) if token.lower_value in allowed else None
    return _length_value(token, minimum=-128, maximum=128)


def _validate_size(tokens: list[Any]) -> str | None:
    token = _single_token(tokens)
    if token is None:
        return None
    return _length_value(
        token, minimum=0, maximum=1600, allow_percent=True, allow_auto=True
    )


def _validate_max_size(tokens: list[Any]) -> str | None:
    token = _single_token(tokens)
    if token is None:
        return None
    if token.type == "ident" and token.lower_value == "none":
        return "none"
    return _length_value(token, minimum=0, maximum=1600, allow_percent=True)


def _validate_text_indent(tokens: list[Any]) -> str | None:
    token = _single_token(tokens)
    if token is None:
        return None
    return _length_value(
        token,
        minimum=-256,
        maximum=256,
        allow_percent=True,
        percent_min=-100,
        percent_max=100,
    )


def _validate_opacity(tokens: list[Any]) -> str | None:
    token = _single_token(tokens)
    if token is None:
        return None
    value = _finite_number(token)
    if value is None:
        return None
    limit = 100 if token.type == "percentage" else 1
    return _serialize(token) if 0 <= value <= limit else None


def _validate_margin(tokens: list[Any]) -> str | None:
    components = _split_whitespace(tokens)
    if not 1 <= len(components) <= 4:
        return None
    rendered: list[str] = []
    for component in components:
        if len(component) != 1:
            return None
        value = _length_value(
            component[0],
            minimum=-256,
            maximum=256,
            allow_percent=True,
            percent_min=-100,
            percent_max=100,
            allow_auto=True,
        )
        if value is None:
            return None
        rendered.append(value)
    return " ".join(rendered)


def _validate_padding(tokens: list[Any]) -> str | None:
    components = _split_whitespace(tokens)
    if not 1 <= len(components) <= 4:
        return None
    rendered: list[str] = []
    for component in components:
        if len(component) != 1:
            return None
        value = _length_value(
            component[0],
            minimum=0,
            maximum=256,
            allow_percent=True,
        )
        if value is None:
            return None
        rendered.append(value)
    return " ".join(rendered)


def _border_width_value(token: Any) -> str | None:
    if token.type == "ident" and token.lower_value in {
        "thin",
        "medium",
        "thick",
    }:
        return _serialize(token)
    return _length_value(token, minimum=0, maximum=8)


def _validate_border_width(tokens: list[Any]) -> str | None:
    components = _split_whitespace(tokens)
    if not 1 <= len(components) <= 4:
        return None
    rendered: list[str] = []
    for component in components:
        if len(component) != 1:
            return None
        value = _border_width_value(component[0])
        if value is None:
            return None
        rendered.append(value)
    return " ".join(rendered)


_BORDER_STYLES = frozenset(
    {
        "none",
        "hidden",
        "dotted",
        "dashed",
        "solid",
        "double",
        "groove",
        "ridge",
        "inset",
        "outset",
    }
)


def _validate_border_style(tokens: list[Any]) -> str | None:
    components = _split_whitespace(tokens)
    if not 1 <= len(components) <= 4:
        return None
    rendered: list[str] = []
    for component in components:
        token = _single_token(component)
        if token is None or token.type != "ident":
            return None
        if token.lower_value not in _BORDER_STYLES:
            return None
        rendered.append(_serialize(token))
    return " ".join(rendered)


def _validate_border_color(tokens: list[Any]) -> str | None:
    components = _split_whitespace(tokens)
    if not 1 <= len(components) <= 4:
        return None
    rendered: list[str] = []
    for component in components:
        token = _single_token(component)
        if token is None:
            return None
        color = _parse_color(token)
        if color is None:
            return None
        rendered.append(color)
    return " ".join(rendered)


def _validate_border(tokens: list[Any]) -> str | None:
    components = _split_whitespace(tokens)
    if not 1 <= len(components) <= 3:
        return None
    found = {"width": False, "style": False, "color": False}
    rendered: list[str] = []
    for component in components:
        token = _single_token(component)
        if token is None:
            return None
        width = _border_width_value(token)
        if width is not None:
            if found["width"]:
                return None
            found["width"] = True
            rendered.append(width)
            continue
        if token.type == "ident" and token.lower_value in _BORDER_STYLES:
            if found["style"]:
                return None
            found["style"] = True
            rendered.append(_serialize(token))
            continue
        color = _parse_color(token)
        if color is not None:
            if found["color"]:
                return None
            found["color"] = True
            rendered.append(color)
            continue
        return None
    return " ".join(rendered)


def _validate_border_spacing(tokens: list[Any]) -> str | None:
    components = _split_whitespace(tokens)
    if not 1 <= len(components) <= 2:
        return None
    rendered: list[str] = []
    for component in components:
        token = _single_token(component)
        if token is None:
            return None
        value = _length_value(token, minimum=0, maximum=32)
        if value is None:
            return None
        rendered.append(value)
    return " ".join(rendered)


def _validate_text_decoration(tokens: list[Any]) -> str | None:
    components = _split_whitespace(tokens)
    if not 1 <= len(components) <= 3:
        return None
    allowed = {"none", "underline", "overline", "line-through"}
    rendered: list[str] = []
    for component in components:
        token = _single_token(component)
        if token is None or token.type != "ident":
            return None
        if token.lower_value not in allowed:
            return None
        rendered.append(_serialize(token))
    if "none" in rendered and len(rendered) > 1:
        return None
    return " ".join(rendered)


def _radius_value(token: Any) -> str | None:
    if token.type == "number":
        value = _finite_number(token)
        return _serialize(token) if value == 0 else None
    if token.type == "dimension":
        pixels = _dimension_px(token)
        if pixels is None or not 0 <= pixels <= 64:
            return None
        return _serialize(token)
    if token.type == "percentage":
        value = _finite_number(token)
        if value is None or not 0 <= value <= 100:
            return None
        return _serialize(token)
    return None


def _radius_group(tokens: list[Any], maximum: int) -> str | None:
    components = _split_whitespace(tokens)
    if not 1 <= len(components) <= maximum:
        return None
    rendered: list[str] = []
    for component in components:
        token = _single_token(component)
        if token is None:
            return None
        value = _radius_value(token)
        if value is None:
            return None
        rendered.append(value)
    return " ".join(rendered)


def _validate_border_radius(tokens: list[Any]) -> str | None:
    parts = _split_literal(tokens, "/")
    if len(parts) > 2:
        return None
    rendered: list[str] = []
    for part in parts:
        group = _radius_group(part, 4)
        if group is None:
            return None
        rendered.append(group)
    return " / ".join(rendered)


def _validate_border_radius_longhand(tokens: list[Any]) -> str | None:
    if any(token.type == "literal" and token.value == "/" for token in tokens):
        return None
    return _radius_group(tokens, 2)


def _style_property_names() -> frozenset[str]:
    return frozenset(_STYLE_VALIDATORS)


_STYLE_VALIDATORS = {
    "color": _validate_color,
    "background-color": _validate_color,
    "font-family": _validate_font_family,
    "font-size": _validate_font_size,
    "font-style": _keyword_validator(frozenset({"normal", "italic", "oblique"})),
    "font-weight": _validate_font_weight,
    "font-variant": _keyword_validator(frozenset({"normal", "small-caps"})),
    "line-height": _validate_line_height,
    "letter-spacing": _validate_letter_spacing,
    "text-align": _keyword_validator(
        frozenset({"left", "right", "center", "justify", "start", "end"})
    ),
    "text-decoration": _validate_text_decoration,
    "text-transform": _keyword_validator(
        frozenset({"none", "capitalize", "uppercase", "lowercase"})
    ),
    "white-space": _keyword_validator(frozenset({"normal", "pre-wrap", "pre-line"})),
    "word-break": _keyword_validator(frozenset({"normal", "break-all", "break-word"})),
    "overflow-wrap": _keyword_validator(frozenset({"break-word", "anywhere"})),
    "margin": _validate_margin,
    "margin-top": _validate_margin,
    "margin-right": _validate_margin,
    "margin-bottom": _validate_margin,
    "margin-left": _validate_margin,
    "padding": _validate_padding,
    "padding-top": _validate_padding,
    "padding-right": _validate_padding,
    "padding-bottom": _validate_padding,
    "padding-left": _validate_padding,
    "border": _validate_border,
    "border-width": _validate_border_width,
    "border-style": _validate_border_style,
    "border-color": _validate_border_color,
    "border-spacing": _validate_border_spacing,
    "border-radius": _validate_border_radius,
    "border-collapse": _keyword_validator(frozenset({"collapse", "separate"})),
    "table-layout": _keyword_validator(frozenset({"auto", "fixed"})),
    "vertical-align": _validate_vertical_align,
    "width": _validate_size,
    "height": _validate_size,
    "max-width": _validate_max_size,
    "max-height": _validate_max_size,
    "min-width": _validate_size,
    "min-height": _validate_size,
    # Only the single-color form; re-serialized as background-color.
    "background": _validate_color,
    "text-indent": _validate_text_indent,
    "list-style-type": _keyword_validator(
        frozenset(
            {
                "none",
                "disc",
                "circle",
                "square",
                "decimal",
                "decimal-leading-zero",
                "lower-alpha",
                "upper-alpha",
                "lower-latin",
                "upper-latin",
                "lower-roman",
                "upper-roman",
            }
        )
    ),
    "direction": _keyword_validator(frozenset({"ltr", "rtl"})),
    "overflow": _keyword_validator(frozenset({"visible", "hidden", "auto"})),
    "visibility": _keyword_validator(frozenset({"visible", "hidden"})),
    "opacity": _validate_opacity,
    "float": _keyword_validator(frozenset({"left", "right", "none"})),
    "display": _keyword_validator(
        frozenset(
            {
                "block",
                "inline",
                "inline-block",
                "none",
                "list-item",
                "table",
                "table-row",
                "table-cell",
                "table-row-group",
                "table-header-group",
                "table-footer-group",
                "table-column",
                "table-column-group",
                "inline-table",
            }
        )
    ),
}
for _side in ("top", "right", "bottom", "left"):
    _STYLE_VALIDATORS[f"border-{_side}"] = _validate_border
    _STYLE_VALIDATORS[f"border-{_side}-width"] = _validate_border_width
    _STYLE_VALIDATORS[f"border-{_side}-style"] = _validate_border_style
    _STYLE_VALIDATORS[f"border-{_side}-color"] = _validate_border_color
for _corner in ("top-left", "top-right", "bottom-right", "bottom-left"):
    _STYLE_VALIDATORS[f"border-{_corner}-radius"] = _validate_border_radius_longhand
del _side, _corner
# Shorthands accepted in a restricted form and emitted as their longhand.
_STYLE_ALIASES = {"background": "background-color"}


@lru_cache(maxsize=1)
def _warn_missing_tinycss2() -> None:
    logger.warning("tinycss2 non installato: stili inline disabilitati")


def _sanitize_style(value: str) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _STYLE_MAX_LEN
        or not _text_is_safe(value)
    ):
        return None
    try:
        import tinycss2
    except Exception:  # noqa: BLE001 - optional dependency may fail to import
        _warn_missing_tinycss2()
        return None
    try:
        declarations = tinycss2.parse_declaration_list(
            value, skip_comments=True, skip_whitespace=False
        )
    except Exception:  # noqa: BLE001 - malformed CSS is fail-closed
        return None
    rendered: list[str] = []
    count = 0
    for declaration in declarations:
        if declaration.type == "whitespace":
            continue
        if declaration.type != "declaration":
            continue
        count += 1
        if count > _STYLE_MAX_DECLS:
            return None
        name = declaration.lower_name
        validator = _STYLE_VALIDATORS.get(name)
        if validator is None:
            continue
        tokens = _strip_whitespace(declaration.value)
        if not tokens or not _tokens_are_safe(tokens):
            continue
        try:
            serialized = validator(tokens)
        except Exception:  # noqa: BLE001, S112 - validators must be total
            continue
        if serialized is None:
            continue
        rendered.append(f"{_STYLE_ALIASES.get(name, name)}:{serialized}")
    if not rendered:
        return None
    result = ";".join(rendered)
    if len(result) > _STYLE_SERIALIZED_MAX:
        return None
    if not _text_is_safe(result):
        return None
    try:
        result.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return result


def _legacy_int(value: str, minimum: int, maximum: int) -> str | None:
    value = value.strip()
    if not re.fullmatch(r"-?[0-9]+", value):
        return None
    number = int(value)
    if not minimum <= number <= maximum:
        return None
    return str(number)


def _legacy_size(value: str, maximum: int) -> str | None:
    value = value.strip()
    percent = re.fullmatch(r"([0-9]+)%", value)
    if percent:
        number = int(percent.group(1))
        return f"{number}%" if 0 <= number <= 100 else None
    return _legacy_int(value, 0, maximum)


def _legacy_bgcolor(value: str) -> str | None:
    value = value.strip()
    if re.fullmatch(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?", value):
        return value.lower()
    if value.lower() in _NAMED_COLORS:
        return value.lower()
    return None


def _legacy_font_face(value: str) -> str | None:
    families = [family.strip() for family in value.split(",")]
    if not 1 <= len(families) <= 8:
        return None
    for family in families:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,63}", family):
            return None
    return ", ".join(families)


def _legacy_font_size(value: str) -> str | None:
    value = value.strip()
    return value if re.fullmatch(r"[+-]?[1-7]", value) else None


def _legacy_enum(*choices: str) -> Any:
    allowed = frozenset(choices)

    def validate(value: str) -> str | None:
        value = value.strip().lower()
        return value if value in allowed else None

    return validate


def _legacy_int_validator(minimum: int, maximum: int) -> Any:
    def validate(value: str) -> str | None:
        return _legacy_int(value, minimum, maximum)

    return validate


def _legacy_size_validator(maximum: int) -> Any:
    def validate(value: str) -> str | None:
        return _legacy_size(value, maximum)

    return validate


_LEGACY_ATTR_VALIDATORS = {
    ("table", "border"): _legacy_int_validator(0, 50),
    ("table", "cellpadding"): _legacy_int_validator(0, 50),
    ("table", "cellspacing"): _legacy_int_validator(0, 50),
    ("table", "bgcolor"): _legacy_bgcolor,
    ("table", "width"): _legacy_size_validator(10_000),
    ("table", "height"): _legacy_size_validator(2_000),
    ("table", "align"): _legacy_enum("left", "right", "center", "justify"),
    ("tr", "bgcolor"): _legacy_bgcolor,
    ("tr", "height"): _legacy_size_validator(2_000),
    ("tr", "align"): _legacy_enum("left", "right", "center", "justify"),
    ("tr", "valign"): _legacy_enum("top", "middle", "bottom", "baseline"),
    ("td", "bgcolor"): _legacy_bgcolor,
    ("td", "height"): _legacy_size_validator(2_000),
    ("td", "align"): _legacy_enum("left", "right", "center", "justify"),
    ("td", "valign"): _legacy_enum("top", "middle", "bottom", "baseline"),
    ("td", "colspan"): _legacy_int_validator(1, 100),
    ("td", "rowspan"): _legacy_int_validator(1, 100),
    ("td", "width"): _legacy_size_validator(10_000),
    ("th", "bgcolor"): _legacy_bgcolor,
    ("th", "height"): _legacy_size_validator(2_000),
    ("th", "align"): _legacy_enum("left", "right", "center", "justify"),
    ("th", "valign"): _legacy_enum("top", "middle", "bottom", "baseline"),
    ("th", "colspan"): _legacy_int_validator(1, 100),
    ("th", "rowspan"): _legacy_int_validator(1, 100),
    ("th", "width"): _legacy_size_validator(10_000),
    ("th", "scope"): _legacy_enum("row", "col", "rowgroup", "colgroup"),
    ("col", "span"): _legacy_int_validator(1, 100),
    ("col", "width"): _legacy_size_validator(10_000),
    ("col", "bgcolor"): _legacy_bgcolor,
    ("col", "align"): _legacy_enum("left", "right", "center", "justify"),
    ("col", "valign"): _legacy_enum("top", "middle", "bottom", "baseline"),
    ("colgroup", "span"): _legacy_int_validator(1, 100),
    ("colgroup", "width"): _legacy_size_validator(10_000),
    ("colgroup", "bgcolor"): _legacy_bgcolor,
    ("colgroup", "align"): _legacy_enum("left", "right", "center", "justify"),
    ("colgroup", "valign"): _legacy_enum("top", "middle", "bottom", "baseline"),
    ("ol", "start"): _legacy_int_validator(-10_000, 10_000),
    ("li", "value"): _legacy_int_validator(-10_000, 10_000),
    ("img", "width"): _legacy_size_validator(10_000),
    ("img", "height"): _legacy_size_validator(2_000),
    ("img", "align"): _legacy_enum("left", "right", "top", "middle", "bottom"),
    ("font", "color"): _legacy_bgcolor,
    ("font", "face"): _legacy_font_face,
    ("font", "size"): _legacy_font_size,
    # Quoted-text marker added by the scanner (see _quote_marker).
    ("div", "data-quote"): _legacy_enum("block", "after"),
    ("blockquote", "data-quote"): _legacy_enum("block", "after"),
}
# Attributes allowed on every tag (``"*"`` in ``_ALLOWED_HTML_ATTRIBUTES``).
_GLOBAL_ATTR_VALIDATORS = {
    "dir": _legacy_enum("ltr", "rtl", "auto"),
}


def _html_attribute_filter(
    tag: str,
    attribute: str,
    value: str,
    style_cache: dict[str, str | None] | None = None,
    cid_urls: Mapping[str, str] | None = None,
) -> str | None:
    """nh3 attribute callback; *style_cache* memoizes styles of one document.

    Inlined ``<style>`` CSS repeats the same ``style`` value on many elements
    (e.g. 533 attributes, 95 distinct), so parsing each value once matters.
    """
    try:
        normalized_tag = tag.lower()
        normalized_attribute = attribute.lower()
        if normalized_attribute == "style":
            if os.environ.get("GMAIL_HTML_STYLES", "1").strip() == "0":
                return None
            if style_cache is None:
                return _sanitize_style(value)
            if value not in style_cache:
                style_cache[value] = _sanitize_style(value)
            return style_cache[value]
        validator = _LEGACY_ATTR_VALIDATORS.get(
            (normalized_tag, normalized_attribute)
        ) or _GLOBAL_ATTR_VALIDATORS.get(normalized_attribute)
        if validator is not None:
            return validator(value)
        if normalized_tag == "img" and normalized_attribute == "data-src":
            return _filter_image_src(value, cid_urls)
        if len(value) > _ATTR_VALUE_MAX_LEN:
            return None
        return value
    except Exception:  # noqa: BLE001 - callback must never fail open
        return None


# Selectors that depend on user interaction or generate content: an inliner
# cannot evaluate them, and applying them unconditionally would be wrong.
_STATEFUL_SELECTOR_RE = re.compile(
    r"::|:(?:hover|focus|focus-within|focus-visible|active|visited|target|checked)\b",
    re.IGNORECASE,
)


@lru_cache(maxsize=1)
def _warn_missing_css_inline() -> None:
    logger.warning("css-inline non installato: CSS dei blocchi <style> ignorato")


# Pure function of the CSS text; newsletters from one sender share their
# stylesheet, so a small cache skips the tinycss2 parse on the next ones.
@lru_cache(maxsize=32)
def _filter_stylesheet(css: str) -> str:
    """Reduce ``<style>`` CSS to plain rules with allow-listed declarations.

    At-rules (``@media``, ``@import``, ``@font-face``…) and stateful selectors
    are dropped; every declaration is validated and re-serialized as in
    ``_sanitize_style`` (``!important`` is kept only to resolve the cascade:
    the final style sanitizer strips it).  This keeps the inlined ``style``
    attributes short; it is not the security boundary, which stays the
    sanitizer run on the inlined document.
    """
    if not css or len(css) > _INLINE_CSS_MAX_CHARS:
        return ""
    import tinycss2

    rendered: list[str] = []
    for rule in tinycss2.parse_stylesheet(
        css, skip_comments=True, skip_whitespace=True
    ):
        if rule.type != "qualified-rule":
            continue
        if len(rendered) >= _INLINE_CSS_MAX_RULES:
            break
        selectors = [
            selector
            for selector in (
                tinycss2.serialize(part).strip()
                for part in _split_literal(rule.prelude, ",")
            )
            if selector and not _STATEFUL_SELECTOR_RE.search(selector)
        ]
        if not selectors:
            continue
        declarations: list[str] = []
        for declaration in tinycss2.parse_declaration_list(
            rule.content, skip_comments=True, skip_whitespace=True
        ):
            if declaration.type != "declaration":
                continue
            name = declaration.lower_name
            validator = _STYLE_VALIDATORS.get(name)
            tokens = _strip_whitespace(declaration.value)
            if validator is None or not tokens or not _tokens_are_safe(tokens):
                continue
            value = validator(tokens)
            if value is None:
                continue
            important = " !important" if declaration.important else ""
            declarations.append(f"{_STYLE_ALIASES.get(name, name)}:{value}{important}")
        if declarations:
            rendered.append(f"{', '.join(selectors)}{{{';'.join(declarations)}}}")
    return "\n".join(rendered)


def _scan_inlined_html(raw: str, styles: list[str]) -> str:
    """Apply the email's ``<style>`` CSS as inline styles, then scan it again.

    Returns ``""`` whenever inlining is disabled, unavailable or fails, so the
    caller falls back to the document without inlining.
    """
    if os.environ.get("GMAIL_HTML_INLINE_CSS", "1").strip() == "0":
        return ""
    if os.environ.get("GMAIL_HTML_STYLES", "1").strip() == "0":
        return ""
    try:
        import css_inline
    except ImportError:
        _warn_missing_css_inline()
        return ""
    try:
        css = _filter_stylesheet("\n".join(styles))
        if not css:
            return ""
        inliner = css_inline.CSSInliner(
            inline_style_tags=False,
            keep_style_tags=False,
            keep_link_tags=False,
            keep_at_rules=False,
            load_remote_stylesheets=False,
            extra_css=css,
        )
        inlined = inliner.inline(raw)
        if not isinstance(inlined, str) or len(inlined) > _MAX_HTML_CHARS:
            return ""
        return _pre_scan_html(inlined)
    except Exception:
        logger.warning("Inlining CSS fallito", exc_info=True)
        return ""


def sanitize_html(raw: str, cid_urls: Mapping[str, str] | None = None) -> str:
    """Return a conservative, script-free HTML fragment for the reader.

    Email HTML is fully attacker-controlled.  Rendering it with ``innerHTML``
    without sanitizing would allow event handlers or scripts to steal the web
    UI Bearer token from ``localStorage``.  ``nh3`` removes disallowed tags,
    attributes, URL schemes and CSS; remote images are rewritten to signed
    same-origin proxy URLs in ``data-src`` and loaded by default in the client
    (tracking accepted by the user), never fetched directly by the browser.
    Inline CSS is re-serialized from a strict allow-list with ``tinycss2``;
    ``<style>`` blocks are first inlined (``css-inline``) so their rules go
    through the same allow-list.  If the inlined document is rejected, the
    original one is sanitized instead.  ``cid_urls`` maps the lowercase
    Content-ID of inline images to their signed proxy URL; without it
    ``cid:`` images are dropped.
    """
    if not raw or len(raw) > _MAX_HTML_CHARS:
        return ""
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError:
        return ""
    try:
        import nh3
    except ImportError:
        logger.warning("nh3 non installato: rendering HTML disabilitato")
        return ""
    styles: list[str] = []
    try:
        scanned = _pre_scan_html(raw, styles)
    except Exception:
        logger.warning("Scansione HTML fallita", exc_info=True)
        return ""
    if not scanned:
        return ""
    if styles:
        inlined = _scan_inlined_html(raw, styles)
        if inlined:
            result = _clean_scanned_html(nh3, inlined, cid_urls)
            if result:
                return result
    return _clean_scanned_html(nh3, scanned, cid_urls)


def _clean_scanned_html(
    nh3: Any, scanned: str, cid_urls: Mapping[str, str] | None = None
) -> str:
    """Run nh3 on a pre-scanned document and enforce the output invariants."""
    try:
        result = nh3.clean(
            scanned,
            tags=set(_ALLOWED_HTML_TAGS),
            clean_content_tags=set(_CLEAN_CONTENT_TAGS),
            attributes=_ALLOWED_HTML_ATTRIBUTES,
            attribute_filter=partial(
                _html_attribute_filter, style_cache={}, cid_urls=cid_urls
            ),
            url_schemes={"http", "https", "mailto"},
            url_relative="deny",
            link_rel="noopener noreferrer",
            strip_comments=True,
            filter_style_properties=set(_STYLE_VALIDATORS),
        )
    except Exception:
        logger.warning("Sanitizzazione HTML fallita", exc_info=True)
        return ""
    if not isinstance(result, str):
        return ""
    try:
        result.encode("utf-8")
    except UnicodeEncodeError:
        return ""
    if len(result) > _HTML_MAX_OUTPUT_CHARS:
        return ""
    try:
        if not _pre_scan_html(result):
            return ""
    except Exception:  # noqa: BLE001 - output must fail closed
        return ""
    return result


def _raw_html(message: Message) -> str:
    parts: list[str] = []
    for part in message.walk():
        if part.is_multipart() or _is_attachment_part(part):
            continue
        if part.get_content_type() == "text/html":
            parts.append(_payload_text(part))
    return "\n".join(parts)


def _extract_html(message: Message) -> str:
    return sanitize_html(_raw_html(message))


_CID_REFERENCE_RE = re.compile(r"""cid:([^\s"'<>()]+)""", re.IGNORECASE)


def _mark_inline_images(attachments: list[dict[str, Any]], raw_html: str) -> None:
    """Flag image parts that the HTML body shows via ``cid:`` as ``inline``.

    A ``Content-ID`` alone is not enough (Apple Mail sets one on regular
    attachments too): the part must be referenced by the raw HTML.
    """
    referenced = {
        unquote(match).strip("<>").lower()
        for match in _CID_REFERENCE_RE.findall(raw_html)
    }
    for attachment in attachments:
        content_id = (attachment.get("content_id") or "").lower()
        content_type = str(attachment.get("content_type") or "")
        attachment["inline"] = bool(
            content_id
            and content_id in referenced
            and content_type.startswith("image/")
        )


def _extract_text(message: Message) -> str:
    plain: list[str] = []
    html: list[str] = []
    for part in message.walk():
        if part.is_multipart() or _is_attachment_part(part):
            continue
        content_type = part.get_content_type()
        if content_type == "text/plain":
            plain.append(_payload_text(part).strip())
        elif content_type == "text/html":
            html.append(_payload_text(part))
    text = "\n".join(part for part in plain if part)
    if text:
        return text
    return "\n".join(
        converted for converted in (html_to_text(part) for part in html) if converted
    )


def _extract_attachments(
    message: Message, part_map: _PartMap | None = None
) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    mapped = part_map.attachments if part_map is not None else ()
    for part in message.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        if (
            not filename
            and part.get_content_disposition() != "attachment"
            # Inline images are attachments too; skip only inline text parts.
            and (not part.get("Content-ID") or part.get_content_maintype() == "text")
        ):
            continue
        decoded_name = decode_mime_header(filename)
        safe_name = sanitize_filename(decoded_name) or f"attachment-{len(attachments)}"
        payload = part.get_payload(decode=True) or b""
        content_id = decode_mime_header(part.get("Content-ID")).strip().strip("<>")
        if part_map is not None and len(attachments) < len(mapped):
            size = mapped[len(attachments)].octets
        else:
            size = _encoded_size(part)
        attachments.append(
            {
                "index": len(attachments),
                "filename": safe_name,
                "content_type": part.get_content_type(),
                "size": size,
                "content_id": content_id or None,
                "data": payload,
            }
        )
    return attachments


def _parse_full(
    raw: bytes,
    part_map: _PartMap | None = None,
    *,
    folder: str | None = None,
    uid: int | None = None,
) -> dict[str, Any]:
    """Parse a full message; with *folder*/*uid* inline images get proxy URLs."""
    message = message_from_bytes(raw)
    raw_html = _raw_html(message)
    attachments = _extract_attachments(message, part_map)
    _mark_inline_images(attachments, raw_html)
    cid_urls = (
        _cid_image_urls(attachments, folder, uid)
        if folder is not None and uid is not None
        else None
    )
    return {
        "from": decode_mime_header(message.get("From")),
        "to": decode_mime_header(message.get("To")),
        "cc": decode_mime_header(message.get("Cc")),
        "subject": decode_mime_header(message.get("Subject")),
        "date_ts": _date_to_ts(message.get("Date")),
        "message_id": decode_mime_header(message.get("Message-ID")).strip(),
        "in_reply_to": decode_mime_header(message.get("In-Reply-To")).strip(),
        "references": decode_mime_header(message.get("References")).strip(),
        "text": _extract_text(message),
        "html": sanitize_html(raw_html, cid_urls),
        "attachments": attachments,
    }


def parse_message(raw: bytes) -> dict[str, Any]:
    """Parse a raw RFC 822 message into the API-neutral detail shape.

    Attachment indices are assigned by walking the MIME tree once, so the
    detail listing and the download endpoint always agree on ``index``.
    """
    return _parse_full(raw)


def _has_attachment_hint(message: Message) -> bool:
    """Cheap attachment hint from headers only (no body download).

    ``multipart/mixed`` and ``multipart/related`` almost always carry a file
    or an inline image.  This is a heuristic for the list badge; the exact
    attachment list is computed when the message is opened.
    """
    content_type = str(message.get("Content-Type") or "").lower()
    return (
        content_type.startswith(("multipart/mixed", "multipart/related"))
        or bool(message.get_filename())
        or message.get_content_disposition() == "attachment"
    )


def parse_summary(raw: bytes) -> dict[str, Any]:
    """Parse a header-only message into the cheap list representation.

    The list must never download full bodies: on a large Gmail mailbox that
    means transferring every attachment of the page (slow and memory-hungry).
    ``snippet`` stays empty in the POC; the body is fetched only on open.
    """
    message = message_from_bytes(raw)
    return {
        "from": decode_mime_header(message.get("From")),
        "to": decode_mime_header(message.get("To")),
        "subject": decode_mime_header(message.get("Subject")),
        "date_ts": _date_to_ts(message.get("Date")),
        "snippet": "",
        "has_attachments": _has_attachment_hint(message),
    }


def public_attachments(attachments: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip raw payloads from parsed attachments for JSON responses."""
    return [
        {key: value for key, value in attachment.items() if key != "data"}
        for attachment in attachments
    ]


def append_quote(user_text: str, original_text: str) -> str:
    """Append a ``> `` quoted copy of the original message exactly once."""
    original = (original_text or "").strip()
    if not original:
        return user_text
    quoted = "\n".join(f"> {line}" if line else ">" for line in original.splitlines())
    body = (user_text or "").rstrip()
    return f"{body}\n\n{quoted}" if body else quoted


def _reply_subject(subject: str) -> str:
    if subject.lower().startswith("re:"):
        return subject
    return f"Re: {subject}" if subject else "Re:"


def _mailbox(folder: str) -> str:
    return f'"{folder}"'


def _unquote_mailbox(raw: bytes) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == ord('"') and raw[-1] == ord('"'):
        raw = raw[1:-1]
    unescaped = bytearray()
    index = 0
    while index < len(raw):
        if raw[index] == ord("\\") and index + 1 < len(raw):
            unescaped.append(raw[index + 1])
            index += 2
        else:
            unescaped.append(raw[index])
            index += 1
    return bytes(unescaped).decode("utf-8", errors="replace")


def _parse_list_entry(raw: bytes) -> tuple[set[str], str] | None:
    line = raw.strip()
    if not line.startswith(b"("):
        return None
    close = line.find(b")")
    if close < 0:
        return None
    flags = {flag.lower() for flag in line[1:close].decode("ascii", "ignore").split()}
    rest = line[close + 1 :].strip()
    if rest.startswith(b'"'):
        index = 1
        while index < len(rest):
            if rest[index] == ord("\\"):
                index += 2
                continue
            if rest[index] == ord('"'):
                break
            index += 1
        if index >= len(rest):
            return None
        rest = rest[index + 1 :].strip()
    elif rest[:3].upper() == b"NIL":
        rest = rest[3:].strip()
    else:
        return None
    name = _unquote_mailbox(rest)
    return (flags, name) if name else None


def _find_trash_mailbox(connection: imaplib.IMAP4) -> str:
    """Return the special-use ``\\Trash`` mailbox, falling back to Gmail's name."""
    try:
        status, data = connection.list('""', '"*"')
    except (imaplib.IMAP4.error, OSError):
        status, data = "NO", []
    if status == "OK":
        for item in data or []:
            if not isinstance(item, (bytes, bytearray)):
                continue
            parsed = _parse_list_entry(bytes(item))
            if parsed and "\\trash" in parsed[0]:
                return parsed[1]
    return "[Gmail]/Trash"


def _uidvalidity(connection: imaplib.IMAP4) -> int:
    try:
        # ``imaplib`` returns ``("UIDVALIDITY", [b"<value>"])`` here: the first
        # element is the response *type*, not an ``OK`` status.  Comparing it
        # with ``"OK"`` silently returned 0 on every real server.
        _typ, data = connection.response("UIDVALIDITY")
    except (imaplib.IMAP4.error, OSError):
        return 0
    if not data or not data[0]:
        return 0
    try:
        return int(data[0])
    except (TypeError, ValueError):
        return 0


def _update_flags(target: dict[str, Any], meta: bytes) -> None:
    match = _FLAGS_RE.search(meta)
    if not match:
        return
    raw = match.group(1).decode("ascii", errors="ignore")
    target["flags"].update(flag for flag in raw.split() if flag)


def _parse_fetch_data(data: list[Any]) -> list[dict[str, Any]]:
    """Group imaplib FETCH items into ``{uid, flags, raw}`` records."""
    records: list[dict[str, Any]] = []
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2:
            meta, literal = item[0], item[1]
            if not isinstance(meta, (bytes, bytearray)):
                continue
            meta = bytes(meta)
            uid_match = _UID_RE.search(meta)
            if uid_match:
                records.append(
                    {"uid": int(uid_match.group(1)), "flags": set(), "raw": b""}
                )
            if not records:
                continue
            _update_flags(records[-1], meta)
            if literal:
                records[-1]["raw"] += bytes(literal)
        elif isinstance(item, (bytes, bytearray)):
            meta = bytes(item)
            uid_match = _UID_RE.search(meta)
            if uid_match:
                records.append(
                    {"uid": int(uid_match.group(1)), "flags": set(), "raw": b""}
                )
            if records:
                _update_flags(records[-1], meta)
    return records


def _fetch_full_on(connection: imaplib.IMAP4, uid: int) -> dict[str, Any]:
    status, data = connection.uid("FETCH", str(uid), "(FLAGS BODY.PEEK[])")
    if status != "OK":
        raise GmailUpstreamError("Lettura messaggio fallita")
    records = _parse_fetch_data(list(data or []))
    if not records:
        raise GmailNotFound("Messaggio non trovato")
    return records[0]


def _structure_uid(meta: bytes) -> int | None:
    match = _UID_RE.search(meta)
    return int(match.group(1)) if match is not None else None


def _parse_structure_fetch(
    data: list[Any], expected_uid: int
) -> _StructureRecord | None:
    uid_seen: int | None = None
    block: bytes | None = None
    headers = bytearray()
    have_headers = False
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2:
            meta, literal = item[0], item[1]
            if not isinstance(meta, (bytes, bytearray)):
                return None
            meta = bytes(meta)
            uid = _structure_uid(meta)
            if uid is not None:
                if uid_seen is not None and uid_seen != uid:
                    return None
                uid_seen = uid
            if block is None:
                block = _extract_bodystructure(meta)
            if b"BODY[HEADER" in meta.upper() and isinstance(
                literal, (bytes, bytearray)
            ):
                headers.extend(literal)
                have_headers = True
        elif isinstance(item, (bytes, bytearray)):
            meta = bytes(item)
            uid = _structure_uid(meta)
            if uid is not None:
                if uid_seen is not None and uid_seen != uid:
                    return None
                uid_seen = uid
            if block is None:
                block = _extract_bodystructure(meta)
        else:
            return None
    if uid_seen != expected_uid or block is None or not have_headers:
        return None
    try:
        root = parse_bodystructure(block)
        if root is None:
            return None
        index = _index_bodystructure(root)
    except Exception:  # noqa: BLE001 - malformed structure means fallback
        return None
    return _StructureRecord(uid=expected_uid, headers=bytes(headers), index=index)


def _fetch_structure(connection: imaplib.IMAP4, uid: int) -> _StructureRecord | None:
    status, data = connection.uid("FETCH", str(uid), _STRUCTURE_FETCH_ITEM)
    if status != "OK":
        return None
    return _parse_structure_fetch(list(data or []), uid)


def _parse_part_fetch(
    data: list[Any], expected_uid: int, expected_parts: Iterable[str]
) -> dict[str, bytes]:
    expected = set(expected_parts)
    if not expected or any(
        _PART_NUMBER_RE.fullmatch(part) is None for part in expected
    ):
        raise _PartFetchError
    uid_seen: int | None = None
    sections: dict[str, bytes] = {}
    for item in data:
        if isinstance(item, tuple) and len(item) >= 2:
            meta, literal = item[0], item[1]
            if not isinstance(meta, (bytes, bytearray)):
                raise _PartFetchError
            meta = bytes(meta)
            uid = _structure_uid(meta)
            if uid is not None:
                if uid_seen is not None and uid_seen != uid:
                    raise _PartFetchError
                uid_seen = uid
            labels = _BODY_SECTION_RE.findall(meta)
            if len(labels) != 1:
                raise _PartFetchError
            try:
                label = labels[0].decode("ascii")
            except UnicodeDecodeError:
                raise _PartFetchError from None
            if _PART_NUMBER_RE.fullmatch(label) is None or label not in expected:
                raise _PartFetchError
            if label in sections or not isinstance(literal, (bytes, bytearray)):
                raise _PartFetchError
            sections[label] = bytes(literal)
        elif isinstance(item, (bytes, bytearray)):
            meta = bytes(item)
            uid = _structure_uid(meta)
            if uid is not None:
                if uid_seen is not None and uid_seen != uid:
                    raise _PartFetchError
                uid_seen = uid
            if _BODY_SECTION_RE.search(meta):
                raise _PartFetchError
        else:
            raise _PartFetchError
    if uid_seen != expected_uid or set(sections) != expected:
        raise _PartFetchError
    return sections


def _fetch_part_payloads(
    connection: imaplib.IMAP4, uid: int, parts: Iterable[str]
) -> dict[str, bytes]:
    selected = list(parts)
    if not selected:
        return {}
    if any(_PART_NUMBER_RE.fullmatch(part) is None for part in selected):
        raise _PartFetchError
    item = "(" + " ".join(f"BODY.PEEK[{part}]" for part in selected) + ")"
    status, data = connection.uid("FETCH", str(uid), item)
    if status != "OK":
        raise _PartFetchError
    return _parse_part_fetch(list(data or []), uid, selected)


def _fetch_render_parts(
    connection: imaplib.IMAP4,
    uid: int,
    index: _PartMap,
    extra: Iterable[_MimePart] = (),
) -> dict[str, bytes]:
    """Fetch the text parts (plus *extra* parts) in one ``UID FETCH``."""
    selected = [part.walk_index for part in index.render_parts]
    if len(selected) > _MAX_RENDER_PARTS:
        raise _PartFetchError
    selected += [part.walk_index for part in extra]
    return _fetch_part_payloads(connection, uid, selected)


def _mini_part_message(part: _MimePart, payload: bytes) -> EmailMessage:
    if (
        _MIME_TOKEN_RE.fullmatch(part.maintype) is None
        or _MIME_TOKEN_RE.fullmatch(part.subtype) is None
    ):
        raise _PartFetchError
    mini = EmailMessage()
    mini["Content-Type"] = part.content_type
    charset = _params_get(part.params, "charset").strip()
    if charset and _CHARSET_RE.fullmatch(charset):
        mini.set_param("charset", charset)
    encoding = (part.encoding or "").strip().lower()
    if encoding:
        if _MIME_TOKEN_RE.fullmatch(encoding) is None:
            raise _PartFetchError
        mini["Content-Transfer-Encoding"] = encoding
    mini.set_payload(payload)
    return mini


def _decode_fetched_text(part: _MimePart, payload: bytes) -> str:
    try:
        return _payload_text(_mini_part_message(part, payload))
    except _PartFetchError:
        raise
    except Exception as exc:
        raise _PartFetchError from exc


def _decode_fetched_bytes(part: _MimePart, payload: bytes) -> bytes:
    try:
        mini = _mini_part_message(part, payload)
        data = mini.get_payload(decode=True)
    except _PartFetchError:
        raise
    except Exception as exc:
        raise _PartFetchError from exc
    if data is None:
        return payload
    if isinstance(data, str):
        return data.encode("utf-8", errors="replace")
    return bytes(data)


def _safe_part_filename(part: _MimePart, index: int) -> str:
    return sanitize_filename(part.filename or "") or f"attachment-{index}"


def _assemble_detail(
    uid: int,
    record: _StructureRecord,
    payloads: dict[str, bytes],
    folder: str | None = None,
) -> dict[str, Any]:
    headers = message_from_bytes(record.headers)
    plain: list[str] = []
    html: list[str] = []
    for part in record.index.render_parts:
        payload = payloads.get(part.walk_index)
        if payload is None:
            continue
        text = _decode_fetched_text(part, payload)
        if part.subtype == "plain":
            plain.append(text.strip())
        else:
            html.append(text)
    text = "\n".join(part for part in plain if part)
    if not text:
        text = "\n".join(
            converted
            for converted in (html_to_text(part) for part in html)
            if converted
        )
    attachments = [
        {
            "index": index,
            "filename": _safe_part_filename(part, index),
            "content_type": part.content_type,
            "size": part.octets,
            "content_id": part.content_id,
        }
        for index, part in enumerate(record.index.attachments)
    ]
    _mark_inline_images(attachments, "\n".join(html))
    cid_urls = _cid_image_urls(attachments, folder, uid) if folder is not None else None
    return {
        "uid": uid,
        "from": decode_mime_header(headers.get("From")),
        "to": decode_mime_header(headers.get("To")),
        "cc": decode_mime_header(headers.get("Cc")),
        "subject": decode_mime_header(headers.get("Subject")),
        "date_ts": _date_to_ts(headers.get("Date")),
        "message_id": decode_mime_header(headers.get("Message-ID")).strip(),
        "in_reply_to": decode_mime_header(headers.get("In-Reply-To")).strip(),
        "references": decode_mime_header(headers.get("References")).strip(),
        "text": text,
        "html": sanitize_html("\n".join(html), cid_urls),
        "attachments": attachments,
    }


def _summary_sort_key(message: dict[str, Any]) -> tuple[bool, int, int]:
    date_ts = message["date_ts"]
    if date_ts is None:
        return (False, 0, message["uid"])
    return (True, date_ts, message["uid"])


def fetch_message_list(
    folder: str | None = None,
    offset: int = 0,
    limit: int | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """Return a date-descending page of message summaries.

    Gmail IMAP rejects ``UID SORT`` with ``BAD``, so ordering is done locally
    on the newest :data:`_LIST_WINDOW` UIDs (``UID SEARCH ALL`` + header-only
    fetch).  Messages are sorted by ``date_ts`` descending with ``uid`` as the
    tie-breaker; messages with a missing or invalid ``Date`` go last.  The
    offset/limit page is applied after that ordering.  ``total`` counts only
    the messages in the window, not the entire mailbox.

    The ordered window is cached per ``(folder, account fingerprint)`` for
    :data:`_GMAIL_LIST_CACHE_TTL_S` seconds.  ``refresh=True`` drops the entry
    and bypasses the read, then stores the fresh result.
    """
    settings = load_settings()
    folder_value = validate_folder(folder or settings.folder)
    page_size = _validate_limit(limit)
    start = _validate_offset(offset)
    fingerprint = _account_fingerprint(settings)
    cache_key = (folder_value, fingerprint)

    if refresh:
        _invalidate_inbox(folder_value, fingerprint)
        with _inbox_cache_lock:
            epoch0 = _inbox_epoch
    else:
        with _inbox_cache_lock:
            cached = _inbox_cache_get_locked(cache_key, _now())
            if cached is not None:
                return _list_response(cached, folder_value, start, page_size)
            epoch0 = _inbox_epoch

    with imap_connect(settings) as connection:
        status, _ = connection.select(_mailbox(folder_value), readonly=True)
        if status != "OK":
            raise GmailUpstreamError("Cartella non disponibile")
        uidvalidity = _uidvalidity(connection)
        status, data = connection.uid("SEARCH", None, "ALL")
        if status != "OK":
            raise GmailUpstreamError("Ricerca messaggi fallita")

        uids: list[int] = []
        for chunk in data or []:
            if not chunk:
                continue
            for value in bytes(chunk).split():
                try:
                    uids.append(int(value))
                except ValueError:
                    continue
        window = sorted(set(uids), reverse=True)[:_LIST_WINDOW]

        if not window:
            messages: list[dict[str, Any]] = []
        else:
            # Headers only, restricted to the fields used by ``parse_summary``:
            # full ``BODY.PEEK[HEADER]`` would transfer every Received/DKIM/
            # List-* header of the window (1.1 MB on a 200-message mailbox).
            status, data = connection.uid(
                "FETCH",
                ",".join(str(uid) for uid in window),
                _LIST_FETCH_ITEM,
            )
            if status != "OK":
                raise GmailUpstreamError("Lettura messaggi fallita")
            by_uid = {
                record["uid"]: record for record in _parse_fetch_data(list(data or []))
            }

            messages = []
            for uid in window:
                record = by_uid.get(uid)
                if record is None:
                    continue
                parsed = parse_summary(record["raw"])
                flags = list(_canonical_flags(record["flags"]))
                lower_flags = {flag.lower() for flag in flags}
                messages.append(
                    {
                        "uid": uid,
                        "from": parsed["from"],
                        "to": parsed["to"],
                        "subject": parsed["subject"],
                        "date_ts": parsed["date_ts"],
                        "snippet": parsed["snippet"],
                        "has_attachments": parsed["has_attachments"],
                        "seen": "\\seen" in lower_flags,
                        "flags": flags,
                    }
                )
            messages.sort(key=_summary_sort_key, reverse=True)

    entry = _InboxCacheEntry(
        uidvalidity=uidvalidity,
        messages=tuple(_freeze_message(message) for message in messages),
        expires_at=_now() + _GMAIL_LIST_CACHE_TTL_S,
    )
    with _inbox_cache_lock:
        _inbox_cache_put_locked(cache_key, entry, epoch0)
    return _list_response(entry, folder_value, start, page_size)


def fetch_message(uid: int | str, folder: str | None = None) -> dict[str, Any]:
    """Return the full, rendered-as-text detail for one message."""
    settings = load_settings()
    uid_value = _validate_uid(uid)
    folder_value = validate_folder(folder or settings.folder)
    with imap_connect(settings) as connection:
        status, _ = connection.select(_mailbox(folder_value), readonly=True)
        if status != "OK":
            raise GmailUpstreamError("Cartella non disponibile")

        part_map: _PartMap | None = None
        if _PARTIAL_FETCH_ENABLED:
            structure = _fetch_structure(connection, uid_value)
            if structure is not None:
                part_map = structure.index
                candidates = _inline_image_candidates(part_map)
                try:
                    try:
                        payloads = _fetch_render_parts(
                            connection, uid_value, part_map, candidates
                        )
                    except _PartFetchError:
                        if not candidates:
                            raise
                        # The extra image parts must never cost the fast path.
                        payloads = _fetch_render_parts(connection, uid_value, part_map)
                    detail = _assemble_detail(
                        uid_value, structure, payloads, folder_value
                    )
                    _cache_inline_images(
                        folder_value,
                        uid_value,
                        part_map,
                        detail["attachments"],
                        payloads,
                    )
                    return detail
                except _PartFetchError:
                    pass

        record = _fetch_full_on(connection, uid_value)
        parsed = _parse_full(
            record["raw"], part_map, folder=folder_value, uid=uid_value
        )
        for attachment in parsed["attachments"]:
            if attachment.get("inline"):
                _inline_cache_put(
                    folder_value, uid_value, attachment["index"], attachment["data"]
                )
        return {
            "uid": record["uid"],
            "from": parsed["from"],
            "to": parsed["to"],
            "cc": parsed["cc"],
            "subject": parsed["subject"],
            "date_ts": parsed["date_ts"],
            "message_id": parsed["message_id"],
            "in_reply_to": parsed["in_reply_to"],
            "references": parsed["references"],
            "text": parsed["text"],
            "html": parsed["html"],
            "attachments": public_attachments(parsed["attachments"]),
        }


def fetch_attachment(
    uid: int | str, index: int | str, folder: str | None = None
) -> AttachmentDownload:
    """Return one attachment using the same MIME index as the detail endpoint."""
    try:
        attachment_index = int(index)
    except (TypeError, ValueError):
        raise GmailValidationError("Indice allegato non valido") from None
    if attachment_index < 0:
        raise GmailValidationError("Indice allegato non valido")

    settings = load_settings()
    uid_value = _validate_uid(uid)
    folder_value = validate_folder(folder or settings.folder)
    with imap_connect(settings) as connection:
        status, _ = connection.select(_mailbox(folder_value), readonly=True)
        if status != "OK":
            raise GmailUpstreamError("Cartella non disponibile")

        part_map: _PartMap | None = None
        if _PARTIAL_FETCH_ENABLED:
            structure = _fetch_structure(connection, uid_value)
            if structure is not None:
                part_map = structure.index
                if attachment_index >= len(part_map.attachments):
                    raise GmailNotFound("Allegato non trovato")
                part = part_map.attachments[attachment_index]
                if (
                    part.encoding in ("7bit", "8bit", "binary")
                    and part.octets is not None
                    and part.octets > _MAX_DOWNLOAD_BYTES
                ):
                    raise GmailValidationError(
                        "Allegato troppo grande", status_code=413
                    )
                payload: bytes | None = None
                try:
                    payload = _fetch_part_payloads(
                        connection, uid_value, [part.walk_index]
                    ).get(part.walk_index)
                except _PartFetchError:
                    payload = None
                if payload is not None:
                    try:
                        data = _decode_fetched_bytes(part, payload)
                    except _PartFetchError:
                        pass
                    else:
                        if len(data) > _MAX_DOWNLOAD_BYTES:
                            raise GmailValidationError(
                                "Allegato troppo grande", status_code=413
                            )
                        return AttachmentDownload(
                            filename=_safe_part_filename(part, attachment_index),
                            content_type=part.content_type,
                            data=data,
                        )

        record = _fetch_full_on(connection, uid_value)
        parsed = _parse_full(record["raw"], part_map)
        attachments = parsed["attachments"]
        if attachment_index >= len(attachments):
            raise GmailNotFound("Allegato non trovato")
        attachment = attachments[attachment_index]
        data = attachment["data"]
        if len(data) > _MAX_DOWNLOAD_BYTES:
            raise GmailValidationError("Allegato troppo grande", status_code=413)
        return AttachmentDownload(
            filename=attachment["filename"],
            content_type=attachment["content_type"],
            data=data,
        )


def mark_read(uid: int | str, folder: str | None = None) -> None:
    """Set the ``\\Seen`` flag on a message."""
    settings = load_settings()
    uid_value = _validate_uid(uid)
    folder_value = validate_folder(folder or settings.folder)
    with imap_connect(settings) as connection:
        status, _ = connection.select(_mailbox(folder_value), readonly=False)
        if status != "OK":
            raise GmailUpstreamError("Cartella non disponibile")
        status, _ = connection.uid("STORE", str(uid_value), "+FLAGS", "(\\Seen)")
        if status != "OK":
            raise GmailUpstreamError("Aggiornamento flag fallito")

    _mark_read_cached(folder_value, _account_fingerprint(settings), uid_value)


def delete_messages(uids: object, folder: str | None = None) -> dict[str, Any]:
    """Move several messages to Gmail's Trash in a single IMAP connection.

    The Trash mailbox is discovered through the special-use ``\\Trash`` LIST
    attribute because user-supplied folder validation rejects ``[``/``]``.
    Gmail does not advertise UIDPLUS in ``CAPABILITY``, so plain ``EXPUNGE``
    is used: it expunges every ``\\Deleted`` message already present in the
    selected folder, not only the UIDs in this request.  UIDs that do not
    exist are reported as ``missing`` instead of failing the whole batch.
    """
    settings = load_settings()
    uid_values = _validate_uids(uids)
    folder_value = validate_folder(folder or settings.folder)
    try:
        with imap_connect(settings) as connection:
            status, _ = connection.select(_mailbox(folder_value), readonly=False)
            if status != "OK":
                raise GmailUpstreamError("Cartella non disponibile")
            status, data = connection.uid(
                "FETCH", ",".join(str(uid) for uid in uid_values), "(FLAGS)"
            )
            if status != "OK":
                raise GmailUpstreamError("Lettura messaggi fallita")
            existing = {record["uid"] for record in _parse_fetch_data(list(data or []))}
            existing_uids = [uid for uid in uid_values if uid in existing]
            missing = [uid for uid in uid_values if uid not in existing]
            if not existing_uids:
                return {"ok": True, "trashed": 0, "missing": missing}

            sequence = ",".join(str(uid) for uid in existing_uids)
            trash = _find_trash_mailbox(connection)
            status, _ = connection.uid("COPY", sequence, _mailbox(trash))
            if status != "OK":
                raise GmailUpstreamError("Spostamento nel Cestino fallito")
            status, _ = connection.uid("STORE", sequence, "+FLAGS", "(\\Deleted)")
            if status != "OK":
                raise GmailUpstreamError("Aggiornamento flag fallito")
            status, _ = connection.expunge()
            if status != "OK":
                raise GmailUpstreamError("Espulsione messaggio fallita")
    finally:
        # A delete changes membership/order, and COPY/STORE/EXPUNGE may have
        # completed partially: always invalidate every cached folder.
        _invalidate_inbox_all()
    return {"ok": True, "trashed": len(existing_uids), "missing": missing}


def delete_message(uid: int | str, folder: str | None = None) -> None:
    """Move one message to Gmail's Trash, raising if it no longer exists."""
    result = delete_messages([_validate_uid(uid)], folder)
    if not result["trashed"]:
        raise GmailNotFound("Messaggio non trovato")


def _validate_attachments(
    attachments: Iterable[tuple[str, bytes]],
) -> list[tuple[str, bytes]]:
    normalized: list[tuple[str, bytes]] = []
    total = 0
    for filename, data in attachments:
        if len(normalized) >= _MAX_ATTACHMENTS:
            raise GmailValidationError("Troppi allegati")
        if not isinstance(data, (bytes, bytearray)):
            raise GmailValidationError("Allegato non valido")
        payload = bytes(data)
        if len(payload) > _MAX_ATTACHMENT_BYTES:
            raise GmailValidationError("Allegato troppo grande", status_code=413)
        total += len(payload)
        if total > _MAX_TOTAL_ATTACHMENT_BYTES:
            raise GmailValidationError("Allegati troppo grandi", status_code=413)
        safe_name = sanitize_filename(filename) or "attachment"
        normalized.append((safe_name, payload))
    return normalized


def _add_attachment(message: EmailMessage, filename: str, data: bytes) -> None:
    content_type, _ = mimetypes.guess_type(filename)
    if content_type and "/" in content_type:
        maintype, subtype = content_type.split("/", 1)
    else:
        maintype, subtype = "application", "octet-stream"
    message.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)


def send_email(
    *,
    to: str = "",
    cc: str = "",
    bcc: str = "",
    subject: str = "",
    text: str = "",
    reply_to_uid: int | None = None,
    reply_to_folder: str | None = None,
    attachments: Iterable[tuple[str, bytes]] = (),
) -> str:
    """Send a message and return its ``Message-ID``.

    Replies fetch the original message, derive threading headers and append a
    ``> `` quote of the original plain text to *text* (once, server-side).
    """
    settings = load_settings()
    to_list = _parse_addresses(to, "to")
    cc_list = _parse_addresses(cc, "cc")
    bcc_list = _parse_addresses(bcc, "bcc")
    clean_subject = validate_header_value(subject or "", "subject")
    body = _validate_text(text or "")
    files = _validate_attachments(attachments)

    in_reply_to = ""
    references = ""
    if reply_to_uid is not None:
        original = fetch_message(reply_to_uid, reply_to_folder or settings.folder)
        original_message_id = original["message_id"]
        if not to_list:
            to_list = _parse_addresses(original["from"], "to")
        clean_subject = _reply_subject(clean_subject or original["subject"])
        body = append_quote(body, original["text"])
        if original_message_id:
            in_reply_to = original_message_id
            previous = original["references"]
            references = (
                f"{previous} {original_message_id}" if previous else original_message_id
            )

    if not (to_list or cc_list or bcc_list):
        raise GmailValidationError("Destinatario mancante")

    message = EmailMessage()
    message["From"] = settings.address
    if to_list:
        message["To"] = ", ".join(to_list)
    if cc_list:
        message["Cc"] = ", ".join(cc_list)
    if bcc_list:
        message["Bcc"] = ", ".join(bcc_list)
    message["Subject"] = clean_subject
    message["Date"] = formatdate(localtime=True)
    domain = settings.address.rpartition("@")[2] or None
    message_id = make_msgid(domain=domain)
    message["Message-ID"] = message_id
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = references
    message.set_content(body)

    for filename, data in files:
        _add_attachment(message, filename, data)

    with smtp_connect(settings) as smtp:
        smtp.send_message(message)
    # The sent copy lands in Sent/archive: we do not know which cached folder
    # changed, so drop every window.
    _invalidate_inbox_all()
    return message_id


@contextmanager
def imap_connect(settings: GmailSettings | None = None) -> Iterator[imaplib.IMAP4]:
    """Yield an authenticated IMAP connection, pooled unless the flag is off."""
    settings = settings or load_settings()
    if not _IMAP_POOL_ENABLED:
        connection: imaplib.IMAP4 | None = None
        try:
            connection = imaplib.IMAP4_SSL(
                settings.imap_host,
                settings.imap_port,
                timeout=settings.timeout_s,
            )
            connection.login(settings.address, settings.password)
            yield connection
        finally:
            _close_conn(connection)
        return

    pc = _imap_acquire(settings)
    try:
        yield pc.conn
    except BaseException as exc:
        _imap_release(pc, not _is_keep_exception(exc))
        raise
    else:
        _imap_release(pc, False)


@contextmanager
def smtp_connect(settings: GmailSettings | None = None) -> Iterator[smtplib.SMTP]:
    """Open SMTP on the configured port and require STARTTLS before login."""
    settings = settings or load_settings()
    smtp = smtplib.SMTP(
        settings.smtp_host,
        settings.smtp_port,
        timeout=settings.timeout_s,
    )
    try:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(settings.address, settings.password)
        yield smtp
    finally:
        try:
            smtp.quit()
        except (smtplib.SMTPException, OSError):
            try:
                smtp.close()
            except (smtplib.SMTPException, OSError):
                pass
