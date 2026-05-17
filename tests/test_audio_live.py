"""Hermetic tests for :class:`local_dictation.audio.LiveRecorder`.

These tests never touch real audio hardware or the real ``ffmpeg`` binary.
A small Python stub script is dropped into a temporary directory which is
prepended to ``PATH``; the stub mimics the surface area of ffmpeg that
``LiveRecorder.start()`` relies on (raw PCM on stdout, a WAV file on disk,
and optional astats-style metadata lines on a /dev/fd sidecar).
"""

from __future__ import annotations

import os
import re
import struct
import textwrap
import threading
import time
import unittest
import wave
from pathlib import Path

# Save the original PATH up front; tests mutate os.environ but the
# subprocess in LiveRecorder reads the live environment.
_ORIGINAL_PATH = os.environ.get("PATH", "")

from local_dictation.audio import LiveRecorder, RecorderError
from local_dictation.config import Config


# ---------- ffmpeg stub helpers ----------

# Common preamble for every stub: parse argv, locate the WAV path, write a
# valid (tiny) WAV header, write known PCM bytes to stdout, then either
# exit cleanly on 'q\n' or after writing the payload.
_STUB_HEADER = r'''#!/usr/bin/env python3
"""Test stub that mimics just enough of ffmpeg for LiveRecorder."""

import os
import re
import struct
import sys
import threading
import time
import wave

argv = sys.argv[1:]

# Extract WAV target: the LAST positional argument is the wav path; -y just
# means "overwrite" in ffmpeg's CLI. LiveRecorder passes "-y <path>" with
# the path as the final argv entry.
wav_path = None
for i, a in enumerate(argv):
    if a == "-y" and i + 1 < len(argv):
        wav_path = argv[i + 1]

# Extract meter fd from -filter_complex if present.
meter_fd = None
for i, a in enumerate(argv):
    if a == "-filter_complex" and i + 1 < len(argv):
        m = re.search(r"/dev/fd/(\d+)", argv[i + 1])
        if m:
            meter_fd = int(m.group(1))

# Read mode-knobs from the env (set per test).
mode = os.environ.get("LD_STUB_MODE", "normal")
payload_size = int(os.environ.get("LD_STUB_PAYLOAD_BYTES", "32768"))
fail_msg = os.environ.get("LD_STUB_FAIL_MSG", "")
trace_file = os.environ.get("LD_STUB_TRACE_FILE", "")


def trace(event: str) -> None:
    if trace_file:
        try:
            with open(trace_file, "a") as fh:
                fh.write(event + "\n")
        except OSError:
            pass
'''


_STUB_NORMAL = r'''

# --- normal mode ---
# Write an empty/minimal WAV header to the target path first so the file
# exists even if we are killed before producing payload. Use the stdlib
# wave module so the file is genuinely valid.
if wav_path:
    with wave.open(wav_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        # Write the same payload to WAV as to stdout so the contents
        # match what on_pcm receives.
        pattern = (b"\x00\x01" * (payload_size // 2))
        wf.writeframes(pattern)

# Background thread: read stdin; if 'q\n' is seen, signal main loop to stop.
stop_event = threading.Event()

def stdin_watcher():
    try:
        for line in sys.stdin.buffer:
            if line.strip() == b"q":
                trace("got_q")
                stop_event.set()
                return
    except Exception:
        return

t = threading.Thread(target=stdin_watcher, daemon=True)
t.start()

# Emit synthetic meter lines so the meter wiring can be observed.
if meter_fd is not None:
    try:
        # ametadata=print emits frame markers followed by key=value lines;
        # the reader only cares about the Peak_level/RMS_level keys.
        meter_data = (
            "frame:0 pts:0 pts_time:0\n"
            "lavfi.astats.Overall.Peak_level=-6.5\n"
            "lavfi.astats.Overall.RMS_level=-12.0\n"
        )
        os.write(meter_fd, meter_data.encode())
    except OSError:
        pass

# Write the PCM payload to stdout. Chunks small enough that the reader
# sees several callbacks (exercises the even-length invariant too).
pattern = b"\x00\x01" * (payload_size // 2)
chunk = 4096
written = 0
try:
    while written < len(pattern):
        end = min(written + chunk, len(pattern))
        sys.stdout.buffer.write(pattern[written:end])
        sys.stdout.buffer.flush()
        written = end
        if stop_event.is_set():
            break
except BrokenPipeError:
    pass

trace("payload_done")

# Now park until stdin closes or 'q' arrives. ffmpeg in real life keeps
# capturing until told to stop; replicate that so stop() actually has to
# perform a handshake.
while not stop_event.is_set():
    if sys.stdin.closed:
        break
    time.sleep(0.05)

trace("exit_clean")
sys.exit(0)
'''


_STUB_FAIL = r'''

# --- fail-fast mode ---
sys.stderr.write(fail_msg or "stub: simulated immediate failure\n")
sys.stderr.flush()
sys.exit(2)
'''


_STUB_FULL = _STUB_HEADER + r'''

if mode == "fail":
''' + textwrap.indent(_STUB_FAIL.strip("\n"), "    ") + r'''
else:
''' + textwrap.indent(_STUB_NORMAL.strip("\n"), "    ") + r'''
'''


# ---------- test infrastructure ----------


class _StubDir:
    """Drop an ``ffmpeg`` stub into a private tmp dir and prepend it to PATH."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.stub_path = tmp_path / "ffmpeg"
        self.trace_path = tmp_path / "trace.log"
        self._original_path = os.environ.get("PATH", "")

    def install(self) -> None:
        self.stub_path.write_text(_STUB_FULL)
        self.stub_path.chmod(0o755)
        os.environ["PATH"] = f"{self.tmp_path}:{self._original_path}"
        os.environ["LD_STUB_TRACE_FILE"] = str(self.trace_path)

    def uninstall(self) -> None:
        os.environ["PATH"] = self._original_path
        for var in (
            "LD_STUB_MODE",
            "LD_STUB_PAYLOAD_BYTES",
            "LD_STUB_FAIL_MSG",
            "LD_STUB_TRACE_FILE",
        ):
            os.environ.pop(var, None)

    def read_trace(self) -> list[str]:
        try:
            return self.trace_path.read_text().splitlines()
        except FileNotFoundError:
            return []


def _make_config(tmp_path: Path) -> Config:
    cfg = Config()
    cfg.temp_dir = tmp_path / "recordings"
    cfg.temp_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _expected_pattern(n_bytes: int) -> bytes:
    return b"\x00\x01" * (n_bytes // 2)


# ---------- tests ----------


class LiveRecorderTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.stub = _StubDir(self.tmp_path / "bin")
        self.stub.tmp_path.mkdir(parents=True, exist_ok=True)
        self.stub.install()
        self.cfg = _make_config(self.tmp_path)

    def tearDown(self) -> None:
        self.stub.uninstall()
        self._tmp.cleanup()

    # 1. PCM delivery
    def test_pcm_delivery_total_bytes_and_content(self) -> None:
        os.environ["LD_STUB_MODE"] = "normal"
        os.environ["LD_STUB_PAYLOAD_BYTES"] = "32768"

        received: list[bytes] = []
        lock = threading.Lock()

        def on_pcm(chunk: bytes) -> None:
            with lock:
                received.append(chunk)

        rec = LiveRecorder(self.cfg, meter_callback=None)
        wav_path = rec.start(on_pcm)
        try:
            # Wait for the stub to write its full payload before stopping
            # so we get the entire stream.
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                with lock:
                    total = sum(len(c) for c in received)
                if total >= 32768:
                    break
                time.sleep(0.02)
        finally:
            rec.stop()

        joined = b"".join(received)
        self.assertEqual(
            len(joined), 32768, f"expected 32768 PCM bytes, got {len(joined)}"
        )
        self.assertEqual(joined, _expected_pattern(32768))
        self.assertTrue(wav_path.exists())

    # 2. Even-length chunks
    def test_pcm_chunks_are_even_length(self) -> None:
        os.environ["LD_STUB_MODE"] = "normal"
        os.environ["LD_STUB_PAYLOAD_BYTES"] = "32768"

        received: list[int] = []
        lock = threading.Lock()

        def on_pcm(chunk: bytes) -> None:
            with lock:
                received.append(len(chunk))

        rec = LiveRecorder(self.cfg)
        rec.start(on_pcm)
        try:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                with lock:
                    total = sum(received)
                if total >= 32768:
                    break
                time.sleep(0.02)
        finally:
            rec.stop()

        with lock:
            snapshot = list(received)
        self.assertGreater(len(snapshot), 0)
        for n in snapshot:
            self.assertEqual(n % 2, 0, f"odd-length chunk: {n}")

    # 3. WAV finalisation
    def test_stop_returns_existing_nonempty_wav(self) -> None:
        os.environ["LD_STUB_MODE"] = "normal"
        os.environ["LD_STUB_PAYLOAD_BYTES"] = "8192"

        def on_pcm(chunk: bytes) -> None:
            pass

        rec = LiveRecorder(self.cfg)
        wav_path = rec.start(on_pcm)
        time.sleep(0.3)
        returned = rec.stop()

        self.assertEqual(returned, wav_path)
        self.assertTrue(returned.exists())
        self.assertGreater(returned.stat().st_size, 0)

        # And it's a real, readable WAV.
        with wave.open(str(returned), "rb") as wf:
            self.assertEqual(wf.getnchannels(), 1)
            self.assertEqual(wf.getsampwidth(), 2)
            self.assertEqual(wf.getframerate(), 16000)

    # 4. Clean stop via 'q\n'
    def test_clean_stop_does_not_escalate_to_signals(self) -> None:
        os.environ["LD_STUB_MODE"] = "normal"
        os.environ["LD_STUB_PAYLOAD_BYTES"] = "4096"

        def on_pcm(chunk: bytes) -> None:
            pass

        rec = LiveRecorder(self.cfg)
        rec.start(on_pcm)
        time.sleep(0.2)
        rec.stop()

        trace = self.stub.read_trace()
        # The stub records 'got_q' the moment it observes 'q\n' on stdin.
        # Reaching exit_clean without needing SIGTERM proves the graceful
        # path worked.
        self.assertIn("got_q", trace, f"trace was: {trace}")
        self.assertIn("exit_clean", trace, f"trace was: {trace}")

    # 5. Spawn failure surfaces stderr
    def test_immediate_failure_raises_with_stderr_tail(self) -> None:
        os.environ["LD_STUB_MODE"] = "fail"
        os.environ["LD_STUB_FAIL_MSG"] = "ffmpeg-stub: source unavailable\n"

        def on_pcm(chunk: bytes) -> None:
            pass

        rec = LiveRecorder(self.cfg)
        with self.assertRaises(RecorderError) as cm:
            rec.start(on_pcm)
        self.assertIn("source unavailable", str(cm.exception))

    # 6. Meter wiring - optional but plumbed via the stub.
    def test_meter_callback_fires(self) -> None:
        os.environ["LD_STUB_MODE"] = "normal"
        os.environ["LD_STUB_PAYLOAD_BYTES"] = "4096"

        meter_events: list[tuple[float, float]] = []
        lock = threading.Lock()

        def on_meter(peak: float, rms: float) -> None:
            with lock:
                meter_events.append((peak, rms))

        def on_pcm(chunk: bytes) -> None:
            pass

        rec = LiveRecorder(self.cfg, meter_callback=on_meter)
        rec.start(on_pcm)
        try:
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                with lock:
                    if meter_events:
                        break
                time.sleep(0.02)
        finally:
            rec.stop()

        with lock:
            snapshot = list(meter_events)
        # The stub writes exactly one peak/rms pair; if the wiring is
        # broken we'll see zero events.
        self.assertGreaterEqual(len(snapshot), 1)
        peak, rms = snapshot[0]
        self.assertAlmostEqual(peak, -6.5, places=2)
        self.assertAlmostEqual(rms, -12.0, places=2)

    # WAV filename matches cleanup_stale_wavs glob.
    def test_wav_filename_matches_rec_prefix(self) -> None:
        os.environ["LD_STUB_MODE"] = "normal"
        os.environ["LD_STUB_PAYLOAD_BYTES"] = "2048"

        def on_pcm(chunk: bytes) -> None:
            pass

        rec = LiveRecorder(self.cfg)
        wav_path = rec.start(on_pcm)
        try:
            time.sleep(0.15)
        finally:
            rec.stop()

        self.assertTrue(wav_path.name.startswith("rec-"))
        self.assertTrue(wav_path.name.endswith(".wav"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
