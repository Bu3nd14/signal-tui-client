from __future__ import annotations

from protocols.config import _get, _get_int, _load_dotenv


def get_openai_api_key() -> str | None:
    value = _get("openai_api_key", "OPENAI_API_KEY", "").strip()
    if not value:
        # Fallback su .env (come get_whatsapp_api_key): _get non legge .env.
        value = _load_dotenv().get("OPENAI_API_KEY", "").strip()
    return value or None


def get_transcription_model() -> str:
    value = _get("transcription_model", "TRANSCRIPTION_MODEL", "gpt-transcribe").strip()
    return value or "gpt-transcribe"


def get_transcription_language() -> str | None:
    value = _get("transcription_language", "TRANSCRIPTION_LANGUAGE", "").strip()
    return value or None


def get_transcription_base_url() -> str:
    value = _get(
        "transcription_base_url",
        "TRANSCRIPTION_BASE_URL",
        "https://api.openai.com/v1",
    ).strip()
    return (value or "https://api.openai.com/v1").rstrip("/")


def get_transcription_timeout() -> int:
    timeout = _get_int("transcription_timeout", "TRANSCRIPTION_TIMEOUT", 120)
    return timeout if timeout > 0 else 120


def get_transcription_enabled() -> bool:
    return get_openai_api_key() is not None
