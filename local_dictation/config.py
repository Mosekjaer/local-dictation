from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Literal

Language = Literal["auto", "da", "en"]
PasteMethod = Literal["auto", "wtype", "ydotool_type", "ydotool", "clipboard"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
NotificationMode = Literal["all", "errors_only", "none"]


def _expand(p: str) -> Path:
    return Path(os.path.expandvars(p)).expanduser()


def _xdg_runtime_dir() -> Path:
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg)
    return Path(f"/run/user/{os.getuid()}") if hasattr(os, "getuid") else Path("/tmp")


@dataclass
class Config:
    # whisper.cpp
    whisper_cli: Path = field(
        default_factory=lambda: _expand(
            "~/stt-bench/whisper.cpp/build-vulkan/bin/whisper-cli"
        )
    )
    whisper_server_bin: Path = field(
        default_factory=lambda: _expand(
            "~/stt-bench/whisper.cpp/build-vulkan/bin/whisper-server"
        )
    )
    model: Path = field(
        default_factory=lambda: _expand("~/stt-bench/whisper.cpp/models/ggml-small.bin")
    )
    vad_model: Path | None = field(
        default_factory=lambda: _expand(
            "~/stt-bench/whisper.cpp/models/for-tests-silero-v6.2.0-ggml.bin"
        )
    )
    language: Language = "auto"

    # audio
    audio_source: str = "default"
    channels: int = 1
    sample_rate: int = 16000
    temp_dir: Path = field(
        default_factory=lambda: _expand("~/.cache/local-dictation/recordings")
    )

    # output
    paste_method: PasteMethod = "auto"
    restore_clipboard: bool = True

    # live typing
    live_typing: bool = False
    live_chunk_ms: int = 500
    live_window_s: int = 10
    live_stable_passes: int = 2
    live_hallucination_filter: bool = True
    whisper_server_host: str = "127.0.0.1"
    whisper_server_port: int = 38080

    # ui
    notifications: NotificationMode = "all"
    status_window: bool = False

    # logging
    log_file: Path = field(
        default_factory=lambda: _expand("~/.cache/local-dictation/local-dictation.log")
    )
    log_level: LogLevel = "INFO"

    # ipc
    socket_path: Path = field(
        default_factory=lambda: _xdg_runtime_dir() / "local-dictation.sock"
    )
    events_socket_path: Path = field(
        default_factory=lambda: _xdg_runtime_dir() / "local-dictation-events.sock"
    )

    @classmethod
    def default_config_path(cls) -> Path:
        return _expand("~/.config/local-dictation/config.toml")

    def ensure_dirs(self) -> None:
        for p in (
            self.temp_dir,
            self.log_file.parent,
            self.socket_path.parent,
            self.events_socket_path.parent,
        ):
            try:
                p.mkdir(parents=True, exist_ok=True)
            except (PermissionError, FileExistsError):
                pass


_PATH_FIELDS = {
    "whisper_cli",
    "whisper_server_bin",
    "model",
    "vad_model",
    "temp_dir",
    "log_file",
    "socket_path",
    "events_socket_path",
}


def load(path: Path | None = None) -> Config:
    """Load Config from TOML. Missing keys fall back to dataclass defaults.

    `path=None` uses ~/.config/local-dictation/config.toml; if that file is
    absent, returns the all-defaults Config (no error)."""
    target = path if path is not None else Config.default_config_path()
    cfg = Config()

    if not target.exists():
        return cfg

    with open(target, "rb") as fh:
        data = tomllib.load(fh)

    known = {f.name for f in fields(cfg)}
    for key, value in data.items():
        if key not in known:
            continue
        if key == "vad_model" and isinstance(value, str) and value == "":
            value = None
        elif key in _PATH_FIELDS and isinstance(value, str):
            value = _expand(value)
        if key == "notifications":
            value = _normalize_notifications(value)
        setattr(cfg, key, value)

    return cfg


def _normalize_notifications(value: object) -> NotificationMode:
    """Accept "all" / "errors_only" / "none", or a legacy bool.
    Any other value falls back to "all" with a warning."""
    if isinstance(value, bool):
        return "all" if value else "none"
    if isinstance(value, str):
        norm = value.strip().lower()
        if norm in ("all", "errors_only", "none"):
            return norm  # type: ignore[return-value]
    import logging
    logging.getLogger(__name__).warning(
        "invalid notifications=%r; falling back to 'all'", value
    )
    return "all"


def write_default(path: Path, example_source: Path) -> bool:
    """Copy example_source -> path if path does not exist. Returns True if written."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(example_source.read_text())
    return True
