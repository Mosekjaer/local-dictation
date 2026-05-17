"""Streaming dictation engine.

Drives a long-running ``whisper-server`` HTTP subprocess and runs a
sliding-window re-transcription scheme over a PCM ring buffer. Exposes
:class:`TranscriptDelta` events to a callback as text is committed.

The public contract is documented in
``.plans/0003-live-typing-streaming.md``. Briefly:

* :meth:`StreamEngine.start` spawns whisper-server (lazily) and a worker
  thread that periodically POSTs the current ring buffer to
  ``/inference``.
* :meth:`StreamEngine.feed` appends PCM s16le @ 16 kHz mono to the ring.
* :meth:`StreamEngine.stop` runs one authoritative final pass on a full
  WAV path and returns its text. The worker thread is stopped but the
  whisper-server subprocess stays alive across utterances; only
  :meth:`StreamEngine.shutdown` kills it.

The committed-text invariant the typer relies on: every emitted
``TranscriptDelta``'s ``committed_text`` starts with the previous
delta's ``committed_text``. Never shrinks.

Spike findings used here (see plan section "Spike findings"):

* whisper-server lives at
  ``~/stt-bench/whisper.cpp/build-vulkan/bin/whisper-server`` and serves
  ``POST /inference`` multipart with field ``file=@audio.wav`` returning
  ``{"text": " ...\n"}``. We strip the leading space and trailing
  newline. ``GET /`` returns 200 once the server is ready.
"""

from __future__ import annotations

import io
import json
import logging
import os
import signal
import struct
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TranscriptDelta:
    """One incremental transcription event.

    ``committed_text`` is the cumulative committed prefix. Each delta's
    ``committed_text`` must start with the previous delta's
    ``committed_text`` (monotonicity).

    ``tentative_text`` is the uncommitted current best-guess tail. It may
    freely change between deltas.

    ``is_final`` is True only on the authoritative final pass produced by
    :meth:`StreamEngine.stop`. On final, ``tentative_text == ""``.
    """

    committed_text: str
    tentative_text: str
    is_final: bool


class StreamEngineError(Exception):
    """Raised for unrecoverable streaming-engine errors."""


# ---------------------------------------------------------------------------
# Config field accessors with defaults
# ---------------------------------------------------------------------------


def _expand_path(p: str) -> Path:
    return Path(os.path.expandvars(p)).expanduser()


_DEFAULT_WHISPER_SERVER_BIN = _expand_path(
    "~/stt-bench/whisper.cpp/build-vulkan/bin/whisper-server"
)
_DEFAULT_VAD_MODEL = _expand_path(
    "~/stt-bench/whisper.cpp/models/for-tests-silero-v6.2.0-ggml.bin"
)
_DEFAULT_MODEL = _expand_path("~/stt-bench/whisper.cpp/models/ggml-small.bin")
_DEFAULT_TEMP_DIR = _expand_path("~/.cache/local-dictation/recordings")

# Hallucinations the encoder tends to invent during silence. Compared
# case-insensitively against the trimmed tentative tail; on exact match
# we set tentative to "".
_HALLUCINATION_PHRASES = frozenset(
    {
        "you",
        "thanks for watching",
        "[music]",
        ".",
    }
)


_SAMPLE_RATE = 16000
_BYTES_PER_SAMPLE = 2  # s16le
_CHANNELS = 1


# ---------------------------------------------------------------------------
# Stable-prefix commit
# ---------------------------------------------------------------------------


def _common_prefix(a: str, b: str) -> str:
    """Longest common character prefix of two strings."""
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return a[:i]


def _stable_prefix(history: list[str], required_passes: int) -> str:
    """Longest character prefix that is byte-identical across the last
    ``required_passes`` raw transcriptions AND is a prefix of the most
    recent.

    With ``required_passes=2`` and history ``[..., t1, t2]``, the stable
    prefix is the longest prefix of ``t2`` that ``t1`` also starts with.

    With fewer entries than required, returns "" (nothing has had time
    to stabilise).
    """
    if required_passes < 1 or len(history) < required_passes:
        return ""
    window = history[-required_passes:]
    base = window[-1]
    prefix = base
    for older in window[:-1]:
        prefix = _common_prefix(prefix, older)
        if not prefix:
            return ""
    return prefix


def _filter_hallucination(text: str) -> str:
    """If the trimmed tentative tail matches a known silence-hallucination
    phrase exactly (case-insensitive), return "" — otherwise return the
    input unchanged.
    """
    stripped = text.strip().lower()
    if stripped in _HALLUCINATION_PHRASES:
        return ""
    return text


def _normalize_server_text(raw: str) -> str:
    """whisper-server returns ``" some text\n"`` and, for multi-segment
    passes, embeds ``\n`` between segments. Replace CR/LF with spaces so
    the live typer never emits a real Enter keystroke (which would submit
    in Claude Code, chat inputs, etc.), then collapse the resulting
    whitespace runs and strip boundaries.
    """
    return " ".join(raw.replace("\r", "\n").split())


# ---------------------------------------------------------------------------
# Multipart encoding (stdlib only)
# ---------------------------------------------------------------------------


def _encode_multipart_wav(wav_bytes: bytes, filename: str = "audio.wav") -> tuple[bytes, str]:
    """Encode a single-file multipart/form-data body with field ``file``.

    Returns ``(body, content_type)``.
    """
    boundary = f"----LDStream{uuid.uuid4().hex}"
    buf = io.BytesIO()
    buf.write(f"--{boundary}\r\n".encode("ascii"))
    buf.write(
        (
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: audio/wav\r\n\r\n"
        ).encode("ascii")
    )
    buf.write(wav_bytes)
    buf.write(b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode("ascii"))
    return buf.getvalue(), f"multipart/form-data; boundary={boundary}"


def _pcm_to_wav_bytes(pcm: bytes) -> bytes:
    """Wrap raw PCM s16le 16 kHz mono in a WAV container in memory."""
    out = io.BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(_CHANNELS)
        wf.setsampwidth(_BYTES_PER_SAMPLE)
        wf.setframerate(_SAMPLE_RATE)
        wf.writeframes(pcm)
    return out.getvalue()


# ---------------------------------------------------------------------------
# StreamEngine
# ---------------------------------------------------------------------------


class StreamEngine:
    """See module docstring for the contract."""

    # Time the tentative tail must remain unchanged before we force-commit it.
    _FORCE_COMMIT_AFTER_S = 0.7

    # How many consecutive HTTP failures before we surface them via on_delta.
    _MAX_CONSEC_FAILURES = 5

    def __init__(self, config: Any) -> None:
        self._config = config

        # Tunables (use getattr so this works before daemon-live finalises Config).
        self._server_bin: Path = Path(
            getattr(config, "whisper_server_bin", _DEFAULT_WHISPER_SERVER_BIN)
        )
        self._host: str = getattr(config, "whisper_server_host", "127.0.0.1")
        self._port: int = int(getattr(config, "whisper_server_port", 38080))
        self._model: Path = Path(getattr(config, "model", _DEFAULT_MODEL))
        self._vad_model: Optional[Path] = self._coerce_optional_path(
            getattr(config, "vad_model", _DEFAULT_VAD_MODEL)
        )
        self._chunk_ms: int = int(getattr(config, "live_chunk_ms", 500))
        self._window_s: int = int(getattr(config, "live_window_s", 10))
        self._stable_passes: int = int(getattr(config, "live_stable_passes", 2))
        self._temp_dir: Path = Path(getattr(config, "temp_dir", _DEFAULT_TEMP_DIR))
        self._hallucination_filter: bool = bool(
            getattr(config, "live_hallucination_filter", True)
        )

        # Derived.
        self._window_bytes = self._window_s * _SAMPLE_RATE * _BYTES_PER_SAMPLE
        self._min_chunk_bytes = (_SAMPLE_RATE // 2) * _BYTES_PER_SAMPLE  # ~500 ms

        # Subprocess + readiness.
        self._proc: Optional[subprocess.Popen[bytes]] = None
        self._proc_lock = threading.Lock()

        # Per-utterance state.
        self._ring = bytearray()
        self._ring_lock = threading.Lock()
        self._history: list[str] = []  # raw transcriptions
        self._committed_text = ""
        self._tentative_text = ""
        self._last_tentative_change_ts = 0.0
        self._last_emitted_committed = ""
        self._last_emitted_tentative = ""
        self._on_delta: Optional[Callable[[TranscriptDelta], None]] = None
        self._language: str = "auto"

        # Worker.
        self._worker: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._consec_failures = 0

    # ---- subprocess lifecycle ---------------------------------------------

    def _spawn_server(self) -> None:
        """Spawn whisper-server if not already running. Waits up to ~5 s for
        readiness (``GET /`` returning 200)."""
        with self._proc_lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            if not self._server_bin.exists() or not os.access(self._server_bin, os.X_OK):
                raise StreamEngineError(
                    f"whisper-server not found or not executable: {self._server_bin}"
                )
            if not self._model.exists():
                raise StreamEngineError(f"whisper model not found: {self._model}")

            argv: list[str] = [
                str(self._server_bin),
                "-m",
                str(self._model),
                "-l",
                self._language,
                "--host",
                self._host,
                "--port",
                str(self._port),
                "-t",
                "4",
                "--no-timestamps",
            ]
            if self._vad_model is not None and self._vad_model.exists():
                argv += ["--vad", "--vad-model", str(self._vad_model)]
            else:
                if self._vad_model is not None:
                    log.warning(
                        "stream: VAD model %s not found; running whisper-server without VAD",
                        self._vad_model,
                    )

            log.info("stream: spawning whisper-server: %s", " ".join(argv))
            try:
                self._proc = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
            except (FileNotFoundError, OSError) as exc:
                raise StreamEngineError(
                    f"failed to spawn whisper-server: {exc}"
                ) from exc

        # Wait for readiness outside the lock.
        deadline = time.monotonic() + 5.0
        last_err: Optional[Exception] = None
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                stderr_tail = b""
                try:
                    if self._proc.stderr is not None:
                        stderr_tail = self._proc.stderr.read(2000) or b""
                except Exception:  # pragma: no cover - defensive
                    pass
                raise StreamEngineError(
                    "whisper-server exited during startup "
                    f"(rc={self._proc.returncode}, stderr={stderr_tail!r})"
                )
            try:
                self._http_get_root()
                log.info("stream: whisper-server ready on %s:%d", self._host, self._port)
                return
            except Exception as exc:  # noqa: BLE001 - any connect error is retryable
                last_err = exc
                time.sleep(0.1)

        raise StreamEngineError(
            f"whisper-server did not become ready within 5 s: {last_err!r}"
        )

    def _http_get_root(self) -> None:
        url = f"http://{self._host}:{self._port}/"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            if resp.status != 200:
                raise StreamEngineError(f"whisper-server / returned {resp.status}")

    def shutdown(self) -> None:
        """Kill the whisper-server subprocess. Idempotent."""
        self._stop_event.set()
        worker = self._worker
        self._worker = None
        if worker is not None and worker.is_alive():
            worker.join(timeout=2.0)

        with self._proc_lock:
            proc = self._proc
            self._proc = None
        if proc is None:
            return
        if proc.poll() is not None:
            return
        try:
            proc.send_signal(signal.SIGTERM)
        except OSError:
            pass
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                log.warning("stream: whisper-server did not die after SIGKILL")

    # ---- per-utterance ----------------------------------------------------

    def start(
        self,
        language: str,
        on_delta: Callable[[TranscriptDelta], None],
    ) -> None:
        """Begin a new live-dictation utterance.

        Spawns whisper-server lazily on the first call. Resets the ring
        buffer and commit history. Starts the worker thread that polls
        the ring every ``live_chunk_ms`` ms.
        """
        if self._worker is not None and self._worker.is_alive():
            raise StreamEngineError("StreamEngine.start called while already running")

        self._language = language
        self._on_delta = on_delta
        self._reset_utterance_state()
        self._stop_event.clear()

        try:
            self._spawn_server()
        except StreamEngineError:
            raise

        # Warm-up POST: one second of silence in a background thread so the
        # first real pass doesn't pay the cold-encoder cost. Don't block start.
        threading.Thread(
            target=self._warmup_post,
            name="ld-stream-warmup",
            daemon=True,
        ).start()

        self._worker = threading.Thread(
            target=self._worker_loop,
            name="ld-stream-worker",
            daemon=True,
        )
        self._worker.start()

    def _reset_utterance_state(self) -> None:
        with self._ring_lock:
            self._ring = bytearray()
        self._history = []
        self._committed_text = ""
        self._tentative_text = ""
        self._last_tentative_change_ts = 0.0
        self._last_emitted_committed = ""
        self._last_emitted_tentative = ""
        self._consec_failures = 0

    def feed(self, pcm: bytes) -> None:
        """Append PCM s16le @ 16 kHz mono to the ring buffer. Bytes beyond
        ``live_window_s`` seconds are dropped from the head."""
        if not pcm:
            return
        with self._ring_lock:
            self._ring.extend(pcm)
            overflow = len(self._ring) - self._window_bytes
            if overflow > 0:
                del self._ring[:overflow]

    def _snapshot_ring(self) -> bytes:
        with self._ring_lock:
            return bytes(self._ring)

    # ---- worker -----------------------------------------------------------

    def _worker_loop(self) -> None:
        interval = max(0.05, self._chunk_ms / 1000.0)
        while not self._stop_event.is_set():
            # Sleep up-front so we don't immediately fire before any audio.
            self._stop_event.wait(interval)
            if self._stop_event.is_set():
                return
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001
                log.exception("stream: worker tick raised: %s", exc)

    def _tick(self) -> None:
        pcm = self._snapshot_ring()
        if len(pcm) < self._min_chunk_bytes:
            # Force-commit a stale tentative even if no new audio.
            self._maybe_force_commit_stale()
            return

        wav_bytes = _pcm_to_wav_bytes(pcm)
        text = self._post_with_retry(wav_bytes)
        if text is None:
            # Already handled (retry exhausted or skipped).
            return

        self._ingest_transcription(text)

    def _post_with_retry(self, wav_bytes: bytes) -> Optional[str]:
        """POST a WAV body to /inference with one retry. Returns the
        transcription text, or ``None`` if we should skip this pass."""
        attempt = 0
        last_exc: Optional[Exception] = None
        while attempt < 2:
            try:
                raw = self._post_inference(wav_bytes)
                self._consec_failures = 0
                return _normalize_server_text(raw)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                attempt += 1
                if attempt < 2:
                    time.sleep(0.2)

        self._consec_failures += 1
        log.warning(
            "stream: /inference POST failed after retry: %s (consec=%d)",
            last_exc,
            self._consec_failures,
        )
        if self._consec_failures >= self._MAX_CONSEC_FAILURES:
            # Surface the stall via an unchanged delta + log error.
            log.error(
                "stream: %d consecutive /inference failures; daemon should fall back",
                self._consec_failures,
            )
            self._emit_delta_if_changed(force=True)
        return None

    def _post_inference(self, wav_bytes: bytes) -> str:
        """Single POST to ``/inference``. Raises on any HTTP / network /
        decode failure.

        Test seam: monkeypatch this method on the instance to inject fake
        transcriptions without standing up a real server.
        """
        body, content_type = _encode_multipart_wav(wav_bytes)
        url = f"http://{self._host}:{self._port}/inference"
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": content_type},
        )
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            if resp.status != 200:
                raise StreamEngineError(f"/inference returned HTTP {resp.status}")
            payload = resp.read()
        try:
            obj = json.loads(payload.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise StreamEngineError(f"/inference returned invalid JSON: {exc}") from exc
        if not isinstance(obj, dict) or "text" not in obj:
            raise StreamEngineError(f"/inference response missing 'text' field: {obj!r}")
        text = obj["text"]
        if not isinstance(text, str):
            raise StreamEngineError(
                f"/inference returned non-string text: {type(text).__name__}"
            )
        return text

    def _warmup_post(self) -> None:
        """Send one second of silence to /inference so the encoder is warm
        when real audio arrives. Errors are logged and swallowed."""
        silence = b"\x00\x00" * _SAMPLE_RATE
        wav_bytes = _pcm_to_wav_bytes(silence)
        try:
            self._post_inference(wav_bytes)
        except Exception as exc:  # noqa: BLE001
            log.debug("stream: warm-up POST failed (non-fatal): %s", exc)

    # ---- commit algorithm -------------------------------------------------

    def _ingest_transcription(self, raw: str) -> None:
        """Update commit state from one fresh transcription and possibly
        emit a delta."""
        self._history.append(raw)
        # Cap history to what stable-prefix actually needs.
        max_keep = max(self._stable_passes + 1, 2)
        if len(self._history) > max_keep:
            del self._history[: len(self._history) - max_keep]

        new_committed = self._compute_new_committed(raw)
        tentative_raw = raw[len(new_committed):] if raw.startswith(new_committed) else raw

        # Apply hallucination filter only to tentative tail.
        if self._hallucination_filter:
            tentative_raw = _filter_hallucination(tentative_raw)

        # Update state. Monotonicity is enforced inside _compute_new_committed.
        prev_tentative = self._tentative_text
        self._committed_text = new_committed
        self._tentative_text = tentative_raw

        if tentative_raw != prev_tentative:
            self._last_tentative_change_ts = time.monotonic()

        self._emit_delta_if_changed()

    def _compute_new_committed(self, latest_raw: str) -> str:
        """Decide the new committed prefix given the current history and
        the previously-committed prefix.

        Monotonicity: the result must start with ``self._committed_text``.
        If the stable-prefix computation produces something that does not
        start with the previous committed prefix (because the server
        revised earlier text), the previous prefix wins.
        """
        stable = _stable_prefix(self._history, self._stable_passes)

        # Stable must also be a prefix of the latest raw. _stable_prefix
        # already enforces this since it walks the most-recent entry.
        # Monotonicity: keep the longer of (previous committed, stable),
        # but only if one is a prefix of the other. If they diverge,
        # the previous committed wins (we never shrink).
        prev = self._committed_text
        if stable.startswith(prev):
            return stable
        # Stable diverged from previously committed. Hold the previous.
        return prev

    def _maybe_force_commit_stale(self) -> None:
        """If the tentative tail has been unchanged for ~700 ms and is
        non-empty, promote it to committed."""
        if not self._tentative_text:
            return
        if self._last_tentative_change_ts == 0.0:
            return
        idle = time.monotonic() - self._last_tentative_change_ts
        if idle < self._FORCE_COMMIT_AFTER_S:
            return
        merged = self._committed_text + self._tentative_text
        self._committed_text = merged
        self._tentative_text = ""
        self._last_tentative_change_ts = time.monotonic()
        self._emit_delta_if_changed()

    def _emit_delta_if_changed(self, *, force: bool = False, is_final: bool = False) -> None:
        if not is_final and not force:
            if (
                self._committed_text == self._last_emitted_committed
                and self._tentative_text == self._last_emitted_tentative
            ):
                return
        delta = TranscriptDelta(
            committed_text=self._committed_text,
            tentative_text="" if is_final else self._tentative_text,
            is_final=is_final,
        )
        self._last_emitted_committed = delta.committed_text
        self._last_emitted_tentative = delta.tentative_text
        cb = self._on_delta
        if cb is None:
            return
        try:
            cb(delta)
        except Exception as exc:  # noqa: BLE001 - never let the consumer kill us
            log.exception("stream: on_delta callback raised: %s", exc)

    # ---- stop -------------------------------------------------------------

    def stop(self, final_wav: Path) -> str:
        """Cancel the worker, run one authoritative pass on the full WAV,
        and return its text. Fires one final ``TranscriptDelta`` with
        ``is_final=True`` and ``tentative_text == ""``.
        """
        self._stop_event.set()
        worker = self._worker
        self._worker = None
        if worker is not None and worker.is_alive():
            worker.join(timeout=2.0)

        final_text: Optional[str] = None
        last_exc: Optional[Exception] = None
        for attempt in range(2):
            try:
                final_text = self._post_inference_from_path(final_wav)
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                log.warning(
                    "stream: final-pass POST failed (attempt %d): %s",
                    attempt + 1,
                    exc,
                )
                if attempt == 0:
                    time.sleep(0.2)

        if final_text is None:
            # Fall back to whatever we already had committed + tentative.
            log.error(
                "stream: final pass failed; emitting accumulated state. last=%s",
                last_exc,
            )
            fallback = self._committed_text + self._tentative_text
            self._committed_text = fallback
            self._tentative_text = ""
            self._emit_delta_if_changed(force=True, is_final=True)
            return fallback

        final_text = _normalize_server_text(final_text)
        # Final is authoritative: overwrite committed entirely. The typer
        # contract allows the final delta to differ from prior committed
        # text — that's why finalize exists.
        self._committed_text = final_text
        self._tentative_text = ""
        self._emit_delta_if_changed(force=True, is_final=True)
        return final_text

    def _post_inference_from_path(self, wav_path: Path) -> str:
        """Read a WAV file from disk and POST it to /inference. Distinct
        method from :meth:`_post_inference` so tests can monkeypatch
        path-based and bytes-based posts independently if desired.
        """
        try:
            wav_bytes = Path(wav_path).read_bytes()
        except OSError as exc:
            raise StreamEngineError(f"cannot read final WAV {wav_path}: {exc}") from exc
        return self._post_inference(wav_bytes)

    # ---- helpers ----------------------------------------------------------

    @staticmethod
    def _coerce_optional_path(value: Any) -> Optional[Path]:
        if value is None:
            return None
        if isinstance(value, Path):
            return value
        if isinstance(value, str):
            return _expand_path(value)
        return Path(str(value))


# ---------------------------------------------------------------------------
# Module-level helpers (intentionally exported for tests)
# ---------------------------------------------------------------------------


__all__ = (
    "StreamEngine",
    "StreamEngineError",
    "TranscriptDelta",
)
