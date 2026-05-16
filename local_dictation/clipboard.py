"""Thin wrappers around wl-clipboard.

The functions here only handle text-shaped clipboards. Binary MIME types
(images, files) are not preserved by `restore()`.
"""

from __future__ import annotations

import logging
import os
import subprocess

log = logging.getLogger(__name__)


def save() -> bytes | None:
    """Return current `wl-paste -n` output as bytes.

    Returns None if the clipboard is empty, wl-paste is missing, or the
    current selection is non-text (wl-paste exits non-zero in that case).
    """
    try:
        proc = subprocess.run(
            ["wl-paste", "-n"],
            capture_output=True,
            check=False,
            timeout=2,
        )
    except FileNotFoundError:
        log.warning("wl-paste not found; cannot save clipboard")
        return None
    except subprocess.TimeoutExpired:
        log.warning("wl-paste timed out; skipping clipboard save")
        return None

    if proc.returncode != 0:
        # wl-paste returns non-zero when the clipboard is empty or the
        # MIME type does not match. Treat as "nothing to save".
        return None

    data = proc.stdout
    if not data:
        return None
    return data


def restore(data: bytes) -> None:
    """Restore `data` to the clipboard after a short delay, fully detached.

    Spawns a helper that sleeps ~250 ms and then runs `wl-copy` with the
    data on stdin. The helper survives the parent dying. Returns
    immediately.
    """
    if not data:
        return

    # Use `sh -c` so we can pipe a sleep into wl-copy without leaving stdin
    # attached to the parent. wl-copy itself forks into the background once
    # it owns the selection, so the helper exits cleanly. start_new_session
    # detaches us from the parent's process group so the helper survives
    # parent exit.
    try:
        proc = subprocess.Popen(
            ["sh", "-c", "sleep 0.25; exec wl-copy"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except FileNotFoundError:
        log.warning("wl-copy not found; cannot restore clipboard")
        return
    except OSError as e:
        log.warning("failed to spawn clipboard restore helper: %s", e)
        return

    assert proc.stdin is not None
    try:
        proc.stdin.write(data)
        proc.stdin.close()
    except OSError as e:
        log.warning("failed to write clipboard data to restore helper: %s", e)


def copy(text: str) -> None:
    """Set the clipboard to `text`. Raises CalledProcessError on failure."""
    subprocess.run(
        ["wl-copy"],
        input=text.encode("utf-8"),
        check=True,
    )


def ydotoold_socket_path() -> str | None:
    """Return the ydotoold socket path if one is reachable, else None."""
    env = os.environ.get("YDOTOOL_SOCKET")
    if env and os.path.exists(env):
        return env
    default = "/tmp/.ydotool_socket"
    if os.path.exists(default):
        return default
    return None
