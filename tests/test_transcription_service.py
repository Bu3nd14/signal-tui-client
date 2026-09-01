from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

import protocols.db as backend
from transcription import store
from transcription.service import TranscriptionService


class FakeClient:
    model = "gpt-transcribe"

    def __init__(self, result="testo", error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls = 0

    def transcribe(self, audio_path):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def isolated_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(backend, "DB_FILE", tmp_path / "protocols.db")
    monkeypatch.setattr(backend, "CACHE_DIR", tmp_path)
    return store


def _wait_for(service, protocol: str, attachment_id: str, status: str):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        current = service.status(protocol, attachment_id)
        if current is not None and current["status"] == status:
            return current
        time.sleep(0.01)
    pytest.fail(f"status {status!r} non raggiunto entro 5 secondi")


def test_submit_worker_persists_success(isolated_store, tmp_path: Path):
    client = FakeClient(result="trascrizione")
    service = TranscriptionService(client, isolated_store)
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"audio")

    service.submit("signal", "audio-1", audio)
    result = _wait_for(service, "signal", "audio-1", "ok")

    assert result["text"] == "trascrizione"
    assert result["model"] == "gpt-transcribe"
    assert result["error"] is None
    assert client.calls == 1


def test_submit_worker_persists_failure(isolated_store, tmp_path: Path):
    client = FakeClient(error=RuntimeError("backend unavailable"))
    service = TranscriptionService(client, isolated_store)

    service.submit("whatsapp", "audio-2", tmp_path / "voice.ogg")
    result = _wait_for(service, "whatsapp", "audio-2", "failed")

    assert result["error"] == "backend unavailable"
    assert result["text"] is None
    assert client.calls == 1


def test_duplicate_pending_submission_calls_client_once(isolated_store, tmp_path: Path):
    started = threading.Event()
    release = threading.Event()

    class BlockingClient(FakeClient):
        def transcribe(self, audio_path):
            self.calls += 1
            started.set()
            assert release.wait(timeout=5)
            return self.result

    client = BlockingClient(result="una volta")
    service = TranscriptionService(client, isolated_store)
    audio = tmp_path / "voice.ogg"

    service.submit("telegram", "audio-3", audio)
    assert started.wait(timeout=5)
    service.submit("telegram", "audio-3", audio)
    release.set()
    result = _wait_for(service, "telegram", "audio-3", "ok")

    assert result["text"] == "una volta"
    assert client.calls == 1
