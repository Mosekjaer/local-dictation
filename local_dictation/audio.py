"""ffmpeg-based PulseAudio recorder.

Spawns ``ffmpeg`` to capture audio from a PulseAudio source into a WAV file.
The process is started non-blocking; ``stop()`` requests a clean shutdown via
ffmpeg's ``q`` command on stdin, escalating to SIGINT/SIGTERM/SIGKILL if
necessary.

This module is owned by the media agent. The Recorder does not delete WAVs
after use; see :func:`cleanup_stale_wavs` for the helper the daemon calls on
startup.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .config import Config

log = logging.getLogger(__name__)


class RecorderError(Exception):
    pass


class Recorder:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._proc: subprocess.Popen[bytes] | None = None
        self._wav_path: Path | None = None

    def start(self) -> Path:
        """Start ffmpeg, return the WAV path it is writing to.

        Raises :class:`RecorderError` on failure. Non-blocking.
        """
        if self._proc is not None and self._proc.poll() is None:
            raise RecorderError("already recording")

        if shutil.which("ffmpeg") is None:
            raise RecorderError("ffmpeg not found")

        temp_dir = self._config.temp_dir
        try:
            temp_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RecorderError(f"cannot create temp_dir {temp_dir}: {exc}") from exc

        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        wav_path = temp_dir / f"rec-{ts}-{os.getpid()}.wav"

        cmd = [
            "ffmpeg",
            "-f",
            "pulse",
            "-i",
            str(self._config.audio_source),
            "-ac",
            str(self._config.channels),
            "-ar",
            str(self._config.sample_rate),
            "-y",
            str(wav_path),
        ]
        log.debug("starting ffmpeg: %s", " ".join(cmd))

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise RecorderError("ffmpeg not found") from exc
        except OSError as exc:
            raise RecorderError(f"failed to spawn ffmpeg: {exc}") from exc

        # Give ffmpeg a brief moment to fail fast (bad source, etc.).
        time.sleep(0.1)
        if proc.poll() is not None:
            stderr_bytes = b""
            try:
                if proc.stderr is not None:
                    stderr_bytes = proc.stderr.read() or b""
            except Exception:  # pragma: no cover - defensive
                pass
            stderr_tail = stderr_bytes.decode("utf-8", errors="replace")[-500:]
            raise RecorderError(f"ffmpeg exited immediately: {stderr_tail}")

        self._proc = proc
        self._wav_path = wav_path
        return wav_path

    def stop(self) -> Path:
        """Signal ffmpeg to stop and wait for clean exit.

        Returns the finalised WAV path. Raises :class:`RecorderError` on
        failure or if :meth:`start` was not called.
        """
        if self._proc is None or self._wav_path is None:
            raise RecorderError("not recording")

        proc = self._proc
        wav_path = self._wav_path

        # Already exited?
        if proc.poll() is not None:
            self._proc = None
            self._wav_path = None
            rc = proc.returncode
            stderr_tail = self._read_stderr_tail(proc)
            if rc != 0:
                raise RecorderError(
                    f"ffmpeg exited with rc={rc} stderr={stderr_tail}"
                )
            return wav_path

        # Step 1: try graceful "q\n" on stdin.
        try:
            if proc.stdin is not None and not proc.stdin.closed:
                try:
                    proc.stdin.write(b"q\n")
                    proc.stdin.flush()
                except (BrokenPipeError, OSError) as exc:
                    log.debug("stdin write failed during stop: %s", exc)
                try:
                    proc.stdin.close()
                except Exception:  # pragma: no cover - defensive
                    pass
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("error closing stdin: %s", exc)

        if not self._wait(proc, 2.0):
            log.warning("ffmpeg did not exit after q; sending SIGINT")
            self._signal(proc, signal.SIGINT)
            if not self._wait(proc, 2.0):
                log.warning("ffmpeg did not exit after SIGINT; sending SIGTERM")
                self._signal(proc, signal.SIGTERM)
                if not self._wait(proc, 2.0):
                    log.warning("ffmpeg did not exit after SIGTERM; sending SIGKILL")
                    self._signal(proc, signal.SIGKILL)
                    self._wait(proc, 2.0)

        self._proc = None
        self._wav_path = None

        rc = proc.returncode
        stderr_tail = self._read_stderr_tail(proc)

        # ffmpeg returns 0 on clean "q" stop; non-zero on signal kill is
        # tolerated as long as the WAV exists with non-zero size.
        if rc != 0:
            if wav_path.exists() and wav_path.stat().st_size > 0:
                log.info(
                    "ffmpeg rc=%s but WAV exists (%d bytes); accepting",
                    rc,
                    wav_path.stat().st_size,
                )
            else:
                raise RecorderError(
                    f"ffmpeg failed: rc={rc} stderr={stderr_tail}"
                )

        if not wav_path.exists():
            raise RecorderError(f"WAV file missing after stop: {wav_path}")

        return wav_path

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ---- internals ----

    @staticmethod
    def _wait(proc: subprocess.Popen[bytes], timeout: float) -> bool:
        try:
            proc.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False

    @staticmethod
    def _signal(proc: subprocess.Popen[bytes], sig: signal.Signals) -> None:
        try:
            proc.send_signal(sig)
        except ProcessLookupError:
            pass
        except OSError as exc:  # pragma: no cover - defensive
            log.debug("send_signal(%s) failed: %s", sig, exc)

    @staticmethod
    def _read_stderr_tail(proc: subprocess.Popen[bytes]) -> str:
        if proc.stderr is None:
            return ""
        try:
            data = proc.stderr.read() or b""
        except Exception:  # pragma: no cover - defensive
            return ""
        return data.decode("utf-8", errors="replace")[-500:]


def cleanup_stale_wavs(temp_dir: Path, max_age_hours: int = 24) -> int:
    """Delete ``rec-*.wav`` files older than ``max_age_hours``.

    Returns count deleted. Best-effort; logs and continues on errors.
    Never raises.
    """
    if not temp_dir.exists():
        return 0

    cutoff = time.time() - (max_age_hours * 3600)
    deleted = 0
    try:
        entries = list(temp_dir.glob("rec-*.wav"))
    except OSError as exc:
        log.warning("cleanup_stale_wavs: cannot list %s: %s", temp_dir, exc)
        return 0

    for path in entries:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                deleted += 1
        except FileNotFoundError:
            continue
        except OSError as exc:
            log.warning("cleanup_stale_wavs: cannot remove %s: %s", path, exc)
            continue

    if deleted:
        log.info("cleaned up %d stale WAV file(s) from %s", deleted, temp_dir)
    return deleted
