"""Download mode: serve an attachment or a text through the temporary HTTP server.

The server (port 10042, one file at a time, see :mod:`protocols.download`)
belongs to the core: it serves files the core owns.  A frontend asks for a URL
and shows it; nothing about the files crosses the API.  Handlers are sync and
may block (a lazy attachment download, writing or copying the served file).
"""

from __future__ import annotations

from pathlib import Path

from server import blobs
from server.api import ApiError, ChatAPI, operation
from server.models_api import (
    DownloadOut,
    DownloadServeAttachmentRequest,
    DownloadServeTextRequest,
)
from server.ops.common import PROTOCOLS


def _checked(url: str) -> DownloadOut:
    # The helpers report failures as ``ERROR: ...`` strings (the text the
    # TUI has always shown after its "❌").
    if url.startswith("ERROR:"):
        raise ApiError("backend_error", url)
    return DownloadOut(url=url)


@operation(
    "download.serve_attachment",
    request=DownloadServeAttachmentRequest,
    response=DownloadOut,
)
def download_serve_attachment(
    api: ChatAPI, req: DownloadServeAttachmentRequest
) -> DownloadOut:
    """Serve an attachment file; ``not_found`` when it cannot be resolved."""
    from protocols import download

    missing = ApiError(
        "not_found", f"ERROR: Attachment file not found (id={req.attachment_id[:80]})"
    )
    if req.protocol not in PROTOCOLS:
        raise missing
    try:
        blob = blobs.attachment_blob(api.manager, req.protocol, req.attachment_id)
    except ApiError as exc:
        if exc.code == "not_found":
            raise missing from exc
        raise
    path = Path(blob.path)
    if not path.is_file():
        raise missing
    try:
        return _checked(download.serve_file_path(path))
    except OSError as exc:
        raise ApiError("backend_error", f"ERROR: Cannot serve file: {exc}") from exc


@operation(
    "download.serve_text", request=DownloadServeTextRequest, response=DownloadOut
)
def download_serve_text(api: ChatAPI, req: DownloadServeTextRequest) -> DownloadOut:
    """Serve *text* as a file named *filename* (a plain name, no directories)."""
    from protocols import download

    name = req.filename
    if not name or name in {".", ".."} or Path(name).name != name or "\0" in name:
        raise ApiError("invalid_request", "Invalid request")
    try:
        return _checked(download.serve_text_as_file(req.text, filename=name))
    except OSError as exc:
        raise ApiError("backend_error", f"ERROR: Cannot serve file: {exc}") from exc
