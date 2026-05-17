"""Paste strategy with fallback chain.

Order (when `config.paste_method = "auto"`):

1. `wtype` if installed — direct text injection via the wlroots
   virtual-keyboard protocol. Does not touch the clipboard. Returns
   "typed". Not supported by every COSMIC build.
2. `ydotool type --file -` if ydotool and a reachable ydotoold socket
   are present — direct character injection via /dev/uinput, layout-
   dependent. Does not touch the clipboard. Returns "typed".
3. `wl-copy` + `ydotool key 29:1 47:1 47:0 29:0` (Ctrl+V) if ydotool and
   a reachable ydotoold socket are present. Mutates the clipboard.
   Returns "pasted".
4. `wl-copy` only. The user pastes manually. Returns "copied_only".

If `config.paste_method` is set explicitly we honour it strictly and never
fall through to a different strategy.

When `config.restore_clipboard` is true and the active strategy mutates
the clipboard (cases 3 and 4), `clipboard.save()` runs before and a
detached `clipboard.restore()` runs ~250 ms after.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from typing import Literal

from . import clipboard
from .config import Config

PasteMethod = Literal["auto", "wtype", "ydotool_type", "ydotool", "clipboard"]
PasteResult = Literal["typed", "pasted", "copied_only"]

log = logging.getLogger(__name__)

# Ctrl(29) down, V(47) down, V up, Ctrl up. ydotool key syntax.
_CTRL_V_KEYS = ["29:1", "47:1", "47:0", "29:0"]


class OutputError(Exception):
    """Raised when every viable paste strategy has failed."""


class Output:
    def __init__(self, config: Config) -> None:
        self._config = config

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #

    def paste(self, text: str) -> PasteResult:
        """Run the configured paste strategy. Returns the strategy used.

        Raises OutputError only when every applicable fallback failed.
        """
        if not text:
            # Nothing to do but the caller should still see a sensible
            # return value. Treat as a no-op copy.
            log.debug("paste() called with empty text; treating as copied_only")
            return "copied_only"

        method = self._config.paste_method
        errors: list[str] = []

        # Strategy 1: wtype
        if method in ("auto", "wtype"):
            if self._have("wtype"):
                try:
                    self._wtype(text)
                    return "typed"
                except OutputError as e:
                    errors.append(f"wtype: {e}")
                    if method == "wtype":
                        raise
            else:
                if method == "wtype":
                    raise OutputError("wtype not installed but paste_method=wtype")
                errors.append("wtype: not installed")

        # Strategy 2: ydotool type — direct typing, no clipboard mutation
        if method in ("auto", "ydotool_type"):
            if self._have("ydotool") and clipboard.ydotoold_socket_path():
                try:
                    self._ydotool_type(text)
                    return "typed"
                except OutputError as e:
                    errors.append(f"ydotool_type: {e}")
                    if method == "ydotool_type":
                        raise
            else:
                missing = []
                if not self._have("ydotool"):
                    missing.append("ydotool")
                if not clipboard.ydotoold_socket_path():
                    missing.append("ydotoold socket")
                msg = "ydotool_type unavailable: " + ", ".join(missing)
                if method == "ydotool_type":
                    raise OutputError(msg)
                errors.append(msg)

        # Strategy 3: clipboard + ydotool Ctrl+V
        if method in ("auto", "ydotool"):
            if self._have("ydotool") and self._have("wl-copy") and clipboard.ydotoold_socket_path():
                try:
                    self._clipboard_paste(text)
                    return "pasted"
                except OutputError as e:
                    errors.append(f"ydotool: {e}")
                    if method == "ydotool":
                        raise
            else:
                missing = []
                if not self._have("ydotool"):
                    missing.append("ydotool")
                if not self._have("wl-copy"):
                    missing.append("wl-copy")
                if not clipboard.ydotoold_socket_path():
                    missing.append("ydotoold socket")
                msg = "ydotool path unavailable: " + ", ".join(missing)
                if method == "ydotool":
                    raise OutputError(msg)
                errors.append(msg)

        # Strategy 4: wl-copy only
        if method in ("auto", "clipboard"):
            if self._have("wl-copy"):
                try:
                    self._clipboard_only(text)
                    return "copied_only"
                except OutputError as e:
                    errors.append(f"wl-copy: {e}")
                    if method == "clipboard":
                        raise
            else:
                msg = "wl-copy not installed"
                if method == "clipboard":
                    raise OutputError(msg)
                errors.append(msg)

        raise OutputError("all paste strategies failed: " + " | ".join(errors))

    # ------------------------------------------------------------------ #
    # strategies
    # ------------------------------------------------------------------ #

    def _wtype(self, text: str) -> None:
        # `wtype -` reads stdin and types it. No clipboard mutation, so we
        # skip the save/restore dance.
        try:
            proc = subprocess.run(
                ["wtype", "-"],
                input=text.encode("utf-8"),
                capture_output=True,
                check=False,
                timeout=30,
            )
        except FileNotFoundError as e:
            raise OutputError(f"wtype binary disappeared: {e}") from e
        except subprocess.TimeoutExpired as e:
            raise OutputError("wtype timed out") from e
        if proc.returncode != 0:
            raise OutputError(
                f"wtype exited {proc.returncode}: {proc.stderr.decode(errors='replace').strip()}"
            )

    def _ydotool_type(self, text: str) -> None:
        # `ydotool type --file -` reads UTF-8 from stdin and synthesises
        # the corresponding keypresses through /dev/uinput. Layout-
        # dependent: characters not reachable on the active keymap may
        # be mistyped. No clipboard mutation.
        try:
            proc = subprocess.run(
                ["ydotool", "type", "--file", "-"],
                input=text.encode("utf-8"),
                capture_output=True,
                check=False,
                timeout=30,
            )
        except FileNotFoundError as e:
            raise OutputError(f"ydotool binary disappeared: {e}") from e
        except subprocess.TimeoutExpired as e:
            raise OutputError("ydotool type timed out") from e
        if proc.returncode != 0:
            raise OutputError(
                f"ydotool type exited {proc.returncode}: "
                f"{proc.stderr.decode(errors='replace').strip()}"
            )

    def _clipboard_paste(self, text: str) -> None:
        saved = clipboard.save() if self._config.restore_clipboard else None

        try:
            clipboard.copy(text)
        except subprocess.CalledProcessError as e:
            raise OutputError(f"wl-copy failed: {e}") from e
        except FileNotFoundError as e:
            raise OutputError(f"wl-copy missing: {e}") from e

        try:
            proc = subprocess.run(
                ["ydotool", "key", *_CTRL_V_KEYS],
                capture_output=True,
                check=False,
                timeout=5,
            )
        except FileNotFoundError as e:
            raise OutputError(f"ydotool missing: {e}") from e
        except subprocess.TimeoutExpired as e:
            raise OutputError("ydotool timed out") from e
        if proc.returncode != 0:
            raise OutputError(
                f"ydotool exited {proc.returncode}: {proc.stderr.decode(errors='replace').strip()}"
            )

        if saved is not None:
            clipboard.restore(saved)

    def _clipboard_only(self, text: str) -> None:
        saved = clipboard.save() if self._config.restore_clipboard else None
        try:
            clipboard.copy(text)
        except subprocess.CalledProcessError as e:
            raise OutputError(f"wl-copy failed: {e}") from e
        except FileNotFoundError as e:
            raise OutputError(f"wl-copy missing: {e}") from e

        if saved is not None:
            clipboard.restore(saved)

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _have(binary: str) -> bool:
        return shutil.which(binary) is not None

    # Private helper used by IncrementalTyper. Live mode must never mutate
    # the clipboard for every revision, so we only allow the two direct-
    # typing strategies here; the clipboard-paste fallbacks are off-limits.
    def _type_text(self, text: str) -> None:
        """Type `text` using wtype or ydotool. No clipboard mutation.

        Raises OutputError if neither typed strategy is usable.
        """
        if not text:
            return

        method = self._config.paste_method
        errors: list[str] = []

        if method in ("auto", "wtype"):
            if self._have("wtype"):
                try:
                    self._wtype(text)
                    return
                except OutputError as e:
                    errors.append(f"wtype: {e}")
                    if method == "wtype":
                        raise
            else:
                if method == "wtype":
                    raise OutputError("wtype not installed but paste_method=wtype")
                errors.append("wtype: not installed")

        if method in ("auto", "ydotool_type"):
            if self._have("ydotool") and clipboard.ydotoold_socket_path():
                try:
                    self._ydotool_type(text)
                    return
                except OutputError as e:
                    errors.append(f"ydotool_type: {e}")
                    if method == "ydotool_type":
                        raise
            else:
                missing = []
                if not self._have("ydotool"):
                    missing.append("ydotool")
                if not clipboard.ydotoold_socket_path():
                    missing.append("ydotoold socket")
                msg = "ydotool_type unavailable: " + ", ".join(missing)
                if method == "ydotool_type":
                    raise OutputError(msg)
                errors.append(msg)

        raise OutputError(
            "no direct-typing strategy available for live mode: "
            + " | ".join(errors)
        )


# ---------------------------------------------------------------------- #
# Incremental typer for live dictation
# ---------------------------------------------------------------------- #


# One backspace as ydotool key tokens: key 14 (KEY_BACKSPACE) down, then up.
_BACKSPACE_KEYS = ("14:1", "14:0")


class IncrementalTyper:
    """Wrap an Output instance with diff-against-last-call typing.

    Thread-safe. Calls to set_text() / finalize() are serialised by an
    internal lock so concurrent invocations from the StreamEngine worker
    thread don't interleave subprocess calls.
    """

    # Sanity cap on backspaces emitted in a single revision.
    # If the diff implies > BACKSPACE_CAP characters to delete, the typer
    # logs a warning and only types the new suffix (no backspaces). The
    # final_wav pass at the end of the utterance will re-sync via
    # finalize().
    BACKSPACE_CAP: int = 50

    def __init__(self, output: Output) -> None:
        self._output = output
        self._last = ""
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #

    def set_text(self, target: str) -> None:
        """Make the focused window's text-since-baseline match `target`.

        Computes longest common prefix vs the last typed state; emits
        N backspaces for the changed suffix, then types the new suffix.

        If N > BACKSPACE_CAP, types the new suffix only and logs a
        warning. Raises OutputError if the underlying Output strategy
        fails.
        """
        with self._lock:
            self._apply_locked(target)

    def finalize(self, target: str) -> None:
        """Same as set_text but also resets internal baseline to "" so a
        subsequent live session starts fresh.
        """
        with self._lock:
            self._apply_locked(target)
            self._last = ""

    def reset(self) -> None:
        """Forget the baseline without typing anything. Used if the
        daemon needs to abandon the current session (e.g. transitioning
        to ERROR).
        """
        with self._lock:
            self._last = ""

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _apply_locked(self, target: str) -> None:
        last = self._last
        if target == last:
            return

        prefix_len = _common_prefix_len(last, target)
        backspaces = len(last) - prefix_len
        suffix = target[prefix_len:]

        if backspaces > self.BACKSPACE_CAP:
            log.warning(
                "IncrementalTyper: diff implies %d backspaces (> cap %d); "
                "appending new suffix only — final pass will re-sync",
                backspaces,
                self.BACKSPACE_CAP,
            )
            if suffix:
                self._output._type_text(suffix)
            # Treat the visible state as "last + suffix" — we did not
            # backspace, so the on-screen text is now last+suffix. Set
            # baseline to that so subsequent diffs are computed against
            # the actual screen state.
            self._last = last + suffix
            return

        if backspaces > 0:
            self._emit_backspaces(backspaces)

        if suffix:
            self._output._type_text(suffix)

        self._last = target

    def _emit_backspaces(self, count: int) -> None:
        # Emit ALL backspaces in a single ydotool key invocation. One
        # subprocess call per keystroke would be wasteful and racy.
        argv = ["ydotool", "key", *(_BACKSPACE_KEYS * count)]
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                check=False,
                timeout=5,
            )
        except FileNotFoundError as e:
            raise OutputError(f"ydotool missing for backspace: {e}") from e
        except subprocess.TimeoutExpired as e:
            raise OutputError("ydotool key (backspace) timed out") from e
        if proc.returncode != 0:
            raise OutputError(
                f"ydotool key (backspace) exited {proc.returncode}: "
                f"{proc.stderr.decode(errors='replace').strip()}"
            )


def _common_prefix_len(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i
