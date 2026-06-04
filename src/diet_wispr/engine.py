"""Transcription engines behind one streaming interface, for A/B comparison.

- BatchEngine: accumulate audio, then transcribe + (optional) cleanup on finish.
  This is the original pipeline, fed incrementally.
- RealtimeEngine: stream audio to OpenAI's Realtime transcription WebSocket while
  you speak, so the transcript is ready almost immediately on release.

Both implement start()/feed()/finish(), so the app feeds the same live audio
chunks to whichever engine is active and can switch between them at runtime.

Threading: feed() is called from the app's pump thread (NOT the sounddevice
audio callback), so it may block briefly on the network. The realtime recv loop
runs on its own thread; the SDK serialises sends, so concurrent send (pump) and
recv (bg) is safe.
"""

from __future__ import annotations

import base64
import io
import threading

import numpy as np
import soundfile as sf
from openai import OpenAI

from .config import Settings
from .transcribe import Transcriber


class BatchEngine:
    name = "batch"

    def __init__(self, settings: Settings, transcriber: Transcriber) -> None:
        self._settings = settings
        self._transcriber = transcriber
        self._buf = bytearray()

    def prewarm(self) -> None:
        pass

    def start(self) -> None:
        self._buf = bytearray()

    def feed(self, pcm16: bytes) -> None:
        self._buf += pcm16

    def finish(self) -> str:
        if len(self._buf) < 2:
            return ""
        data = np.frombuffer(bytes(self._buf), dtype=np.int16)
        wav = io.BytesIO()
        sf.write(wav, data, self._settings.sample_rate, format="WAV", subtype="PCM_16")
        wav.seek(0)  # rewind so the upload isn't empty
        wav.name = "audio.wav"
        text = self._transcriber.transcribe(wav)
        if self._settings.cleanup_enabled and text:
            text = self._transcriber.clean(text)
        return text

    def close(self) -> None:
        pass


class RealtimeEngine:
    name = "realtime"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = OpenAI(api_key=settings.openai_key)
        self._conn = None
        self._conn_thread: threading.Thread | None = None
        self._connected = threading.Event()
        self._completed = threading.Event()
        self._lock = threading.Lock()
        self._parts: list[str] = []
        self._final = ""
        self._error: object = None
        self._closing = False

    # --- connection lifecycle ---

    def _ensure_connected(self) -> None:
        if self._connected.is_set():
            if self._error:
                raise RuntimeError(f"realtime connection error: {self._error}")
            return
        self._conn_thread = threading.Thread(target=self._run_conn, daemon=True)
        self._conn_thread.start()
        if not self._connected.wait(timeout=15):
            raise RuntimeError("realtime: timed out opening connection")
        if self._error:
            raise RuntimeError(f"realtime connection error: {self._error}")

    def prewarm(self) -> None:
        """Open the connection ahead of time so the first dictation is snappy."""
        try:
            self._ensure_connected()
        except Exception as exc:
            print(f"[diet-wispr] realtime prewarm failed: {exc}")

    def _run_conn(self) -> None:
        try:
            # NOTE: do NOT pass model= here. For a transcription session the URL
            # model param expects a realtime *conversation* model; the transcription
            # model goes in session.update -> transcription.model below. Passing a
            # transcribe model to connect is rejected with invalid_model.
            with self._client.realtime.connect(
                extra_query={"intent": "transcription"},
            ) as conn:
                self._conn = conn
                conn.send(
                    {
                        "type": "session.update",
                        "session": {
                            "type": "transcription",
                            "audio": {
                                "input": {
                                    "format": {"type": "audio/pcm", "rate": 24000},
                                    "transcription": {"model": self._settings.stt_model},
                                    "turn_detection": None,  # we commit manually
                                }
                            },
                        },
                    }
                )
                self._connected.set()
                while not self._closing:
                    self._handle(conn.recv())
        except Exception as exc:  # connection dropped/closed or setup failed
            self._error = exc
            self._connected.set()  # unblock any waiter
            self._completed.set()

    def _handle(self, event) -> None:
        t = getattr(event, "type", "")
        if t == "conversation.item.input_audio_transcription.delta":
            if getattr(event, "delta", None):
                with self._lock:
                    self._parts.append(event.delta)
        elif t == "conversation.item.input_audio_transcription.completed":
            with self._lock:
                self._final = event.transcript or "".join(self._parts)
            self._completed.set()
        elif t == "conversation.item.input_audio_transcription.failed":
            self._error = getattr(event, "error", "transcription failed")
            self._completed.set()
        elif t == "error":
            self._error = getattr(event, "error", "realtime error")
            self._completed.set()

    # --- streaming interface ---

    def start(self) -> None:
        self._ensure_connected()
        with self._lock:
            self._parts = []
            self._final = ""
        self._error = None
        self._completed.clear()
        self._conn.send({"type": "input_audio_buffer.clear"})

    def feed(self, pcm16: bytes) -> None:
        if self._conn is None:
            return
        audio = base64.b64encode(pcm16).decode("ascii")
        self._conn.send({"type": "input_audio_buffer.append", "audio": audio})

    def finish(self) -> str:
        if self._conn is None:
            return ""
        self._conn.send({"type": "input_audio_buffer.commit"})
        got = self._completed.wait(timeout=15)
        if self._error:
            raise RuntimeError(f"realtime error: {self._error}")
        with self._lock:
            text = self._final if (got and self._final) else "".join(self._parts)
        return text.strip()

    def close(self) -> None:
        self._closing = True
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass


def make_engine(name: str, settings: Settings, transcriber: Transcriber):
    """Construct an engine by name. Batch reuses the shared Transcriber."""
    if name == "realtime":
        return RealtimeEngine(settings)
    return BatchEngine(settings, transcriber)
