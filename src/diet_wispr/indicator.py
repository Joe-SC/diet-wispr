"""A small always-on-top recording indicator built on tkinter (stdlib).

Design notes:
- The window is created once and stays mapped for the whole session; it only
  recolors. We never withdraw/re-show it, so it never steals foreground focus
  from the user's target app (which would break the Ctrl+V paste).
- set_state()/set_started() are called from the listener and worker threads and
  only assign attributes. All actual Tk widget mutation happens in _tick(), which
  runs on the Tk (main) thread via root.after. Never touch widgets off-thread.
- Tk's mainloop swallows Ctrl+C, so clicking the dot is the quit path.
"""

from __future__ import annotations

import time
import tkinter as tk
from collections.abc import Callable

IDLE = "idle"
RECORDING = "recording"
TRANSCRIBING = "transcribing"

# state -> (dot fill, text fill, label)
_STYLE = {
    IDLE: ("#444444", "#888888", "idle"),
    RECORDING: ("#e23b3b", "#ff7676", "REC"),
    TRANSCRIBING: ("#e0a000", "#ffcf5c", "working"),
}

_W, _H = 150, 46
_BG = "#1e1e1e"
_MARGIN = 24
_BOTTOM_GAP = 72  # clear the Windows taskbar when anchored to the bottom


def _clamp(x: int, y: int, sw: int, sh: int) -> tuple[int, int]:
    """Keep the window fully on-screen."""
    return max(0, min(x, sw - _W)), max(0, min(y, sh - _H))


def _place(position: str, sw: int, sh: int) -> tuple[int, int]:
    """Top-left (x, y) for a position: a named anchor or literal 'x,y' pixels."""
    pos = position.strip().lower().replace("_", "-").replace("centre", "center")
    # Literal coordinates, e.g. "885,962" (how a dragged position is remembered).
    if "," in pos:
        xs, _, ys = pos.partition(",")
        try:
            return _clamp(int(xs), int(ys), sw, sh)
        except ValueError:
            pass  # malformed -> fall through to the named-anchor logic
    if pos in ("center", "middle"):
        return (sw - _W) // 2, (sh - _H) // 2
    vert, _, horiz = pos.partition("-")
    if vert == "top":
        y = _MARGIN
    elif vert == "center":
        y = (sh - _H) // 2
    else:  # bottom (default)
        y = sh - _H - _BOTTOM_GAP
    if horiz == "left":
        x = _MARGIN
    elif horiz == "right":
        x = sw - _W - _MARGIN
    else:  # center (default)
        x = (sw - _W) // 2
    return x, y


_ENGINE_TAG = {"batch": "b", "realtime": "rt"}


class Indicator:
    def __init__(
        self,
        on_quit: Callable[[], None] | None = None,
        on_toggle_engine: Callable[[], None] | None = None,
        on_moved: Callable[[int, int], None] | None = None,
        engine: str = "",
        position: str = "bottom-center",
    ) -> None:
        self._on_quit = on_quit
        self._on_toggle_engine = on_toggle_engine
        self._on_moved = on_moved
        self._state = IDLE
        self._started = 0.0
        self._engine = engine
        # drag state (set on press, read on motion/release)
        self._drag_moved = False
        self._press_x = 0
        self._press_y = 0
        self._win_x = 0
        self._win_y = 0

        self.root = tk.Tk()
        self.root.overrideredirect(True)  # borderless; off the taskbar/alt-tab
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-alpha", 0.9)
        except tk.TclError:
            pass

        x, y = _place(
            position, self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        )
        self.root.geometry(f"{_W}x{_H}+{x}+{y}")
        self.root.configure(bg=_BG)

        self.canvas = tk.Canvas(
            self.root, width=_W, height=_H, bg=_BG, highlightthickness=0
        )
        self.canvas.pack(fill="both", expand=True)
        cy = _H // 2
        self._dot = self.canvas.create_oval(
            14, cy - 7, 28, cy + 7, fill=_STYLE[IDLE][0], outline=""
        )
        self._text = self.canvas.create_text(
            40, cy, anchor="w", fill=_STYLE[IDLE][1], text=_STYLE[IDLE][2],
            font=("Segoe UI", 11),
        )
        # Left button: click (no movement) quits; press-and-drag repositions the
        # window. We can't bind quit to <Button-1> directly because a drag also
        # starts with a press, so the click-vs-drag decision is deferred to release.
        # Right-click toggles the engine (for A/B comparison).
        # Bind only the canvas (it fills the window). Binding the root toplevel
        # too would double-fire, because the canvas's bindtags include the root,
        # so each click would toggle twice and land back on the original engine.
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Button-3>", lambda _e: self._toggle_engine())

        self._tick()

    # --- thread-safe state setters (attribute writes only) ---

    def set_state(self, state: str) -> None:
        self._state = state

    def set_started(self, ts: float) -> None:
        self._started = ts

    def set_engine(self, engine: str) -> None:
        self._engine = engine

    # --- Tk-thread work ---

    _DRAG_THRESHOLD = 3  # px of movement before a press counts as a drag, not a click

    def _on_press(self, event: tk.Event) -> None:
        self._drag_moved = False
        self._press_x, self._press_y = event.x_root, event.y_root
        self._win_x, self._win_y = self.root.winfo_x(), self.root.winfo_y()

    def _on_drag(self, event: tk.Event) -> None:
        dx = event.x_root - self._press_x
        dy = event.y_root - self._press_y
        if abs(dx) > self._DRAG_THRESHOLD or abs(dy) > self._DRAG_THRESHOLD:
            self._drag_moved = True
        if self._drag_moved:
            # overrideredirect windows reposition with a geometry "+x+y" offset.
            self.root.geometry(f"+{self._win_x + dx}+{self._win_y + dy}")

    def _on_release(self, _event: tk.Event) -> None:
        if self._drag_moved:
            if self._on_moved is not None:
                self._on_moved(self.root.winfo_x(), self.root.winfo_y())
        else:
            self._quit()  # a plain click (no drag) is the quit gesture

    def _toggle_engine(self) -> None:
        if self._on_toggle_engine is not None:
            self._on_toggle_engine()

    def _quit(self) -> None:
        if self._on_quit is not None:
            self._on_quit()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _tick(self) -> None:
        state = self._state
        dot, txt, label = _STYLE.get(state, _STYLE[IDLE])
        if state == RECORDING:
            elapsed = max(0, int(time.time() - self._started))
            label = f"REC {elapsed // 60}:{elapsed % 60:02d}"
        if self._engine:
            label = f"{label} ·{_ENGINE_TAG.get(self._engine, self._engine)}"
        self.canvas.itemconfig(self._dot, fill=dot)
        self.canvas.itemconfig(self._text, fill=txt, text=label)
        try:
            self.root.after(100, self._tick)
        except tk.TclError:
            pass  # window destroyed

    def mainloop(self) -> None:
        self.root.mainloop()
