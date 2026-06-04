"""OpenAI speech-to-text and optional LLM cleanup, with retry."""

from __future__ import annotations

import io

from openai import (
    APIConnectionError,
    APIError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import Settings

_RETRYABLE = (RateLimitError, APIError, InternalServerError, APIConnectionError)

_CLEANUP_SYSTEM_PROMPT = (
    "You clean up dictated speech-to-text output. Remove filler words (um, uh, "
    "like, you know), fix punctuation and capitalization, and tidy obvious "
    "transcription artifacts. Do NOT paraphrase, summarize, translate, or add "
    "content: keep the speaker's wording and meaning. Return only the cleaned "
    "text, with no preamble or quotation marks."
)


class Transcriber:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # Mirrors the workspace pattern: explicit key, not the SDK default env var.
        self._client = OpenAI(api_key=settings.openai_key)

    @retry(
        retry=retry_if_exception_type(_RETRYABLE),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        reraise=True,
    )
    def transcribe(self, wav_buf: io.BytesIO) -> str:
        """Transcribe a WAV buffer. Uses the default JSON response and reads .text."""
        wav_buf.seek(0)  # belt-and-suspenders: ensure we send from the start
        result = self._client.audio.transcriptions.create(
            model=self._settings.stt_model,
            file=("audio.wav", wav_buf),
            # Leave response_format unset (JSON): passing "text" returns a bare
            # str and breaks the .text accessor; gpt-4o-transcribe models also
            # do not support verbose_json / timestamps.
        )
        return (result.text or "").strip()

    @retry(
        retry=retry_if_exception_type(_RETRYABLE),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        reraise=True,
    )
    def clean(self, text: str) -> str:
        """Light LLM cleanup pass. Returns input unchanged on empty text."""
        if not text:
            return text
        resp = self._client.chat.completions.create(
            model=self._settings.cleanup_model,
            messages=[
                {"role": "system", "content": _CLEANUP_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            temperature=0,
        )
        cleaned = (resp.choices[0].message.content or "").strip()
        return cleaned or text

    def process(self, wav_buf: io.BytesIO) -> str:
        """Full path: transcribe, then clean if enabled."""
        text = self.transcribe(wav_buf)
        if self._settings.cleanup_enabled and text:
            text = self.clean(text)
        return text
