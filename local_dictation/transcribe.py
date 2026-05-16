"""whisper-cli wrapper.

Spawns the configured ``whisper-cli`` binary per request, reads the resulting
``<wav>.txt`` file, runs it through :func:`clean_text`, and returns the
result. No long-running whisper-server in v1.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Literal

from .config import Config

log = logging.getLogger(__name__)

Language = Literal["auto", "da", "en"]

_BLANK_RUN = re.compile(r"\n{3,}")


class TranscriberError(Exception):
    pass


class Transcriber:
    def __init__(self, config: Config) -> None:
        self._config = config

    def transcribe(self, wav_path: Path, language: Language) -> str:
        """Run whisper-cli on ``wav_path``, read the .txt output, clean and return.

        ``language='auto'`` omits ``-l``. Returns ``''`` on empty transcription
        (not an error). Raises :class:`TranscriberError` on whisper-cli failure
        or missing model/binary.
        """
        whisper_cli = self._config.whisper_cli
        model = self._config.model

        if not whisper_cli.exists() or not os.access(whisper_cli, os.X_OK):
            raise TranscriberError(f"whisper-cli not found: {whisper_cli}")
        if not model.exists():
            raise TranscriberError(f"model not found: {model}")

        argv: list[str] = [
            str(whisper_cli),
            "-m",
            str(model),
            "-f",
            str(wav_path),
            "--no-prints",
            "-otxt",
        ]
        if language != "auto":
            argv += ["-l", language]

        log.debug("running whisper-cli: %s", " ".join(argv))

        try:
            result = subprocess.run(argv, capture_output=True, check=False)
        except FileNotFoundError as exc:
            raise TranscriberError(f"whisper-cli not found: {whisper_cli}") from exc
        except OSError as exc:
            raise TranscriberError(f"failed to spawn whisper-cli: {exc}") from exc

        if result.returncode != 0:
            stderr_tail = result.stderr.decode("utf-8", errors="replace")[-500:]
            raise TranscriberError(
                f"whisper-cli failed: rc={result.returncode} stderr={stderr_tail}"
            )

        # whisper-cli writes <wav_path>.txt
        txt_path = wav_path.with_suffix(wav_path.suffix + ".txt")
        if not txt_path.exists():
            raise TranscriberError(f"output file missing: {txt_path}")

        try:
            raw = txt_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise TranscriberError(
                f"cannot read output file {txt_path}: {exc}"
            ) from exc

        return clean_text(raw)


def clean_text(s: str) -> str:
    """Trim leading/trailing whitespace; collapse runs of >=2 blank lines
    to one blank line. Preserve punctuation, Danish/English chars, inline
    whitespace, single newlines.
    """
    return _BLANK_RUN.sub("\n\n", s.strip())
