# diet-wispr

A lean, self-hosted voice-dictation tool. Press a hotkey, speak, and the
transcribed (and optionally cleaned-up) text is pasted into whatever window has
focus. Built so you don't need to install a third-party dictation app — it's
just a Python script calling an OpenAI API key you already have.

## Pipeline

```
[hotkey] -> [mic] -> [ engine ] -> [clipboard paste]
 pynput   sounddevice    |          pyperclip + Ctrl+V
                         |
        batch:    record -> transcribe -> cleanup (gpt-4o-mini)
        realtime: stream audio live -> transcript ready on release
```

Two interchangeable engines (switch live by **right-clicking the dot**):
- **batch** — record, then `gpt-4o-mini-transcribe` + a `gpt-4o-mini` cleanup pass
  (removes filler words, fixes punctuation). Slightly slower; polished output.
- **realtime** — streams audio to OpenAI's Realtime API *while you speak*, so the
  transcript is ready almost immediately on release. Faster; raw output.

## Setup

```powershell
cd C:\Source\diet-wispr
uv sync
copy .env-example .env      # then edit .env and set OPENAI_KEY
```

This project uses `OPENAI_KEY` (the Orbis workspace convention), **not** the
SDK's default `OPENAI_API_KEY`.

## Run

```powershell
uv run diet-wispr
```

A small dim dot appears bottom-center. In the default **push-to-talk** mode,
**hold `Right Ctrl`** while you speak (the dot turns red and shows elapsed time),
then **release** to transcribe (the dot goes amber `working`, then the text
pastes into the focused window and the dot returns to dim).

- **Left-click the dot to quit.**
- **Right-click the dot to switch engine** (batch ↔ realtime) for A/B comparison.
- **Drag the dot to move it** anywhere on screen. The new position is saved to
  `config.toml`, so it reopens where you left it. (A plain click without dragging
  still quits.)

Each dictation logs a line you can compare:
`<engine> | rec <duration>s | latency <release→text>s | <preview>`.

Set `hotkey.mode = "toggle"` in `config.toml` for tap-on / tap-off instead.

### If the hotkey doesn't fire

Set `[debug] log_keys = true` in `config.toml` and re-run: every key the listener
sees is printed, so you can confirm the combo is arriving.

## Configuration (`config.toml`)

| Setting | Meaning |
|---|---|
| `hotkey.combo` | Trigger key, e.g. `right ctrl`, `f9`, or a combo like `ctrl+shift+f9`. Use a key that emits NO character (see below). |
| `hotkey.mode` | `toggle` (tap on / tap off) or `ptt` (hold to talk). |
| `audio.mic_device` | Mic name substring or index; empty = system default. Resolved device is printed on startup. |
| `audio.sample_rate` | Capture rate. `24000` is required by the realtime engine; batch tolerates it. Falls back to the device default if rejected. |
| `audio.min_seconds` | Recordings shorter than this are discarded (no API call). |
| `transcription.engine` | `batch` (transcribe + cleanup) or `realtime` (streaming). Right-click the dot to switch live. |
| `transcription.stt_model` | Default `gpt-4o-mini-transcribe` (used by both engines). |
| `cleanup.enabled` / `cleanup.cleanup_model` | Light filler-word / punctuation cleanup pass. |
| `output.restore_clipboard` | Restore the prior clipboard after pasting. |
| `output.paste_delay` | Seconds to wait after Ctrl+V before restoring the clipboard. |
| `feedback.beep` | Start/stop beep so you know the mic is live. |
| `indicator.enabled` | Show the floating recording dot. Click to quit, right-click to switch engine, drag to move. |
| `indicator.position` | Where the dot starts: a named anchor (`bottom-center`, `top-right`, `center`, ...) or literal `"x,y"` pixels. Dragging the dot rewrites this. |
| `debug.log_keys` | Print every key the listener sees (hotkey troubleshooting). |

## Known limitations (Windows)

- **Use a hotkey that emits no character.** A printable key (like space or a
  letter) gets *typed into your app* while you hold it. Modifier-only keys
  (`right ctrl`, `right alt`) or function keys (`f9`) are safe. Also avoid
  `Alt+...` — `Alt+Space` opens the Windows system menu. The default is `right ctrl`.
- **Focus = paste target.** The text pastes into whatever window has focus when
  you stop recording. With push-to-talk you naturally stay in your target app;
  just don't alt-tab away before releasing.
- **Clipboard paste** briefly overwrites your clipboard, then restores it. If
  another app changes the clipboard during the paste window, the restore can
  clobber that change. Set `output.restore_clipboard = false` to disable.
- **UIPI:** the simulated `Ctrl+V` cannot inject into apps running at a higher
  integrity level (e.g. an admin/elevated window). Run the target app at the
  same integrity as this tool.
- The mic must be permitted for the terminal you launch from.
- For push-to-talk, prefer a combo ending in a normal key over an all-modifier
  combo, which is awkward to hold.
