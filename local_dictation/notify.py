"""Desktop notifications + optional GTK status window.

Both surfaces are entirely optional. `Notifier.on_state` must never raise:
a broken notify-send or a missing PyGObject must not break the dictation
flow. Failures are logged and the offending surface is disabled for the
remainder of the process lifetime.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Any

from .config import Config
from .state import State

log = logging.getLogger(__name__)


# Mapping: state -> (body, urgency, timeout_ms)
_STATE_MAP: dict[State, tuple[str, str, int]] = {
    State.RECORDING:    ("Recording…",    "low",      1500),
    State.TRANSCRIBING: ("Transcribing…", "low",      3000),
    State.IDLE:         ("Pasted",        "low",      1500),
    State.ERROR:        ("",              "critical",    0),
}

_TITLE = "Local Dictation"


class Notifier:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._notify_send_disabled = False
        self._notify_send_warned = False
        self._status_window_disabled = not config.status_window
        self._status_window: _StatusWindow | None = None

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #

    def on_state(self, state: State, detail: str = "") -> None:
        """Update both notification surfaces. Never raises."""
        try:
            if self._config.notifications:
                self._notify_send(state, detail)
        except Exception:  # noqa: BLE001
            log.exception("notify-send dispatch failed (suppressed)")

        try:
            if self._config.status_window and not self._status_window_disabled:
                self._update_status_window(state, detail)
        except Exception:  # noqa: BLE001
            log.exception("status window update failed (suppressed)")
            self._status_window_disabled = True

    # ------------------------------------------------------------------ #
    # notify-send
    # ------------------------------------------------------------------ #

    def _notify_send(self, state: State, detail: str) -> None:
        if self._notify_send_disabled:
            return

        if shutil.which("notify-send") is None:
            if not self._notify_send_warned:
                log.warning("notify-send not on PATH; desktop notifications disabled")
                self._notify_send_warned = True
            self._notify_send_disabled = True
            return

        body, urgency, timeout_ms = _STATE_MAP.get(
            state, (detail or state.name, "low", 1500)
        )
        if state is State.IDLE and detail:
            body = detail
        elif state is State.ERROR:
            body = detail or "Error"

        cmd = [
            "notify-send",
            "-a", "local-dictation",
            "-u", urgency,
        ]
        if timeout_ms > 0:
            cmd += ["-t", str(timeout_ms)]
        cmd += [_TITLE, body]

        try:
            subprocess.run(cmd, check=False, capture_output=True, timeout=2)
        except FileNotFoundError:
            self._notify_send_disabled = True
            log.warning("notify-send disappeared between checks")
        except subprocess.TimeoutExpired:
            log.warning("notify-send timed out")

    # ------------------------------------------------------------------ #
    # GTK status window (lazy)
    # ------------------------------------------------------------------ #

    def _update_status_window(self, state: State, detail: str) -> None:
        if self._status_window is None:
            self._status_window = _StatusWindow.try_create()
            if self._status_window is None:
                self._status_window_disabled = True
                return
        self._status_window.update(state, detail)


# ---------------------------------------------------------------------- #
# GTK status window — defined at module level so type hints resolve,
# but `gi` is imported lazily inside try_create().
# ---------------------------------------------------------------------- #


class _StatusWindow:
    """Tiny always-on-top window showing the current state."""

    def __init__(self, gtk: Any, gdk: Any) -> None:
        self._gtk = gtk
        self._gdk = gdk

        self._window = gtk.Window(title="local-dictation")
        self._window.set_default_size(240, 60)
        self._window.set_resizable(False)
        self._window.set_keep_above(True)
        self._window.set_decorated(False)
        self._window.set_skip_taskbar_hint(True)

        self._label = gtk.Label(label="IDLE")
        self._label.set_justify(gtk.Justification.CENTER)
        self._window.add(self._label)
        self._window.show_all()

    @classmethod
    def try_create(cls) -> "_StatusWindow | None":
        try:
            import gi  # type: ignore[import-not-found]
            gi.require_version("Gtk", "3.0")
            from gi.repository import Gtk, Gdk  # type: ignore[import-not-found]
        except Exception:  # noqa: BLE001
            log.warning("PyGObject/Gtk 3 unavailable; status window disabled")
            return None
        try:
            return cls(Gtk, Gdk)
        except Exception:  # noqa: BLE001
            log.exception("failed to construct GTK status window; disabling")
            return None

    def update(self, state: State, detail: str) -> None:
        text = state.name
        if detail:
            text = f"{state.name}\n{detail}"
        self._label.set_text(text)

        # Colour the label red on ERROR, default otherwise. We use CSS via
        # the gtk style context for forward-compatibility.
        ctx = self._label.get_style_context()
        if state is State.ERROR:
            ctx.add_class("local-dictation-error")
        else:
            ctx.remove_class("local-dictation-error")

        # Pump pending GTK events so the label visibly updates without a
        # main loop. This is best-effort.
        while self._gtk.events_pending():
            self._gtk.main_iteration_do(False)
