"""Shared helpers for protocol media (avatar size limit + content-type sniffing)."""

from __future__ import annotations

MAX_AVATAR_BYTES = 5 * 1024 * 1024


def sniff_image_content_type(data: bytes) -> str:
    """Best-effort image type from a small magic-byte prefix.

    Falls back to ``image/jpeg`` (signal-cli/Telegram store avatars as JPEG).
    """
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"
