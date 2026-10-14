"""Speech-to-text client for LaneHub.

One multipart POST to `HUB_STT_URL` with the audio as `file`; the JSON reply
must carry `text`. That is the shape of OpenAI's /v1/audio/transcriptions and
of most self-hosted Whisper servers, so either works: the key goes as
`Authorization: Bearer`, `model` and `language` are sent only when set.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import settings

LOG = logging.getLogger("lanehub.stt")


class SttError(Exception):
    """Raised when STT fails or returns an unexpected response."""
    pass


def _redact_key(text: str) -> str:
    if settings.stt_key:
        return text.replace(settings.stt_key, "<key>")
    return text


async def transcribe(data: bytes, filename: str, mime: str | None) -> dict[str, Any]:
    """Transcribe audio bytes using the configured STT service.

    POSTs multipart form with field `file` and optional `language` to settings.stt_url.
    Returns parsed JSON containing at least `text`.
    Raises SttError on non-2xx response, invalid JSON, missing text, or network failure.
    """
    if not settings.stt_url:
        raise SttError("STT service is not configured (HUB_STT_URL is empty)")

    headers = {}
    if settings.stt_key:
        headers["Authorization"] = f"Bearer {settings.stt_key}"

    files = {"file": (filename or "audio.ogg", data, mime or "audio/ogg")}
    form_data: dict[str, str] = {}
    if settings.stt_model:
        form_data["model"] = settings.stt_model
    if settings.stt_language:
        form_data["language"] = settings.stt_language

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                settings.stt_url,
                files=files,
                data=form_data if form_data else None,
                headers=headers,
                timeout=settings.stt_timeout,
            )
    except Exception as exc:
        raise SttError(f"STT request failed: {_redact_key(str(exc))}") from exc

    if not resp.is_success:
        raise SttError(f"STT returned HTTP {resp.status_code}: {_redact_key(resp.text[:200])}")

    try:
        res = resp.json()
    except Exception as exc:
        raise SttError("STT returned invalid JSON") from exc

    if not isinstance(res, dict) or "text" not in res or not isinstance(res["text"], str):
        raise SttError("STT response missing 'text' string")

    return res
