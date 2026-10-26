"""Voice-note transcription operations and the OpenAI key setting.

The service is core state (:class:`server.transcribe.TranscriptionHost`,
``api.transcription``).  Messages are the Italian texts the SPA has always
shown.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from server.api import ApiError, ChatAPI, operation
from server.models_api import (
    EmptyRequest,
    OpenAIKeyRequest,
    StatusOut,
    TranscribeConfigOut,
    TranscribeStatusRequest,
    TranscribeSubmitRequest,
)
from server.ops.common import PROTOCOLS

logger = logging.getLogger(__name__)

_UNCONFIGURED = "Trascrizione non configurata"


def _service(api: ChatAPI) -> Any:
    service = api.transcription.service
    if service is None:
        raise ApiError("backend_down", _UNCONFIGURED)
    return service


@operation("transcribe.config", request=EmptyRequest, response=TranscribeConfigOut)
def transcribe_config(api: ChatAPI, req: EmptyRequest) -> TranscribeConfigOut:
    """Whether transcription is available (an OpenAI key is configured)."""
    return TranscribeConfigOut(enabled=api.transcription.service is not None)


@operation(
    "transcribe.status",
    request=TranscribeStatusRequest,
    response=dict[str, Any],
)
def transcribe_status(api: ChatAPI, req: TranscribeStatusRequest) -> dict[str, Any]:
    """Stored transcription record of an attachment, or ``{"status": "unknown"}``.

    ``backend_down`` when transcription is not configured.
    """
    current = _service(api).status(req.protocol, req.attachment_id)
    return current if current is not None else {"status": "unknown"}


@operation(
    "transcribe.submit", request=TranscribeSubmitRequest, response=dict[str, Any]
)
def transcribe_submit(api: ChatAPI, req: TranscribeSubmitRequest) -> dict[str, Any]:
    """Queue an attachment for transcription (or report its current state).

    The audio file is resolved through ``media.attachment``, so it is confined
    to the protocol's media root.  Sync: may trigger a lazy download.
    """
    proto = req.protocol
    attachment_id = req.attachment_id
    if (
        not isinstance(proto, str)
        or proto not in PROTOCOLS
        or not isinstance(attachment_id, str)
    ):
        raise ApiError("invalid_request", "Invalid request")
    attachment_id = attachment_id.strip()
    if not attachment_id:
        raise ApiError("invalid_request", "Invalid request")

    service = _service(api)
    current = service.status(proto, attachment_id)
    if current is not None and current["status"] == "ok":
        return {"status": "done", "text": current.get("text", "")}
    if current is not None and current["status"] == "pending":
        return {"status": "pending"}

    try:
        blob = api.call_json(
            "media.attachment", {"protocol": proto, "attachment_id": attachment_id}
        )
    except ApiError:
        raise ApiError("not_found", "Allegato non trovato") from None
    service.submit(proto, attachment_id, Path(blob["path"]))
    return {"status": "pending"}


@operation("config.set_openai_key", request=OpenAIKeyRequest, response=StatusOut)
def config_set_openai_key(api: ChatAPI, req: OpenAIKeyRequest) -> StatusOut:
    """Save the OpenAI key to ``config.json`` and (re)start transcription."""
    api_key = req.api_key
    if not isinstance(api_key, str):
        raise ApiError("invalid_request", "Chiave non valida")
    api_key = api_key.strip()
    if not api_key or not api_key.startswith("sk-"):
        raise ApiError("invalid_request", "Chiave non valida")

    from protocols.config import PROJECT_DIR, _load_config

    config_path = PROJECT_DIR / "config.json"
    data = _load_config()
    data["openai_api_key"] = api_key
    try:
        with config_path.open("w", encoding="utf-8") as config_file:
            json.dump(data, config_file, indent=2)
            config_file.write("\n")
    except OSError:
        logger.exception("Impossibile salvare la configurazione OpenAI")
        raise ApiError("internal", "Errore di salvataggio") from None

    try:
        api.transcription.init()
    except Exception:
        logger.exception("Impossibile inizializzare la trascrizione OpenAI")
        raise ApiError("internal", "Errore di inizializzazione") from None
    return StatusOut(status="ok")
