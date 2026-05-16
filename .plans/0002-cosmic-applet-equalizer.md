# Implementation Plan: COSMIC panel applet — local-dictation equalizer

Status: draft
Execution state: not started

## Goal

Build a COSMIC panel applet (`local-dictation-applet`) that visualises the state of the existing `local-dictation` daemon as a small equalizer widget in the COSMIC panel. The applet reflects four states — `idle`, `recording` (with live audio bars), `transcribing` (pulsing animation), `error` — and is intended to sit in the panel center group, with two instances flanking the datetime applet.

The applet is a separate Rust crate that depends on libcosmic and Iced. It talks to the existing Python daemon via a new push-style Unix socket. Audio levels are produced by adding an `astats` filter to the existing ffmpeg recorder; no new audio backend.

## Requirements

- [ ] New Rust workspace `cosmic-applet/` inside this repo with one binary crate `local-dictation-applet`
- [ ] Applet renders an equalizer (N bars, configurable, default 7) that animates per state
- [ ] Live bar heights driven by audio peak+RMS dB values streamed from the daemon at ≥10 Hz during recording
- [ ] Idle: bars collapse to a thin baseline
- [ ] Recording: bars driven by real-time peak/RMS
- [ ] Transcribing: bars pulse with a fixed sinusoidal waveform
- [ ] Error: bars flash red once, then idle
- [ ] Daemon exposes a new push-style Unix socket at `$XDG_RUNTIME_DIR/local-dictation-events.sock` (`0600`) emitting newline-delimited JSON events (state, meter)
- [ ] Daemon's existing request/response socket (`local-dictation.sock`) is **unchanged**
- [ ] Recorder produces RMS+peak metrics during recording without writing a second WAV stream
- [ ] Applet survives daemon restart: reconnects with backoff, shows `idle` (or `error` if the daemon was down >1 s)
- [ ] Two applet instances can sit on the panel — either as two slots in the panel config OR as one applet drawing both sides of the datetime (decision deferred to Phase 4)
- [ ] `cargo install --path cosmic-applet/local-dictation-applet --root ~/.local` installs the binary
- [ ] An XDG `.desktop` file + COSMIC applet manifest let the panel discover and place the applet
- [ ] No changes to existing local-dictation success criteria; the daemon must run with or without the applet listening

## Assumptions

- [ ] Pop!_OS COSMIC alpha (≥ March 2026) is the only supported target; the libcosmic API may break between COSMIC releases. Verified locally: `cargo 1.95.0`, `rustc 1.95.0`. Verify by `cosmic-comp --version` against the libcosmic git rev pinned in Cargo.toml.
- [ ] libcosmic is consumed from git (no crates.io release as of 2026-05). Pinning to a specific commit avoids accidental breakage on `cargo update`.
- [ ] COSMIC panel discovers user applets via XDG_DATA_DIRS lookup of `applications/` `.desktop` files plus a COSMIC-specific applet manifest. Verify by reading `~/.config/cosmic/com.system76.CosmicPanel.Panel/v1/` and the `cosmic-applets` package layout on the system.
- [ ] The applet runs as a normal user process. No root, no uinput, no setuid.
- [ ] ffmpeg's `astats` audio filter is available in the system ffmpeg (verified: `ffmpeg -filters | grep astats` should list it on Pop!_OS 24.04).
- [ ] The daemon process is the sole producer of meter events; subscribers are read-only.

## Agent Team Readiness

- [ ] Recommended team size: **2 agents + 1 lead**.
- [ ] Parallelisation: Agent A handles **all Python daemon changes** (audio metering, event broadcast, IPC fan-out). Agent B handles **the Rust applet crate** (UI, socket subscriber, packaging). They share a single broadcast-protocol contract defined upfront by the lead.
- [ ] Phase 1 (broadcast protocol contract + Cargo workspace skeleton) is single-agent (lead). Phase 2 (Python) and Phase 3 (Rust) run in parallel. Phase 4 (panel integration + install) bridges both.
- [ ] Integration contract is explicit: socket path, JSON event shape, reconnect semantics.
- [ ] File ownership boundaries are disjoint (no cross-editing of Python and Rust trees).

## Suggested Agent Structure

### Agent: daemon (Agent A)

- [ ] Owns:
  - `local_dictation/audio.py` — add astats metric extraction
  - `local_dictation/events.py` — **new**, broadcast socket server + event queue
  - `local_dictation/daemon.py` — wire events into the state machine
  - `local_dictation/config.py` — add `events_socket_path` field
  - `config.example.toml` — document the new key
  - `tests/test_events_protocol.py` — **new**, unit tests for the event encoder
- [ ] Does NOT touch: anything under `cosmic-applet/`
- [ ] Responsibilities:
  - Emit `state` events on every state transition
  - Emit `meter` events at 10 Hz while recording (or as ffmpeg's astats emit, whichever is sparser)
  - Accept multiple subscribers, drop slow ones rather than block recording
- [ ] Produces: broadcast protocol (see Integration Contracts)
- [ ] Validation:
  - `python -m unittest tests.test_events_protocol -v`
  - `python -m compileall local_dictation`
  - `nc -U $XDG_RUNTIME_DIR/local-dictation-events.sock` while toggling — prints one JSON line per event

### Agent: applet (Agent B)

- [ ] Owns:
  - `cosmic-applet/Cargo.toml` — workspace manifest
  - `cosmic-applet/local-dictation-applet/Cargo.toml`
  - `cosmic-applet/local-dictation-applet/src/main.rs`
  - `cosmic-applet/local-dictation-applet/src/subscriber.rs` — Unix-socket reader
  - `cosmic-applet/local-dictation-applet/src/equalizer.rs` — bar widget
  - `cosmic-applet/local-dictation-applet/src/state.rs` — Iced app state
  - `cosmic-applet/local-dictation-applet/resources/com.frederik.LocalDictationApplet.desktop`
  - `cosmic-applet/local-dictation-applet/resources/com.frederik.LocalDictationApplet.applet.ron` (or whatever COSMIC's manifest format settles on)
- [ ] Does NOT touch: anything under `local_dictation/`
- [ ] Responsibilities:
  - Subscribe to the broadcast socket, decode JSON events
  - Iced app rendering N bars with smoothing
  - Reconnect on EOF / refused with capped exponential backoff
  - Theme integration via libcosmic's theme API (light/dark follow)
- [ ] Produces: a runnable applet binary + installable manifest
- [ ] Validation:
  - `cargo build --release` from workspace root
  - `cargo test` in the applet crate
  - `cargo run --release` outside the panel — opens a standalone window showing the equalizer; toggling the daemon visibly drives the bars

## Affected Files

- [ ] `local_dictation/audio.py` — add `astats` filter, parse RMS/peak from ffmpeg stderr in a reader thread, expose a `meter_callback`
- [ ] `local_dictation/events.py` — **new**, broadcast server: accept loop, per-subscriber send queue, drop on slow consumer
- [ ] `local_dictation/daemon.py` — start/stop the broadcast server; fire `state` events on every transition; forward meter callbacks
- [ ] `local_dictation/config.py` — add `events_socket_path: Path` (default `$XDG_RUNTIME_DIR/local-dictation-events.sock`)
- [ ] `config.example.toml` — document `events_socket_path`
- [ ] `tests/test_events_protocol.py` — **new**, encoder/decoder roundtrip, slow-consumer drop policy
- [ ] `cosmic-applet/Cargo.toml` — workspace
- [ ] `cosmic-applet/local-dictation-applet/Cargo.toml` — applet crate
- [ ] `cosmic-applet/local-dictation-applet/src/main.rs` — entry point, libcosmic applet bootstrap
- [ ] `cosmic-applet/local-dictation-applet/src/subscriber.rs` — Unix socket reader, JSON decode, channel to UI
- [ ] `cosmic-applet/local-dictation-applet/src/equalizer.rs` — bar widget, smoothing, idle/recording/transcribing/error visual states
- [ ] `cosmic-applet/local-dictation-applet/src/state.rs` — Iced app `Model` / `Message` types
- [ ] `cosmic-applet/local-dictation-applet/resources/*.desktop` — XDG launcher entry
- [ ] `cosmic-applet/local-dictation-applet/resources/*.applet.ron` — COSMIC applet manifest (format TBD per Q4)
- [ ] `README.md` — new section: "Optional: COSMIC panel applet"
- [ ] `install.sh` — extend to optionally build + install the applet (gated behind `--with-applet`)

## Dependencies

- [ ] Rust toolchain ≥ 1.80 (verified: 1.95.0 on this machine)
- [ ] `libcosmic` git rev (pin in Cargo.toml; see Q5)
- [ ] `iced` (transitively via libcosmic)
- [ ] `serde` + `serde_json` for event decoding
- [ ] `tokio` (single-threaded runtime) for the async socket subscriber
- [ ] ffmpeg with the `astats` filter compiled in (verified default on Pop!_OS 24.04)
- [ ] **No new Python deps.** Stdlib `socket`, `selectors`, `threading`, `json`, `queue`.

## Integration Contracts

### Contract: daemon → applet (broadcast events)

- [ ] Producer: daemon (Agent A)
- [ ] Consumer: applet (Agent B)
- [ ] Files involved:
  - `local_dictation/events.py` (server)
  - `cosmic-applet/local-dictation-applet/src/subscriber.rs` (client)
- [ ] Interface type: Unix domain socket, server-push, newline-delimited UTF-8 JSON

```
Socket path: $XDG_RUNTIME_DIR/local-dictation-events.sock  (configurable)
Permissions: 0600
Protocol: subscriber opens a connection. Server pushes one JSON object per
line until the subscriber closes the connection. There is no request from
the subscriber; it reads only.

Events:
  {"type":"state","state":"idle"|"recording"|"transcribing"|"error","ts":<unix_seconds_float>}
  {"type":"meter","peak_db":<float>,"rms_db":<float>,"ts":<unix_seconds_float>}
  {"type":"hello","version":1,"daemon_state":"idle"|"recording"|"transcribing"|"error","ts":<...>}

On connect, server immediately sends one "hello" event with the current
daemon state so a fresh subscriber does not need to wait for the next
transition.

Slow subscribers: each subscriber has a bounded send queue (default 64
events). Once full, further events for that subscriber are dropped;
the subscriber is disconnected after 5 consecutive drops.

peak_db / rms_db semantics: dBFS, range -inf to 0. -inf is encoded as
the JSON number -120 (clamped) so consumers don't need to parse Infinity.
```

- [ ] Success cases:
  - Subscriber sees `hello` within 100 ms of connect
  - During recording, ≥ 8 meter events per second arrive
  - State events fire within 50 ms of the daemon's internal transition
- [ ] Error cases:
  - Socket missing → applet retries with exponential backoff (200 ms, 400 ms, … capped at 5 s); shows `idle` after the first failed retry and `error` after 5 s without a connect
  - Slow subscriber → server drops events, disconnects after 5 dropped events in a row; applet sees EOF and reconnects
- [ ] Evidence:
  - `local_dictation/ipc.py` already uses Unix sockets; same pattern is reused
  - astats periodicity is set via the filter's `reset` arg; 0.1 s gives 10 Hz

### Contract: recorder → events (in-process)

- [ ] Producer: `audio.Recorder` (Agent A)
- [ ] Consumer: `daemon._Daemon` (Agent A)
- [ ] Files involved:
  - `local_dictation/audio.py`
  - `local_dictation/daemon.py`
- [ ] Interface type: Python callback

```python
# audio.py
class Recorder:
    def __init__(self, config: Config, meter_callback: Callable[[float, float], None] | None = None): ...
    # meter_callback is invoked from a reader thread with (peak_db, rms_db).
    # It must never raise. The daemon supplies a callback that fans out
    # to the events server.
```

- [ ] Success cases:
  - Callback fires at the astats reset rate (~10 Hz) while recording
  - Callback is not invoked while idle or during transcription
- [ ] Error cases:
  - astats output is unparseable → log + skip that line; do not crash recording
  - Callback raises → caught, logged, callback disabled for the rest of this recording
- [ ] Evidence: ffmpeg astats metadata key names are documented and stable

## Cross-Cutting Concerns

- [ ] Concern: **libcosmic-version-pinning** (Q5)
  - Owner: applet (Agent B) + lead
  - Decision: pin `libcosmic` to a specific git rev in `Cargo.toml`. Document the rev and the corresponding COSMIC release in the README. Bump only when explicitly requested.
  - Evidence: libcosmic has no crates.io release as of 2026-05; git-rev pinning is the only stable option

- [ ] Concern: **audio-metering-source** (Q2)
  - Owner: daemon (Agent A)
  - Decision: **ffmpeg `astats` via `ametadata=print` to a separate pipe** — keep the existing `-y <wav>` output untouched, add `-filter_complex` that splits the stream and prints `lavfi.astats.Overall.{Peak_level,RMS_level}` to fd 3 (or stderr if fd 3 is not usable). Recorder reads that pipe in a daemon thread and calls the meter callback.
  - Why this over alternatives:
    - PipeWire-native (python-pipewire / sounddevice) — large new dep, rewrites the recorder for v1 audio levels alone; rejected
    - tee + second ffmpeg — doubles process count, complicates lifecycle; rejected
    - astats on stderr — works but stderr is already used for error reporting; using a numbered fd keeps the channels separate
  - Evidence: ffmpeg `astats` filter exists since 2014, well-supported on Pop!_OS

- [ ] Concern: **slow-subscriber-policy**
  - Owner: daemon (Agent A)
  - Decision: per-subscriber bounded queue (`queue.Queue(maxsize=64)`). Sender thread does `put_nowait`; on `Full`, increments a per-subscriber drop counter. ≥5 consecutive drops → disconnect the subscriber. The daemon never blocks recording on a slow applet.
  - Evidence: this matches the constraint "applet must not affect dictation behaviour"

- [ ] Concern: **two-applet-instances** (Q4)
  - Owner: applet (Agent B) + lead
  - Decision: defer to Phase 4; first attempt is to add two entries with distinct applet IDs to the COSMIC panel config. If COSMIC's applet runtime rejects two instances of the same binary, fall back to a single applet that renders two equalizers as one wide widget (the panel still places it in one slot, but the visual sits across both sides). The fallback is purely a UI rearrangement, no protocol change.
  - Evidence: COSMIC panel config (`com.system76.CosmicPanel.Panel/v1/`) is a plain RON/TOML file listing applet entries; two entries with the same applet name may or may not spawn two processes. Confirm before designing around it.

- [ ] Concern: **idempotent-install**
  - Owner: lead
  - Decision: `install.sh --with-applet` runs `cargo install --path cosmic-applet/local-dictation-applet --root ~/.local`, copies the `.desktop` file to `~/.local/share/applications/`, copies the applet manifest to `~/.local/share/cosmic/applets/` (or wherever COSMIC's discovery path settles), and prints instructions to add the applet via COSMIC Settings. Uninstall removes all three artefacts.
  - Evidence: standard XDG paths; `cargo install` is idempotent

## Risks

- [ ] **High**: libcosmic API churn breaks the applet on the next COSMIC update.
  - Mitigation: pin libcosmic to a known-good git rev; document the rev in the README; CI is out of scope but a manual rebuild check after every COSMIC release is.
  - Owner: applet

- [ ] **Medium**: COSMIC's applet runtime does not allow two instances of the same applet binary.
  - Mitigation: Phase 4 experiment confirms this first. Fallback design (one applet drawing both sides) is sketched but not implemented unless needed.
  - Owner: applet + lead

- [ ] **Medium**: ffmpeg `astats` output rate is not exactly 10 Hz; it depends on input frame size. The applet's smoothing must tolerate jitter.
  - Mitigation: applet uses an exponential moving average with a time-constant tuned for ~5–15 Hz input. Plan validates the actual event rate.
  - Owner: daemon + applet

- [ ] **Medium**: Slow subscriber blocks the recording path if the queue is unbounded.
  - Mitigation: per-subscriber bounded queue + drop policy specified in Cross-Cutting Concerns
  - Owner: daemon

- [ ] **Low**: The applet starts before the daemon does on login.
  - Mitigation: subscriber backoff loop handles this naturally. Initial state shown is `idle`.
  - Owner: applet

- [ ] **Low**: Two applet processes consume meaningful CPU painting at 60 fps.
  - Mitigation: cap the redraw to 30 fps; only redraw on event arrival or animation tick.
  - Owner: applet

## Implementation Phases

### Phase 1: protocol + workspace skeleton (lead, single-agent)

- [ ] Lock the broadcast event JSON shape (state / meter / hello) (`README.md` or new doc inline in plan)
  - Action: Write the contract into both `local_dictation/events.py` module docstring (Phase 2) and into a Rust `events.rs` doc-comment (Phase 3) so both sides have the same source of truth.
  - Why: parallel agents need a single canonical shape.
  - Dependencies: none
  - Risk: Low
  - Evidence: existing Integration Contracts section

- [ ] Create `cosmic-applet/` directory and Cargo workspace manifest with one member crate
  - Action: `cosmic-applet/Cargo.toml` lists `local-dictation-applet` as a member. The member crate has a stub `main.rs` that compiles to a no-op binary.
  - Why: gives Agent B a stable build target to iterate on.
  - Dependencies: rustc/cargo on PATH
  - Risk: Low
  - Evidence: cargo verified at /usr/bin/cargo 1.95.0

- [ ] Add `events_socket_path: Path` to `Config` with the default `$XDG_RUNTIME_DIR/local-dictation-events.sock`
  - Action: extend the dataclass + TOML loader.
  - Why: agents reference this from day one.
  - Dependencies: existing `config.py`
  - Risk: Low
  - Evidence: existing `socket_path` field follows the same pattern

### Phase 2: daemon broadcast + audio metering (Agent A, parallel with Phase 3)

- [ ] Extend `audio.Recorder` to accept an optional `meter_callback: Callable[[float, float], None]` and to spawn ffmpeg with `astats` printing to a numbered pipe (`pass_fds=(3,)`, `-progress pipe:3` is for progress; for astats use `-filter_complex ... ametadata=print:file=/dev/fd/3`).
  - Action: build the filter graph; spawn a daemon `threading.Thread` that reads lines from fd 3, parses `lavfi.astats.Overall.RMS_level=<float>` and `Peak_level`, invokes the callback. Suppress callback if it raises.
  - Why: this is the only way to get live levels without a second audio capture process.
  - Dependencies: ffmpeg with astats filter
  - Risk: Medium (filter syntax fragile; verify against `ffmpeg -filters`)
  - Evidence: astats with `ametadata=print` is the documented pattern for level metering

- [ ] Implement `local_dictation/events.py` — broadcast server with multi-subscriber accept loop, per-subscriber bounded queue, slow-consumer drop policy, JSON encoder
  - Action: Threading model: one accept thread, one sender thread per subscriber. Producer (daemon) pushes events via a single `publish(event: dict)` function; the publisher fans out to each subscriber's queue with `put_nowait`. On `Full`, increment that subscriber's drop counter; on >5 consecutive drops, close the connection.
  - Why: decouples producer (recorder/daemon) from slow consumers.
  - Dependencies: stdlib `socket`, `threading`, `queue`, `selectors`
  - Risk: Medium (correct shutdown of sender threads on daemon SIGTERM)
  - Evidence: existing `ipc.py` shows the daemon's Unix-socket and shutdown patterns

- [ ] Wire events into `daemon._Daemon`: start `events.serve()` in a thread on `run()`, fire `publish({"type":"state",...})` on every state transition, register a meter callback on the Recorder that calls `publish({"type":"meter",...})`
  - Action: lazy-construct the events server alongside `_ensure_components`. Publish "hello" only from inside the events server on new connection (using a `get_state()` callable supplied by the daemon).
  - Why: events must follow the same source of truth as the existing `notifier.on_state` path; both fire in the same critical section.
  - Dependencies: previous two steps
  - Risk: Medium (race between transition and publish must not invert order)
  - Evidence: existing `_notify` pattern in daemon.py:66

- [ ] Add `tests/test_events_protocol.py` (`unittest`) covering: encoder produces expected JSON, slow-consumer drop counter increments, "hello" event includes the current state, peak_db -inf is clamped to -120
  - Action: pure-function tests on the encoder + in-process subscriber tests using a `socketpair()`.
  - Why: protocol shape must not regress.
  - Dependencies: events.py
  - Risk: Low
  - Evidence: matches the contract above

- [ ] Update `local-dictation doctor` to add a `events-socket-path-writable` WARN-only check (parent dir writable)
  - Action: minor `doctor.py` extension.
  - Why: surface misconfigured `events_socket_path` before launch.
  - Dependencies: events.py
  - Risk: Low
  - Evidence: existing doctor patterns

### Phase 3: Rust applet (Agent B, parallel with Phase 2)

- [ ] Author `cosmic-applet/local-dictation-applet/Cargo.toml` with libcosmic, iced, serde_json, tokio (single-threaded), serde, anyhow
  - Action: pin libcosmic to a specific git rev (see Q5); list features needed for applets
  - Why: deterministic build
  - Dependencies: Phase 1 workspace
  - Risk: Medium (libcosmic feature flags are alpha; expect trial)
  - Evidence: libcosmic README "Building" section

- [ ] Implement `subscriber.rs` — async task that connects to the broadcast socket, decodes newline-delimited JSON, forwards `Event` enum values via a tokio `mpsc::channel` to the Iced runtime; reconnects with exponential backoff (200 ms → 5 s cap)
  - Action: `tokio::net::UnixStream`, `tokio::io::BufReader::lines()`, `serde_json::from_str`.
  - Why: this is the only inbound channel from the daemon
  - Dependencies: events protocol locked in Phase 1
  - Risk: Low
  - Evidence: protocol is plain JSON

- [ ] Implement `equalizer.rs` — Iced `Widget` rendering N bars with smoothing
  - Action: state machine: idle (baseline), recording (peak/RMS-driven), transcribing (sinusoidal pulse), error (single red flash then idle). Smoothing: per-bar EMA with α = 0.4. Bars are randomly assigned a frequency band so the equalizer looks alive even with a single peak value (split RMS across bars with a small per-bar noise pattern, weighted by the central RMS energy).
  - Why: visual centrepiece of the applet
  - Dependencies: state machine wiring
  - Risk: Medium (Iced's custom-widget API has changed between versions; verify against the pinned libcosmic)
  - Evidence: Iced docs for custom widgets

- [ ] Implement `state.rs` + `main.rs` — Iced `Application` model, `Message::EventReceived`, redraw on event or animation tick at 30 fps
  - Action: standard libcosmic `cosmic::applet::run::<MyApplet>()` entry.
  - Why: ties everything together
  - Dependencies: subscriber + equalizer
  - Risk: Medium (libcosmic applet boilerplate is alpha)
  - Evidence: libcosmic examples directory

- [ ] Add `cargo test` cases for the subscriber's JSON decoder and the equalizer's smoothing function (pure logic only — no UI tests)
  - Action: write fixture JSON lines, decode, assert the resulting `Event` variants
  - Why: catches protocol drift early
  - Dependencies: subscriber.rs
  - Risk: Low
  - Evidence: standard rust test pattern

### Phase 4: panel integration + install (applet agent + lead, after Phase 2 & 3)

- [ ] Author the COSMIC applet manifest (`resources/com.frederik.LocalDictationApplet.applet.ron` or current format) + XDG `.desktop` file
  - Action: read an existing COSMIC system applet manifest as a template; copy and rename.
  - Why: required for COSMIC panel discovery
  - Dependencies: Phase 3 binary
  - Risk: Medium (the exact format may change; reference the local COSMIC install)
  - Evidence: `/usr/share/cosmic/applets/` on this machine should contain templates

- [ ] Experiment: try adding two entries with the same applet ID to `~/.config/cosmic/com.system76.CosmicPanel.Panel/v1/`; verify whether COSMIC spawns two processes and renders two widgets
  - Action: edit the panel config, restart `cosmic-panel`, observe.
  - Why: confirms whether two-instance design works
  - Dependencies: Phase 3 binary installed
  - Risk: Medium (if it fails, fall back to the single-widget design — see Cross-Cutting Concerns)
  - Evidence: Q4

- [ ] Extend `install.sh` with `--with-applet` flag: `cargo install --path cosmic-applet/local-dictation-applet --root $HOME/.local`, copy `.desktop` to `~/.local/share/applications/`, copy manifest to `~/.local/share/cosmic/applets/`, print the COSMIC Settings instructions
  - Action: shell glue, no rust changes.
  - Why: one-command optional install
  - Dependencies: Phase 3 binary
  - Risk: Low
  - Evidence: existing `install.sh` patterns

- [ ] Extend `uninstall.sh` to remove `~/.local/bin/local-dictation-applet`, the `.desktop`, and the applet manifest. With `--purge` also remove `~/.cache/local-dictation-applet/` if it exists.
  - Action: parallel to existing uninstall logic
  - Why: clean removal
  - Dependencies: install logic
  - Risk: Low
  - Evidence: existing `uninstall.sh`

- [ ] README: add an "Optional: COSMIC panel applet" section with screenshots/placement instructions
  - Action: brief copy describing the two-instance layout and how to enable
  - Why: discoverability
  - Dependencies: Phase 4 complete
  - Risk: Low
  - Evidence: existing README structure

## Agent Validation

### Agent A (daemon)

- [ ] `cd /home/frederik/Documents/dev/stt && python3 -m compileall -q local_dictation`
  - Expected: exit 0
  - Evidence: standard syntax check

- [ ] `cd /home/frederik/Documents/dev/stt && python3 -m unittest discover -s tests -q`
  - Expected: all tests pass (existing + new events tests)
  - Evidence: Phase 2 tests

- [ ] Manual: start daemon, run `nc -U $XDG_RUNTIME_DIR/local-dictation-events.sock`, then `local-dictation toggle` twice
  - Expected: `hello`, `state=recording`, ≥8 meter events per second, `state=transcribing`, `state=idle`
  - Evidence: Phase 2 events implementation

### Agent B (applet)

- [ ] `cd /home/frederik/Documents/dev/stt/cosmic-applet && cargo build --release`
  - Expected: exit 0, produces `target/release/local-dictation-applet`
  - Evidence: Phase 3 crate

- [ ] `cd /home/frederik/Documents/dev/stt/cosmic-applet && cargo test`
  - Expected: subscriber + smoothing tests pass
  - Evidence: Phase 3 tests

- [ ] Manual: `./target/release/local-dictation-applet --standalone` (a dev mode that opens a normal window instead of running as a panel applet)
  - Expected: window opens; running `local-dictation toggle` against a live daemon visibly drives the bars
  - Evidence: Phase 3 wiring

## Build Validation

- [ ] `cd /home/frederik/Documents/dev/stt && python3 -m compileall -q local_dictation`
- [ ] `cd /home/frederik/Documents/dev/stt && python3 -m unittest discover -s tests -q`
- [ ] `cd /home/frederik/Documents/dev/stt && python3 -m pip install -e . --dry-run` inside a fresh venv (matches Plan 0001 build check)
- [ ] `cd /home/frederik/Documents/dev/stt/cosmic-applet && cargo build --release`
- [ ] `cd /home/frederik/Documents/dev/stt/cosmic-applet && cargo test --quiet`
- [ ] `bash -n install.sh && bash -n uninstall.sh`
- [ ] `systemd-analyze --user verify systemd/local-dictation.service systemd/ydotoold.service` (no change expected; just confirm Plan 0001 still passes)
- [ ] Do not run Playwright, browser automation, or E2E validation — none requested

## Success Criteria

- [ ] After `./install.sh --with-applet` and configuring the COSMIC panel, two equalizer widgets appear flanking the datetime applet
- [ ] Pressing the dictation hotkey causes both equalizers to show live bars within 100 ms
- [ ] On release / second toggle, equalizers switch to the transcribing pulse, then back to idle when the text lands
- [ ] Killing and restarting the daemon while the applet is up: applet briefly shows idle/error, then reconnects automatically and resumes
- [ ] CPU usage of one applet process is < 1 % at idle on this machine
- [ ] All Plan 0001 success criteria still pass (the applet must not regress existing functionality)
- [ ] All agent-owned validation commands pass
- [ ] Build validation passes

## Final Verification

- [ ] Every checked implementation item corresponds to an actual repository change
- [ ] No required implementation step remains unchecked
- [ ] All file ownership boundaries were respected (lead checks `git diff` against agent scope)
- [ ] All integration contracts were implemented as written or explicitly revised
- [ ] The build completed without errors
- [ ] No browser, Playwright, agent-browser, Cypress, or E2E validation was added unless explicitly requested

## Open Questions

- [ ] **Q1 — broadcast transport (D-Bus vs Unix socket).** This plan picks **Unix socket** for parity with the existing IPC, no D-Bus name to claim, and trivial Rust consumption via tokio. Confirm or override before Phase 2 starts. D-Bus would buy automatic activation but adds a `dbus-python` (or `jeepney`) dep and a session-name registration concern.

- [ ] **Q2 — audio metering source.** Plan picks **ffmpeg `astats` on a numbered pipe** over PipeWire-native capture or a tee'd second ffmpeg. Confirm. Switching to PipeWire-native is plausible later for lower latency but is a full v2 rewrite of the recorder.

- [ ] **Q3 — bar count and refresh rate.** Plan picks **7 bars at 30 fps applet redraw, driven by 10 Hz meter events.** Open to 5/9/11 bars and to 60 fps if CPU budget allows. Choose before Phase 3 finalises `equalizer.rs`.

- [ ] **Q4 — two-applet-instances vs single-wide-applet.** Plan **tries two instances first** as a Phase 4 experiment; if COSMIC's applet runtime refuses, fall back to a single applet that renders both sides. Need to read `/usr/share/cosmic/applets/` and `~/.config/cosmic/com.system76.CosmicPanel.Panel/v1/` to confirm the panel config supports listing the same applet ID twice. If unknown, accept the risk and proceed.

- [ ] **Q5 — libcosmic version pinning.** Plan pins to a specific git rev in `Cargo.toml`. Confirm: (a) which rev (latest tag on `main` at the start of Phase 3, recorded in the README), and (b) whether to also pin the libcosmic toolchain via `rust-toolchain.toml`. The latter is recommended given alpha churn.
