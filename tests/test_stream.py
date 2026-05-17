"""Unit tests for local_dictation.stream.

These tests never start a real whisper-server. The HTTP layer is bypassed
by monkeypatching ``StreamEngine._post_inference`` to return a script of
fake transcriptions, and by driving the commit algorithm via the
internal ``_ingest_transcription`` hook directly. The worker thread is
not started.
"""

from __future__ import annotations

import io
import json
import unittest
import wave
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import List, Optional
from unittest import mock

from local_dictation.stream import (
    StreamEngine,
    StreamEngineError,
    TranscriptDelta,
    _common_prefix,
    _encode_multipart_wav,
    _filter_hallucination,
    _normalize_server_text,
    _stable_prefix,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


@dataclass
class _FakeConfig:
    whisper_server_bin: Path = field(default_factory=lambda: Path("/nonexistent/whisper-server"))
    whisper_server_host: str = "127.0.0.1"
    whisper_server_port: int = 38080
    model: Path = field(default_factory=lambda: Path("/nonexistent/model.bin"))
    vad_model: Optional[Path] = None
    live_chunk_ms: int = 500
    live_window_s: int = 10
    live_stable_passes: int = 2
    temp_dir: Path = field(default_factory=lambda: Path("/tmp"))
    live_hallucination_filter: bool = True


def _make_engine(**overrides) -> StreamEngine:
    cfg = _FakeConfig(**overrides)
    engine = StreamEngine(cfg)
    # Wire a no-op callback initially; tests override.
    engine._on_delta = lambda d: None
    return engine


def _collect_deltas(engine: StreamEngine) -> list[TranscriptDelta]:
    """Replace the callback with a list-appender and return the list."""
    received: list[TranscriptDelta] = []
    engine._on_delta = received.append
    return received


def _write_wav(path: Path, seconds: float = 0.1) -> None:
    n_samples = int(seconds * 16000)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * n_samples)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class CommonPrefixTests(unittest.TestCase):
    def test_empty_inputs(self) -> None:
        self.assertEqual(_common_prefix("", ""), "")
        self.assertEqual(_common_prefix("abc", ""), "")
        self.assertEqual(_common_prefix("", "abc"), "")

    def test_full_match(self) -> None:
        self.assertEqual(_common_prefix("abc", "abc"), "abc")

    def test_partial_match(self) -> None:
        self.assertEqual(_common_prefix("hello world", "hello there"), "hello ")

    def test_no_match(self) -> None:
        self.assertEqual(_common_prefix("abc", "xyz"), "")


class StablePrefixTests(unittest.TestCase):
    def test_too_short_returns_empty(self) -> None:
        self.assertEqual(_stable_prefix([], 2), "")
        self.assertEqual(_stable_prefix(["hello"], 2), "")

    def test_k_equals_two(self) -> None:
        self.assertEqual(_stable_prefix(["hello", "hello world"], 2), "hello")
        self.assertEqual(
            _stable_prefix(["hello world", "hello world how"], 2),
            "hello world",
        )


class NormalizeServerTextTests(unittest.TestCase):
    def test_strips_leading_space_and_trailing_newline(self) -> None:
        self.assertEqual(_normalize_server_text(" hello\n"), "hello")
        self.assertEqual(_normalize_server_text("  hello  "), "hello")

    def test_collapses_internal_newlines_to_spaces(self) -> None:
        # Multi-segment whisper-server output. The live typer feeds this
        # to wtype/ydotool — a literal \n there becomes Enter, which
        # would submit in Claude Code mid-dictation.
        self.assertEqual(
            _normalize_server_text(" Hello world.\n And then more.\n"),
            "Hello world. And then more.",
        )
        self.assertEqual(_normalize_server_text("a\r\nb"), "a b")
        self.assertEqual(_normalize_server_text("a\n\n\nb"), "a b")


class HallucinationFilterTests(unittest.TestCase):
    def test_filters_you(self) -> None:
        self.assertEqual(_filter_hallucination(" you "), "")
        self.assertEqual(_filter_hallucination("You"), "")

    def test_filters_music_marker(self) -> None:
        self.assertEqual(_filter_hallucination("[Music]"), "")
        self.assertEqual(_filter_hallucination(" [music] "), "")

    def test_keeps_normal_text(self) -> None:
        self.assertEqual(_filter_hallucination("hello world"), "hello world")


class MultipartEncodingTests(unittest.TestCase):
    def test_body_contains_wav_payload(self) -> None:
        wav = b"RIFF\x00\x00\x00\x00WAVEfmt "
        body, ctype = _encode_multipart_wav(wav, filename="x.wav")
        self.assertTrue(ctype.startswith("multipart/form-data; boundary="))
        self.assertIn(b'name="file"; filename="x.wav"', body)
        self.assertIn(wav, body)
        # The trailing boundary terminates the body.
        self.assertTrue(body.rstrip().endswith(b"--"))


# ---------------------------------------------------------------------------
# Commit-algorithm tests using _ingest_transcription directly.
#
# We bypass the worker thread / HTTP entirely; the commit algorithm is
# pure given a sequence of raw transcriptions.
# ---------------------------------------------------------------------------


class CommitAlgorithmTests(unittest.TestCase):
    def test_stable_prefix_commit_k2(self) -> None:
        """Plan: feed 'hello', 'hello world', 'hello world how' — after
        the third pass committed_text must be 'hello world'."""
        engine = _make_engine(live_stable_passes=2)
        deltas = _collect_deltas(engine)
        for t in ("hello", "hello world", "hello world how"):
            engine._ingest_transcription(t)
        self.assertEqual(engine._committed_text, "hello world")
        # The final emitted delta reflects this.
        self.assertGreaterEqual(len(deltas), 1)
        self.assertEqual(deltas[-1].committed_text, "hello world")
        self.assertFalse(deltas[-1].is_final)

    def test_tentative_only_change(self) -> None:
        """After committed has settled, feeding two more transcriptions
        whose only difference is the tail keeps committed_text stable and
        only changes tentative_text.

        With ``stable_passes=2`` the stable prefix is the common prefix
        of the LAST TWO raw transcriptions. So to get a steady committed
        across two new passes, both new passes must share their full
        leading text up to where the tail varies.
        """
        engine = _make_engine(live_stable_passes=2)
        # Establish committed via a run of identical passes.
        for t in ("hello world together ", "hello world together "):
            engine._ingest_transcription(t)
        self.assertEqual(engine._committed_text, "hello world together ")

        deltas = _collect_deltas(engine)

        # Two new passes that share "hello world together " and differ
        # only on the tail. Stable prefix of consecutive pairs stays
        # at "hello world together " — committed does not change.
        engine._ingest_transcription("hello world together foo")
        committed_after_first = engine._committed_text
        tentative_after_first = engine._tentative_text

        engine._ingest_transcription("hello world together bar")
        committed_after_second = engine._committed_text
        tentative_after_second = engine._tentative_text

        self.assertEqual(committed_after_first, "hello world together ")
        self.assertEqual(committed_after_second, "hello world together ")
        self.assertNotEqual(tentative_after_first, tentative_after_second)

    def test_committed_never_shrinks_on_contradiction(self) -> None:
        """If the server contradicts earlier committed text, committed
        must NOT shrink. The new content surfaces in tentative_text."""
        engine = _make_engine(live_stable_passes=2)
        # Commit "hello" by feeding it twice.
        engine._ingest_transcription("hello")
        engine._ingest_transcription("hello")
        self.assertEqual(engine._committed_text, "hello")

        deltas = _collect_deltas(engine)
        # Server now contradicts itself.
        engine._ingest_transcription("goodbye")

        self.assertEqual(engine._committed_text, "hello")
        # The fresh transcription does not start with "hello", so the
        # whole thing is exposed in tentative.
        self.assertEqual(engine._tentative_text, "goodbye")
        self.assertEqual(deltas[-1].committed_text, "hello")
        self.assertEqual(deltas[-1].tentative_text, "goodbye")

    def test_hallucination_filter_blanks_you(self) -> None:
        """A pass returning ' you\\n' (after normalisation: 'you') must
        produce tentative_text == ''."""
        engine = _make_engine(live_stable_passes=2)
        # First pass establishes nothing committed.
        engine._ingest_transcription(_normalize_server_text(" you\n"))
        # Tentative should have been filtered to "".
        self.assertEqual(engine._tentative_text, "")
        self.assertEqual(engine._committed_text, "")

    def test_monotonicity_across_all_deltas(self) -> None:
        """Walk every emitted delta and assert each delta's
        committed_text starts with the previous delta's committed_text."""
        engine = _make_engine(live_stable_passes=2)
        deltas = _collect_deltas(engine)
        sequence = [
            "hello",
            "hello world",
            "hello world how",
            "hello world how are",
            # Server now revises earlier text — committed must still hold.
            "hi there",
            "hello world how are you",
            "hello world how are you doing",
            "hello world how are you doing today",
        ]
        for t in sequence:
            engine._ingest_transcription(t)

        self.assertGreater(len(deltas), 0)
        prev = ""
        for delta in deltas:
            self.assertTrue(
                delta.committed_text.startswith(prev),
                f"monotonicity violated: {delta.committed_text!r} does not "
                f"start with {prev!r}",
            )
            prev = delta.committed_text


# ---------------------------------------------------------------------------
# HTTP-failure fall-through using the _tick path with mocked _post_inference.
# ---------------------------------------------------------------------------


class HttpFailureTests(unittest.TestCase):
    def test_retry_once_then_skip(self) -> None:
        """If _post_inference always raises, _tick retries once and then
        skips this pass; no delta is fired, engine keeps running."""
        engine = _make_engine(live_stable_passes=2)
        deltas = _collect_deltas(engine)

        # Feed enough audio to clear the min-chunk threshold.
        engine.feed(b"\x00\x00" * 16000)  # 1 s of silence

        call_count = {"n": 0}

        def boom(_wav_bytes: bytes) -> str:
            call_count["n"] += 1
            raise StreamEngineError("simulated network failure")

        engine._post_inference = boom  # type: ignore[method-assign]

        with mock.patch("local_dictation.stream.time.sleep"):
            engine._tick()

        # Exactly two calls: original + one retry.
        self.assertEqual(call_count["n"], 2)
        # No deltas fired.
        self.assertEqual(deltas, [])
        # Engine survived; can still run another tick.
        with mock.patch("local_dictation.stream.time.sleep"):
            engine._tick()
        self.assertEqual(call_count["n"], 4)


# ---------------------------------------------------------------------------
# stop() final-pass behaviour with a mocked POST.
# ---------------------------------------------------------------------------


class StopFinalPassTests(unittest.TestCase):
    def test_final_pass_calls_post_once_and_returns_text(self) -> None:
        engine = _make_engine(live_stable_passes=2)
        deltas = _collect_deltas(engine)

        # Pre-populate some committed state.
        engine._ingest_transcription("hello")
        engine._ingest_transcription("hello")
        self.assertEqual(engine._committed_text, "hello")
        deltas.clear()  # focus only on what stop() emits

        calls: List[bytes] = []

        def fake_post(wav_bytes: bytes) -> str:
            calls.append(wav_bytes)
            return " hello world how are you\n"

        engine._post_inference = fake_post  # type: ignore[method-assign]

        with TemporaryDirectory() as td:
            wav_path = Path(td) / "final.wav"
            _write_wav(wav_path, seconds=0.2)
            result = engine.stop(wav_path)

        self.assertEqual(result, "hello world how are you")
        self.assertEqual(len(calls), 1)
        # One final delta with is_final=True and empty tentative.
        self.assertEqual(len(deltas), 1)
        self.assertTrue(deltas[-1].is_final)
        self.assertEqual(deltas[-1].tentative_text, "")
        self.assertEqual(deltas[-1].committed_text, "hello world how are you")

    def test_final_pass_failure_falls_back_to_accumulated_state(self) -> None:
        engine = _make_engine(live_stable_passes=2)
        deltas = _collect_deltas(engine)
        engine._ingest_transcription("hello")
        engine._ingest_transcription("hello world")
        # committed=hello, tentative includes " world"

        def boom(_wav_bytes: bytes) -> str:
            raise StreamEngineError("boom")

        engine._post_inference = boom  # type: ignore[method-assign]

        with TemporaryDirectory() as td:
            wav_path = Path(td) / "final.wav"
            _write_wav(wav_path, seconds=0.1)
            with mock.patch("local_dictation.stream.time.sleep"):
                result = engine.stop(wav_path)

        # Fallback merges committed + tentative.
        self.assertEqual(result, "hello world")
        self.assertTrue(deltas[-1].is_final)
        self.assertEqual(deltas[-1].tentative_text, "")
        self.assertEqual(deltas[-1].committed_text, "hello world")


# ---------------------------------------------------------------------------
# feed() ring-buffer behaviour
# ---------------------------------------------------------------------------


class RingBufferTests(unittest.TestCase):
    def test_feed_appends_and_caps_to_window(self) -> None:
        engine = _make_engine(live_window_s=1)  # 1 s window = 32_000 bytes
        engine.feed(b"\xaa\xbb" * 8000)  # 0.5 s
        self.assertEqual(len(engine._ring), 16_000)
        engine.feed(b"\xcc\xdd" * 16000)  # +1 s; total 1.5 s, should cap at 1 s
        self.assertEqual(len(engine._ring), 32_000)
        # The tail should be from the second feed.
        self.assertEqual(engine._ring[-4:], b"\xcc\xdd\xcc\xdd")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
