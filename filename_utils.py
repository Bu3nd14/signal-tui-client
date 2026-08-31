from __future__ import annotations

import re
import unicodedata
from pathlib import Path


def sanitize_filename(filename: str | None, *, max_length: int = 255) -> str:
    normalized = unicodedata.normalize("NFKC", filename or "").replace("\\", "/")
    basename = normalized.rsplit("/", 1)[-1]
    sanitized = re.sub(r"[^\w .()\-]", "_", basename)
    sanitized = re.sub(r"\s+", " ", sanitized).strip(" .")
    if len(sanitized) <= max_length:
        return sanitized
    suffix = Path(sanitized).suffix
    return f"{sanitized[: max_length - len(suffix)].rstrip(' .')}{suffix}"
