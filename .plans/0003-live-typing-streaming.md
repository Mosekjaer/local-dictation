# Implementation Plan: Live Typing As You Speak

Status: draft
Execution state: not started

## Goal

Add a streaming dictation mode where transcribed text appears at the cursor
incrementally while the user is still speaking, rather than the current
batch behaviour where text only appears after release. The default
push-to-talk batch path must keep working unchanged.

## Honest Difficulty Assessment

This is **moderate complexity**, not trivial:

- Whisper was trained on 30 s contexts and is most accurate when given a
  complete utterance. Streaming inherently sacrifices accuracy.
- Streaming whisper revises text from the last 1–3 s as more context
  arrives. If we have already typed it, we must delete-and-retype. The
  current `output.py` is fire-and-forget and has no concept of an
  outstanding cursor.
- We have to feed audio to whisper as it is produced (not via a WAV
  file written on stop), which means a different ffmpeg invocation —
  PCM on stdout instead of WAV on disk — and an in-memory ring buffer.
- VAD is necessary to avoid hallucinations during silence.
- ydotool backspace bursts on Wayland have real-world delivery quirks
  (timing, focus, key-repeat) that need testing.

Quality realistic expectation: latency 700 ms – 2 s per committed token;
accuracy noticeably worse than batch; occasional visible mid-word
corrections.

## Recommended Approach

Use the **already-built `whisper-server` binary** (long-running HTTP) plus
a **sliding-window re-transcription** scheme with **stable-prefix commit**
logic, driven by **VAD** for segmentation. This avoids the need to build
the SDL2-based `stream` example and keeps the heavy lifting in the same
whisper.cpp toolchain the project already depends on.

Pipeline:

1. ffmpeg streams 16 kHz mono PCM s16le on stdout (not a WAV file).
2. Daemon keeps a ring buffer of the last N seconds (e.g. 10 s).
3. Every ~500 ms, the current ring contents are posted to
   `whisper-server` `POST /inference`.
4. The returned text is diffed against the previously-committed prefix.
   Tokens that have remained identical for K consecutive passes are
   "committed" and dispatched to the output layer.
5. VAD: on a detected silence > ~700 ms, the in-flight uncommitted tail
   is force-committed (it will not get any more context anyway).
6. On stop, one final pass with full audio acts as the authoritative
   transcription; any disagreement with already-typed text causes a
   backspace-and-retype of the diff.

## Requirements

- [ ] New live-dictation mode that types incrementally at the cursor
- [ ] Push-to-talk batch mode remains the default and is unchanged
- [ ] A `live_typing` config key or `--live` CLI flag enables streaming
- [ ] The applet's existing meter/state events continue to function
- [ ] New `transcript_partial` and `transcript_final` events are
      broadcast on the events socket
- [ ] No additional system dependencies beyond what is already required,
      except a running `whisper-server` instance owned by the daemon
- [ ] Streaming must degrade gracefully: if `whisper-server` cannot be
      reached, the daemon falls back to current batch behaviour with a
      warning notification

## Assumptions

- [ ] `whisper-server` at `~/stt-bench/whisper.cpp/build-vulkan/bin/whisper-server`
      is the same binary version as `whisper-cli` and accepts the same
      model files. **Verified**: present in the user's build directory.
- [ ] `whisper-server` accepts WAV uploads at `POST /inference` (multipart
      form with `file=@audio.wav`) and returns JSON with a `text` field.
      **Needs verification** — the help output documents flags but not
      the request schema.
- [ ] ydotool `key 14:1 14:0` reliably synthesises a single backspace at
      the focused window when ydotoold is running.
- [ ] ffmpeg can write raw PCM s16le 16 kHz mono to stdout with
      `-f s16le -ar 16000 -ac 1 -` while also keeping the existing
      `astats` meter sidecar functional. **Needs verification** for
      the dual-output filter graph.
- [ ] The VAD model `ggml-silero-v5.1.2.bin` (or equivalent) is
      available alongside the main model. **Needs verification** — the
      user has `whisper-vad-speech-segments` built but the model file
      path is unknown.
- [ ] Latency target of < 2 s from speech-end to text-appears is
      acceptable to the user.
- [ ] The user accepts the accuracy regression vs. batch mode in live
      mode, because batch mode remains available.

## Agent Team Readiness

- [ ] This plan can be executed by an agent team: yes, but a single
      implementer is recommended (see below)
- [ ] Recommended team size: 1 (single agent) or 3 (parallel team)
- [ ] Parallelization strategy: if a team is used, partition along
      module boundaries — streaming engine, incremental output, daemon
      integration — with the events protocol as the shared contract.
- [ ] Integration contracts are explicit enough for parallel work: yes
      for the events protocol and the daemon-to-output interface; the
      whisper-server request shape is an open question.
- [ ] File ownership boundaries are defined to prevent conflicts: yes
- [ ] Validation can be assigned per agent: yes, per-module pytest
      targets plus a daemon-level smoke test

**Recommendation: single-agent implementation.** The codebase is small
(~12 Python files) and the work is sequential by nature — you cannot
test the daemon integration until the streaming engine and incremental
output are both in place. Parallelisation buys little. Use multi-agent
only if the user explicitly wants it.

## Suggested Agent Structure

### Agent: stream-engine

- [ ] Owns:
  - `local_dictation/stream.py` (new) — sliding-window re-transcription engine and whisper-server client
  - `tests/test_stream.py` (new)
- [ ] Does NOT touch:
  - `local_dictation/audio.py` — owned by recorder-live agent
  - `local_dictation/output.py` — owned by output-live agent
  - `local_dictation/daemon.py` — owned by daemon-live agent
- [ ] Responsibilities:
  - Manage `whisper-server` subprocess lifecycle (spawn, health-check, kill on shutdown)
  - Accept PCM chunks from the recorder
  - Maintain ring buffer; periodically POST to `/inference`
  - Run stable-prefix commit algorithm
  - Emit `TranscriptDelta` events to the daemon
- [ ] Produces:
  - `StreamEngine.start()`, `StreamEngine.feed(pcm: bytes)`, `StreamEngine.stop() -> str` (returns final authoritative text)
  - Callback `on_delta(delta: TranscriptDelta)` invoked from worker thread
- [ ] Consumes:
  - PCM frames from `audio.LiveRecorder`
- [ ] Validation:
  - `pytest tests/test_stream.py -q` — unit-tests the commit algorithm with mocked HTTP

### Agent: recorder-live

- [ ] Owns:
  - `local_dictation/audio.py` — extends `Recorder` with `LiveRecorder` that streams PCM on stdout while preserving the meter sidecar
  - `tests/test_audio_live.py` (new)
- [ ] Does NOT touch:
  - `local_dictation/stream.py`
  - `local_dictation/output.py`
  - `local_dictation/daemon.py`
- [ ] Responsibilities:
  - Spawn ffmpeg with PCM-to-stdout output while keeping the existing astats meter pipe
  - Provide a chunk-iterator (or push callback) for PCM frames
  - Clean shutdown on stop
- [ ] Produces:
  - `LiveRecorder` class with `start(on_pcm: Callable[[bytes], None])` and `stop() -> Path` (final WAV path, for the authoritative final pass)
  - During live mode the WAV is still produced from the same PCM stream via `-map 0 -f wav <path>` (tee), so the final-pass transcription can use the entire utterance
- [ ] Consumes:
  - Nothing new — uses existing `Config`
- [ ] Validation:
  - `pytest tests/test_audio_live.py -q`

### Agent: output-live

- [ ] Owns:
  - `local_dictation/output.py` — extends `Output` with `IncrementalTyper`
  - `tests/test_output_live.py` (new)
- [ ] Does NOT touch:
  - `local_dictation/audio.py`, `stream.py`, `daemon.py`
- [ ] Responsibilities:
  - Implement `IncrementalTyper.set_text(new: str)`:
      - Compute longest common prefix with previously-typed text
      - Emit N backspaces for the removed suffix
      - Type the new suffix
  - Use `wtype` / `ydotool type` for appended text, `ydotool key 14:1 14:0` for backspaces
  - Provide `IncrementalTyper.finalize(text: str)` for the end-of-utterance authoritative pass
- [ ] Produces:
  - `IncrementalTyper` class with the above methods
- [ ] Consumes:
  - Existing `Output.paste` strategies as the underlying typing primitive
- [ ] Validation:
  - `pytest tests/test_output_live.py -q` — uses a mock subprocess to verify backspace/append sequences

### Agent: daemon-live (lead)

- [ ] Owns:
  - `local_dictation/daemon.py` — wires new `LIVE_RECORDING` state path
  - `local_dictation/config.py` — adds `live_typing: bool` and tunables
  - `local_dictation/cli.py` — adds `--live` flag on `toggle`/`start`
  - `local_dictation/events.py` — adds `transcript_partial` / `transcript_final` event types
  - `local_dictation/state.py` — adds `LIVE_RECORDING` if needed
  - `config.example.toml` — documents the new keys
  - `README.md` — adds a "Live typing" section with caveats
- [ ] Does NOT touch:
  - The internals of `stream.py`, `LiveRecorder`, `IncrementalTyper`
- [ ] Responsibilities:
  - State machine, glue, dependency injection
  - Owns the `live_typing` cross-cutting concern
  - Final integration tests and README updates
- [ ] Produces:
  - End-to-end live mode
- [ ] Consumes:
  - Contracts from the three module agents
- [ ] Validation:
  - `python -m compileall local_dictation` — build-only
  - `pytest tests -q` — full suite
  - `local-dictation doctor` — exits 0

## Affected Files

- [ ] `local_dictation/stream.py` (new) — sliding-window engine and whisper-server client
- [ ] `local_dictation/audio.py` — add `LiveRecorder` (or a streaming mode flag) alongside existing `Recorder`
- [ ] `local_dictation/output.py` — add `IncrementalTyper`
- [ ] `local_dictation/daemon.py` — new live-mode state path; choose engine based on config
- [ ] `local_dictation/config.py` — new keys: `live_typing`, `live_chunk_ms`, `live_window_s`, `live_stable_passes`, `whisper_server_host`, `whisper_server_port`, `vad_model`
- [ ] `local_dictation/cli.py` — `--live` on `toggle`/`start`
- [ ] `local_dictation/events.py` — `transcript_partial` / `transcript_final` event types
- [ ] `local_dictation/state.py` — possibly add `LIVE_RECORDING`
- [ ] `local_dictation/doctor.py` — check for whisper-server binary, VAD model, ydotool backspace capability
- [ ] `config.example.toml` — document new keys
- [ ] `README.md` — describe live mode and tradeoffs
- [ ] `tests/test_stream.py` (new)
- [ ] `tests/test_audio_live.py` (new)
- [ ] `tests/test_output_live.py` (new)

## Dependencies

- [ ] `whisper-server` binary (already built at `~/stt-bench/whisper.cpp/build-vulkan/bin/whisper-server`)
- [ ] A VAD model — likely `ggml-silero-v5.1.2.bin`; **needs verification** of path
- [ ] Python `httpx` or stdlib `urllib` for the HTTP client (prefer stdlib — no new deps)
- [ ] No new system packages

## Integration Contracts

### Contract: LiveRecorder → StreamEngine

- [ ] Producer: `recorder-live` agent (`local_dictation/audio.py`)
- [ ] Consumer: `stream-engine` agent (`local_dictation/stream.py`)
- [ ] Files involved:
  - `local_dictation/audio.py`
  - `local_dictation/stream.py`
- [ ] Interface type: function callback
- [ ] Contract details:

```python
# audio.py
class LiveRecorder:
    def start(
        self,
        on_pcm: Callable[[bytes], None],
        meter_callback: Callable[[float, float], None] | None = None,
    ) -> Path:
        """Begin recording.

        Calls `on_pcm` from a background thread with raw PCM s16le @ 16 kHz mono,
        in chunks of 320 ms (10240 bytes) or whatever the OS pipe delivers.
        Returns the WAV path that will hold the full recording when stop() is called.
        """

    def stop(self) -> Path:
        """Stop ffmpeg, finalise WAV, return the path."""
```

- [ ] Success cases:
  - `on_pcm` fires continuously while recording
  - `stop()` returns a complete WAV with the full utterance
- [ ] Error cases:
  - ffmpeg spawn failure raises `RecorderError` (existing class)
- [ ] Evidence:
  - Mirrors existing `Recorder` interface in `audio.py:72`

### Contract: StreamEngine → IncrementalTyper

- [ ] Producer: `stream-engine` agent
- [ ] Consumer: `output-live` agent (via the daemon)
- [ ] Files involved:
  - `local_dictation/stream.py`
  - `local_dictation/output.py`
- [ ] Interface type: callback + dataclass
- [ ] Contract details:

```python
# stream.py
@dataclass(frozen=True)
class TranscriptDelta:
    committed_text: str        # full committed prefix (cumulative)
    tentative_text: str        # uncommitted current best-guess tail
    is_final: bool             # True only on stop() after the authoritative final pass

class StreamEngine:
    def start(self, language: str, on_delta: Callable[[TranscriptDelta], None]) -> None: ...
    def feed(self, pcm: bytes) -> None: ...
    def stop(self, final_wav: Path) -> str:
        """Force a final authoritative pass using the full WAV; returns the canonical text."""
```

- [ ] Success cases:
  - `on_delta` fires from a worker thread; consumer must be thread-safe
  - `committed_text` is monotonic: each delta's `committed_text` starts with the previous delta's `committed_text`
  - On final, `committed_text` is the authoritative canonical text and `tentative_text == ""`
- [ ] Error cases:
  - whisper-server HTTP failure: engine logs, retries once, then surfaces via `on_delta` with no change and an event on the events socket
- [ ] Evidence:
  - Designed for this plan; commit-monotonicity is the invariant the typer relies on

### Contract: IncrementalTyper invariants

- [ ] Producer: daemon (calls IncrementalTyper from the StreamEngine callback)
- [ ] Consumer: focused application via wtype/ydotool
- [ ] Files involved:
  - `local_dictation/output.py`
- [ ] Interface type: class
- [ ] Contract details:

```python
# output.py
class IncrementalTyper:
    def __init__(self, output: Output) -> None: ...

    def set_text(self, target: str) -> None:
        """Make the focused window's text-since-start match `target`.

        Computes diff against last `set_text` call; emits N backspaces
        for the changed suffix, then types the new suffix.
        Thread-safe; serialises calls internally.
        """

    def finalize(self, target: str) -> None:
        """Same as set_text but also resets internal state so the next
        live session starts from an empty baseline."""
```

- [ ] Success cases:
  - Visible window text matches the cumulative `target` after each call
- [ ] Error cases:
  - Underlying `Output` raises `OutputError`: the typer surfaces it; the daemon transitions to ERROR
  - Backspace count > 200 (sanity cap): typer logs a warning and clears via a single ydotool keystroke sequence
- [ ] Evidence:
  - The 200-char cap is heuristic; revise based on what a typical Whisper revision span looks like in testing

### Contract: events.py wire protocol

- [ ] Producer: daemon (`events.publish`)
- [ ] Consumer: applet and any other subscriber
- [ ] Files involved:
  - `local_dictation/events.py`
  - `cosmic-applet/local-dictation-applet-core/src/*` (display only; no code change required to remain compatible)
- [ ] Interface type: JSON event over Unix socket
- [ ] Contract details:

```json
{"type":"transcript_partial","committed":"hello wor","tentative":"world how","ts":1747400000.123}
{"type":"transcript_final","text":"hello world how are you","ts":1747400000.987}
```

- [ ] Success cases:
  - Existing subscribers ignore unknown `type` values (verify this is true of the current applet)
- [ ] Error cases:
  - None at protocol level
- [ ] Evidence:
  - Mirrors the existing `state` and `meter` event style at `events.py:9-13`

## Cross-Cutting Concerns

- [ ] Concern: `live_typing` mode selection
  - Owner: daemon-live
  - Applies to: `config.py`, `cli.py`, `daemon.py`
  - Decision: a single boolean `live_typing` in config plus `--live`/`--no-live` CLI override on `toggle`/`start`. When live mode is requested but its preconditions fail (no whisper-server, no VAD model, no ydotoold), the daemon falls back to batch and emits an `error` notification with a one-time hint.
  - Evidence: mirrors how `paste_method=auto` falls through strategies in `output.py:69-144`

- [ ] Concern: whisper-server lifecycle
  - Owner: stream-engine
  - Applies to: `stream.py`, `doctor.py`
  - Decision: the daemon spawns `whisper-server` lazily on the first live-mode use, keeps it alive for the daemon's lifetime, and kills it on shutdown. Host defaults to `127.0.0.1`, port to a config-configurable value (default `38080` to avoid common collisions).
  - Evidence: matches the lazy-init pattern in `daemon.py:51-80`

- [ ] Concern: thread safety of the typer
  - Owner: output-live
  - Applies to: `output.py`
  - Decision: `IncrementalTyper.set_text` takes an internal `threading.Lock` and serialises subprocess calls. The StreamEngine callback fires from a worker thread; the typer must not deadlock with the daemon's main lock.
  - Evidence: matches the worker pattern in `daemon.py:207-215`

- [ ] Concern: ydotool backspace timing
  - Owner: output-live
  - Applies to: `output.py`
  - Decision: backspaces are emitted in a single `ydotool key` call with the full sequence (`14:1 14:0` per backspace). Insert a 5 ms delay between backspaces only if testing shows dropped keystrokes.
  - Evidence: ydotool key syntax in `output.py:40` shows the same approach

- [ ] Concern: notifications during live mode
  - Owner: daemon-live
  - Applies to: `daemon.py`, `notify.py`
  - Decision: do not emit the "transcribing" notification on each chunk. Emit only `recording` at start, `idle` (with `live: X chars`) at finalize, and `error` on failure. This honours the existing `notifications` config knob.
  - Evidence: matches the suppression intent of the `errors_only` mode added in commit `5049951`

- [ ] Concern: WAV retention
  - Owner: recorder-live
  - Applies to: `audio.py`
  - Decision: the final WAV is still produced (tee) so `cleanup_stale_wavs` keeps working; no change to its retention policy.
  - Evidence: `audio.py:410-440`

## Risks

- [ ] **High**: whisper revises text that has already been typed in a different application context (e.g. user has clicked away). Backspaces would delete the wrong text.
  - Mitigation: detect Wayland focus changes if possible (it is not, in general). Practical mitigation: cap the backspace span to N characters and abort live mode if Whisper tries to revise beyond that span — fall back to appending only.
  - Owner: output-live + daemon-live

- [ ] **High**: latency at first commit is dominated by whisper-server's first-pass compute on a cold encoder. May be > 3 s for the first commit, then drop.
  - Mitigation: send a 1 s zero-PCM warm-up request to `whisper-server` when the daemon starts (or when entering live mode for the first time).
  - Owner: stream-engine

- [ ] **Medium**: whisper-server's request/response schema is undocumented in `--help`.
  - Mitigation: spike before implementation — `curl -F file=@sample.wav http://127.0.0.1:38080/inference` and record the response shape. See open questions.
  - Owner: stream-engine (must do the spike first)

- [ ] **Medium**: ffmpeg cannot easily produce both raw PCM on stdout and the existing astats sidecar; the filter graph may need an `asplit` or a `tee` muxer.
  - Mitigation: prototype the ffmpeg invocation before writing the LiveRecorder class.
  - Owner: recorder-live

- [ ] **Medium**: hallucinations during silence ("you", "thanks for watching", "[Music]"). VAD inside whisper-server reduces but does not eliminate this.
  - Mitigation: use `--vad` flag on whisper-server; additionally filter known-hallucination phrases at the stream-engine layer.
  - Owner: stream-engine

- [ ] **Low**: ydotool / wtype may type backspaces in the wrong window if focus changes mid-session.
  - Mitigation: not technically solvable on Wayland. Document the limitation in README under "Known Wayland limitations".
  - Owner: daemon-live (docs)

- [ ] **Low**: the applet may not gracefully ignore the new `transcript_partial` event type.
  - Mitigation: confirm the applet's event handler has a default-case branch. If not, add one before shipping the new event.
  - Owner: daemon-live (verification step in Phase 4)

## Implementation Phases

### Phase 0: Spike (do this before writing any code)

- [ ] Verify `whisper-server` request schema (`local_dictation/stream.py` not yet created)
  - Action: start whisper-server manually, `curl -F file=@/tmp/sample.wav http://127.0.0.1:8080/inference`, record JSON response.
  - Why: the request/response contract is the foundation of `stream.py` and is currently an assumption.
  - Dependencies: a small sample WAV; can synthesise with `ffmpeg -f lavfi -i sine=f=440:d=1 /tmp/sample.wav`.
  - Risk: Low.
  - Agent: stream-engine
  - Evidence: capture command output in a comment at the top of `stream.py`.

- [ ] Verify ffmpeg dual-output filter graph (PCM-on-stdout + astats sidecar) (`local_dictation/audio.py` reference)
  - Action: prototype `ffmpeg -f pulse -i default -ac 1 -ar 16000 -af "asplit=2[a][b];[a]astats=...,ametadata=print:file=/dev/fd/N;[b]anull" -f s16le pipe:1 -y /tmp/x.wav` and confirm both outputs work.
  - Why: validates the recorder-live design.
  - Dependencies: existing astats filter from `audio.py:124-130`.
  - Risk: Medium.
  - Agent: recorder-live
  - Evidence: working command-line in a scratch script.

- [ ] Locate the VAD model file
  - Action: `ls ~/stt-bench/whisper.cpp/models/ | grep -i vad`. If absent, document the download command from whisper.cpp upstream.
  - Why: live mode without VAD will hallucinate badly.
  - Risk: Low.
  - Agent: daemon-live
  - Evidence: path recorded in `config.example.toml`.

### Phase 1: Streaming engine

- [ ] Add `local_dictation/stream.py` (`local_dictation/stream.py`)
  - Action: implement `StreamEngine` with whisper-server subprocess lifecycle, HTTP client (stdlib `urllib.request`), ring buffer, periodic transcription thread, stable-prefix commit logic.
  - Why: core of live mode.
  - Dependencies: Phase 0 spike output.
  - Risk: Medium.
  - Agent: stream-engine
  - Evidence: `tests/test_stream.py` covers (a) prefix-commit with N=2 stable passes, (b) revision detection, (c) graceful HTTP failure fall-through.

- [ ] Add `tests/test_stream.py` (`tests/test_stream.py`)
  - Action: unit-test the commit algorithm with a mocked HTTP client; no real whisper-server needed.
  - Why: the algorithm is the trickiest part and must be testable without GPU/audio.
  - Risk: Low.
  - Agent: stream-engine
  - Evidence: `pytest tests/test_stream.py -q` passes.

### Phase 2: Live recorder

- [ ] Add `LiveRecorder` in `local_dictation/audio.py` (`local_dictation/audio.py`)
  - Action: new class alongside `Recorder`; ffmpeg spawns with the dual-output graph; spawns a background reader thread that calls `on_pcm` with raw PCM frames.
  - Why: live mode needs PCM as it arrives, not after stop.
  - Dependencies: Phase 0 ffmpeg spike.
  - Risk: Medium.
  - Agent: recorder-live
  - Evidence: `tests/test_audio_live.py` runs against a fake ffmpeg binary (a Python script on PATH).

- [ ] Add `tests/test_audio_live.py` (`tests/test_audio_live.py`)
  - Action: stub ffmpeg with a small Python script that writes a known PCM stream; assert `on_pcm` receives all bytes.
  - Risk: Low.
  - Agent: recorder-live
  - Evidence: `pytest tests/test_audio_live.py -q` passes.

### Phase 3: Incremental typer

- [ ] Add `IncrementalTyper` in `local_dictation/output.py` (`local_dictation/output.py`)
  - Action: new class that wraps `Output`; tracks last-typed text; on `set_text(target)` computes longest-common-prefix and emits backspaces + new suffix.
  - Why: turns a stream of best-guess transcripts into smooth incremental typing.
  - Dependencies: existing `Output` class.
  - Risk: Medium (Wayland keystroke timing).
  - Agent: output-live
  - Evidence: `tests/test_output_live.py` uses a fake subprocess runner to capture the sequence of `ydotool`/`wtype` invocations.

- [ ] Add `tests/test_output_live.py` (`tests/test_output_live.py`)
  - Action: parametrise across (append-only, suffix-replace, full-rewrite) cases.
  - Risk: Low.
  - Agent: output-live
  - Evidence: `pytest tests/test_output_live.py -q` passes.

### Phase 4: Daemon integration

- [ ] Add live-mode state path in `local_dictation/daemon.py` (`local_dictation/daemon.py`)
  - Action: on toggle/start with `language` and `live=True`, instantiate `LiveRecorder`, `StreamEngine`, `IncrementalTyper`; wire the recorder's `on_pcm` to the engine's `feed`, the engine's `on_delta` to the typer's `set_text` and to `events.publish({"type":"transcript_partial",...})`. On stop, call `engine.stop(final_wav)` and `typer.finalize(text)`.
  - Why: end-to-end glue.
  - Dependencies: phases 1-3.
  - Risk: Medium.
  - Agent: daemon-live
  - Evidence: smoke-test `local-dictation toggle --live` records a fixed phrase and ends with the correct text in a focused terminal.

- [ ] Add config keys in `local_dictation/config.py` (`local_dictation/config.py`)
  - Action: add `live_typing: bool = False`, `live_chunk_ms: int = 500`, `live_window_s: int = 10`, `live_stable_passes: int = 2`, `whisper_server_host: str = "127.0.0.1"`, `whisper_server_port: int = 38080`, `vad_model: Path | None = None`.
  - Why: tunables and feature flag.
  - Risk: Low.
  - Agent: daemon-live
  - Evidence: `pytest tests/test_config.py -q` (extend existing config test if present, else add).

- [ ] Add `--live`/`--no-live` flag in `local_dictation/cli.py` (`local_dictation/cli.py`)
  - Action: add the flag on `toggle` and `start`; thread it through to the IPC request as `"live": true`.
  - Why: per-invocation override.
  - Risk: Low.
  - Agent: daemon-live
  - Evidence: `local-dictation toggle --live` succeeds; help text mentions the flag.

- [ ] Extend `local_dictation/events.py` (`local_dictation/events.py`)
  - Action: document the new event types in the module docstring; no code change required (publish is generic).
  - Why: keep the wire-protocol doc accurate.
  - Risk: Low.
  - Agent: daemon-live
  - Evidence: docstring updated.

- [ ] Doctor checks in `local_dictation/doctor.py` (`local_dictation/doctor.py`)
  - Action: add an optional (non-required) check for `whisper-server` and the VAD model; warn but do not fail if missing.
  - Why: surfaces misconfiguration early.
  - Risk: Low.
  - Agent: daemon-live
  - Evidence: `local-dictation doctor` lists the new checks.

- [ ] Documentation updates (`README.md`, `config.example.toml`)
  - Action: add a "Live typing" section to README that honestly describes the tradeoffs (lower accuracy, mid-word corrections may be visible, focus-change caveat). Add commented keys to `config.example.toml`.
  - Why: users need to know what they are opting into.
  - Risk: Low.
  - Agent: daemon-live
  - Evidence: rendered README review.

### Phase 5: Verify the applet still works

- [ ] Confirm the applet ignores unknown event types (`cosmic-applet/local-dictation-applet-core/src/*`)
  - Action: read the applet's event-handling code; if it panics or warns loudly on unknown types, add a no-op branch.
  - Why: do not break the applet when broadcasting `transcript_partial`.
  - Dependencies: none.
  - Risk: Low.
  - Agent: daemon-live
  - Evidence: applet builds cleanly (`cd cosmic-applet && cargo build --release`) and live mode does not produce error logs in the applet.

## Agent Validation

### stream-engine

- [ ] Run `pytest tests/test_stream.py -q`
  - Expected result: all tests pass
  - Evidence: green pytest output

### recorder-live

- [ ] Run `pytest tests/test_audio_live.py -q`
  - Expected result: all tests pass
  - Evidence: green pytest output

### output-live

- [ ] Run `pytest tests/test_output_live.py -q`
  - Expected result: all tests pass
  - Evidence: green pytest output

### daemon-live (lead)

- [ ] Run `python -m compileall local_dictation`
  - Expected result: no syntax errors
  - Evidence: exit code 0
- [ ] Run `pytest tests -q`
  - Expected result: all tests pass, including new ones
  - Evidence: green pytest output
- [ ] Run `local-dictation doctor`
  - Expected result: exits 0 (warnings about missing whisper-server/VAD are tolerated)
  - Evidence: exit code 0

## Build Validation

- [ ] Run the project's build command: `python -m compileall local_dictation && pytest tests -q`
- [ ] Confirm the build completes without errors
- [ ] Do not run Playwright, browser automation, agent-browser, Cypress, or E2E validation unless explicitly requested

## Success Criteria

- [ ] `local-dictation toggle --live` causes text to appear at the cursor while the user is still speaking, with < 2 s end-to-end latency on commit
- [ ] On stop, the final visible text matches the authoritative whole-utterance transcription (within whatever revision the typer applies)
- [ ] `local-dictation toggle` without `--live` behaves identically to the current implementation
- [ ] The applet continues to display state and meter correctly during live mode
- [ ] `pytest tests -q` passes
- [ ] `local-dictation doctor` exits 0 on a correctly configured system
- [ ] README clearly documents the accuracy/latency tradeoffs and known limitations of live mode

## Final Verification

- [ ] Every checked implementation item corresponds to an actual repository change
- [ ] No required implementation step remains unchecked
- [ ] All file ownership boundaries were respected
- [ ] All integration contracts were implemented as written or explicitly revised
- [ ] The build completed without errors
- [ ] No browser, Playwright, agent-browser, Cypress, or E2E validation was added unless explicitly requested

## Open Questions

- [ ] **What is the exact whisper-server `POST /inference` request/response schema?** Phase 0 spike must answer this before Phase 1 can start. If the schema differs significantly from what is assumed (e.g. JSON-only, no multipart), the stream.py HTTP client design changes.
- [ ] **Is a Silero VAD model already on disk, or does the user need to download one?** If absent, do we (a) ship a download step in `install.sh`, (b) require manual fetch with instructions in README, or (c) operate without VAD and accept more hallucinations?
- [ ] **What latency/accuracy tradeoff is acceptable to the user?** Suggested defaults: 500 ms chunk interval, 10 s window, K=2 stable passes. These can be tuned post-implementation.
- [ ] **How aggressively should the typer back-correct?** Two options: (1) cap to N (~50) characters of backspace and otherwise let revisions silently diverge from the final text; (2) always re-sync, even if it means erasing a screenful. Recommend (1) with a final authoritative pass at stop.
- [ ] **Should `--live` be a CLI flag, a separate subcommand (`local-dictation live`), or both?** Current plan: a flag on `toggle`/`start`. Open to either alternative.
- [ ] **Should the existing batch mode continue to use the per-spawn `whisper-cli`, or also switch to `whisper-server` for consistency?** Out of scope for this plan; would be a follow-up.
- [ ] **Does the user actually want this feature?** This plan was generated in response to a "is it possible" question. Confirm before any code is written.
