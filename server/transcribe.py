"""The cloud transcription service as core state.

The service (one worker thread plus the on-disk store) belongs to the core,
not to an adapter: ``ChatCore.start`` creates it from the configured OpenAI
key, ``ChatCore.shutdown`` stops it, and the ``transcribe.*`` /
``config.set_openai_key`` operations reach it through ``ChatAPI.transcription``.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)


class TranscriptionHost:
    """Holds the (optional) :class:`~transcription.service.TranscriptionService`.

    ``service`` is ``None`` while no OpenAI key is configured.  :meth:`init` is
    idempotent in the sense that it always stops the previous service before it
    builds the next one (a changed key replaces the client).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.service: Any = None

    def init(self) -> Any:
        """(Re)build the service from the current configuration."""
        from transcription import store
        from transcription.client import CloudTranscriptionClient
        from transcription.config import (
            get_openai_api_key,
            get_transcription_base_url,
            get_transcription_language,
            get_transcription_model,
            get_transcription_timeout,
        )
        from transcription.service import TranscriptionService

        with self._lock:
            current = self.service
            if current is not None:
                current.stop()
            self.service = None

            api_key = get_openai_api_key()
            if not api_key:
                logger.info("Trascrizione cloud OpenAI disattivata")
                return None

            client = CloudTranscriptionClient(
                api_key,
                base_url=get_transcription_base_url(),
                model=get_transcription_model(),
                language=get_transcription_language(),
                timeout=get_transcription_timeout(),
            )
            self.service = TranscriptionService(client, store)
            logger.info("Trascrizione cloud OpenAI attiva")
            return self.service

    def stop(self) -> None:
        """Stop the worker (``ChatCore.shutdown``).  Safe to call repeatedly."""
        with self._lock:
            service, self.service = self.service, None
        if service is not None:
            service.stop()
