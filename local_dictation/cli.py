"""argparse entry point for ``local-dictation``.

Subcommands:
    doctor      — run dependency checks, no daemon required.
    toggle      — IPC: start or stop recording depending on state.
    start       — IPC: begin recording.
    stop        — IPC: stop recording and transcribe.
    status      — IPC: print current state.
    transcribe  — synchronous, bypasses the daemon entirely.
    daemon      — hidden; runs the long-lived process for systemd.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from . import config as config_module
from . import ipc, logging_setup
from .config import Config

log = logging.getLogger(__name__)

_LANG_CHOICES = ("auto", "da", "en")
_SYSTEMD_UNIT = "local-dictation.service"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="local-dictation",
        description="Local push-to-talk dictation via whisper.cpp.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="path to config TOML (default: ~/.config/local-dictation/config.toml)",
    )
    sub = parser.add_subparsers(dest="cmd", metavar="COMMAND")
    sub.required = True

    sub.add_parser("doctor", help="run dependency checks and exit")

    p_toggle = sub.add_parser(
        "toggle", help="toggle recording (idle->record, record->transcribe)"
    )
    p_toggle.add_argument("--language", choices=_LANG_CHOICES, default=None)

    p_start = sub.add_parser("start", help="start recording")
    p_start.add_argument("--language", choices=_LANG_CHOICES, default=None)

    sub.add_parser("stop", help="stop recording and transcribe")
    sub.add_parser("status", help="print daemon state")

    p_transcribe = sub.add_parser(
        "transcribe", help="transcribe a WAV file (no daemon required)"
    )
    p_transcribe.add_argument("file", type=Path, help="path to a WAV file")
    p_transcribe.add_argument("--language", choices=_LANG_CHOICES, default=None)

    # Hidden subcommand used by systemd; deliberately not listed in --help.
    # No `help=` keyword keeps it out of the subcommand listing entirely.
    p_daemon = sub.add_parser("daemon")
    p_daemon.set_defaults(_hidden=True)
    # Drop it from the choices listing rendered by the subparser action.
    # argparse stores the help table on the action via `_choices_actions`.
    for action in sub._choices_actions:  # type: ignore[attr-defined]
        if getattr(action, "dest", None) == "daemon":
            sub._choices_actions.remove(action)  # type: ignore[attr-defined]
            break

    return parser


def _load_config(path: Optional[Path]) -> Config:
    try:
        return config_module.load(path)
    except FileNotFoundError:
        # load() handles a missing default path; this fires when an explicit
        # --config path was supplied but does not exist.
        sys.stderr.write(f"config file not found: {path}\n")
        sys.exit(1)
    except Exception as exc:
        sys.stderr.write(f"failed to load config: {exc}\n")
        sys.exit(1)


# ---- IPC client helpers ----


def _send_with_autostart(req: dict, config: Config) -> dict:
    """Send an IPC request; on missing/refused socket, try to start the unit once."""
    try:
        return ipc.send_request(req, config.socket_path)
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        log.info("daemon not reachable (%s); attempting autostart", exc)
        if _try_start_service():
            time.sleep(1.0)
            try:
                return ipc.send_request(req, config.socket_path)
            except (FileNotFoundError, ConnectionRefusedError, TimeoutError) as exc2:
                _die_no_daemon(exc2)
        else:
            _die_no_daemon(exc)
    except TimeoutError as exc:
        sys.stderr.write(f"daemon did not respond: {exc}\n")
        sys.exit(1)
    # Unreachable
    return {"ok": False, "error": "unreachable"}


def _try_start_service() -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "--user", "start", _SYSTEMD_UNIT],
            timeout=5,
            capture_output=True,
        )
    except FileNotFoundError:
        return False
    except subprocess.TimeoutExpired:
        return False
    except OSError:
        return False
    return result.returncode == 0


def _die_no_daemon(exc: BaseException) -> None:
    sys.stderr.write(
        "daemon not running; run: systemctl --user start local-dictation\n"
    )
    log.debug("autostart failed: %s", exc)
    sys.exit(1)


# ---- subcommand handlers ----


def _cmd_doctor(config: Config) -> int:
    from . import doctor as doctor_mod

    try:
        checks = doctor_mod.run_checks(config)
    except NotImplementedError:
        sys.stderr.write("doctor module not implemented yet\n")
        return 2
    except Exception as exc:
        sys.stderr.write(f"doctor failed: {exc}\n")
        return 2

    any_required_fail = False
    for check in checks:
        status = check.status
        tag = f"[{status:>4}]"
        detail = f" — {check.detail}" if check.detail else ""
        print(f"{tag} {check.name}{detail}")
        if status == "FAIL" and check.required:
            any_required_fail = True
    return 2 if any_required_fail else 0


def _cmd_ipc_simple(cmd: str, config: Config, language: Optional[str] = None) -> int:
    req: dict = {"cmd": cmd}
    if language is not None:
        req["language"] = language
    elif cmd in {"toggle", "start"}:
        req["language"] = None  # explicit "use config default"

    reply = _send_with_autostart(req, config)
    if not reply.get("ok"):
        sys.stderr.write(f"{reply.get('error', 'unknown error')}\n")
        return 1
    info = reply.get("info") or reply.get("state") or ""
    if info:
        print(info)
    return 0


def _cmd_status(config: Config) -> int:
    reply = _send_with_autostart({"cmd": "status"}, config)
    if not reply.get("ok"):
        sys.stderr.write(f"{reply.get('error', 'unknown error')}\n")
        return 1
    print(reply.get("state", ""))
    return 0


def _cmd_transcribe(file_path: Path, language: Optional[str], config: Config) -> int:
    from .transcribe import Transcriber, TranscriberError

    if not file_path.exists():
        sys.stderr.write(f"file not found: {file_path}\n")
        return 1

    lang = language if language is not None else config.language
    try:
        transcriber = Transcriber(config)
        text = transcriber.transcribe(file_path, lang)  # type: ignore[arg-type]
    except TranscriberError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    except Exception as exc:
        sys.stderr.write(f"transcribe failed: {exc}\n")
        return 1

    sys.stdout.write(text)
    if text and not text.endswith("\n"):
        sys.stdout.write("\n")
    return 0


def _cmd_daemon(config: Config) -> int:
    from . import daemon

    return daemon.run(config)


# ---- main ----


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    config = _load_config(args.config)
    config.ensure_dirs()
    logging_setup.configure(config)

    cmd = args.cmd
    if cmd == "doctor":
        return _cmd_doctor(config)
    if cmd == "toggle":
        return _cmd_ipc_simple("toggle", config, args.language)
    if cmd == "start":
        return _cmd_ipc_simple("start", config, args.language)
    if cmd == "stop":
        return _cmd_ipc_simple("stop", config)
    if cmd == "status":
        return _cmd_status(config)
    if cmd == "transcribe":
        return _cmd_transcribe(args.file, args.language, config)
    if cmd == "daemon":
        return _cmd_daemon(config)

    parser.error(f"unknown command: {cmd}")
    return 2  # pragma: no cover - parser.error exits


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
