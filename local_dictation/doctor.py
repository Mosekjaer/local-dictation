"""Environment checks. `run_checks(config)` returns a list of Check
records the CLI can render. Required failures are fatal — the CLI exits
2 when any `required=True` check has status `FAIL`.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .config import Config

log = logging.getLogger(__name__)

Status = Literal["OK", "WARN", "FAIL"]


@dataclass
class Check:
    name: str
    status: Status
    detail: str = ""
    required: bool = False


# ---------------------------------------------------------------------- #
# helpers
# ---------------------------------------------------------------------- #


def _which(binary: str) -> str | None:
    return shutil.which(binary)


def _ydotoold_socket() -> str | None:
    env = os.environ.get("YDOTOOL_SOCKET")
    if env and os.path.exists(env):
        return env
    default = "/tmp/.ydotool_socket"
    if os.path.exists(default):
        return default
    return None


# ---------------------------------------------------------------------- #
# individual checks
# ---------------------------------------------------------------------- #


def _check_python() -> Check:
    major, minor = sys.version_info[:2]
    detail = f"{major}.{minor}.{sys.version_info[2]}"
    if (major, minor) >= (3, 10):
        return Check("python", "OK", detail, required=True)
    return Check("python", "FAIL", f"need >=3.10, got {detail}", required=True)


def _check_ffmpeg() -> Check:
    path = _which("ffmpeg")
    if path:
        return Check("ffmpeg", "OK", path, required=True)
    return Check("ffmpeg", "FAIL", "ffmpeg not on PATH", required=True)


def _check_whisper_cli(config: Config) -> Check:
    p = config.whisper_cli
    if p.exists() and os.access(p, os.X_OK):
        return Check("whisper-cli", "OK", str(p), required=True)
    if p.exists():
        return Check("whisper-cli", "FAIL", f"{p} is not executable", required=True)
    return Check("whisper-cli", "FAIL", f"{p} does not exist", required=True)


def _check_model(config: Config) -> Check:
    p = config.model
    if p.exists():
        return Check("model", "OK", str(p), required=True)
    return Check("model", "FAIL", f"{p} does not exist", required=True)


def _check_path_bin(name: str, *, required: bool, warn_detail: str = "") -> Check:
    path = _which(name)
    if path:
        return Check(name, "OK", path, required=required)
    status: Status = "FAIL" if required else "WARN"
    detail = warn_detail or f"{name} not on PATH"
    return Check(name, status, detail, required=required)


def _check_wayland_session() -> Check:
    val = os.environ.get("XDG_SESSION_TYPE", "")
    if val == "wayland":
        return Check("wayland-session", "OK", "XDG_SESSION_TYPE=wayland", required=True)
    return Check(
        "wayland-session",
        "FAIL",
        f"XDG_SESSION_TYPE={val or '(unset)'}, expected wayland",
        required=True,
    )


def _check_temp_dir(config: Config) -> Check:
    p = config.temp_dir
    test_file: Path | None = None
    try:
        p.mkdir(parents=True, exist_ok=True)
        test_file = p / ".doctor-write-test"
        test_file.write_text("ok")
        return Check("temp-dir-writable", "OK", str(p), required=True)
    except OSError as e:
        return Check("temp-dir-writable", "FAIL", f"{p}: {e}", required=True)
    finally:
        if test_file is not None:
            try:
                test_file.unlink(missing_ok=True)
            except OSError:
                pass


def _check_ydotoold() -> Check:
    sock = _ydotoold_socket()
    if sock:
        return Check("ydotoold", "OK", f"socket at {sock}", required=False)
    return Check(
        "ydotoold",
        "WARN",
        "ydotoold socket not found; clipboard paste via Ctrl+V will not work",
        required=False,
    )


def _check_systemctl_user() -> Check:
    if _which("systemctl") is None:
        return Check("systemctl-user", "WARN", "systemctl not on PATH", required=False)
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "--version"],
            capture_output=True,
            check=False,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return Check("systemctl-user", "WARN", f"could not run: {e}", required=False)
    if proc.returncode == 0:
        first = proc.stdout.decode(errors="replace").splitlines()[:1]
        return Check(
            "systemctl-user",
            "OK",
            first[0] if first else "available",
            required=False,
        )
    return Check(
        "systemctl-user",
        "WARN",
        f"systemctl --user --version exit {proc.returncode}",
        required=False,
    )


def _check_cosmic_desktop() -> Check:
    val = os.environ.get("XDG_CURRENT_DESKTOP", "")
    if "COSMIC" in val.upper():
        return Check("cosmic-desktop", "OK", val, required=False)
    return Check(
        "cosmic-desktop",
        "WARN",
        f"XDG_CURRENT_DESKTOP={val or '(unset)'}; not COSMIC but should still work",
        required=False,
    )


# ---------------------------------------------------------------------- #
# entry point
# ---------------------------------------------------------------------- #


def run_checks(config: Config) -> list[Check]:
    """Run every environment check and return the results in order."""
    checks: list[Check] = []

    # required
    checks.append(_check_python())
    checks.append(_check_ffmpeg())
    checks.append(_check_whisper_cli(config))
    checks.append(_check_model(config))
    checks.append(_check_path_bin("wl-copy", required=True))
    checks.append(_check_path_bin("wl-paste", required=True))
    checks.append(_check_wayland_session())
    checks.append(_check_temp_dir(config))

    # optional
    checks.append(
        _check_path_bin(
            "wtype",
            required=False,
            warn_detail="optional; alternative direct-typing path. ydotool_type also provides clipboard-free typing",
        )
    )
    checks.append(_check_path_bin("ydotool", required=False))
    checks.append(_check_ydotoold())
    checks.append(
        _check_path_bin(
            "notify-send",
            required=False,
            warn_detail="no desktop notifications",
        )
    )
    checks.append(_check_systemctl_user())
    checks.append(_check_cosmic_desktop())

    return checks
