"""Wire the pieces together and run the dictation loop.

Audio flows: sounddevice callback -> recorder queue -> pump thread -> active
engine (batch or realtime). The pump runs off the listener thread so hotkey
callbacks stay cheap, and it streams chunks live (which the realtime engine needs).
"""

from __future__ import annotations

import sys
import threading
import time

from .config import Settings, load_settings, save_indicator_position
from .engine import make_engine
from .hotkey import HotkeyListener
from .indicator import IDLE, RECORDING, TRANSCRIBING, Indicator
from .output import paste_text
from .recorder import Recorder, describe_device
from .transcribe import Transcriber

try:
    import winsound  # Windows-only
except ImportError:  # pragma: no cover - non-Windows fallback
    winsound = None


def _beep(freq: int, ms: int, enabled: bool) -> None:
    """Fire-and-forget beep on a daemon thread so we never block a callback."""
    if not (enabled and winsound is not None):
        return
    threading.Thread(target=winsound.Beep, args=(freq, ms), daemon=True).start()


class App:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._recorder = Recorder(
            sample_rate=settings.sample_rate, device=settings.mic_device
        )
        self._transcriber = Transcriber(settings)
        self._engines: dict = {}
        self._engine = self._engine_for(settings.engine)
        self._lock = threading.Lock()
        self._recording = False
        self._busy = False
        self._indicator: Indicator | None = None
        self._listener: HotkeyListener | None = None
        self._pump: threading.Thread | None = None
        self._fed_samples = 0
        self._t_start = 0.0
        self._t_release = 0.0

    def _engine_for(self, name: str):
        if name not in self._engines:
            self._engines[name] = make_engine(name, self._settings, self._transcriber)
        return self._engines[name]

    def _set_indicator(self, state: str) -> None:
        if self._indicator is not None:
            self._indicator.set_state(state)

    # --- recording lifecycle (state transitions guarded by _lock) ---

    def _start_recording(self) -> None:
        try:
            self._recorder.start()  # begin capturing immediately
        except Exception as exc:
            print(f"[diet-wispr] could not start recording: {exc}", file=sys.stderr)
            return
        self._recording = True
        self._fed_samples = 0
        self._t_start = time.time()
        if self._indicator is not None:
            self._indicator.set_started(self._t_start)
        self._set_indicator(RECORDING)
        _beep(880, 120, self._settings.beep)
        print(f"[diet-wispr] recording... ({self._engine.name})")
        self._pump = threading.Thread(target=self._run_pump, daemon=True)
        self._pump.start()

    def _stop_recording(self) -> None:
        self._recording = False
        self._busy = True
        self._t_release = time.time()
        _beep(660, 120, self._settings.beep)
        self._set_indicator(TRANSCRIBING)

    def _run_pump(self) -> None:
        """Feed live audio to the active engine, then finalize + paste."""
        engine = self._engine
        try:
            engine.start()  # connects the realtime socket if needed (off-listener)
            while self._recording:
                chunk = self._recorder.read(timeout=0.1)
                if chunk is not None:
                    engine.feed(chunk.tobytes())
                    self._fed_samples += len(chunk)
            # recording stopped: stop the stream and drain whatever's left
            self._recorder.stop_stream()
            while (chunk := self._recorder.read(timeout=0.0)) is not None:
                engine.feed(chunk.tobytes())
                self._fed_samples += len(chunk)
            self._finalize(engine)
        except Exception as exc:
            print(f"[diet-wispr] error: {exc}", file=sys.stderr)
        finally:
            self._recorder.stop_stream()
            self._set_indicator(IDLE)
            with self._lock:
                self._busy = False
                self._recording = False

    def _finalize(self, engine) -> None:
        rate = self._recorder.sample_rate
        rec_dur = max(0.0, self._t_release - self._t_start)
        if self._fed_samples / float(rate) < self._settings.min_seconds:
            print("[diet-wispr] (nothing captured / too short)")
            return
        text = engine.finish()
        latency = time.time() - self._t_release
        if not text:
            print(f"[diet-wispr] {engine.name} | rec {rec_dur:.1f}s | (empty transcript)")
            return
        paste_text(
            text,
            restore_clipboard=self._settings.restore_clipboard,
            paste_delay=self._settings.paste_delay,
        )
        preview = text if len(text) <= 70 else text[:67] + "..."
        print(
            f"[diet-wispr] {engine.name} | rec {rec_dur:.1f}s | "
            f"latency {latency:.2f}s | {preview}"
        )

    # --- hotkey edges ---

    def on_activate(self) -> None:
        with self._lock:
            if self._busy:
                return
            if self._settings.mode == "toggle":
                if self._recording:
                    self._stop_recording()
                else:
                    self._start_recording()
            elif not self._recording:  # ptt: press starts
                self._start_recording()

    def on_deactivate(self) -> None:
        if self._settings.mode != "ptt":
            return
        with self._lock:
            if self._recording:
                self._stop_recording()

    def _toggle_engine(self) -> None:
        with self._lock:
            if self._busy or self._recording:
                return  # don't switch mid-dictation
            new = "realtime" if self._engine.name == "batch" else "batch"
            self._engine = self._engine_for(new)
        if self._indicator is not None:
            self._indicator.set_engine(self._engine.name)
        print(f"[diet-wispr] engine -> {self._engine.name}")
        threading.Thread(target=self._engine.prewarm, daemon=True).start()

    def _on_indicator_moved(self, x: int, y: int) -> None:
        """Persist a new dot position (from a drag) so it survives a restart."""
        save_indicator_position(x, y)
        print(f"[diet-wispr] indicator moved -> {x},{y} (saved)")

    # --- run ---

    def _quit(self) -> None:
        if self._listener is not None:
            self._listener.stop()
        for engine in self._engines.values():
            try:
                engine.close()
            except Exception:
                pass

    def run(self) -> None:
        self._listener = HotkeyListener(
            self._settings.combo,
            self.on_activate,
            self.on_deactivate,
            log_keys=self._settings.log_keys,
        )
        mode_help = (
            "tap to start, tap to stop"
            if self._settings.mode == "toggle"
            else "hold to talk, release to transcribe"
        )
        use_indicator = self._settings.indicator_enabled
        quit_help = "click the dot" if use_indicator else "Ctrl+C"
        toggle_help = " | right-click the dot to switch engine" if use_indicator else ""
        print("=" * 64)
        print("  diet-wispr  -  personal dictation")
        print(f"  hotkey : {self._settings.combo}  ({self._settings.mode}: {mode_help})")
        print(f"  mic    : {describe_device(self._settings.mic_device)}")
        print(f"  engine : {self._engine.name}  (batch=transcribe+cleanup, realtime=streaming){toggle_help}")
        print(f"  quit   : {quit_help}")
        print("=" * 64)

        # Prewarm realtime so the first dictation isn't slowed by the connect.
        threading.Thread(target=self._engine.prewarm, daemon=True).start()

        if use_indicator:
            self._indicator = Indicator(
                on_quit=self._quit,
                on_toggle_engine=self._toggle_engine,
                on_moved=self._on_indicator_moved,
                engine=self._engine.name,
                position=self._settings.indicator_position,
            )
            self._listener.start()
            try:
                self._indicator.mainloop()
            except KeyboardInterrupt:
                self._quit()
            finally:
                print("\n[diet-wispr] bye")
        else:
            self._listener.start()
            try:
                while self._listener.is_alive():
                    time.sleep(0.2)
            except KeyboardInterrupt:
                pass
            finally:
                print("\n[diet-wispr] bye")
                self._quit()


def main() -> None:
    try:
        settings = load_settings()
    except (ValueError, FileNotFoundError) as exc:
        print(f"[diet-wispr] config error: {exc}", file=sys.stderr)
        sys.exit(1)
    App(settings).run()


if __name__ == "__main__":
    main()
