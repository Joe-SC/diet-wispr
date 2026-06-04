"""Microphone capture as a live stream of PCM chunks.

The sounddevice callback runs on a PortAudio thread and must stay cheap: it only
copies each block onto a queue. A pump thread (in app.py) drains the queue via
read() and feeds the active engine, so audio can be streamed to the realtime API
while you speak. The active engine owns any encoding.
"""

from __future__ import annotations

import queue

import numpy as np
import sounddevice as sd


class Recorder:
    def __init__(
        self,
        sample_rate: int = 24000,
        device: str | int | None = None,
        channels: int = 1,
    ) -> None:
        self.requested_sample_rate = sample_rate
        self.sample_rate = sample_rate  # may be lowered to the device default
        self.device = device
        self.channels = channels
        self._queue: queue.Queue[np.ndarray] = queue.Queue()
        self._stream: sd.InputStream | None = None
        self._overflows = 0

    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ANN001
        # Exceptions raised here do NOT propagate to the main thread, so we never
        # rely on raising. We only count overflows and enqueue a copy.
        if status and status.input_overflow:
            self._overflows += 1
        self._queue.put(indata.copy())

    def _open_stream(self, sample_rate: int) -> sd.InputStream:
        return sd.InputStream(
            samplerate=sample_rate,
            device=self.device,
            channels=self.channels,
            dtype="int16",
            callback=self._callback,
        )

    def start(self) -> None:
        """Begin capturing. Falls back to the device default rate if needed."""
        while not self._queue.empty():  # drop stale frames from a prior session
            self._queue.get_nowait()
        self._overflows = 0
        try:
            self._stream = self._open_stream(self.requested_sample_rate)
            self.sample_rate = self.requested_sample_rate
        except Exception:
            info = sd.query_devices(self.device, "input")
            default_rate = int(info["default_samplerate"])
            self._stream = self._open_stream(default_rate)
            self.sample_rate = default_rate
        self._stream.start()

    def read(self, timeout: float = 0.1) -> np.ndarray | None:
        """Pop the next captured chunk (mono int16), or None if none arrived."""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def stop_stream(self) -> None:
        """Stop and close the input stream. Remaining chunks stay readable."""
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    @property
    def overflow_count(self) -> int:
        return self._overflows


def describe_device(device: str | int | None) -> str:
    """Human-readable description of the resolved input device, for startup logging."""
    try:
        info = sd.query_devices(device, "input")
        return f"{info['name']} (default rate {int(info['default_samplerate'])} Hz)"
    except Exception as exc:  # pragma: no cover - diagnostic only
        return f"<could not resolve input device {device!r}: {exc}>"
