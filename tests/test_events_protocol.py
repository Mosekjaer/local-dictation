"""Tests for local_dictation.events broadcast protocol.

These tests exercise the JSON encoder, dBFS clamping, the slow-subscriber
drop policy, the hello-on-connect contract, and clean shutdown semantics.
They never invoke ffmpeg or whisper — events.py is pure stdlib.
"""

from __future__ import annotations

import json
import math
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from local_dictation.events import (
    EventsServer,
    _QUEUE_MAX,
    _DROP_DISCONNECT_THRESHOLD,
    clamp_db,
    encode_event,
)
from local_dictation.state import State


class EncoderTests(unittest.TestCase):
    def test_state_shape(self) -> None:
        ev = {"type": "state", "state": "recording", "ts": 123.456}
        line = encode_event(ev)
        self.assertTrue(line.endswith(b"\n"))
        decoded = json.loads(line)
        self.assertEqual(decoded["type"], "state")
        self.assertEqual(decoded["state"], "recording")
        self.assertAlmostEqual(decoded["ts"], 123.456)

    def test_meter_shape(self) -> None:
        ev = {
            "type": "meter",
            "peak_db": -12.5,
            "rms_db": -18.0,
            "ts": 1.0,
        }
        decoded = json.loads(encode_event(ev))
        self.assertEqual(decoded["type"], "meter")
        self.assertAlmostEqual(decoded["peak_db"], -12.5)
        self.assertAlmostEqual(decoded["rms_db"], -18.0)

    def test_hello_shape(self) -> None:
        ev = {
            "type": "hello",
            "version": 1,
            "daemon_state": "idle",
            "ts": 2.0,
        }
        decoded = json.loads(encode_event(ev))
        self.assertEqual(decoded["type"], "hello")
        self.assertEqual(decoded["version"], 1)
        self.assertEqual(decoded["daemon_state"], "idle")

    def test_clamp_minus_inf_to_minus_120(self) -> None:
        self.assertEqual(clamp_db(float("-inf")), -120.0)

    def test_clamp_nan_to_minus_120(self) -> None:
        self.assertEqual(clamp_db(float("nan")), -120.0)

    def test_clamp_below_floor(self) -> None:
        self.assertEqual(clamp_db(-1000.0), -120.0)

    def test_clamp_above_ceiling(self) -> None:
        self.assertEqual(clamp_db(3.0), 0.0)

    def test_clamp_passthrough(self) -> None:
        self.assertAlmostEqual(clamp_db(-42.5), -42.5)
        self.assertEqual(clamp_db(0.0), 0.0)


class _ServerHarness:
    """Helper that spins up an EventsServer on a tempdir socket."""

    def __init__(self, state_value: str = "idle") -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "events.sock"

        class _S:
            def __init__(self, v: str) -> None:
                self.value = v

        self._state = _S(state_value)
        self.server = EventsServer(self.path, get_state=lambda: self._state)

    def set_state(self, value: str) -> None:
        self._state.value = value

    def close(self) -> None:
        self.server.stop()
        self.tmp.cleanup()


def _connect(path: Path, timeout: float = 2.0) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    deadline = time.monotonic() + timeout
    while True:
        try:
            sock.connect(str(path))
            return sock
        except (FileNotFoundError, ConnectionRefusedError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


def _readline(sock: socket.socket, timeout: float = 1.0) -> bytes:
    sock.settimeout(timeout)
    buf = bytearray()
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            return bytes(buf)
        buf.extend(chunk)
        if b"\n" in buf:
            line, _, _ = buf.partition(b"\n")
            return bytes(line)


class HelloEventTests(unittest.TestCase):
    def test_hello_includes_current_state(self) -> None:
        h = _ServerHarness(state_value="recording")
        h.server.start()
        try:
            sock = _connect(h.path)
            try:
                line = _readline(sock)
                payload = json.loads(line)
                self.assertEqual(payload["type"], "hello")
                self.assertEqual(payload["version"], 1)
                self.assertEqual(payload["daemon_state"], "recording")
                self.assertIsInstance(payload["ts"], float)
            finally:
                sock.close()
        finally:
            h.close()

    def test_hello_reflects_state_change_before_connect(self) -> None:
        h = _ServerHarness(state_value="idle")
        h.server.start()
        try:
            h.set_state("transcribing")
            sock = _connect(h.path)
            try:
                line = _readline(sock)
                payload = json.loads(line)
                self.assertEqual(payload["daemon_state"], "transcribing")
            finally:
                sock.close()
        finally:
            h.close()


class PublishTests(unittest.TestCase):
    def test_state_event_delivered(self) -> None:
        h = _ServerHarness(state_value="idle")
        h.server.start()
        try:
            sock = _connect(h.path)
            try:
                _readline(sock)  # hello
                h.server.publish({
                    "type": "state",
                    "state": "recording",
                    "ts": 1.0,
                })
                line = _readline(sock)
                self.assertEqual(json.loads(line)["state"], "recording")
            finally:
                sock.close()
        finally:
            h.close()

    def test_publish_does_not_block_when_queue_full(self) -> None:
        """A subscriber that never reads must not stall publish()."""
        h = _ServerHarness()
        h.server.start()
        try:
            sock = _connect(h.path)
            try:
                # Don't read; the hello + every publish piles into the queue.
                # Push well past _QUEUE_MAX in a bounded amount of time.
                t0 = time.monotonic()
                for i in range(_QUEUE_MAX * 4):
                    h.server.publish({"type": "state", "state": "idle", "ts": float(i)})
                elapsed = time.monotonic() - t0
                # publish should be O(subscribers); even 4*64 calls should
                # take well under a second.
                self.assertLess(elapsed, 1.0)
            finally:
                sock.close()
        finally:
            h.close()

    def test_slow_subscriber_drop_then_disconnect(self) -> None:
        """≥5 consecutive drops disconnects the subscriber (sees EOF)."""
        h = _ServerHarness()
        h.server.start()
        try:
            sock = _connect(h.path)
            try:
                sock.settimeout(2.0)
                # Read the hello so the queue starts (close to) empty.
                _readline(sock)
                # Stop reading. Flood enough events to overrun the queue
                # plus _DROP_DISCONNECT_THRESHOLD additional drops.
                total = _QUEUE_MAX + _DROP_DISCONNECT_THRESHOLD + 16
                for i in range(total):
                    h.server.publish({
                        "type": "state",
                        "state": "idle",
                        "ts": float(i),
                    })
                # The server should disconnect us after the drop threshold.
                # Drain whatever is buffered, then expect EOF.
                deadline = time.monotonic() + 3.0
                got_eof = False
                buf = bytearray()
                while time.monotonic() < deadline:
                    try:
                        sock.settimeout(0.5)
                        chunk = sock.recv(4096)
                    except socket.timeout:
                        continue
                    if not chunk:
                        got_eof = True
                        break
                    buf.extend(chunk)
                self.assertTrue(
                    got_eof,
                    f"expected EOF after slow-consumer disconnect; got {len(buf)} bytes",
                )
            finally:
                sock.close()
        finally:
            h.close()


class ShutdownTests(unittest.TestCase):
    def test_stop_disconnects_subscribers(self) -> None:
        h = _ServerHarness()
        h.server.start()
        sock = _connect(h.path)
        try:
            sock.settimeout(2.0)
            _readline(sock)  # hello
            # stop() should cause our recv to return b"" (EOF) within a
            # reasonable timeout.
            stopper = threading.Thread(target=h.server.stop, daemon=True)
            stopper.start()
            stopper.join(timeout=3.0)
            self.assertFalse(stopper.is_alive(), "stop() did not return in time")

            sock.settimeout(2.0)
            deadline = time.monotonic() + 2.0
            got_eof = False
            while time.monotonic() < deadline:
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    got_eof = True
                    break
            self.assertTrue(got_eof, "subscriber did not see EOF after stop()")
        finally:
            sock.close()
            h.tmp.cleanup()

    def test_start_is_idempotent(self) -> None:
        h = _ServerHarness()
        h.server.start()
        try:
            h.server.start()  # should be a no-op
            sock = _connect(h.path)
            try:
                line = _readline(sock)
                self.assertEqual(json.loads(line)["type"], "hello")
            finally:
                sock.close()
        finally:
            h.close()


class StateEnumIntegrationTests(unittest.TestCase):
    """Sanity check that ``State.value`` matches the wire vocabulary."""

    def test_state_values(self) -> None:
        self.assertEqual(State.IDLE.value, "idle")
        self.assertEqual(State.RECORDING.value, "recording")
        self.assertEqual(State.TRANSCRIBING.value, "transcribing")
        self.assertEqual(State.ERROR.value, "error")


if __name__ == "__main__":
    unittest.main()
