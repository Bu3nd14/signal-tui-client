from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from pathlib import Path

import aiohttp

_MAX_AUDIO_SIZE = 25 * 1024 * 1024

# Formati accettati dall'API OpenAI (i vocali Signal sono .oga/opus → transcodifica).
_SUPPORTED_EXTENSIONS = {".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".wav", ".webm"}


class TranscriptionError(Exception):
    pass


def _transcode_to_mp3(path: Path) -> Path:
    """Transcodifica l'audio in mp3 con ffmpeg (formati non supportati da OpenAI)."""
    if not _ffmpeg_available():
        raise TranscriptionError(
            "Formato audio non supportato da OpenAI e ffmpeg non disponibile "
            "per la transcodifica"
        )
    fd, tmp_path = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", str(path),
                "-ac", "1", "-b:a", "64k",
                tmp_path,
            ],
            capture_output=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise TranscriptionError(
                "Transcodifica ffmpeg fallita: "
                f"{result.stderr.decode(errors='replace')[:200]}"
            )
        return Path(tmp_path)
    except subprocess.TimeoutExpired as exc:
        raise TranscriptionError("Transcodifica ffmpeg in timeout") from exc
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _ffmpeg_available() -> bool:
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, timeout=10
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class CloudTranscriptionClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-transcribe",
        language: str | None = None,
        timeout: int = 120,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.language = language
        self.timeout = timeout

    async def transcribe_async(self, audio_path: str | os.PathLike[str]) -> str:
        path = Path(audio_path)
        if os.path.getsize(path) > _MAX_AUDIO_SIZE:
            raise TranscriptionError("File audio troppo grande (max 25MB)")

        upload_path = path
        cleanup = False
        if path.suffix.lower() not in _SUPPORTED_EXTENSIONS:
            upload_path = _transcode_to_mp3(path)
            cleanup = True

        try:
            try:
                return await self._upload(upload_path)
            except TranscriptionError as exc:
                # L'API può rifiutare formati non documentati (es. .aac): se non
                # abbiamo ancora transcodificato, riprova con ffmpeg.
                if cleanup or "Unsupported file format" not in str(exc):
                    raise
                upload_path = _transcode_to_mp3(path)
                cleanup = True
                return await self._upload(upload_path)
        finally:
            if cleanup:
                upload_path.unlink(missing_ok=True)

    async def _upload(self, upload_path: Path) -> str:
        form = aiohttp.FormData()
        form.add_field("model", self.model)
        if self.language:
            field = "language" if self.model == "whisper-1" else "languages"
            form.add_field(field, self.language)

        timeout = aiohttp.ClientTimeout(total=self.timeout)
        headers = {"Authorization": f"Bearer {self.api_key}"}
        with upload_path.open("rb") as audio_file:  # noqa: ASYNC230
            form.add_field("file", audio_file, filename=upload_path.name)
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.post(
                    f"{self.base_url}/audio/transcriptions",
                    data=form,
                    headers=headers,
                ) as response,
            ):
                if response.status != 200:
                    body = (await response.text())[:200]
                    raise TranscriptionError(
                        f"Errore trascrizione HTTP {response.status}: {body}"
                    )
                data = await response.json()
        return data.get("text", "").strip()

    def transcribe(self, audio_path: str | os.PathLike[str]) -> str:
        return asyncio.run(self.transcribe_async(audio_path))
