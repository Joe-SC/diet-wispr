"""Deliver transcribed text into the focused window via clipboard paste."""

from __future__ import annotations

import time

import pyperclip
from pynput.keyboard import Controller, Key, KeyCode

_kbd = Controller()
_V = KeyCode.from_char("v")  # letter keys are KeyCodes, not Key.v


def _safe_get_clipboard() -> str | None:
    """Read the clipboard, tolerating transient Windows clipboard locks."""
    for _ in range(3):
        try:
            return pyperclip.paste()
        except pyperclip.PyperclipException:
            time.sleep(0.05)
    return None


def _safe_set_clipboard(value: str) -> bool:
    for _ in range(3):
        try:
            pyperclip.copy(value)
            return True
        except pyperclip.PyperclipException:
            time.sleep(0.05)
    return False


def _send_paste() -> None:
    with _kbd.pressed(Key.ctrl):
        _kbd.press(_V)
        _kbd.release(_V)


def paste_text(
    text: str,
    *,
    restore_clipboard: bool = True,
    paste_delay: float = 0.15,
) -> None:
    """Copy text, simulate Ctrl+V into the focused window, then restore clipboard.

    The restore is best-effort and inherently racy: if another app changes the
    clipboard during the paste window, the restore can clobber it. Disable via
    restore_clipboard=False.
    """
    if not text:
        return

    previous = _safe_get_clipboard() if restore_clipboard else None
    try:
        if not _safe_set_clipboard(text):
            return  # couldn't own the clipboard; nothing to paste
        # Give the OS a beat to register the new clipboard before pasting.
        time.sleep(0.02)
        _send_paste()
        # Wait for the target app to consume the paste before we restore.
        time.sleep(paste_delay)
    finally:
        if restore_clipboard and previous is not None:
            _safe_set_clipboard(previous)
