"""Long-running daemon: owns the state machine, drives Recorder/Transcriber/Output.

A single thread owns the IPC server. Each transcription runs on a short-lived
worker thread so the IPC loop stays responsive — toggling while already
transcribing is rejected, not queued.

State transitions::

    IDLE         -> RECORDING       on toggle/start
    RECORDING    -> TRANSCRIBING    on toggle/stop, worker thread spawned
    TRANSCRIBING -> IDLE            when worker thread finishes
    *            -> ERROR -> IDLE   on worker exception
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from pathlib import Path
from typing import Any, Optional

from . import audio, ipc, logging_setup
from .config import Config
from .events import EventsServer
from .state import State
from .stream import TranscriptDelta

log = logging.getLogger(__name__)

_VALID_LANGS = {"auto", "da", "en"}


class _Daemon:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._state: State = State.IDLE
        self._pending_language: str = config.language

        # Lazily constructed so import-time stubs don't blow up.
        self._recorder: Optional[Any] = None
        self._transcriber: Optional[Any] = None
        self._output: Optional[Any] = None
        self._notifier: Optional[Any] = None
        self._worker: Optional[threading.Thread] = None
        self._events: Optional[EventsServer] = None

        # Live-typing components — lazily built on the first --live request.
        self._live_recorder: Optional[Any] = None
        self._stream_engine: Optional[Any] = None
        self._incremental_typer: Optional[Any] = None
        self._is_live: bool = False  # True only while a live session is active

    # ---- component lifecycle ----

    def _ensure_components(self) -> None:
        if self._recorder is None:
            from .audio import Recorder

            def _meter_cb(peak_db: float, rms_db: float) -> None:
                events = self._events
                if events is None:
                    return
                events.publish(
                    {
                        "type": "meter",
                        "peak_db": peak_db,
                        "rms_db": rms_db,
                        "ts": time.time(),
                    }
                )

            self._recorder = Recorder(self._config, meter_callback=_meter_cb)
        if self._transcriber is None:
            from .transcribe import Transcriber

            self._transcriber = Transcriber(self._config)
        if self._output is None:
            from .output import Output

            self._output = Output(self._config)
        if self._notifier is None:
            from .notify import Notifier

            self._notifier = Notifier(self._config)

    def _ensure_live_components(self) -> None:
        """Build LiveRecorder, StreamEngine, IncrementalTyper on demand.

        Called only when a live-mode session is requested. Raises on
        failure; the caller falls back to batch mode.
        """
        self._ensure_components()
        if self._live_recorder is None:
            from .audio import LiveRecorder

            def _meter_cb(peak_db: float, rms_db: float) -> None:
                events = self._events
                if events is None:
                    return
                events.publish(
                    {
                        "type": "meter",
                        "peak_db": peak_db,
                        "rms_db": rms_db,
                        "ts": time.time(),
                    }
                )

            self._live_recorder = LiveRecorder(self._config, meter_callback=_meter_cb)
        if self._stream_engine is None:
            from .stream import StreamEngine

            self._stream_engine = StreamEngine(self._config)
        if self._incremental_typer is None:
            from .output import IncrementalTyper

            assert self._output is not None
            self._incremental_typer = IncrementalTyper(self._output)

    def _on_stream_delta(self, delta: TranscriptDelta) -> None:
        """StreamEngine callback. Fired from the engine's worker thread.

        For partial deltas: drive the incremental typer and broadcast
        ``transcript_partial``. For the final delta: just broadcast
        ``transcript_final`` — the typer is driven by ``finalize`` in
        :meth:`_run_live_finalization` so the canonical text replaces any
        residual partial diff in a single visible operation.
        """
        events = self._events
        if delta.is_final:
            if events is not None:
                events.publish(
                    {
                        "type": "transcript_final",
                        "text": delta.committed_text,
                        "ts": time.time(),
                    }
                )
            return

        typer = self._incremental_typer
        if typer is not None:
            try:
                typer.set_text(delta.committed_text)
            except Exception as exc:
                # The typer is best-effort: a single failed keystroke
                # batch should not kill the live session. The final pass
                # will re-sync.
                log.warning("incremental type failed (continuing): %s", exc)

        if events is not None:
            events.publish(
                {
                    "type": "transcript_partial",
                    "committed": delta.committed_text,
                    "tentative": delta.tentative_text,
                    "ts": time.time(),
                }
            )

    def _notify(self, state: State, detail: str = "", *, quiet: bool = False) -> None:
        # `quiet=True` skips the desktop toast but still publishes the state
        # event for subscribers (applet). Used for the TRANSCRIBING transition
        # in live mode, where text has already appeared incrementally.
        if not quiet and self._notifier is not None:
            try:
                self._notifier.on_state(state, detail)
            except Exception as exc:  # pragma: no cover - notifier must not raise
                log.warning("notifier raised (suppressed): %s", exc)
        # Broadcast state to event subscribers (the panel applet, etc.).
        # Fires after the desktop notifier so observers see them in order.
        events = self._events
        if events is not None:
            events.publish(
                {
                    "type": "state",
                    "state": state.value,
                    "ts": time.time(),
                }
            )

    # ---- request handling ----

    def handle(self, req: dict) -> dict:
        cmd = req.get("cmd")
        if not isinstance(cmd, str):
            return {"ok": False, "error": "missing or invalid 'cmd'"}

        if cmd == "status":
            return self._status()

        if cmd == "toggle":
            return self._toggle(self._language_from(req), self._live_from(req))
        if cmd == "start":
            return self._start(self._language_from(req), self._live_from(req))
        if cmd == "stop":
            return self._stop()

        return {"ok": False, "error": f"unknown cmd: {cmd}"}

    def _live_from(self, req: dict) -> bool:
        """Resolve the effective live-mode flag for this request.

        Per-request ``live`` (from ``--live`` / ``--no-live``) overrides
        ``config.live_typing``. Missing or invalid → config default.
        """
        if "live" in req:
            val = req["live"]
            if isinstance(val, bool):
                return val
        return bool(getattr(self._config, "live_typing", False))

    def _language_from(self, req: dict) -> str:
        lang = req.get("language", None)
        if lang is None:
            return self._config.language
        if not isinstance(lang, str) or lang not in _VALID_LANGS:
            # Fall back rather than reject; the CLI argparser already restricts
            # values, so this branch protects against direct socket clients.
            return self._config.language
        return lang

    def _status(self) -> dict:
        with self._lock:
            state = self._state
        return {"ok": True, "state": state.value, "info": state.value}

    def _toggle(self, language: str, live: bool) -> dict:
        with self._lock:
            current = self._state
        if current in (State.IDLE, State.ERROR):
            return self._begin_recording(language, live)
        if current == State.RECORDING:
            return self._end_recording()
        # TRANSCRIBING
        self._notify(State.ERROR, "busy: transcribing")
        return {"ok": False, "error": "busy: transcribing"}

    def _start(self, language: str, live: bool) -> dict:
        with self._lock:
            current = self._state
        if current == State.RECORDING:
            return {"ok": False, "error": "already recording"}
        if current == State.TRANSCRIBING:
            self._notify(State.ERROR, "busy: transcribing")
            return {"ok": False, "error": "busy: transcribing"}
        return self._begin_recording(language, live)

    def _stop(self) -> dict:
        with self._lock:
            current = self._state
        if current == State.RECORDING:
            return self._end_recording()
        if current == State.TRANSCRIBING:
            return {"ok": False, "error": "busy: transcribing"}
        return {"ok": False, "error": "not recording"}

    # ---- recording transitions ----

    def _begin_recording(self, language: str, live: bool) -> dict:
        try:
            self._ensure_components()
        except Exception as exc:
            log.exception("failed to initialise components")
            return {"ok": False, "error": f"init failed: {exc}"}

        if live:
            try:
                self._ensure_live_components()
            except Exception as exc:
                log.warning("live mode unavailable, falling back to batch: %s", exc)
                self._notify(State.ERROR, f"live unavailable: {exc}")
                live = False

        if live:
            return self._begin_live_recording(language)
        return self._begin_batch_recording(language)

    def _begin_batch_recording(self, language: str) -> dict:
        with self._lock:
            if self._state not in (State.IDLE, State.ERROR):
                return {"ok": False, "error": f"cannot start from {self._state.value}"}
            try:
                assert self._recorder is not None
                self._recorder.start()
            except Exception as exc:
                log.exception("recorder.start failed")
                self._state = State.IDLE
                self._notify(State.ERROR, str(exc))
                return {"ok": False, "error": str(exc)}
            self._state = State.RECORDING
            self._is_live = False
            self._pending_language = language

        self._notify(State.RECORDING, "recording")
        return {"ok": True, "state": State.RECORDING.value, "info": "recording"}

    def _begin_live_recording(self, language: str) -> dict:
        with self._lock:
            if self._state not in (State.IDLE, State.ERROR):
                return {"ok": False, "error": f"cannot start from {self._state.value}"}
            try:
                assert self._stream_engine is not None
                self._stream_engine.start(language, self._on_stream_delta)
            except Exception as exc:
                log.exception("stream engine start failed; falling back to batch")
                self._notify(State.ERROR, f"live unavailable: {exc}")
                # Fall through to batch from inside the lock — we never
                # transitioned state, so it's safe.
                return self._begin_batch_recording_locked(language)
            try:
                assert self._live_recorder is not None
                self._live_recorder.start(on_pcm=self._stream_engine.feed)
            except Exception as exc:
                log.exception("live recorder start failed; rolling back engine")
                try:
                    self._stream_engine.shutdown()
                except Exception:
                    pass
                self._stream_engine = None  # force re-init next time
                self._state = State.IDLE
                self._is_live = False
                self._notify(State.ERROR, str(exc))
                return {"ok": False, "error": str(exc)}
            self._state = State.RECORDING
            self._is_live = True
            self._pending_language = language

        self._notify(State.RECORDING, "recording")
        return {"ok": True, "state": State.RECORDING.value, "info": "recording (live)"}

    def _begin_batch_recording_locked(self, language: str) -> dict:
        """Variant called while ``self._lock`` is already held."""
        try:
            assert self._recorder is not None
            self._recorder.start()
        except Exception as exc:
            log.exception("recorder.start failed")
            self._state = State.IDLE
            self._notify(State.ERROR, str(exc))
            return {"ok": False, "error": str(exc)}
        self._state = State.RECORDING
        self._is_live = False
        self._pending_language = language
        self._notify(State.RECORDING, "recording")
        return {"ok": True, "state": State.RECORDING.value, "info": "recording"}

    def _end_recording(self) -> dict:
        with self._lock:
            if self._state != State.RECORDING:
                return {"ok": False, "error": f"cannot stop from {self._state.value}"}
            is_live = self._is_live

        if is_live:
            return self._end_live_recording()
        return self._end_batch_recording()

    def _end_batch_recording(self) -> dict:
        with self._lock:
            try:
                assert self._recorder is not None
                wav_path = self._recorder.stop()
            except Exception as exc:
                log.exception("recorder.stop failed")
                self._state = State.IDLE
                self._notify(State.ERROR, str(exc))
                return {"ok": False, "error": str(exc)}
            self._state = State.TRANSCRIBING
            language = self._pending_language

        self._notify(State.TRANSCRIBING, "transcribing")

        worker = threading.Thread(
            target=self._run_transcription,
            args=(wav_path, language),
            name="ld-transcribe",
            daemon=True,
        )
        with self._lock:
            self._worker = worker
        worker.start()

        return {"ok": True, "state": State.TRANSCRIBING.value, "info": "transcribing"}

    def _end_live_recording(self) -> dict:
        with self._lock:
            try:
                assert self._live_recorder is not None
                wav_path = self._live_recorder.stop()
            except Exception as exc:
                log.exception("live recorder.stop failed")
                self._state = State.IDLE
                self._is_live = False
                self._notify(State.ERROR, str(exc))
                return {"ok": False, "error": str(exc)}
            self._state = State.TRANSCRIBING

        # Suppress the "Transcribing…" toast: in live mode the user has
        # already seen text appear at the cursor. The state event still
        # goes out so the applet pulses while the final pass runs.
        self._notify(State.TRANSCRIBING, "live finalize", quiet=True)

        worker = threading.Thread(
            target=self._run_live_finalization,
            args=(wav_path,),
            name="ld-live-finalize",
            daemon=True,
        )
        with self._lock:
            self._worker = worker
        worker.start()

        return {"ok": True, "state": State.TRANSCRIBING.value, "info": "transcribing"}

    # ---- worker ----

    def _run_transcription(self, wav_path: Path, language: str) -> None:
        try:
            assert self._transcriber is not None
            text = self._transcriber.transcribe(wav_path, language)  # type: ignore[arg-type]
        except Exception as exc:
            log.exception("transcribe failed")
            with self._lock:
                self._state = State.IDLE
            self._notify(State.ERROR, str(exc))
            return

        paste_detail = ""
        if text:
            try:
                assert self._output is not None
                result = self._output.paste(text)
            except Exception as exc:
                log.exception("paste failed")
                with self._lock:
                    self._state = State.IDLE
                self._notify(State.ERROR, str(exc))
                return
            paste_detail = _paste_detail(result)
        else:
            paste_detail = "no speech detected"

        with self._lock:
            self._state = State.IDLE
        self._notify(State.IDLE, paste_detail)

    def _run_live_finalization(self, wav_path: Path) -> None:
        """Worker: drive the authoritative final pass on the recorded WAV.

        Blocks on whisper-server, fires the final ``TranscriptDelta``
        (handled by :meth:`_on_stream_delta` for the broadcast), then
        finalises the incremental typer with the canonical text.
        """
        try:
            assert self._stream_engine is not None
            text = self._stream_engine.stop(wav_path)
        except Exception as exc:
            log.exception("stream engine final pass failed")
            with self._lock:
                self._state = State.IDLE
                self._is_live = False
            self._notify(State.ERROR, f"live finalize: {exc}")
            return

        if text:
            try:
                assert self._incremental_typer is not None
                self._incremental_typer.finalize(text)
            except Exception as exc:
                log.exception("incremental typer finalize failed")
                with self._lock:
                    self._state = State.IDLE
                    self._is_live = False
                self._notify(State.ERROR, f"live finalize type: {exc}")
                return
            detail = f"live: {len(text)} chars"
        else:
            # Empty final transcription — also reset typer baseline.
            if self._incremental_typer is not None:
                try:
                    self._incremental_typer.reset()
                except Exception:  # pragma: no cover - reset is trivial
                    pass
            detail = "no speech detected"

        with self._lock:
            self._state = State.IDLE
            self._is_live = False
        self._notify(State.IDLE, detail)

    # ---- shutdown ----

    def shutdown(self) -> None:
        log.info("daemon shutdown: stopping recorder if active")
        with self._lock:
            recorder = self._recorder
            live_recorder = self._live_recorder
            stream_engine = self._stream_engine
            state = self._state
            is_live = self._is_live
        if state == State.RECORDING:
            if is_live and live_recorder is not None:
                try:
                    live_recorder.stop()
                except Exception as exc:
                    log.warning("live_recorder.stop during shutdown failed: %s", exc)
            elif recorder is not None:
                try:
                    recorder.stop()
                except Exception as exc:
                    log.warning("recorder.stop during shutdown failed: %s", exc)
        if stream_engine is not None:
            try:
                stream_engine.shutdown()
            except Exception as exc:
                log.warning("stream_engine.shutdown failed: %s", exc)


def _paste_detail(result: str) -> str:
    if result == "typed":
        return "typed"
    if result == "pasted":
        return "pasted"
    if result == "copied_only":
        return "copied — paste manually"
    return str(result)


def run(config: Config) -> int:
    """Run the daemon. Blocks until SIGTERM/SIGINT."""
    logging_setup.configure(config)
    config.ensure_dirs()

    log.info("local-dictation daemon starting")

    # Best-effort housekeeping on startup.
    try:
        audio.cleanup_stale_wavs(config.temp_dir)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("cleanup_stale_wavs failed: %s", exc)

    daemon = _Daemon(config)

    events = EventsServer(
        config.events_socket_path,
        get_state=lambda: daemon._state,
    )
    events.start()
    daemon._events = events

    # Signal handling: ipc.serve catches KeyboardInterrupt/SystemExit, so
    # translate SIGTERM into SystemExit to unwind cleanly.
    def _on_term(signum: int, _frame: Any) -> None:
        log.info("received signal %s; shutting down", signum)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)

    try:
        ipc.serve(config.socket_path, daemon.handle)
    except (KeyboardInterrupt, SystemExit):
        pass
    except OSError as exc:
        log.error("ipc.serve failed: %s", exc)
        daemon.shutdown()
        events.stop()
        return 1
    finally:
        daemon.shutdown()
        events.stop()

    log.info("daemon exited")
    return 0
