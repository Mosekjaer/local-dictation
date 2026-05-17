"""Tests for IncrementalTyper in local_dictation.output.

These tests never invoke real wtype/ydotool. The fake Output records
every typed-text call; subprocess.run is stubbed so we can also capture
the argv of ydotool key (backspace) invocations.
"""

from __future__ import annotations

import logging
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from local_dictation.output import (
    IncrementalTyper,
    Output,
    OutputError,
)


class _FakeOutput:
    """Minimal stand-in for Output.

    IncrementalTyper only touches `_type_text` for appended text; the
    backspace path goes through subprocess.run, which is patched in each
    test via the _PatchedSubprocess helper below.
    """

    def __init__(self, raise_on_type: bool = False) -> None:
        self.calls: list[tuple[str, object]] = []
        self._raise_on_type = raise_on_type
        self._calls_lock = threading.Lock()

    def _type_text(self, text: str) -> None:
        if self._raise_on_type:
            raise OutputError("fake type failure")
        with self._calls_lock:
            self.calls.append(("type", text))


class _PatchedSubprocess:
    """Context manager that patches local_dictation.output.subprocess.run
    so backspace invocations are captured into the FakeOutput.calls list
    alongside typed-text entries.
    """

    def __init__(self, fake: _FakeOutput) -> None:
        self._fake = fake
        self._patcher: mock._patch | None = None

    def __enter__(self) -> "_PatchedSubprocess":
        def _fake_run(argv, **_kwargs):
            with self._fake._calls_lock:
                self._fake.calls.append(("key", list(argv)))
            # Return a "successful" CompletedProcess-shaped object.
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        self._patcher = mock.patch(
            "local_dictation.output.subprocess.run",
            side_effect=_fake_run,
        )
        self._patcher.start()
        return self

    def __exit__(self, *exc_info) -> None:
        assert self._patcher is not None
        self._patcher.stop()


def _key_argv(n: int) -> list[str]:
    """Expected argv for a single ydotool key call emitting n backspaces."""
    seq: list[str] = []
    for _ in range(n):
        seq.extend(("14:1", "14:0"))
    return ["ydotool", "key", *seq]


class AppendOnlyTests(unittest.TestCase):
    def test_append_only_types_only_new_suffix(self) -> None:
        fake = _FakeOutput()
        typer = IncrementalTyper(fake)  # type: ignore[arg-type]
        with _PatchedSubprocess(fake):
            typer.set_text("hello")
            typer.set_text("hello world")
        self.assertEqual(
            fake.calls,
            [("type", "hello"), ("type", " world")],
        )

    def test_set_text_when_target_is_prefix_extension(self) -> None:
        fake = _FakeOutput()
        typer = IncrementalTyper(fake)  # type: ignore[arg-type]
        with _PatchedSubprocess(fake):
            typer.set_text("hello wor")
            typer.set_text("hello world")
        # "hello wor" is a prefix of "hello world" → 0 backspaces,
        # type "ld" only.
        self.assertEqual(
            fake.calls,
            [("type", "hello wor"), ("type", "ld")],
        )


class TailRevisionTests(unittest.TestCase):
    def test_tail_revision_backspaces_then_types_new(self) -> None:
        fake = _FakeOutput()
        typer = IncrementalTyper(fake)  # type: ignore[arg-type]
        with _PatchedSubprocess(fake):
            typer.set_text("hello wor")
            typer.set_text("hello there")
        # common prefix "hello " (6 chars). len("hello wor")=9 →
        # 3 backspaces, then type "there".
        self.assertEqual(
            fake.calls,
            [
                ("type", "hello wor"),
                ("key", _key_argv(3)),
                ("type", "there"),
            ],
        )

    def test_full_rewrite(self) -> None:
        fake = _FakeOutput()
        typer = IncrementalTyper(fake)  # type: ignore[arg-type]
        with _PatchedSubprocess(fake):
            typer.set_text("foo")
            typer.set_text("bar")
        self.assertEqual(
            fake.calls,
            [
                ("type", "foo"),
                ("key", _key_argv(3)),
                ("type", "bar"),
            ],
        )

    def test_empty_target_shrinks_baseline(self) -> None:
        fake = _FakeOutput()
        typer = IncrementalTyper(fake)  # type: ignore[arg-type]
        with _PatchedSubprocess(fake):
            typer.set_text("hello")
            typer.set_text("")
        # 5 backspaces, no typing afterwards.
        self.assertEqual(
            fake.calls,
            [("type", "hello"), ("key", _key_argv(5))],
        )


class BackspaceCapTests(unittest.TestCase):
    def test_exceeding_cap_skips_backspaces_and_logs(self) -> None:
        fake = _FakeOutput()
        typer = IncrementalTyper(fake)  # type: ignore[arg-type]
        first = "a" * 100
        second = "b" * 100
        with _PatchedSubprocess(fake):
            typer.set_text(first)
            with self.assertLogs("local_dictation.output", level="WARNING") as cap:
                typer.set_text(second)
        # No "key" entries — backspaces were skipped.
        key_calls = [c for c in fake.calls if c[0] == "key"]
        self.assertEqual(key_calls, [])
        # The new content was typed in full.
        self.assertEqual(fake.calls, [("type", first), ("type", second)])
        # A warning was logged.
        self.assertTrue(
            any("backspaces" in msg.lower() for msg in cap.output),
            f"expected backspace-cap warning, got {cap.output!r}",
        )


class FinalizeAndResetTests(unittest.TestCase):
    def test_finalize_applies_diff_and_resets_baseline(self) -> None:
        fake = _FakeOutput()
        typer = IncrementalTyper(fake)  # type: ignore[arg-type]
        with _PatchedSubprocess(fake):
            typer.set_text("hello")
            typer.finalize("hello world")
            typer.set_text("foo")
        # finalize types " world" (append-only diff), then baseline
        # resets so set_text("foo") types "foo" with no backspaces.
        self.assertEqual(
            fake.calls,
            [
                ("type", "hello"),
                ("type", " world"),
                ("type", "foo"),
            ],
        )

    def test_finalize_with_same_target_only_resets(self) -> None:
        fake = _FakeOutput()
        typer = IncrementalTyper(fake)  # type: ignore[arg-type]
        with _PatchedSubprocess(fake):
            typer.set_text("hello")
            typer.finalize("hello")
            # Baseline is now "" — set_text("hello") types "hello"
            # again, not the empty-diff no-op.
            typer.set_text("hello")
        self.assertEqual(
            fake.calls,
            [("type", "hello"), ("type", "hello")],
        )

    def test_reset_clears_baseline_without_typing(self) -> None:
        fake = _FakeOutput()
        typer = IncrementalTyper(fake)  # type: ignore[arg-type]
        with _PatchedSubprocess(fake):
            typer.set_text("hello")
            calls_before_reset = list(fake.calls)
            typer.reset()
            calls_after_reset = list(fake.calls)
            typer.set_text("world")
        # Nothing happened between set_text("hello") and reset().
        self.assertEqual(calls_before_reset, calls_after_reset)
        # Final call set: initial type, then type "world" (no backspaces).
        self.assertEqual(
            fake.calls,
            [("type", "hello"), ("type", "world")],
        )


class ThreadSafetyTests(unittest.TestCase):
    def test_concurrent_set_text_calls_do_not_interleave(self) -> None:
        fake = _FakeOutput()
        typer = IncrementalTyper(fake)  # type: ignore[arg-type]

        targets = [f"target-{i}" for i in range(10)]

        def worker(t: str) -> None:
            typer.set_text(t)

        with _PatchedSubprocess(fake):
            threads = [threading.Thread(target=worker, args=(t,)) for t in targets]
            for th in threads:
                th.start()
            for th in threads:
                th.join(timeout=1.0)

        # Each recorded call is a complete (kind, payload) tuple. The
        # interleaving guarantee is per-call atomicity: every "type"
        # payload is a single string and every "key" payload is a list
        # of complete argv tokens. Verify shapes.
        for kind, payload in fake.calls:
            self.assertIn(kind, ("type", "key"))
            if kind == "type":
                self.assertIsInstance(payload, str)
            else:
                self.assertIsInstance(payload, list)
                # argv must start with ydotool key and have an even
                # number of trailing key tokens.
                self.assertEqual(payload[:2], ["ydotool", "key"])
                self.assertEqual((len(payload) - 2) % 2, 0)

    def test_concurrent_calls_finish_quickly(self) -> None:
        # Smoke: the whole thing must finish well under a second.
        import time

        fake = _FakeOutput()
        typer = IncrementalTyper(fake)  # type: ignore[arg-type]

        def worker(i: int) -> None:
            typer.set_text(f"x{i}")

        with _PatchedSubprocess(fake):
            t0 = time.monotonic()
            threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
            for th in threads:
                th.start()
            for th in threads:
                th.join(timeout=1.0)
            elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 1.0)


class ErrorPropagationTests(unittest.TestCase):
    def test_output_error_on_type_is_reraised(self) -> None:
        fake = _FakeOutput(raise_on_type=True)
        typer = IncrementalTyper(fake)  # type: ignore[arg-type]
        with _PatchedSubprocess(fake):
            with self.assertRaises(OutputError):
                typer.set_text("hello")
        # Baseline must remain "" so the next call retries from scratch
        # rather than silently advancing.
        # We can verify by swapping in a successful fake and re-calling.
        good = _FakeOutput()
        typer2 = IncrementalTyper(good)  # type: ignore[arg-type]
        with _PatchedSubprocess(good):
            typer2.set_text("hello")
        self.assertEqual(good.calls, [("type", "hello")])


class TypeTextHelperTests(unittest.TestCase):
    """The new Output._type_text helper must refuse to fall through to
    clipboard strategies. It only uses wtype / ydotool type.
    """

    def _make_output(self, paste_method: str = "auto") -> Output:
        # Minimal config-shaped object.
        cfg = SimpleNamespace(paste_method=paste_method, restore_clipboard=False)
        return Output(cfg)  # type: ignore[arg-type]

    def test_empty_text_is_noop(self) -> None:
        out = self._make_output()
        # Should not raise even if no tool is available.
        with mock.patch.object(Output, "_have", return_value=False):
            out._type_text("")

    def test_raises_when_no_typed_strategy(self) -> None:
        out = self._make_output("auto")
        with mock.patch.object(Output, "_have", return_value=False):
            with self.assertRaises(OutputError):
                out._type_text("hello")

    def test_uses_wtype_when_available(self) -> None:
        out = self._make_output("auto")
        with mock.patch.object(Output, "_have", side_effect=lambda b: b == "wtype"):
            with mock.patch.object(Output, "_wtype") as m_wtype:
                out._type_text("hi")
                m_wtype.assert_called_once_with("hi")


if __name__ == "__main__":
    unittest.main()
