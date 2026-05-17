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
import math
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from .config import Config

log = logging.getLogger(__name__)


# Minimum spacing between meter callbacks (~10 Hz). ffmpeg's astats emits
# metadata once per audio frame, which at 16 kHz mono is ~20 Hz; we
# throttle the callback to roughly the rate documented in the broadcast
# protocol.
_METER_MIN_INTERVAL_S = 0.09

# dB clamp matches events.clamp_db; duplicated here to avoid a cross-module
# import in the hot path.
_METER_FLOOR_DB = -120.0

_PEAK_RE = re.compile(
    r"lavfi\.astats\.Overall\.Peak_level\s*=\s*([\-+0-9.eE]+|nan|-?inf)"
)
_RMS_RE = re.compile(
    r"lavfi\.astats\.Overall\.RMS_level\s*=\s*([\-+0-9.eE]+|nan|-?inf)"
)


def _parse_db(raw: str) -> float:
    raw = raw.strip().lower()
    if raw in ("-inf", "+inf", "inf", "nan"):
        return _METER_FLOOR_DB
    try:
        v = float(raw)
    except ValueError:
        return _METER_FLOOR_DB
    if math.isnan(v) or math.isinf(v):
        return _METER_FLOOR_DB
    if v < _METER_FLOOR_DB:
        return _METER_FLOOR_DB
    if v > 0.0:
        return 0.0
    return v


class RecorderError(Exception):
    pass


class Recorder:
    def __init__(
        self,
        config: Config,
        meter_callback: Callable[[float, float], None] | None = None,
    ) -> None:
        self._config = config
        self._proc: subprocess.Popen[bytes] | None = None
        self._wav_path: Path | None = None
        self._meter_callback = meter_callback
        self._meter_thread: threading.Thread | None = None
        self._meter_read_fd: int | None = None

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

        meter_r_fd: int | None = None
        meter_w_fd: int | None = None
        pass_fds: tuple[int, ...] = ()
        af_argv: list[str] = []

        if self._meter_callback is not None:
            try:
                meter_r_fd, meter_w_fd = os.pipe()
            except OSError as exc:
                log.warning("meter pipe creation failed: %s; metering disabled", exc)
                meter_r_fd = None
                meter_w_fd = None

            if meter_w_fd is not None:
                # astats emits metadata once per audio frame; we throttle in
                # the reader. ``length`` is the analysis window in seconds.
                # Two ametadata=print filters route Peak_level and RMS_level
                # to the same numbered fd; ``direct=1`` disables buffering so
                # we see lines in real time.
                filt = (
                    "astats=metadata=1:reset=1:length=0.1,"
                    f"ametadata=print:file=/dev/fd/{meter_w_fd}"
                    ":direct=1:key=lavfi.astats.Overall.Peak_level,"
                    f"ametadata=print:file=/dev/fd/{meter_w_fd}"
                    ":direct=1:key=lavfi.astats.Overall.RMS_level"
                )
                af_argv = ["-af", filt]
                pass_fds = (meter_w_fd,)

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
            *af_argv,
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
                pass_fds=pass_fds,
            )
        except FileNotFoundError as exc:
            self._close_fd(meter_r_fd)
            self._close_fd(meter_w_fd)
            raise RecorderError("ffmpeg not found") from exc
        except OSError as exc:
            self._close_fd(meter_r_fd)
            self._close_fd(meter_w_fd)
            raise RecorderError(f"failed to spawn ffmpeg: {exc}") from exc

        # The child now owns its copy of the write end; close ours so EOF
        # propagates to the reader thread when ffmpeg exits.
        if meter_w_fd is not None:
            self._close_fd(meter_w_fd)
            meter_w_fd = None

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
            self._close_fd(meter_r_fd)
            raise RecorderError(f"ffmpeg exited immediately: {stderr_tail}")

        self._proc = proc
        self._wav_path = wav_path

        # Start the meter reader thread last so it isn't running if the
        # spawn failed.
        if meter_r_fd is not None and self._meter_callback is not None:
            self._meter_read_fd = meter_r_fd
            t = threading.Thread(
                target=self._meter_reader,
                args=(meter_r_fd, self._meter_callback),
                name="ld-meter-reader",
                daemon=True,
            )
            self._meter_thread = t
            t.start()

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

        # Tear the meter reader down before resetting state so the thread
        # cannot outlive this Recorder instance and leak the read fd.
        self._teardown_meter()

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

    def _teardown_meter(self) -> None:
        """Close the meter pipe and join the reader thread.

        Safe to call when no meter is active; idempotent.
        """
        fd = self._meter_read_fd
        self._meter_read_fd = None
        if fd is not None:
            # Closing the read end is enough to make os.fdopen()'s reader
            # see EOF on its next iteration (combined with ffmpeg having
            # already exited, which closes the write end).
            self._close_fd(fd)

        t = self._meter_thread
        self._meter_thread = None
        if t is not None and t.is_alive():
            t.join(timeout=1.0)
            if t.is_alive():  # pragma: no cover - defensive
                log.warning("meter reader thread did not exit within 1s")

    def _meter_reader(
        self,
        read_fd: int,
        callback: Callable[[float, float], None],
    ) -> None:
        """Parse astats lines from ``read_fd`` and dispatch to ``callback``.

        The filter graph emits Peak_level then RMS_level for each audio
        frame. We accumulate one Peak and one RMS, then fire the callback
        once per pair, throttled to ~10 Hz.
        """
        try:
            stream = os.fdopen(read_fd, "r", buffering=1)
        except OSError as exc:
            log.warning("meter reader: fdopen failed: %s", exc)
            return

        last_peak: float | None = None
        last_emit = 0.0
        callback_dead = False

        try:
            for raw in stream:
                if callback_dead:
                    # Drain the pipe so ffmpeg never blocks on a full
                    # write buffer, but no longer fan out.
                    continue
                line = raw.strip()
                if not line:
                    continue
                m_peak = _PEAK_RE.search(line)
                if m_peak is not None:
                    last_peak = _parse_db(m_peak.group(1))
                    continue
                m_rms = _RMS_RE.search(line)
                if m_rms is None:
                    # `frame:N pts:... pts_time:...` separator lines and
                    # anything unexpected land here.
                    log.debug("meter reader: ignored line: %r", line)
                    continue
                rms = _parse_db(m_rms.group(1))
                peak = last_peak if last_peak is not None else rms
                last_peak = None

                now = time.monotonic()
                if now - last_emit < _METER_MIN_INTERVAL_S:
                    continue
                last_emit = now

                try:
                    callback(peak, rms)
                except Exception as exc:
                    log.warning(
                        "meter callback raised (%s); disabling for this recording",
                        exc,
                    )
                    callback_dead = True
        except OSError as exc:
            log.debug("meter reader: stream read failed: %s", exc)
        finally:
            try:
                stream.close()
            except OSError:
                pass

    @staticmethod
    def _close_fd(fd: int | None) -> None:
        if fd is None:
            return
        try:
            os.close(fd)
        except OSError:
            pass

    @staticmethod
    def _read_stderr_tail(proc: subprocess.Popen[bytes]) -> str:
        if proc.stderr is None:
            return ""
        try:
            data = proc.stderr.read() or b""
        except Exception:  # pragma: no cover - defensive
            return ""
        return data.decode("utf-8", errors="replace")[-500:]


class LiveRecorder:
    """Streaming recorder that emits raw PCM while also producing a WAV.

    Spawns a single ffmpeg with a 3-way ``asplit`` filter graph:

    * meter branch -> ``astats`` -> ``ametadata=print:/dev/fd/<W_FD>`` (peak/rms)
    * pcm branch   -> ``-f s16le pipe:1`` (raw 16 kHz mono s16le on stdout)
    * wav branch   -> ``-y <path>`` (canonical WAV preserved for the final
      authoritative re-transcribe used by :class:`StreamEngine`)

    The PCM reader thread invokes ``on_pcm`` with whatever the kernel pipe
    delivers, after stripping a trailing odd byte (so every callback receives
    a whole number of s16le samples). The meter reader is identical to the
    one in :class:`Recorder` and continues to fire at ~10 Hz while live.

    The WAV filename follows the same ``rec-<ts>-<pid>.wav`` pattern as
    :class:`Recorder` so :func:`cleanup_stale_wavs` keeps working.
    """

    # Default PCM read chunk size. Pipe reads may return less; we never
    # buffer to fill the request, we just dispatch what arrives.
    _PCM_READ_BUF = 8192

    def __init__(
        self,
        config: Config,
        meter_callback: Callable[[float, float], None] | None = None,
    ) -> None:
        self._config = config
        self._proc: subprocess.Popen[bytes] | None = None
        self._wav_path: Path | None = None
        self._meter_callback = meter_callback
        self._on_pcm: Callable[[bytes], None] | None = None
        self._meter_thread: threading.Thread | None = None
        self._meter_read_fd: int | None = None
        self._pcm_thread: threading.Thread | None = None

    def start(self, on_pcm: Callable[[bytes], None]) -> Path:
        """Start ffmpeg, return the WAV path it is writing to.

        ``on_pcm`` fires from a background thread with raw PCM s16le @ the
        configured sample rate (mono), with each invocation guaranteed to
        contain an even number of bytes. The callback must return quickly;
        the caller should offload heavy work to its own thread.

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

        meter_r_fd: int | None = None
        meter_w_fd: int | None = None
        meter_enabled = False

        if self._meter_callback is not None:
            try:
                meter_r_fd, meter_w_fd = os.pipe()
                meter_enabled = True
            except OSError as exc:
                log.warning("meter pipe creation failed: %s; metering disabled", exc)
                meter_r_fd = None
                meter_w_fd = None
                meter_enabled = False

        # Build the filter graph. The meter branch terminates in anullsink
        # because the ametadata=print sink is a side-effect on its fd, not a
        # graph output. The pcm and wav branches are each mapped to a real
        # output below.
        if meter_enabled and meter_w_fd is not None:
            filter_complex = (
                "[0:a]asplit=3[meter][pcm][wav];"
                "[meter]astats=metadata=1:reset=1:length=0.1,"
                f"ametadata=print:file=/dev/fd/{meter_w_fd}"
                ":direct=1:key=lavfi.astats.Overall.Peak_level,"
                f"ametadata=print:file=/dev/fd/{meter_w_fd}"
                ":direct=1:key=lavfi.astats.Overall.RMS_level,"
                "anullsink"
            )
            pass_fds: tuple[int, ...] = (meter_w_fd,)
        else:
            # No meter wanted (or pipe creation failed): drop the metering
            # branch but keep asplit=2 so the rest of the wiring is uniform.
            filter_complex = "[0:a]asplit=2[pcm][wav]"
            pass_fds = ()

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-f",
            "pulse",
            "-i",
            str(self._config.audio_source),
            "-filter_complex",
            filter_complex,
            # PCM branch -> stdout
            "-map",
            "[pcm]",
            "-ac",
            str(self._config.channels),
            "-ar",
            str(self._config.sample_rate),
            "-f",
            "s16le",
            "pipe:1",
            # WAV branch -> file
            "-map",
            "[wav]",
            "-ac",
            str(self._config.channels),
            "-ar",
            str(self._config.sample_rate),
            "-y",
            str(wav_path),
        ]
        log.debug("starting ffmpeg (live): %s", " ".join(cmd))

        # NB: -nostdin is set above so ffmpeg does not try to read from our
        # stdin pipe; we still keep stdin=PIPE so we have something to close
        # to make ffmpeg exit promptly if -nostdin behaviour ever changes.
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                pass_fds=pass_fds,
            )
        except FileNotFoundError as exc:
            Recorder._close_fd(meter_r_fd)
            Recorder._close_fd(meter_w_fd)
            raise RecorderError("ffmpeg not found") from exc
        except OSError as exc:
            Recorder._close_fd(meter_r_fd)
            Recorder._close_fd(meter_w_fd)
            raise RecorderError(f"failed to spawn ffmpeg: {exc}") from exc

        # The child now owns its copy of the write end; close ours so EOF
        # propagates to the reader thread when ffmpeg exits.
        if meter_w_fd is not None:
            Recorder._close_fd(meter_w_fd)
            meter_w_fd = None

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
            Recorder._close_fd(meter_r_fd)
            raise RecorderError(f"ffmpeg exited immediately: {stderr_tail}")

        self._proc = proc
        self._wav_path = wav_path
        self._on_pcm = on_pcm

        # Start PCM reader. proc.stdout is non-None because we asked for
        # subprocess.PIPE above.
        assert proc.stdout is not None
        pcm_thread = threading.Thread(
            target=self._pcm_reader,
            args=(proc.stdout, on_pcm),
            name="ld-pcm-reader",
            daemon=True,
        )
        self._pcm_thread = pcm_thread
        pcm_thread.start()

        # Start the meter reader thread last so it isn't running if the
        # spawn failed.
        if meter_r_fd is not None and self._meter_callback is not None:
            self._meter_read_fd = meter_r_fd
            t = threading.Thread(
                target=self._meter_reader,
                args=(meter_r_fd, self._meter_callback),
                name="ld-meter-reader",
                daemon=True,
            )
            self._meter_thread = t
            t.start()

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
            self._teardown_threads()
            self._proc = None
            self._wav_path = None
            self._on_pcm = None
            rc = proc.returncode
            stderr_tail = Recorder._read_stderr_tail(proc)
            if rc != 0:
                raise RecorderError(
                    f"ffmpeg exited with rc={rc} stderr={stderr_tail}"
                )
            return wav_path

        # Step 1: try graceful "q\n" on stdin. ffmpeg was launched with
        # -nostdin so it will not read this, but closing stdin gives a
        # consistent shutdown handshake with our stub ffmpeg in the test
        # suite (which does read stdin and exits on "q\n").
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

        if not Recorder._wait(proc, 2.0):
            log.warning("ffmpeg did not exit after q; sending SIGINT")
            Recorder._signal(proc, signal.SIGINT)
            if not Recorder._wait(proc, 2.0):
                log.warning("ffmpeg did not exit after SIGINT; sending SIGTERM")
                Recorder._signal(proc, signal.SIGTERM)
                if not Recorder._wait(proc, 2.0):
                    log.warning("ffmpeg did not exit after SIGTERM; sending SIGKILL")
                    Recorder._signal(proc, signal.SIGKILL)
                    Recorder._wait(proc, 2.0)

        # Tear down both reader threads before resetting state so they
        # cannot outlive this instance and leak fds / callback references.
        self._teardown_threads()

        self._proc = None
        self._wav_path = None
        self._on_pcm = None

        rc = proc.returncode
        stderr_tail = Recorder._read_stderr_tail(proc)

        # ffmpeg returns 0 on clean stop; non-zero on signal kill is
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

    def _pcm_reader(
        self,
        stdout,  # type: ignore[no-untyped-def]
        callback: Callable[[bytes], None],
    ) -> None:
        """Pump bytes from ffmpeg stdout into ``callback``.

        Every callback invocation receives an even-length byte string so
        the consumer never sees a split s16le sample. A trailing odd byte
        on EOF (which would indicate a truncated final sample) is dropped
        with a debug log.
        """
        # Carry a single byte across reads if the kernel hands us an odd
        # boundary mid-stream. ffmpeg's stdout pipe almost never does this
        # in practice (it writes whole packets), but it costs nothing to
        # handle.
        leftover = b""
        callback_dead = False
        try:
            while True:
                try:
                    chunk = stdout.read(self._PCM_READ_BUF)
                except (OSError, ValueError) as exc:
                    log.debug("pcm reader: read failed: %s", exc)
                    break
                if not chunk:
                    break  # EOF

                if leftover:
                    chunk = leftover + chunk
                    leftover = b""

                if len(chunk) % 2 == 1:
                    leftover = chunk[-1:]
                    chunk = chunk[:-1]

                if not chunk or callback_dead:
                    continue

                try:
                    callback(chunk)
                except Exception as exc:
                    log.warning(
                        "pcm callback raised (%s); disabling for this recording",
                        exc,
                    )
                    callback_dead = True
        finally:
            if leftover:
                log.debug(
                    "pcm reader: dropping %d trailing byte(s) at EOF", len(leftover)
                )
            try:
                stdout.close()
            except Exception:  # pragma: no cover - defensive
                pass

    def _meter_reader(
        self,
        read_fd: int,
        callback: Callable[[float, float], None],
    ) -> None:
        """Identical semantics to :meth:`Recorder._meter_reader`.

        Duplicated rather than shared because the throttling state and
        callback-dead flag are per-recording; factoring it out would not
        meaningfully shrink either class.
        """
        try:
            stream = os.fdopen(read_fd, "r", buffering=1)
        except OSError as exc:
            log.warning("meter reader: fdopen failed: %s", exc)
            return

        last_peak: float | None = None
        last_emit = 0.0
        callback_dead = False

        try:
            for raw in stream:
                if callback_dead:
                    continue
                line = raw.strip()
                if not line:
                    continue
                m_peak = _PEAK_RE.search(line)
                if m_peak is not None:
                    last_peak = _parse_db(m_peak.group(1))
                    continue
                m_rms = _RMS_RE.search(line)
                if m_rms is None:
                    log.debug("meter reader: ignored line: %r", line)
                    continue
                rms = _parse_db(m_rms.group(1))
                peak = last_peak if last_peak is not None else rms
                last_peak = None

                now = time.monotonic()
                if now - last_emit < _METER_MIN_INTERVAL_S:
                    continue
                last_emit = now

                try:
                    callback(peak, rms)
                except Exception as exc:
                    log.warning(
                        "meter callback raised (%s); disabling for this recording",
                        exc,
                    )
                    callback_dead = True
        except OSError as exc:
            log.debug("meter reader: stream read failed: %s", exc)
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _teardown_threads(self) -> None:
        """Join both reader threads with bounded waits; idempotent."""
        fd = self._meter_read_fd
        self._meter_read_fd = None
        if fd is not None:
            Recorder._close_fd(fd)

        t = self._meter_thread
        self._meter_thread = None
        if t is not None and t.is_alive():
            t.join(timeout=1.0)
            if t.is_alive():  # pragma: no cover - defensive
                log.warning("live meter reader thread did not exit within 1s")

        pt = self._pcm_thread
        self._pcm_thread = None
        if pt is not None and pt.is_alive():
            pt.join(timeout=1.0)
            if pt.is_alive():  # pragma: no cover - defensive
                log.warning("live pcm reader thread did not exit within 1s")


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
