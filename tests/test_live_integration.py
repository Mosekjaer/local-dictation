"""Tests for the daemon-side wiring of live mode.

These cover the config defaults, the CLI ``live`` plumbing through to
the daemon's request handler, and the daemon's branching between batch
and live recording paths. They never invoke ffmpeg, whisper-server, or
real subprocess calls — every component the daemon delegates to is
replaced with a tiny stub.
"""

from __future__ import annotations

import threading
import unittest
from pathlib import Path
from typing import Any, Callable, Optional
from unittest.mock import patch

from local_dictation.config import Config, load
from local_dictation.daemon import _Daemon
from local_dictation.state import State
from local_dictation.stream import TranscriptDelta


class ConfigDefaultsTests(unittest.TestCase):
    def test_live_defaults(self) -> None:
        cfg = Config()
        self.assertEqual(cfg.live_typing, False)
        self.assertEqual(cfg.live_chunk_ms, 500)
        self.assertEqual(cfg.live_window_s, 10)
        self.assertEqual(cfg.live_stable_passes, 2)
        self.assertEqual(cfg.live_hallucination_filter, True)
        self.assertEqual(cfg.whisper_server_host, "127.0.0.1")
        self.assertEqual(cfg.whisper_server_port, 38080)
        # vad_model has a default path; tests don't care if it exists on disk.
        self.assertIsInstance(cfg.whisper_server_bin, Path)

    def test_toml_overrides(self) -> None:
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write(
                "live_typing = true\n"
                "live_chunk_ms = 250\n"
                "live_window_s = 6\n"
                "live_stable_passes = 3\n"
                'whisper_server_host = "0.0.0.0"\n'
                "whisper_server_port = 40000\n"
                'vad_model = ""\n'
            )
            path = Path(f.name)
        try:
            cfg = load(path)
        finally:
            path.unlink(missing_ok=True)
        self.assertTrue(cfg.live_typing)
        self.assertEqual(cfg.live_chunk_ms, 250)
        self.assertEqual(cfg.live_window_s, 6)
        self.assertEqual(cfg.live_stable_passes, 3)
        self.assertEqual(cfg.whisper_server_host, "0.0.0.0")
        self.assertEqual(cfg.whisper_server_port, 40000)
        # Empty string → None for the optional VAD model.
        self.assertIsNone(cfg.vad_model)


# ----- Stubs for the daemon's collaborators -----


class _FakeRecorder:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.start_calls = 0
        self.stop_calls = 0

    def start(self) -> Path:
        self.start_calls += 1
        return Path("/tmp/fake.wav")

    def stop(self) -> Path:
        self.stop_calls += 1
        return Path("/tmp/fake.wav")


class _FakeLiveRecorder:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.start_calls = 0
        self.stop_calls = 0
        self.on_pcm: Optional[Callable[[bytes], None]] = None

    def start(self, on_pcm: Callable[[bytes], None]) -> Path:
        self.start_calls += 1
        self.on_pcm = on_pcm
        return Path("/tmp/live.wav")

    def stop(self) -> Path:
        self.stop_calls += 1
        return Path("/tmp/live.wav")


class _FakeStreamEngine:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.start_calls = 0
        self.feed_calls = 0
        self.stop_calls = 0
        self.shutdown_calls = 0
        self.on_delta: Optional[Callable[[TranscriptDelta], None]] = None

    def start(self, language: str, on_delta: Callable[[TranscriptDelta], None]) -> None:
        self.start_calls += 1
        self.on_delta = on_delta

    def feed(self, pcm: bytes) -> None:
        self.feed_calls += 1

    def stop(self, final_wav: Path) -> str:
        self.stop_calls += 1
        # Fire a final delta so any subscribers see transcript_final.
        if self.on_delta is not None:
            self.on_delta(
                TranscriptDelta(committed_text="hello world", tentative_text="", is_final=True)
            )
        return "hello world"

    def shutdown(self) -> None:
        self.shutdown_calls += 1


class _FakeIncrementalTyper:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.set_calls: list[str] = []
        self.finalize_calls: list[str] = []
        self.reset_calls = 0

    def set_text(self, target: str) -> None:
        self.set_calls.append(target)

    def finalize(self, target: str) -> None:
        self.finalize_calls.append(target)

    def reset(self) -> None:
        self.reset_calls += 1


class _FakeOutput:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def paste(self, _text: str) -> str:
        return "typed"


class _FakeTranscriber:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def transcribe(self, _wav: Path, _lang: str) -> str:
        return "batch text"


class _FakeNotifier:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.events: list[tuple[State, str]] = []

    def on_state(self, state: State, detail: str = "") -> None:
        self.events.append((state, detail))


def _install_fakes(daemon: _Daemon) -> tuple[Any, ...]:
    """Pre-populate the daemon with stubbed collaborators so the lazy
    initialisers never reach for the real implementations."""
    fake_rec = _FakeRecorder()
    fake_live_rec = _FakeLiveRecorder()
    fake_engine = _FakeStreamEngine()
    fake_typer = _FakeIncrementalTyper()
    fake_output = _FakeOutput()
    fake_transcriber = _FakeTranscriber()
    fake_notifier = _FakeNotifier()
    daemon._recorder = fake_rec
    daemon._live_recorder = fake_live_rec
    daemon._stream_engine = fake_engine
    daemon._incremental_typer = fake_typer
    daemon._output = fake_output
    daemon._transcriber = fake_transcriber
    daemon._notifier = fake_notifier
    return (
        fake_rec,
        fake_live_rec,
        fake_engine,
        fake_typer,
        fake_output,
        fake_transcriber,
        fake_notifier,
    )


class DaemonLiveDispatchTests(unittest.TestCase):
    def test_live_from_request_overrides_config(self) -> None:
        cfg = Config()
        cfg.live_typing = False
        d = _Daemon(cfg)
        self.assertTrue(d._live_from({"live": True}))
        self.assertFalse(d._live_from({"live": False}))
        cfg.live_typing = True
        self.assertTrue(d._live_from({}))
        self.assertFalse(d._live_from({"live": False}))

    def test_toggle_batch_path_unchanged(self) -> None:
        cfg = Config()
        d = _Daemon(cfg)
        rec, live_rec, engine, *_ = _install_fakes(d)

        reply = d._toggle("en", live=False)

        self.assertTrue(reply["ok"])
        self.assertEqual(d._state, State.RECORDING)
        self.assertFalse(d._is_live)
        self.assertEqual(rec.start_calls, 1)
        self.assertEqual(live_rec.start_calls, 0)
        self.assertEqual(engine.start_calls, 0)

    def test_toggle_live_path_uses_engine_and_live_recorder(self) -> None:
        cfg = Config()
        d = _Daemon(cfg)
        rec, live_rec, engine, *_ = _install_fakes(d)

        reply = d._toggle("en", live=True)

        self.assertTrue(reply["ok"])
        self.assertEqual(d._state, State.RECORDING)
        self.assertTrue(d._is_live)
        self.assertEqual(rec.start_calls, 0)
        self.assertEqual(live_rec.start_calls, 1)
        self.assertEqual(engine.start_calls, 1)
        # Engine got our daemon's _on_stream_delta wired in. Bound-method
        # identity isn't stable across attribute accesses, so compare the
        # underlying function instead.
        self.assertIsNotNone(engine.on_delta)
        assert engine.on_delta is not None
        self.assertIs(engine.on_delta.__func__, _Daemon._on_stream_delta)

    def test_live_session_end_to_end(self) -> None:
        cfg = Config()
        d = _Daemon(cfg)
        rec, live_rec, engine, typer, *_ = _install_fakes(d)

        # Begin live.
        d._toggle("en", live=True)
        # Simulate a partial delta arriving from the engine's worker thread.
        d._on_stream_delta(
            TranscriptDelta(committed_text="hello", tentative_text="world", is_final=False)
        )
        self.assertEqual(typer.set_calls, ["hello"])

        # End — _end_recording spawns the finalisation worker; wait for it.
        d._toggle("en", live=True)
        worker = d._worker
        self.assertIsNotNone(worker)
        assert worker is not None
        worker.join(timeout=2.0)
        self.assertFalse(worker.is_alive())

        # Final pass ran, typer.finalize was called with canonical text,
        # daemon is back to IDLE.
        self.assertEqual(engine.stop_calls, 1)
        self.assertEqual(typer.finalize_calls, ["hello world"])
        self.assertEqual(live_rec.stop_calls, 1)
        self.assertEqual(d._state, State.IDLE)
        self.assertFalse(d._is_live)

    def test_live_init_failure_falls_back_to_batch(self) -> None:
        cfg = Config()
        d = _Daemon(cfg)
        _install_fakes(d)
        # Patch _ensure_live_components to fail; the daemon should fall
        # back to the batch path without raising to the caller.
        with patch.object(
            d,
            "_ensure_live_components",
            side_effect=RuntimeError("whisper-server missing"),
        ):
            reply = d._begin_recording("en", live=True)
        self.assertTrue(reply["ok"])
        self.assertEqual(d._state, State.RECORDING)
        self.assertFalse(d._is_live)


if __name__ == "__main__":
    unittest.main()
