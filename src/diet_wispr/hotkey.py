"""Global hotkey detection for toggle and push-to-talk modes.

A single raw pynput Listener tracks the set of currently-held keys and emits two
edges: on_activate (the full combo just became held) and on_deactivate (a combo
key was released while active). The app wires these to recording start/stop
differently per mode. Callbacks here only update small state and fire the
provided edge callbacks, which must themselves be cheap (they offload heavy work
to a worker thread).
"""

from __future__ import annotations

from collections.abc import Callable

from pynput import keyboard
from pynput.keyboard import Key, KeyCode

# Friendly combo tokens -> canonical pynput key objects.
_MODIFIERS = {
    "ctrl": Key.ctrl,
    "control": Key.ctrl,
    "alt": Key.alt,
    "shift": Key.shift,
    "cmd": Key.cmd,
    "win": Key.cmd,
    "super": Key.cmd,
}
_SPECIAL = {
    "space": Key.space,
    "enter": Key.enter,
    "return": Key.enter,
    "tab": Key.tab,
    "esc": Key.esc,
    "escape": Key.esc,
}
# Side-specific modifiers. These must NOT be canonicalized (canonical() collapses
# ctrl_r -> ctrl, which would also match the left key), so a "right ctrl" combo
# matches only the right key and leaves Left Ctrl free for copy/paste etc.
_SIDE = {
    "right ctrl": Key.ctrl_r, "rctrl": Key.ctrl_r, "ctrl_r": Key.ctrl_r,
    "left ctrl": Key.ctrl_l, "lctrl": Key.ctrl_l, "ctrl_l": Key.ctrl_l,
    "right alt": Key.alt_r, "ralt": Key.alt_r, "alt_r": Key.alt_r, "altgr": Key.alt_r,
    "left alt": Key.alt_l, "lalt": Key.alt_l, "alt_l": Key.alt_l,
    "right shift": Key.shift_r, "rshift": Key.shift_r, "shift_r": Key.shift_r,
    "left shift": Key.shift_l, "lshift": Key.shift_l, "shift_l": Key.shift_l,
}
_SIDE_SPECIFIC = set(_SIDE.values())


def _parse_token(token: str):
    token = " ".join(token.strip().lower().split())  # collapse internal whitespace
    if token in _SIDE:
        return _SIDE[token]
    if token in _MODIFIERS:
        return _MODIFIERS[token]
    if token in _SPECIAL:
        return _SPECIAL[token]
    if token.startswith("f") and token[1:].isdigit():
        fn = getattr(Key, token, None)
        if fn is not None:
            return fn
    if len(token) == 1:
        return KeyCode.from_char(token)
    raise ValueError(f"Unrecognized hotkey token: {token!r}")


def parse_combo(combo: str) -> frozenset:
    """Parse a combo like 'ctrl+alt+space' into a set of canonical keys."""
    keys = {_parse_token(part) for part in combo.split("+") if part.strip()}
    if not keys:
        raise ValueError(f"Empty hotkey combo: {combo!r}")
    return frozenset(keys)


class HotkeyListener:
    def __init__(
        self,
        combo: str,
        on_activate: Callable[[], None],
        on_deactivate: Callable[[], None],
        log_keys: bool = False,
    ) -> None:
        self._required = parse_combo(combo)
        self._on_activate = on_activate
        self._on_deactivate = on_deactivate
        self._log_keys = log_keys
        self._pressed: set = set()
        self._active = False
        self._listener: keyboard.Listener | None = None

    def _forms(self, key) -> set:
        """Both the raw key and its canonical form.

        Tracking both lets a generic combo match via canonical keys (ctrl_l ->
        ctrl, space -> KeyCode(32)) while a side-specific combo matches via the
        raw key (ctrl_r), so Left Ctrl never satisfies a 'right ctrl' requirement.
        """
        forms = {key}
        if self._listener is not None:
            forms.add(self._listener.canonical(key))
        return forms

    def _on_press(self, key) -> None:
        forms = self._forms(key)
        if self._log_keys:
            print(f"[diet-wispr] key: {key!r}", flush=True)
        self._pressed |= forms
        if not self._active and self._required <= self._pressed:
            self._active = True
            self._on_activate()

    def _on_release(self, key) -> None:
        forms = self._forms(key)
        if self._active and (forms & self._required):
            self._active = False
            self._on_deactivate()
        self._pressed -= forms

    def start(self) -> None:
        self._listener = keyboard.Listener(
            on_press=self._on_press, on_release=self._on_release
        )
        # Canonicalize required keys so pressed keys match (e.g. space ->
        # KeyCode(32), ctrl_l -> ctrl) -- EXCEPT side-specific modifiers like
        # ctrl_r, which must stay raw so only the right key matches.
        self._required = frozenset(
            k if k in _SIDE_SPECIFIC else self._listener.canonical(k)
            for k in self._required
        )
        self._listener.start()

    def is_alive(self) -> bool:
        return self._listener is not None and self._listener.is_alive()

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
