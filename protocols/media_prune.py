"""Best-effort pruning of media files no longer referenced by the cache DB."""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_GRACE_S: float = 3600


@dataclass(frozen=True)
class MediaScope:
    protocol: str
    label: str
    dir: Path


@dataclass(frozen=True)
class OrphanFile:
    path: Path
    size: int
    reason: str


@dataclass
class ScopeReport:
    scope: str
    dir: Path
    files_scanned: int = 0
    refs_collected: int = 0
    orphans_found: int = 0
    orphans_deleted: int = 0
    orphans_dryrun: list[OrphanFile] = field(default_factory=list)
    bytes_freed: int = 0
    skipped_grace: int = 0
    errors: int = 0
    skipped_dir_missing: bool = False
    skipped_db_error: bool = False


@dataclass
class MediaPruneReport:
    scopes: list[ScopeReport] = field(default_factory=list)

    def total_orphans(self) -> int:
        return sum(scope.orphans_found for scope in self.scopes)

    def total_bytes(self) -> int:
        return sum(
            scope.bytes_freed + sum(item.size for item in scope.orphans_dryrun)
            for scope in self.scopes
        )


def default_scopes() -> list[MediaScope]:
    """Resolve the media scope registry using the current configuration."""
    from protocols.config import get_whatsapp_media_dir
    from protocols.db import CACHE_DIR
    from protocols.rpc import SIGNAL_CLI_ATTACHMENTS_DIR

    configured_whatsapp_dir = get_whatsapp_media_dir()
    whatsapp_dir = (
        Path(configured_whatsapp_dir)
        if configured_whatsapp_dir
        else Path(CACHE_DIR) / "whatsapp-media"
    )
    return [
        MediaScope("signal", "signal", Path(SIGNAL_CLI_ATTACHMENTS_DIR)),
        MediaScope("whatsapp", "whatsapp", whatsapp_dir),
        MediaScope("telegram", "telegram", Path(CACHE_DIR) / "telegram-media"),
        MediaScope("signal", "quote-thumbs", Path(CACHE_DIR) / "quote-thumbs"),
    ]


def collect_refs(db_file: Path, protocols: Iterable[str]) -> dict[str, set[str]] | None:
    """Collect attachment references, or return None when the DB cannot be read."""
    requested = list(dict.fromkeys(protocols))
    if not requested:
        return {}
    try:
        if not db_file.exists():
            return None

        from protocols.db import _DB_LOCK

        placeholders = ", ".join("?" for _ in requested)
        query = f"""
            SELECT protocol, attachment_id, quote_attachment_id, quote_attachment_path
            FROM messages
            WHERE protocol IN ({placeholders})
              AND (attachment_id IS NOT NULL OR quote_attachment_id IS NOT NULL
                   OR quote_attachment_path IS NOT NULL)
        """
        with _DB_LOCK:
            try:
                conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
            except (OSError, sqlite3.Error):
                conn = sqlite3.connect(db_file)
            try:
                rows = conn.execute(query, requested).fetchall()
            finally:
                conn.close()
    except Exception:
        logger.debug(
            "Unable to collect media references from %s", db_file, exc_info=True
        )
        return None

    result = {protocol: set() for protocol in requested}
    for protocol, *values in rows:
        refs = result.setdefault(protocol, set())
        refs.update(value for value in values if value is not None)
    return result


def resolve_protected(
    scope: MediaScope, refs: Iterable[str], disk_names: Iterable[str]
) -> set[str]:
    """Resolve raw DB references to basenames protected in a media scope."""
    names = set(disk_names)
    protected: set[str] = set()
    for ref in refs:
        if scope.label == "telegram" and ref.startswith("tgref:"):
            try:
                prefix, chat_id, message_id = ref.rsplit(":", 2)
                if prefix != "tgref" or not chat_id or not message_id:
                    raise ValueError
            except ValueError:
                logger.debug("Ignoring malformed Telegram media reference: %r", ref)
                continue
            file_prefix = f"{chat_id}-{message_id}-"
            protected.update(name for name in names if name.startswith(file_prefix))
            continue

        basename = Path(ref).name
        if scope.label == "whatsapp":
            protected.add(basename or ref)
        else:
            protected.add(basename)
    return protected


def _compute_orphans_with_grace_count(
    scope: MediaScope,
    refs: Iterable[str],
    disk_files: Iterable[Path],
    *,
    now_s: float,
    grace_s: float,
) -> tuple[list[OrphanFile], int, int, int]:
    disk: set[Path] = set()
    errors = 0
    for path in disk_files:
        try:
            if path.is_file():
                disk.add(path)
        except Exception:
            errors += 1
            logger.debug("Unable to classify media file %s", path, exc_info=True)
    protected = resolve_protected(scope, refs, {path.name for path in disk})
    orphans: list[OrphanFile] = []
    skipped_grace = 0
    for path in disk:
        if path.name in protected:
            continue
        try:
            stat = path.stat()
        except Exception:
            errors += 1
            logger.debug("Unable to stat media file %s", path, exc_info=True)
            continue
        if now_s - max(stat.st_mtime, stat.st_ctime) < grace_s:
            skipped_grace += 1
            continue
        orphans.append(OrphanFile(path, stat.st_size, "unreferenced"))
    return orphans, skipped_grace, errors, len(disk)


def compute_orphans(
    scope: MediaScope,
    refs: Iterable[str],
    disk_files: Iterable[Path],
    *,
    now_s: float,
    grace_s: float,
) -> list[OrphanFile]:
    """Compute unreferenced files old enough to be safely removed."""
    return _compute_orphans_with_grace_count(
        scope, refs, disk_files, now_s=now_s, grace_s=grace_s
    )[0]


def prune_media(
    *,
    db_file: Path | None = None,
    protocols: Iterable[str] | None = None,
    scopes: list[MediaScope] | None = None,
    grace_s: float = DEFAULT_GRACE_S,
    dry_run: bool = False,
) -> MediaPruneReport:
    """Scan configured media scopes and optionally remove orphan files."""
    report = MediaPruneReport()
    try:
        if db_file is None:
            from protocols.db import DB_FILE

            db_file = DB_FILE
        selected_scopes = default_scopes() if scopes is None else list(scopes)
        if protocols is not None:
            selected_protocols = set(protocols)
            selected_scopes = [
                scope
                for scope in selected_scopes
                if (scope.label == "quote-thumbs" and "signal" in selected_protocols)
                or (
                    scope.label != "quote-thumbs"
                    and scope.protocol in selected_protocols
                )
            ]
    except Exception:
        logger.debug("Unable to initialize media prune", exc_info=True)
        return report

    for scope in selected_scopes:
        scope_report = ScopeReport(scope=scope.label, dir=scope.dir)
        report.scopes.append(scope_report)
        try:
            if not scope.dir.exists():
                scope_report.skipped_dir_missing = True
                continue

            refs_by_protocol = collect_refs(db_file, [scope.protocol])
            if refs_by_protocol is None:
                scope_report.skipped_db_error = True
                scope_report.errors += 1
                continue
            refs = refs_by_protocol.get(scope.protocol, set())
            disk_files = list(scope.dir.iterdir())
            scope_report.refs_collected = len(refs)
            orphans, skipped_grace, file_errors, files_scanned = (
                _compute_orphans_with_grace_count(
                    scope,
                    refs,
                    disk_files,
                    now_s=time.time(),
                    grace_s=grace_s,
                )
            )
            scope_report.files_scanned = files_scanned
            scope_report.errors += file_errors
            scope_report.orphans_found = len(orphans)
            scope_report.skipped_grace = skipped_grace
            if dry_run:
                scope_report.orphans_dryrun = orphans
                continue

            protected = resolve_protected(
                scope, refs, {path.name for path in disk_files}
            )
            for orphan in orphans:
                if orphan.path.name in protected:
                    continue
                try:
                    orphan.path.unlink(missing_ok=True)
                except OSError:
                    scope_report.errors += 1
                    continue
                scope_report.orphans_deleted += 1
                scope_report.bytes_freed += orphan.size
        except Exception:
            scope_report.errors += 1
            logger.debug("Media prune failed for scope %s", scope.label, exc_info=True)

    return report
