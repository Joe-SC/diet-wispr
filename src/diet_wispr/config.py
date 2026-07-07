"""Configuration loading: merges config.toml with the OPENAI_KEY from .env."""

from __future__ import annotations

import os
import re
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# config.py lives at src/diet_wispr/config.py, so the project root is three
# parents up (diet_wispr -> src -> root).
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    # hotkey
    combo: str
    mode: str  # "toggle" | "ptt"
    # audio
    mic_device: str | int | None
    sample_rate: int
    min_seconds: float
    # transcription
    engine: str  # "batch" | "realtime"
    stt_model: str
    # cleanup
    cleanup_enabled: bool
    cleanup_model: str
    # output
    restore_clipboard: bool
    paste_delay: float
    # feedback
    beep: bool
    indicator_enabled: bool
    indicator_position: str
    # debug
    log_keys: bool
    # secrets
    openai_key: str


def _find_config() -> Path:
    """Locate config.toml: explicit env override, then cwd, then project root."""
    override = os.getenv("DIET_WISPR_CONFIG")
    if override:
        return Path(override).expanduser()
    cwd_candidate = Path.cwd() / "config.toml"
    if cwd_candidate.is_file():
        return cwd_candidate
    return _PROJECT_ROOT / "config.toml"


def _coerce_device(value: object) -> str | int | None:
    """An empty string means 'use the default device'; otherwise name or index."""
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text == "":
        return None
    # Allow an integer index expressed as a string in the TOML.
    if text.lstrip("-").isdigit():
        return int(text)
    return text


def save_indicator_position(x: int, y: int) -> None:
    """Persist a dragged indicator position back to config.toml as 'x,y' pixels.

    Uses a targeted line replacement (not a full TOML rewrite) so the file's
    comments and layout survive. There is only one `position` key in the file,
    so matching the first `position = ...` line is unambiguous. Best-effort:
    failures (e.g. read-only file) are swallowed so a drag never crashes the UI.
    """
    try:
        path = _find_config()
        text = path.read_text(encoding="utf-8")
        new_line = f'position = "{x},{y}"'
        pattern = re.compile(r"^position\s*=.*$", re.MULTILINE)
        if pattern.search(text):
            text = pattern.sub(new_line, text, count=1)
        else:  # no key yet: append under an [indicator] header if present, else EOF
            text = text.rstrip() + "\n" + new_line + "\n"
        path.write_text(text, encoding="utf-8")
    except OSError:
        pass


def load_settings() -> Settings:
    """Read config.toml + .env and return a validated Settings object."""
    # .env is loaded from cwd or the project root; load_dotenv is a no-op if absent.
    load_dotenv(_PROJECT_ROOT / ".env")
    load_dotenv()  # also pick up a .env in the current working directory

    openai_key = os.getenv("OPENAI_KEY")
    if not openai_key:
        raise ValueError(
            "OPENAI_KEY environment variable not set. "
            "Copy .env-example to .env and add your key."
        )

    config_path = _find_config()
    if not config_path.is_file():
        # First run: seed config.toml from the shipped template so the tool
        # works out of the box. Per-machine edits (mic, dot position) then stay
        # local — config.toml is gitignored.
        template = _PROJECT_ROOT / "config.example.toml"
        if template.is_file():
            shutil.copyfile(template, config_path)
            print(f"Created {config_path} from config.example.toml. Edit it to taste.")
        else:
            raise FileNotFoundError(
                f"config.toml not found (looked at {config_path}) and no "
                f"config.example.toml to seed it from."
            )
    with config_path.open("rb") as fh:
        cfg = tomllib.load(fh)

    hotkey = cfg.get("hotkey", {})
    audio = cfg.get("audio", {})
    transcription = cfg.get("transcription", {})
    cleanup = cfg.get("cleanup", {})
    output = cfg.get("output", {})
    feedback = cfg.get("feedback", {})
    indicator = cfg.get("indicator", {})
    debug = cfg.get("debug", {})

    mode = str(hotkey.get("mode", "ptt")).lower()
    if mode not in ("toggle", "ptt"):
        raise ValueError(f"hotkey.mode must be 'toggle' or 'ptt', got {mode!r}.")

    engine = str(transcription.get("engine", "batch")).lower()
    if engine not in ("batch", "realtime"):
        raise ValueError(
            f"transcription.engine must be 'batch' or 'realtime', got {engine!r}."
        )

    return Settings(
        combo=str(hotkey.get("combo", "right ctrl")),
        mode=mode,
        mic_device=_coerce_device(audio.get("mic_device")),
        sample_rate=int(audio.get("sample_rate", 24000)),
        min_seconds=float(audio.get("min_seconds", 0.3)),
        engine=engine,
        stt_model=str(transcription.get("stt_model", "gpt-4o-mini-transcribe")),
        cleanup_enabled=bool(cleanup.get("enabled", True)),
        cleanup_model=str(cleanup.get("cleanup_model", "gpt-4o-mini")),
        restore_clipboard=bool(output.get("restore_clipboard", True)),
        paste_delay=float(output.get("paste_delay", 0.15)),
        beep=bool(feedback.get("beep", True)),
        indicator_enabled=bool(indicator.get("enabled", True)),
        indicator_position=str(indicator.get("position", "bottom-center")),
        log_keys=bool(debug.get("log_keys", False)),
        openai_key=openai_key,
    )
