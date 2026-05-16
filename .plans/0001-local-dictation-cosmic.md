# Implementation Plan: local-dictation for Pop!_OS COSMIC

Status: draft
Execution state: not started

## Goal

Build a Python 3 command-line dictation tool (`local-dictation`) that toggles microphone recording on Pop!_OS COSMIC (Wayland), transcribes the recording locally with the existing whisper.cpp Vulkan build, and injects the resulting text into the previously-focused window. All processing is local; no network calls.

The CLI is bound to **Super+Space** through COSMIC's keyboard shortcut settings (the app does **not** register the global hotkey itself, because COSMIC Wayland does not expose a public API for app-side global hotkey registration).

## Requirements

- [ ] CLI binary `local-dictation` with subcommands: `doctor`, `toggle`, `start`, `stop`, `status`, `transcribe`
- [ ] `toggle`, `transcribe` accept `--language {auto,da,en}`; default is config value, fall-back default `auto`
- [ ] Daemon process owns the recording state; `toggle` is a thin client that talks to the daemon over a Unix socket
- [ ] Recording uses `ffmpeg -f pulse -i default -ac 1 -ar 16000 -y <wav>`, mono 16 kHz WAV
- [ ] Transcription uses `~/stt-bench/whisper.cpp/build-vulkan/bin/whisper-cli` with `-m ~/stt-bench/whisper.cpp/models/ggml-small.bin --no-prints -otxt`, passing `-l <code>` only when language ≠ `auto`
- [ ] Text cleanup: trim, collapse ≥2 blank lines to one, preserve punctuation
- [ ] Paste strategy with documented fallback chain (see Cross-Cutting Concerns: paste-strategy)
- [ ] Clipboard preservation when `restore_clipboard = true`, with documented Wayland caveats
- [ ] Desktop notifications via `notify-send` for state transitions; optional GTK always-on-top status window
- [ ] Config at `~/.config/local-dictation/config.toml` with all defaults from the brief
- [ ] `doctor` checks every dependency listed in the brief and exits non-zero on missing required deps
- [ ] `install.sh` creates venv, installs package, writes default config (only if missing), optionally installs and enables the systemd user service
- [ ] `uninstall.sh` reverses `install.sh`; never deletes whisper.cpp, models, or user config (unless `--purge`)
- [ ] systemd user service `local-dictation.service` starts the daemon on login
- [ ] Logs to `~/.cache/local-dictation/local-dictation.log` with rotation-safe append
- [ ] Toggle while transcribing is rejected with a clear notification; no concurrent recordings
- [ ] ffmpeg subprocess stopped with SIGINT then SIGTERM; orphan check on daemon startup
- [ ] README documents COSMIC-specific keyboard binding steps and known Wayland limitations

## Assumptions

- [ ] User stays in the `input` group (verified: present), and `/dev/uinput` is accessible (verified: ACL grants `input` group rw) — so `ydotool` can run as the user once `ydotoold` is up.
- [ ] `wtype` is **not** installed (verified) but PR-ready: the paste layer must work without it and treat it as an optional upgrade.
- [ ] COSMIC has no stable AppIndicator/StatusNotifierItem panel API in COSMIC alpha as of 2026-05; tray indicator is therefore **not** in scope. Confirm by checking `gdbus introspect --session --dest com.system76.CosmicPanel` before promoting tray to "supported".
- [ ] COSMIC Settings → Input → Keyboard Shortcuts supports custom commands and can rebind Super+Space (default in COSMIC is "switch input source"). User must clear the existing binding first; README will say so.
- [ ] `ffmpeg` PulseAudio input `-f pulse -i default` works against PipeWire's `pipewire-pulse` shim (verified: `pactl info` reports `PulseAudio (on PipeWire 1.5.85)`).
- [ ] Python 3.12 stdlib `tomllib` is available (verified) — no third-party TOML dep needed for reads.
- [ ] `XDG_RUNTIME_DIR` exists for a logged-in user session and is the right place for the IPC socket (`/run/user/1000/`).

## Agent Team Readiness

- [ ] This plan can be executed by an agent team: yes
- [ ] Recommended team size: 3 implementation agents + 1 lead
- [ ] Parallelization strategy: Phase 1 (skeleton + contracts) is single-agent (lead). Phases 2–4 run agents A/B/C in parallel because file ownership and contracts are disjoint. Phase 5 (install/docs) waits for Phase 2–4 to converge.
- [ ] Integration contracts are explicit enough for parallel work
- [ ] File ownership boundaries are defined to prevent conflicts
- [ ] Validation can be assigned per agent

## Suggested Agent Structure

### Agent: core (Agent A)

- [ ] Owns:
  - `local_dictation/__init__.py` — package marker, version
  - `local_dictation/cli.py` — argparse entry point, subcommand dispatch
  - `local_dictation/config.py` — TOML loader, dataclass `Config`, default writer
  - `local_dictation/daemon.py` — long-running process, state machine, IPC server
  - `local_dictation/ipc.py` — Unix socket client + server protocol
  - `local_dictation/state.py` — `State` enum (`IDLE`, `RECORDING`, `TRANSCRIBING`, `ERROR`)
  - `local_dictation/logging_setup.py` — file logger configuration
  - `pyproject.toml` — package metadata, `[project.scripts] local-dictation = "local_dictation.cli:main"`
- [ ] Does NOT touch:
  - `local_dictation/audio.py`, `local_dictation/transcribe.py` — owned by Agent B
  - `local_dictation/output.py`, `local_dictation/notify.py`, `local_dictation/doctor.py` — owned by Agent C
  - `install.sh`, `uninstall.sh`, `systemd/local-dictation.service`, `README.md` — owned by Agent C
- [ ] Responsibilities:
  - Load and validate config with sensible defaults
  - Run the daemon, accept IPC commands (`TOGGLE`, `START`, `STOP`, `STATUS`)
  - Enforce the state machine; reject `TOGGLE` during `TRANSCRIBING`
  - Spawn audio recorder via Agent B's `Recorder`, then call Agent B's `Transcriber`
  - Pass cleaned text to Agent C's `Output.paste`
  - Trigger Agent C's notifier on every state change
- [ ] Produces:
  - `Recorder`/`Transcriber`/`Output`/`Notifier` invocation contract (see Integration Contracts)
  - `ipc.py` socket protocol (see Integration Contracts)
- [ ] Consumes:
  - Implementations of those interfaces from Agents B and C
- [ ] Validation:
  - `python -m compileall local_dictation` — exits 0
  - `python -m local_dictation.cli --help` — prints help, exits 0
  - `python -m local_dictation.cli doctor` — runs (may report missing deps)

### Agent: media (Agent B)

- [ ] Owns:
  - `local_dictation/audio.py` — `Recorder` class wrapping ffmpeg
  - `local_dictation/transcribe.py` — `Transcriber` class wrapping whisper-cli + text cleanup
  - `tests/test_transcribe_cleanup.py` — unit tests for the pure-function cleanup logic
- [ ] Does NOT touch:
  - Anything owned by Agent A or Agent C
- [ ] Responsibilities:
  - Start/stop ffmpeg cleanly (SIGINT → wait 2 s → SIGTERM → wait 2 s → SIGKILL)
  - Generate timestamped WAV paths under `config.temp_dir`
  - Run whisper-cli, locate the produced `.txt` (whisper writes `<wav>.txt`), read and clean it
  - Whitespace cleanup: strip leading/trailing whitespace; collapse runs of ≥2 blank lines to exactly one blank line; do **not** alter inline punctuation or Unicode characters
  - Raise typed errors (`RecorderError`, `TranscriberError`) the daemon can catch
- [ ] Produces:
  - `Recorder` and `Transcriber` interfaces (see Integration Contracts)
- [ ] Consumes:
  - `Config` from Agent A
- [ ] Validation:
  - `python -m pytest tests/test_transcribe_cleanup.py -q`
  - `python -c "from local_dictation.audio import Recorder; from local_dictation.transcribe import Transcriber"` — imports clean

### Agent: io (Agent C)

- [ ] Owns:
  - `local_dictation/output.py` — `Output.paste(text)` with fallback chain
  - `local_dictation/clipboard.py` — wl-copy/wl-paste wrappers with optional save/restore
  - `local_dictation/notify.py` — `Notifier` calling `notify-send`; optional `GtkStatusWindow`
  - `local_dictation/doctor.py` — `doctor` command implementation, returns structured `Check` list
  - `install.sh`, `uninstall.sh`
  - `systemd/local-dictation.service`
  - `README.md`
  - `config.example.toml`
- [ ] Does NOT touch:
  - Anything owned by Agent A or Agent B
- [ ] Responsibilities:
  - Paste fallback (paste-strategy concern, see Cross-Cutting Concerns)
  - Clipboard preserve/restore (see clipboard-restore concern)
  - Notifications for every state transition
  - Doctor checks (see doctor-checks concern)
  - Install/uninstall scripts that touch only files this project creates
  - README with COSMIC keyboard-shortcut instructions and Wayland limitations section
- [ ] Produces:
  - `Output` and `Notifier` interfaces (see Integration Contracts)
- [ ] Consumes:
  - `Config` from Agent A
- [ ] Validation:
  - `bash -n install.sh && bash -n uninstall.sh` — shell syntax check
  - `systemd-analyze --user verify systemd/local-dictation.service` — unit file valid
  - `python -c "from local_dictation.output import Output; from local_dictation.notify import Notifier; from local_dictation.doctor import run_checks"` — imports clean

## Affected Files

All new — repository is empty (`/home/frederik/Documents/dev/stt/`).

- [ ] `pyproject.toml` — package metadata, console script entry point
- [ ] `local_dictation/__init__.py` — version
- [ ] `local_dictation/cli.py` — argparse entry
- [ ] `local_dictation/config.py` — TOML config loader
- [ ] `local_dictation/daemon.py` — daemon + state machine
- [ ] `local_dictation/ipc.py` — Unix socket protocol
- [ ] `local_dictation/state.py` — state enum
- [ ] `local_dictation/audio.py` — ffmpeg wrapper
- [ ] `local_dictation/transcribe.py` — whisper-cli wrapper + cleanup
- [ ] `local_dictation/output.py` — paste strategies
- [ ] `local_dictation/clipboard.py` — wl-clipboard helpers
- [ ] `local_dictation/notify.py` — notify-send / GTK status
- [ ] `local_dictation/doctor.py` — doctor checks
- [ ] `local_dictation/logging_setup.py` — file logger
- [ ] `config.example.toml` — committed sample config
- [ ] `systemd/local-dictation.service` — user unit
- [ ] `install.sh` — installer
- [ ] `uninstall.sh` — uninstaller
- [ ] `README.md` — setup + COSMIC instructions
- [ ] `tests/test_transcribe_cleanup.py` — unit tests for text cleanup
- [ ] `.gitignore` — `__pycache__/`, `.venv/`, `*.egg-info/`

## Dependencies

- [ ] **Runtime system deps (required)**: `ffmpeg`, `wl-copy`, `wl-paste`, `notify-send`, `whisper-cli` (existing), `ggml-small.bin` (existing)
- [ ] **Runtime system deps (one of, for typing)**: `ydotool` (verified installed) **or** `wtype` (not installed — optional upgrade); `ydotool` requires `ydotoold` running and user in `input` group (both verified).
- [ ] **Runtime system deps (optional)**: `systemctl --user` (verified) for service, GTK 3 + PyGObject for optional status window
- [ ] **Python**: 3.12 (verified). Stdlib only for the core: `argparse`, `tomllib`, `socket`, `selectors`, `subprocess`, `logging`, `pathlib`, `dataclasses`, `enum`, `signal`, `tempfile`, `shutil`
- [ ] **Optional Python**: `PyGObject` (already on Pop!_OS via `python3-gi`) — only imported when `status_window = true`
- [ ] **No third-party PyPI deps required.** `pyproject.toml` declares `dependencies = []` for the default build.

## Integration Contracts

### Contract: core (daemon) → media (Recorder)

- [ ] Producer: media (Agent B)
- [ ] Consumer: core (Agent A)
- [ ] Files involved:
  - `local_dictation/audio.py`
  - `local_dictation/daemon.py`
- [ ] Interface type: Python class
- [ ] Contract details:

````python
# local_dictation/audio.py
from pathlib import Path

class RecorderError(Exception): ...

class Recorder:
    def __init__(self, config: "Config") -> None: ...

    def start(self) -> Path:
        """Start ffmpeg, return the WAV path it is writing to.
        Raises RecorderError on failure. Non-blocking."""

    def stop(self) -> Path:
        """Signal ffmpeg to stop and wait for clean exit.
        Returns the finalised WAV path. Raises RecorderError on failure
        or if start() was not called."""

    def is_running(self) -> bool: ...
````

- [ ] Success cases:
  - `start()` returns a path; ffmpeg writes a valid 16 kHz mono WAV until `stop()` is called
  - `stop()` returns the same path after ffmpeg exits cleanly
- [ ] Error cases:
  - `start()` raises `RecorderError("ffmpeg not found")` / `RecorderError("ffmpeg exited immediately: <stderr>")`
  - `stop()` raises `RecorderError("not recording")` if called before `start()`
- [ ] Evidence:
  - Brief specifies `ffmpeg -f pulse -i default -ac 1 -ar 16000`; verified PipeWire exposes a pulse-compatible socket via `pactl info`

### Contract: core (daemon) → media (Transcriber)

- [ ] Producer: media (Agent B)
- [ ] Consumer: core (Agent A)
- [ ] Files involved:
  - `local_dictation/transcribe.py`
  - `local_dictation/daemon.py`
- [ ] Interface type: Python class
- [ ] Contract details:

````python
# local_dictation/transcribe.py
from pathlib import Path
from typing import Literal

Language = Literal["auto", "da", "en"]

class TranscriberError(Exception): ...

class Transcriber:
    def __init__(self, config: "Config") -> None: ...

    def transcribe(self, wav_path: Path, language: Language) -> str:
        """Run whisper-cli on wav_path, read the .txt output, clean and return.
        language='auto' omits -l. Returns '' on empty transcription (not an error).
        Raises TranscriberError on whisper-cli failure or missing model/binary."""
````

- [ ] Success cases:
  - Returns cleaned text for valid audio
  - Returns empty string for silent / unintelligible audio (no exception)
- [ ] Error cases:
  - Missing model file → `TranscriberError("model not found: <path>")`
  - Missing whisper-cli → `TranscriberError("whisper-cli not found: <path>")`
  - Non-zero exit → `TranscriberError("whisper-cli failed: rc=<n> stderr=<...>")`
- [ ] Evidence:
  - Exact commands quoted in the brief; verified binary and model exist on disk

### Contract: core (daemon) → io (Output)

- [ ] Producer: io (Agent C)
- [ ] Consumer: core (Agent A)
- [ ] Files involved:
  - `local_dictation/output.py`
  - `local_dictation/daemon.py`
- [ ] Interface type: Python class
- [ ] Contract details:

````python
# local_dictation/output.py
from typing import Literal

PasteMethod = Literal["auto", "wtype", "ydotool", "clipboard"]
PasteResult = Literal["typed", "pasted", "copied_only"]

class OutputError(Exception): ...

class Output:
    def __init__(self, config: "Config") -> None: ...

    def paste(self, text: str) -> PasteResult:
        """Deliver text to the focused window per paste-strategy concern.
        Returns the strategy actually used. Raises OutputError only if every
        fallback failed."""
````

- [ ] Success cases:
  - `auto` chooses the highest-priority available method; returns which one ran
  - `copied_only` is a success — the notifier must tell the user to paste manually
- [ ] Error cases:
  - All strategies unavailable → `OutputError("no paste method available")`
- [ ] Evidence:
  - Brief paste fallback order; environment scan confirms `ydotool`, `wl-copy` present, `wtype` absent

### Contract: core (daemon) → io (Notifier)

- [ ] Producer: io (Agent C)
- [ ] Consumer: core (Agent A)
- [ ] Files involved:
  - `local_dictation/notify.py`
  - `local_dictation/daemon.py`
- [ ] Interface type: Python class
- [ ] Contract details:

````python
# local_dictation/notify.py
class Notifier:
    def __init__(self, config: "Config") -> None: ...

    def on_state(self, state: "State", detail: str = "") -> None:
        """Emit a desktop notification for the given state.
        detail is appended to the body. Never raises; logs on failure."""
````

- [ ] Success cases:
  - For every state transition (`RECORDING`, `TRANSCRIBING`, `IDLE` after paste, `ERROR`) a notification fires
- [ ] Error cases:
  - notify-send missing → log warning, suppress notifications for the rest of the session
- [ ] Evidence:
  - Brief lists the four notifications; `notify-send` verified present

### Contract: toggle CLI ↔ daemon (IPC)

- [ ] Producer: core (Agent A)
- [ ] Consumer: core (Agent A) — both sides
- [ ] Files involved:
  - `local_dictation/ipc.py`
  - `local_dictation/cli.py`
  - `local_dictation/daemon.py`
- [ ] Interface type: Unix domain socket, newline-delimited JSON
- [ ] Contract details:

````
Socket path: $XDG_RUNTIME_DIR/local-dictation.sock
Permissions: 0600
Protocol: one request per connection, newline-terminated UTF-8 JSON
Server replies with one newline-terminated JSON object, then closes.

Request:
  {"cmd": "toggle",  "language": "auto" | "da" | "en" | null}
  {"cmd": "start",   "language": "auto" | "da" | "en" | null}
  {"cmd": "stop"}
  {"cmd": "status"}

Response:
  {"ok": true,  "state": "idle"|"recording"|"transcribing", "info": "..."}
  {"ok": false, "error": "human-readable message"}

`language: null` means "use the configured default".
````

- [ ] Success cases:
  - Daemon up → CLI prints `info` to stdout, exits 0
  - `toggle` while `IDLE` starts recording; while `RECORDING` triggers stop+transcribe+paste; while `TRANSCRIBING` returns `{"ok": false, "error": "busy: transcribing"}` and exits 1
- [ ] Error cases:
  - Socket missing → CLI auto-starts the daemon (one attempt: `systemctl --user start local-dictation.service`), retries once, then errors with `"daemon not running; run: systemctl --user start local-dictation"`
  - Stale socket (file exists, no listener) → daemon startup unlinks and recreates; CLI surfaces a clear message if it sees `ConnectionRefusedError`
- [ ] Evidence:
  - `XDG_RUNTIME_DIR=/run/user/1000` is present in user sessions; standard pattern for systemd user services

## Cross-Cutting Concerns

- [ ] Concern: **paste-strategy**
  - Owner: io (Agent C)
  - Applies to:
    - `local_dictation/output.py`
    - `README.md`
  - Decision: Fallback order is fixed and configurable via `paste_method`:
    1. `wtype` if installed and `paste_method ∈ {auto, wtype}` — direct text injection, no clipboard mutation
    2. clipboard via `wl-copy` then synthesise Ctrl+V via `ydotool key 29:1 47:1 47:0 29:0` if `paste_method ∈ {auto, ydotool}` and `ydotoold` reachable
    3. `wl-copy` only (no key synthesis) if `paste_method = clipboard` or all above fail — notifier instructs user to paste manually
  - Evidence: brief paste fallback order; environment shows `wtype` absent and `ydotool` present, so order 2 is the default working path on this machine
- [ ] Concern: **clipboard-restore**
  - Owner: io (Agent C)
  - Applies to:
    - `local_dictation/clipboard.py`
    - `local_dictation/output.py`
  - Decision: When `restore_clipboard = true`, before `wl-copy` we run `wl-paste --no-newline > saved` (text only) and `wl-paste --list-types` to record MIME types. After paste completes, schedule a delayed restore via a detached subprocess that sleeps ~250 ms (to let the target app finish reading) then runs `wl-copy < saved`. Limitations: only the `text/plain;charset=utf-8` selection is restored; image/file selections are lost. README documents this.
  - Evidence: `wl-clipboard` only persists while the source process is alive — a detached restorer is the standard workaround
- [ ] Concern: **hotkey-registration**
  - Owner: io (Agent C — owns README) with config note in core
  - Applies to:
    - `README.md`
    - `install.sh`
  - Decision: The app does **not** attempt to register a global hotkey. `install.sh` prints exact steps to add a custom binding in COSMIC Settings → Input → Keyboard Shortcuts → Custom: command `local-dictation toggle`, key combo Super+Space. README notes that COSMIC's default Super+Space (switch input source) must be unbound first.
  - Evidence: COSMIC Wayland does not expose a stable global-hotkey D-Bus API as of 2026-05; the COSMIC settings daemon owns shortcut dispatch
- [ ] Concern: **doctor-checks**
  - Owner: io (Agent C)
  - Applies to:
    - `local_dictation/doctor.py`
    - `local_dictation/cli.py`
  - Decision: `doctor` returns a list of `Check(name, status, detail)` where status ∈ `{OK, WARN, FAIL}`. Required FAILs → exit code 2. Required checks: Python ≥3.10, `ffmpeg` on PATH, `whisper_cli` exists & executable, `model` exists, `wl-copy` & `wl-paste` on PATH, `XDG_SESSION_TYPE=wayland`, temp dir writable. WARN-only: `wtype` missing, `ydotool` missing, `ydotoold` not running, `notify-send` missing, `systemctl --user` missing, `XDG_CURRENT_DESKTOP=COSMIC` (warn if not — still works).
  - Evidence: brief lists every check; environment scan informs which are pass-by-default on this machine
- [ ] Concern: **logging**
  - Owner: core (Agent A)
  - Applies to: all modules
  - Decision: `logging_setup.configure()` installs a `RotatingFileHandler` at `~/.cache/local-dictation/local-dictation.log` (5 MB × 3) and a stderr handler at INFO. All modules use `logging.getLogger(__name__)`.
  - Evidence: brief specifies that log path
- [ ] Concern: **temp-file lifecycle**
  - Owner: media (Agent B)
  - Applies to: `local_dictation/audio.py`, `local_dictation/transcribe.py`
  - Decision: WAVs named `rec-YYYYMMDD-HHMMSS-<pid>.wav` under `config.temp_dir`. On daemon startup, delete WAVs older than 24 h. Keep the most recent recording until the next successful transcription completes — useful for debugging.
  - Evidence: brief asks for timestamped temp files and clean shutdown
- [ ] Concern: **concurrency / state guard**
  - Owner: core (Agent A)
  - Applies to: `local_dictation/daemon.py`
  - Decision: The daemon serialises all IPC requests through a single `asyncio` task or a `threading.Lock` around the state machine. `TOGGLE` during `TRANSCRIBING` returns `busy`. `TOGGLE` during `RECORDING` is the stop trigger and is allowed.
  - Evidence: brief: "Handle pressing toggle while transcribing", "Avoid concurrent recordings"

## Risks

- [ ] **High**: COSMIC binds Super+Space to "switch input source" by default. If the user does not unbind it first, the dictation command never fires.
  - Mitigation: README has a screenshot-quality step list; `doctor` warns when it detects the default binding still active (best-effort check of `~/.config/cosmic/com.system76.CosmicSettingsDaemon/v1/shortcuts/custom/`)
  - Owner: io
- [ ] **High**: `ydotool` requires `ydotoold` running. On this machine no user service exists yet; if the user does not start it, Ctrl+V synthesis fails silently and we fall back to clipboard-only.
  - Mitigation: `doctor` reports `ydotoold` status; `install.sh` offers to install and enable a `ydotoold.service` user unit; the notifier explicitly says "copied — paste manually (ydotoold not running)" when fallback 3 fires
  - Owner: io
- [ ] **Medium**: Clipboard restore on Wayland is best-effort — restoring complex MIME types is out of scope and the delayed-restore race can clobber a clipboard the user changed in the meantime.
  - Mitigation: Restore only text/plain, document limitation, allow disabling via `restore_clipboard = false`
  - Owner: io
- [ ] **Medium**: ffmpeg PulseAudio backend stalls if the default sink/source changes mid-recording.
  - Mitigation: log `pactl get-default-source` at start; provide `audio_source` config to pin a specific node; SIGINT then SIGTERM with timeouts on stop
  - Owner: media
- [ ] **Medium**: Cold-start latency — whisper-cli loads `ggml-small.bin` (≈465 MB) on every transcription.
  - Mitigation: Documented as a known cost in README; out of scope to add an always-warm whisper server in v1 (would require switching to the streaming whisper.cpp server binary). Open question 3.
  - Owner: lead
- [ ] **Low**: Focus restoration is not attempted. Because the hotkey is routed via COSMIC's shortcut daemon, focus stays on the previous window — but if the user clicks elsewhere between toggle-start and toggle-stop, paste lands in the wrong window.
  - Mitigation: Documented in README; nothing we can do without a Wayland focus-restoration protocol
  - Owner: io
- [ ] **Low**: Daemon dies → next `toggle` hangs.
  - Mitigation: CLI uses a 2 s connect timeout, falls back to `systemctl --user start local-dictation.service`, retries once, then reports the failure
  - Owner: core

## Implementation Phases

### Phase 1: Skeleton and contracts (lead, single-agent)

- [ ] Initialise the repo layout (`pyproject.toml`, `local_dictation/__init__.py`, `.gitignore`, empty module files matching ownership)
  - Action: Create the empty module files so each downstream agent has a stable import path; commit the `Config` dataclass shape and `State` enum so contracts compile against real types.
  - Why: Agents B/C must `from local_dictation.config import Config` from day one to avoid circular guesses.
  - Dependencies: None
  - Risk: Low
  - Agent: lead
  - Evidence: All Affected Files above

- [ ] Implement `local_dictation/config.py` with `Config` dataclass (all keys from the brief) and a `load(path: Path | None) -> Config` function
  - Action: Use `tomllib` to parse; fall back to defaults for missing keys; `~` expansion via `Path.expanduser()`.
  - Why: Every other module reads `Config`.
  - Dependencies: stdlib `tomllib`, `dataclasses`
  - Risk: Low
  - Agent: lead
  - Evidence: Config defaults from the brief

- [ ] Implement `local_dictation/state.py` with `State` enum (`IDLE`, `RECORDING`, `TRANSCRIBING`, `ERROR`)
  - Action: Plain `enum.Enum`.
  - Why: Used by daemon and notifier.
  - Dependencies: stdlib
  - Risk: Low
  - Agent: lead
  - Evidence: Brief lists these states

- [ ] Write `config.example.toml` with every key from the brief
  - Action: Commit alongside README.
  - Why: Reference for users and the install script.
  - Dependencies: None
  - Risk: Low
  - Agent: lead
  - Evidence: Config defaults section of the brief

### Phase 2: media agent (Agent B, parallel with Phase 3 + 4)

- [ ] Implement `Recorder` in `local_dictation/audio.py`
  - Action: `start()` launches `ffmpeg -f pulse -i config.audio_source -ac config.channels -ar config.sample_rate -y <wav>` with stdin pipe (to allow `q` for clean stop) plus SIGINT fallback; tracks PID; `stop()` sends `q\n` then SIGINT, waits 2 s, escalates.
  - Why: Reliable clean stop is the only way to get a valid WAV header.
  - Dependencies: `subprocess`, `signal`
  - Risk: Medium (ffmpeg signal handling can leave zombie processes)
  - Agent: media
  - Evidence: Brief ffmpeg command; PipeWire pulse shim verified

- [ ] Implement `Transcriber.transcribe()` in `local_dictation/transcribe.py`
  - Action: Build argv: `[whisper_cli, "-m", model, "-f", wav, "--no-prints", "-otxt"] + (["-l", lang] if lang != "auto" else [])`. Run with `capture_output=True`, `check=False`; on non-zero rc raise `TranscriberError` with stderr tail. Read `<wav>.txt`, run cleanup, return.
  - Why: This is the only place that knows the language-flag rule.
  - Dependencies: `subprocess`, `pathlib`
  - Risk: Low — exact command is in the brief and verified manually
  - Agent: media
  - Evidence: Brief quotes the exact whisper-cli invocations for `auto` and `da`

- [ ] Implement pure-function `clean_text(s: str) -> str` in `local_dictation/transcribe.py`
  - Action: `s.strip()`, then collapse runs of ≥2 blank lines (regex `r"\n{3,}"` → `"\n\n"`).
  - Why: Brief mandates whitespace cleanup that preserves punctuation.
  - Dependencies: stdlib `re`
  - Risk: Low
  - Agent: media
  - Evidence: Brief: "trim leading/trailing whitespace; collapse excessive blank lines; preserve normal Danish/English punctuation"

- [ ] Add `tests/test_transcribe_cleanup.py` with cases: empty input → empty; preserves `,.!?æøå`; collapses triple newlines; strips leading spaces
  - Action: Pure unit tests, no subprocess.
  - Why: Cleanup is the one place we have logic worth pinning.
  - Dependencies: stdlib `unittest` (no pytest dep needed; works with `python -m unittest`)
  - Risk: Low
  - Agent: media
  - Evidence: Brief cleanup rules

### Phase 3: io agent — output & notifications (Agent C, parallel)

- [ ] Implement `clipboard.py` with `save() -> bytes | None`, `restore(data: bytes) -> None`, `copy(text: str) -> None` using `wl-paste`/`wl-copy` subprocesses
  - Action: `save()` runs `wl-paste -n` and returns bytes; `restore()` spawns a detached `wl-copy` that sleeps 250 ms then sets the saved bytes (use `setsid` and `Popen(close_fds=True)`).
  - Why: Only way to keep clipboard contents on Wayland once our process exits.
  - Dependencies: `subprocess`
  - Risk: Medium (race with user-initiated clipboard changes during the 250 ms window)
  - Agent: io
  - Evidence: clipboard-restore concern

- [ ] Implement `Output.paste()` with the three-step fallback in `output.py`
  - Action: Probe order: (1) `wtype` if `shutil.which("wtype")`; (2) `wl-copy` + `ydotool key 29:1 47:1 47:0 29:0` if `ydotool` available and `ydotoold` socket reachable (`/tmp/.ydotool_socket` or `$YDOTOOL_SOCKET`); (3) `wl-copy` only. Honour `config.paste_method` override. Save+restore clipboard around methods 2 and 3 when `config.restore_clipboard`.
  - Why: Brief fallback order plus environment constraints.
  - Dependencies: `clipboard.py`, `shutil.which`
  - Risk: Medium (ydotoold socket detection)
  - Agent: io
  - Evidence: paste-strategy concern

- [ ] Implement `Notifier.on_state()` in `notify.py`
  - Action: Map states to titles/bodies:
    - `RECORDING` → "Local Dictation", "Recording…" (urgency low, expire 1500)
    - `TRANSCRIBING` → "Local Dictation", "Transcribing…" (urgency low)
    - `IDLE` (post-paste) → "Local Dictation", "Pasted" (urgency low, expire 1500)
    - `ERROR` → "Local Dictation", `detail` (urgency critical)
    Call `notify-send -a local-dictation -u <level> -t <ms> <title> <body>`; ignore failures after logging.
  - Why: Brief lists the four notifications.
  - Dependencies: `subprocess`
  - Risk: Low
  - Agent: io
  - Evidence: Brief status UI requirements

- [ ] Implement `doctor.py` and wire to `cli.py doctor` subcommand later
  - Action: Concrete checks per doctor-checks concern; print table with colour-free output and `[ OK ] / [WARN] / [FAIL]` prefixes; exit 0 if no required FAILs, else 2.
  - Why: Required CLI command.
  - Dependencies: `shutil.which`, `os.environ`
  - Risk: Low
  - Agent: io
  - Evidence: doctor-checks concern

### Phase 4: core agent — daemon, IPC, CLI (Agent A, parallel)

- [ ] Implement `ipc.py` with `serve(handler)` and `send_request(req: dict) -> dict`
  - Action: `AF_UNIX SOCK_STREAM` at `$XDG_RUNTIME_DIR/local-dictation.sock` (fallback `/tmp/local-dictation-$UID.sock`); set `0o600`; one request → one response → close.
  - Why: Trivial, robust, no third-party dep.
  - Dependencies: `socket`, `json`, `selectors`
  - Risk: Low
  - Agent: core
  - Evidence: Toggle-↔-daemon IPC contract

- [ ] Implement `daemon.py` with the state machine
  - Action: Single-threaded loop; on `TOGGLE`:
    - `IDLE` → `RECORDING` (call `Recorder.start()`, fire notification)
    - `RECORDING` → `TRANSCRIBING` (call `Recorder.stop()`, then `Transcriber.transcribe()` in a worker thread; on completion call `Output.paste()`, restore clipboard, transition to `IDLE`)
    - `TRANSCRIBING` → return busy
  - Why: Brief: never two recordings at once; toggle-during-transcribe must be rejected gracefully.
  - Dependencies: `Recorder`, `Transcriber`, `Output`, `Notifier`
  - Risk: Medium (correct error propagation and notification on every path)
  - Agent: core
  - Evidence: concurrency/state-guard concern

- [ ] Implement `cli.py` with `argparse` subparsers: `doctor`, `toggle [--language]`, `start [--language]`, `stop`, `status`, `transcribe FILE [--language]`, plus a hidden `daemon` subcommand that the systemd unit invokes
  - Action: Each user subcommand opens an IPC connection and sends the appropriate JSON; `transcribe FILE` is a synchronous one-shot that bypasses the daemon (creates a `Transcriber` directly and prints to stdout).
  - Why: Brief lists exactly these commands.
  - Dependencies: `ipc.py`, `transcribe.py`
  - Risk: Low
  - Agent: core
  - Evidence: CLI requirements section of the brief

- [ ] Implement `logging_setup.configure(config)` and call from daemon + CLI
  - Action: Per logging concern.
  - Why: Brief mandates log path.
  - Dependencies: `logging.handlers.RotatingFileHandler`
  - Risk: Low
  - Agent: core
  - Evidence: logging concern

### Phase 5: packaging, install, README (Agent C, after Phases 2–4 land)

- [ ] Write `systemd/local-dictation.service`
  - Action:
    ````
    [Unit]
    Description=Local dictation daemon
    After=graphical-session.target
    PartOf=graphical-session.target

    [Service]
    Type=simple
    ExecStart=%h/.local/share/local-dictation/.venv/bin/local-dictation daemon
    Restart=on-failure
    RestartSec=2
    Environment=PYTHONUNBUFFERED=1

    [Install]
    WantedBy=default.target
    ````
  - Why: Auto-start on login.
  - Dependencies: `local-dictation daemon` subcommand from Phase 4
  - Risk: Low
  - Agent: io
  - Evidence: Brief asks for systemd user service

- [ ] Write `install.sh`
  - Action: `set -euo pipefail`; create `~/.local/share/local-dictation/.venv`; `pip install .` from repo root; symlink `~/.local/bin/local-dictation` → venv entrypoint; create `~/.config/local-dictation/config.toml` from `config.example.toml` only if missing; copy `systemd/local-dictation.service` to `~/.config/systemd/user/`; `systemctl --user daemon-reload && systemctl --user enable --now local-dictation.service`; print COSMIC keyboard-shortcut instructions.
  - Why: One-command install is the brief's deliverable.
  - Dependencies: All previous phases
  - Risk: Medium (PATH / venv permissions)
  - Agent: io
  - Evidence: Brief install script section

- [ ] Write `uninstall.sh`
  - Action: `systemctl --user disable --now local-dictation.service` (ignore if missing); remove unit file, symlink, and `~/.local/share/local-dictation/`; **do not** touch `~/.config/local-dictation/` unless `--purge` is passed; never touch whisper.cpp.
  - Why: Brief uninstall script section.
  - Dependencies: same files as install.sh
  - Risk: Low
  - Agent: io
  - Evidence: Brief uninstall section

- [ ] Write `README.md`
  - Action: Sections: Overview, Requirements, Install, COSMIC keyboard shortcut (step-by-step with the exact menu path and the note about default Super+Space being bound to "switch input source"), Configuration, CLI reference, Troubleshooting (ydotoold, clipboard restore, focus race), Uninstall, Limitations on Wayland.
  - Why: Brief deliverable.
  - Dependencies: Final feature set
  - Risk: Low
  - Agent: io
  - Evidence: Brief README requirements

## Agent Validation

### Agent A (core)

- [ ] Run `python -m compileall local_dictation`
  - Expected result: exits 0
  - Evidence: Standard syntax check
- [ ] Run `python -c "from local_dictation.config import load; from local_dictation.state import State; from local_dictation.ipc import send_request"`
  - Expected result: no ImportError
  - Evidence: Phase 1 + Phase 4 modules
- [ ] Run `python -m local_dictation.cli --help`
  - Expected result: lists every subcommand; exits 0
  - Evidence: Phase 4 CLI

### Agent B (media)

- [ ] Run `python -m unittest tests.test_transcribe_cleanup -v`
  - Expected result: all tests pass
  - Evidence: Phase 2 cleanup tests
- [ ] Run `python -c "from local_dictation.audio import Recorder; from local_dictation.transcribe import Transcriber, clean_text"`
  - Expected result: no ImportError
  - Evidence: Phase 2

### Agent C (io)

- [ ] Run `bash -n install.sh && bash -n uninstall.sh`
  - Expected result: no syntax errors
  - Evidence: Phase 5
- [ ] Run `systemd-analyze --user verify systemd/local-dictation.service`
  - Expected result: no errors (warnings about non-existent ExecStart path are acceptable pre-install)
  - Evidence: Phase 5
- [ ] Run `python -c "from local_dictation.output import Output; from local_dictation.notify import Notifier; from local_dictation.doctor import run_checks; from local_dictation.clipboard import copy, save, restore"`
  - Expected result: no ImportError
  - Evidence: Phase 3

## Build Validation

- [ ] Run the project's build command: `python -m pip install -e . --dry-run`
  - Expected: dependency resolution succeeds; `pyproject.toml` declares the console script
- [ ] Run `python -m compileall local_dictation`
  - Expected: all modules compile
- [ ] Run `python -m unittest discover -s tests -q`
  - Expected: all tests pass
- [ ] Confirm the build completes without errors
- [ ] Do not run Playwright, browser automation, agent-browser, Cypress, or E2E validation — none requested

## Success Criteria

- [ ] `local-dictation doctor` on this machine reports all required checks OK and warns only on `wtype` missing (expected)
- [ ] After install, binding `local-dictation toggle` to Super+Space in COSMIC settings: first press fires a "Recording…" notification; second press fires "Transcribing…" then "Pasted" and the transcribed text appears in the previously focused text field
- [ ] `local-dictation transcribe ~/stt-test/test.wav` prints cleaned text and exits 0 without requiring the daemon
- [ ] `local-dictation toggle --language da` overrides the configured default for the next session only
- [ ] Pressing the hotkey during transcription fires an "ERROR: busy" notification, never starts a second recording
- [ ] All contracts in Integration Contracts are satisfied as written
- [ ] All agent-owned validation commands pass
- [ ] Build validation passes

## Final Verification

- [ ] Every checked implementation item corresponds to an actual repository change
- [ ] No required implementation step remains unchecked
- [ ] All file ownership boundaries were respected (lead checks `git log --name-only` per agent branch)
- [ ] All integration contracts were implemented as written or explicitly revised
- [ ] The build completed without errors
- [ ] No browser, Playwright, agent-browser, Cypress, or E2E validation was added unless explicitly requested

## Open Questions

- [ ] **Q1 — ydotoold service:** Do you want `install.sh` to install + enable a `ydotoold.service` user unit, or just `doctor`-warn if it's missing? On this machine `ydotool` is present but no `ydotoold.service` exists. Auto-install means the installer needs sudo for the system-level uinput permissions (already satisfied by your `input` group membership, so a user-level `ydotoold` should work). My recommendation: ship a `systemd/ydotoold.service` user unit and have `install.sh --with-ydotoold` opt in; default is off.
- [ ] **Q2 — `--purge` flag on uninstall:** Should `uninstall.sh --purge` also remove `~/.config/local-dictation/` and `~/.cache/local-dictation/`? My recommendation: yes, gated behind `--purge` only.
- [ ] **Q3 — Always-warm whisper server (out of scope for v1?):** whisper.cpp also ships a `whisper-server` HTTP binary that keeps the model loaded between requests, saving ~1–2 s per dictation. Worth a v2; v1 spawns `whisper-cli` per request to keep complexity down. Confirm v1 scope is acceptable.
- [ ] **Q4 — Optional GTK status window:** The brief lists a small always-on-top window as a fallback to notifications. Do you want it implemented in v1 (gated by `status_window = false` default) or deferred? Pop!_OS ships `python3-gi` so the runtime dep is free, but the code is non-trivial. My recommendation: defer to v2; rely on notify-send for v1.
