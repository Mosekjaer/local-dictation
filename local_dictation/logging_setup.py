"""File + stderr logging configuration.

Idempotent: calling :func:`configure` twice on the same config does not stack
handlers. Installed handlers are tagged with a marker attribute so subsequent
calls can detect and skip duplicate installation.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from .config import Config

_MARKER = "_local_dictation_handler"
_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
_BACKUP_COUNT = 3


def configure(config: Config) -> None:
    """Install a rotating file handler and a stderr handler on the root logger.

    File handler: ``config.log_file``, 5 MB per file, 3 backups, level from
    ``config.log_level``. Stderr handler: INFO level. The parent directory of
    ``log_file`` is created if missing. If the directory cannot be created or
    the file cannot be opened, the file handler is skipped (we still install
    the stderr handler) and a warning is logged once stderr is up.

    Calling this function more than once for the same process is safe: the
    second call replaces the existing local-dictation handlers in place rather
    than appending duplicates.
    """
    root = logging.getLogger()
    level = _resolve_level(config.log_level)
    root.setLevel(level)

    # Drop any of our previously-installed handlers so this call is idempotent.
    for handler in list(root.handlers):
        if getattr(handler, _MARKER, False):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # pragma: no cover - defensive
                pass

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # stderr handler first so we can log file-handler failures.
    stream_handler = logging.StreamHandler(stream=sys.stderr)
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(fmt)
    setattr(stream_handler, _MARKER, True)
    root.addHandler(stream_handler)

    log_file = config.log_file
    file_error: Exception | None = None
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        file_error = exc

    if file_error is None:
        try:
            file_handler = RotatingFileHandler(
                str(log_file),
                maxBytes=_MAX_BYTES,
                backupCount=_BACKUP_COUNT,
                encoding="utf-8",
            )
        except OSError as exc:
            file_error = exc
        else:
            file_handler.setLevel(level)
            file_handler.setFormatter(fmt)
            setattr(file_handler, _MARKER, True)
            root.addHandler(file_handler)

    if file_error is not None:
        logging.getLogger(__name__).warning(
            "cannot open log file %s: %s", log_file, file_error
        )


def _resolve_level(name: str) -> int:
    value = logging.getLevelName(name.upper() if isinstance(name, str) else name)
    if isinstance(value, int):
        return value
    return logging.INFO
