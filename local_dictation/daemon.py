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

    def _notify(self, state: State, detail: str = "") -> None:
        if self._notifier is not None:
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
            return self._toggle(self._language_from(req))
        if cmd == "start":
            return self._start(self._language_from(req))
        if cmd == "stop":
            return self._stop()

        return {"ok": False, "error": f"unknown cmd: {cmd}"}

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

    def _toggle(self, language: str) -> dict:
        with self._lock:
            current = self._state
        if current in (State.IDLE, State.ERROR):
            return self._begin_recording(language)
        if current == State.RECORDING:
            return self._end_recording()
        # TRANSCRIBING
        self._notify(State.ERROR, "busy: transcribing")
        return {"ok": False, "error": "busy: transcribing"}

    def _start(self, language: str) -> dict:
        with self._lock:
            current = self._state
        if current == State.RECORDING:
            return {"ok": False, "error": "already recording"}
        if current == State.TRANSCRIBING:
            self._notify(State.ERROR, "busy: transcribing")
            return {"ok": False, "error": "busy: transcribing"}
        return self._begin_recording(language)

    def _stop(self) -> dict:
        with self._lock:
            current = self._state
        if current == State.RECORDING:
            return self._end_recording()
        if current == State.TRANSCRIBING:
            return {"ok": False, "error": "busy: transcribing"}
        return {"ok": False, "error": "not recording"}

    # ---- recording transitions ----

    def _begin_recording(self, language: str) -> dict:
        try:
            self._ensure_components()
        except Exception as exc:
            log.exception("failed to initialise components")
            return {"ok": False, "error": f"init failed: {exc}"}

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
            self._pending_language = language

        self._notify(State.RECORDING, "recording")
        return {"ok": True, "state": State.RECORDING.value, "info": "recording"}

    def _end_recording(self) -> dict:
        with self._lock:
            if self._state != State.RECORDING:
                return {"ok": False, "error": f"cannot stop from {self._state.value}"}
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

    # ---- shutdown ----

    def shutdown(self) -> None:
        log.info("daemon shutdown: stopping recorder if active")
        with self._lock:
            recorder = self._recorder
            state = self._state
        if recorder is not None and state == State.RECORDING:
            try:
                recorder.stop()
            except Exception as exc:
                log.warning("recorder.stop during shutdown failed: %s", exc)


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
