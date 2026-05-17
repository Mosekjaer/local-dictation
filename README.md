# local-dictation

Local push-to-talk dictation for Pop!_OS COSMIC. ffmpeg captures audio
from your default PulseAudio source, whisper.cpp (`whisper-cli`)
transcribes locally, and the result is pasted into the focused window.
No cloud, no network calls.

## Requirements

- Python 3.10+
- ffmpeg
- wl-clipboard (`wl-copy`, `wl-paste`)
- ydotool + ydotoold (for synthesised Ctrl+V on Wayland)
- wtype (optional; one of two direct-typing options. The other,
  `ydotool type`, is shipped via the bundled `ydotool` dep above.)
- libnotify-bin (`notify-send`) for desktop notifications
- A built whisper.cpp with `whisper-cli` and a `ggml-small.bin` model

The defaults assume `~/stt-bench/whisper.cpp/build-vulkan/bin/whisper-cli`
and `~/stt-bench/whisper.cpp/models/ggml-small.bin`. Override both in
`~/.config/local-dictation/config.toml` if your layout differs.

Optional, for the always-on-top status window: `python3-gi` (PyGObject).

## Install

```
./install.sh
```

The installer:

1. Verifies Python 3.10+.
2. Creates a venv at `~/.local/share/local-dictation/.venv`.
3. Installs the project into it.
4. Symlinks `~/.local/bin/local-dictation` to the venv entry point.
5. Writes `~/.config/local-dictation/config.toml` from the example if no
   config exists yet.
6. Installs and enables two `--user` systemd units:
   `local-dictation.service` and `ydotoold.service`.

Run `local-dictation doctor` after installation to verify the
environment.

To also build the optional COSMIC panel applet (real-time equalizer
flanking the datetime applet), pass `--with-applet`:

```
./install.sh --with-applet
```

This adds a `cargo install` step for two Rust binaries
(`local-dictation-applet-left`, `local-dictation-applet-right`) and
drops two `.desktop` files into `~/.local/share/applications/`. Cold
build takes ~2 minutes. After install you must edit
`~/.config/cosmic/com.system76.CosmicPanel.Panel/v1/plugins_center` to
add the two applet IDs around `CosmicAppletTime` — the installer prints
the exact lines.

## COSMIC keyboard shortcut

COSMIC does not expose a global hotkey API, so the daemon listens on a
Unix socket and the CLI nudges it via `local-dictation toggle`. Bind
that command to a key combo through Settings:

1. Open **Settings -> Input -> Keyboard -> Keyboard Shortcuts**.
2. **Unbind the default Super+Space** binding (it ships as "switch
   input source"). Custom shortcuts cannot override built-ins, so this
   step is mandatory.
3. Add a **Custom Shortcut**:
   - Command: `local-dictation toggle`
   - Key combo: `Super+Space`

Press the combo once to start recording, again to stop and paste.

## Configuration

All settings live in `~/.config/local-dictation/config.toml`. Every key
is optional; omitted keys use the defaults shown in `config.example.toml`.

| Key                 | Description                                                                                       |
|---------------------|---------------------------------------------------------------------------------------------------|
| `whisper_cli`       | Path to the `whisper-cli` binary built from whisper.cpp.                                          |
| `model`             | Path to the GGML model file (e.g. `ggml-small.bin`).                                              |
| `language`          | Whisper language hint: `auto`, `da`, or `en`.                                                     |
| `audio_source`      | PulseAudio source name. `default` follows the system default.                                     |
| `channels`          | Recording channels. Whisper expects mono; leave at `1`.                                           |
| `sample_rate`       | Recording sample rate in Hz. Whisper wants `16000`.                                               |
| `temp_dir`          | Where audio clips are buffered before transcription.                                              |
| `paste_method`      | `auto`, `wtype`, `ydotool_type`, `ydotool`, or `clipboard`. See below.                            |
| `restore_clipboard` | Save and restore the previous clipboard contents around clipboard-based paste.                    |
| `notifications`     | `"all"`, `"errors_only"`, or `"none"`. `errors_only` keeps the notify-send pings for failures and clipboard-only pastes only (rely on the equalizer otherwise). Legacy `true` / `false` accepted as `"all"` / `"none"`. |
| `status_window`     | Show a small always-on-top GTK window with the current state.                                     |
| `log_file`          | Log file path. Rotated by size in the daemon.                                                     |
| `log_level`         | `DEBUG`, `INFO`, `WARNING`, or `ERROR`.                                                           |
| `socket_path`       | Unix socket the CLI uses to reach the daemon. Defaults to `$XDG_RUNTIME_DIR/local-dictation.sock`. |

`paste_method` strategies, applied in order when set to `auto`:

1. `wtype` — direct text injection via the wlroots virtual-keyboard
   protocol. Leaves the clipboard alone. May not work on COSMIC.
2. `ydotool_type` — `ydotool type --file -` feeds the text to ydotool,
   which synthesises keypresses through `/dev/uinput`. Leaves the
   clipboard alone. Layout-dependent: characters not reachable on the
   active keymap may be mistyped. Requires a running `ydotoold` socket.
3. `ydotool` — `wl-copy` the text, then `ydotool key 29:1 47:1 47:0 29:0`
   (Ctrl+V). Mutates the clipboard. Requires `ydotoold`.
4. `clipboard` — `wl-copy` only. You paste manually.

## CLI reference

| Command                          | Description                                                                                       |
|----------------------------------|---------------------------------------------------------------------------------------------------|
| `local-dictation doctor`         | Run environment checks. Exits 0 if everything required passes, 2 on any required failure.         |
| `local-dictation toggle`         | Toggle between recording and stop+transcribe. Bind this to your hotkey.                           |
| `local-dictation start`          | Start recording. No-op if already recording.                                                      |
| `local-dictation stop`           | Stop recording and transcribe.                                                                    |
| `local-dictation status`         | Print the current state (`idle`, `recording`, `transcribing`).                                    |
| `local-dictation transcribe FILE`| Transcribe `FILE` offline and print the text. Accepts `--language auto\|da\|en`.                  |
| `local-dictation daemon`         | Run the daemon in the foreground. Used by the systemd unit; rarely invoked by hand.               |

`--language auto|da|en` is also accepted on `start`, `toggle`, and
`transcribe` to override the configured default for that invocation.

`--live` / `--no-live` is accepted on `start` and `toggle` to override
`live_typing` in the config. See [Live typing](#live-typing) below.

## Live typing

Default behaviour: hold-to-talk, release, text is typed in one shot.
This is the most accurate mode and remains the default.

`local-dictation toggle --live` switches to a streaming mode where
transcribed text appears at the cursor while you are still speaking.
The daemon spawns a `whisper-server` subprocess on first use and runs a
sliding-window re-transcription loop, committing prefixes that have
been stable across consecutive passes. Tunables live in `config.toml`
under the `live_*` keys.

**Tradeoffs.** Live mode is a deliberate accuracy/latency compromise:

- **Accuracy is worse than batch.** Whisper was trained on 30 s
  contexts; partial-utterance inference is its weak side. Expect a
  few percent more word errors than batch mode produces on the same
  audio.
- **Visible revisions.** Whisper occasionally rewrites the last few
  characters as more context arrives. The typer issues backspaces and
  re-types, so you may see flickering at the cursor.
- **Backspace cap.** A revision spanning more than 50 characters is
  not back-corrected — the typer keeps typing forward and the final
  authoritative pass at end-of-utterance re-syncs the text.
- **Focus changes are unsafe.** If you click into a different window
  mid-session, subsequent backspaces will delete text in the new
  window. Wayland does not let us detect this; do not click away from
  the target while live mode is active.

**Enable.** Per invocation:

```
local-dictation toggle --live
```

Or set as default:

```toml
# ~/.config/local-dictation/config.toml
live_typing = true
```

**Fallback.** If `whisper-server` cannot be reached (binary missing,
port in use, VAD model not on disk), the daemon logs a warning,
shows an error notification, and falls back to batch mode for that
invocation. `local-dictation doctor` surfaces both issues as `WARN`.

## Troubleshooting

**ydotoold not running.** `local-dictation doctor` will warn if the
socket is missing. Check with `systemctl --user status ydotoold` and
restart it with `systemctl --user restart ydotoold`. The default socket
path is `/tmp/.ydotool_socket`; the daemon also honours
`$YDOTOOL_SOCKET`.

**Clipboard restore caveats.** Restore only covers UTF-8 text. If you
had an image or files on the clipboard, those will not come back. Turn
restore off with `restore_clipboard = false` if you find the delay
disruptive.

**Focus race.** If the paste arrives in the wrong window, you released
the hotkey before the target window regained focus. Hold focus on the
target briefly after pressing Super+Space.

**COSMIC default binding.** Custom shortcuts will not fire if a
built-in claims the same combo. Unbind the default Super+Space
("switch input source") before binding `local-dictation toggle`.

## Uninstall

```
./uninstall.sh
```

Removes the venv, CLI symlink, and systemd units. Leaves your config
and cache directories alone.

```
./uninstall.sh --purge
```

Also removes `~/.config/local-dictation/` and
`~/.cache/local-dictation/`.

## Optional: COSMIC panel applet

A real-time equalizer that sits in the COSMIC panel, flanking the
datetime applet, and reflects daemon state visually:

- **Idle**: bars collapsed to a thin baseline.
- **Recording**: 7 bars driven by live RMS/peak audio levels at ~10 Hz.
- **Transcribing**: bars pulse at 1.5 Hz.
- **Error**: brief red flash.

Two binaries (one for each side of the datetime applet) are built from a
shared Rust crate under `cosmic-applet/`. The daemon publishes events on
`$XDG_RUNTIME_DIR/local-dictation-events.sock` (separate from the main
IPC socket); the applet binaries subscribe.

Install with `./install.sh --with-applet`. The installer prints the exact
panel-config snippet to paste into
`~/.config/cosmic/com.system76.CosmicPanel.Panel/v1/plugins_center`. After
editing, restart the panel:

```
systemctl --user restart cosmic-panel.service \
  || pkill cosmic-panel
```

The applet is gated by the broadcast socket — if the daemon isn't
running or its events socket can't be created (check `doctor`), the
applet shows `idle` and reconnects on a 200ms→5s backoff.

The pinned libcosmic git rev is recorded in `cosmic-applet/README.md`.
On COSMIC point releases, rebuild the workspace
(`cd cosmic-applet && cargo build --release`); on a major COSMIC version
bump, update the pin to match.

## Known Wayland limitations

- COSMIC does not expose a global hotkey API; the toggle command is
  invoked by the desktop's own keyboard-shortcut subsystem.
- Clipboard restore is text-only. Non-text MIME types are lost.
- Wayland has no focus-restoration primitive; if you change focus
  between pressing the hotkey and the paste arriving, the text lands in
  whatever window is focused at paste time.
