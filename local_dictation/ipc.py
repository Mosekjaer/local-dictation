"""Unix-socket IPC for the local-dictation daemon.

Protocol: one request per connection. Each side writes a single
newline-terminated UTF-8 JSON object and then the server closes the connection.

Request shapes::

    {"cmd": "toggle",  "language": "auto" | "da" | "en" | null}
    {"cmd": "start",   "language": "auto" | "da" | "en" | null}
    {"cmd": "stop"}
    {"cmd": "status"}

Response shapes::

    {"ok": true,  "state": "idle"|"recording"|"transcribing", "info": "..."}
    {"ok": false, "error": "human-readable message"}
"""

from __future__ import annotations

import json
import logging
import os
import socket
import stat
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

_RECV_LIMIT = 64 * 1024  # 64 KB per request; ample for our JSON.


def send_request(req: dict, socket_path: Path, timeout: float = 2.0) -> dict:
    """Send one request over the local-dictation socket and return the reply.

    Raises:
        FileNotFoundError: socket path does not exist.
        ConnectionRefusedError: nothing is listening on the socket.
        TimeoutError: connection or read exceeded ``timeout`` seconds.
        ValueError: response was not valid JSON.
    """
    path = str(socket_path)
    if not os.path.exists(path):
        raise FileNotFoundError(path)

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        try:
            sock.connect(path)
        except ConnectionRefusedError:
            raise
        except socket.timeout as exc:
            raise TimeoutError(f"connect timed out after {timeout}s") from exc

        payload = (json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            sock.sendall(payload)
        except socket.timeout as exc:
            raise TimeoutError(f"send timed out after {timeout}s") from exc

        # Read until newline or EOF.
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass

        buf = bytearray()
        while True:
            try:
                chunk = sock.recv(4096)
            except socket.timeout as exc:
                raise TimeoutError(f"recv timed out after {timeout}s") from exc
            if not chunk:
                break
            buf.extend(chunk)
            if b"\n" in chunk or len(buf) >= _RECV_LIMIT:
                break

        if not buf:
            raise ValueError("empty response from daemon")

        line, _, _ = buf.partition(b"\n")
        try:
            return json.loads(line.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON from daemon: {exc}") from exc
    finally:
        try:
            sock.close()
        except OSError:
            pass


def serve(
    socket_path: Path,
    handler: Callable[[dict], dict],
) -> None:
    """Bind a Unix socket and serve one request per connection until interrupted.

    Behaviour:
        * Removes a stale socket file if it exists and no listener is bound.
        * Creates the parent directory.
        * Sets file mode 0600 after bind.
        * For each connection: read one newline-terminated UTF-8 JSON request,
          call ``handler(req)``, write ``json(reply)+"\\n"``, close.
        * Handler exceptions are caught and reported as
          ``{"ok": false, "error": str(exc)}``; serving continues.
        * Returns cleanly on KeyboardInterrupt / SystemExit, unlinking the
          socket file on the way out.
    """
    path = str(socket_path)
    parent = Path(path).parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OSError(f"cannot create socket dir {parent}: {exc}") from exc

    _unlink_if_stale(path)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(path)
    except OSError as exc:
        try:
            server.close()
        except OSError:
            pass
        raise OSError(f"cannot bind socket {path}: {exc}") from exc

    try:
        os.chmod(path, 0o600)
    except OSError as exc:  # pragma: no cover - defensive
        log.warning("cannot chmod 0600 %s: %s", path, exc)

    server.listen(8)
    log.info("ipc listening on %s", path)

    try:
        while True:
            try:
                conn, _ = server.accept()
            except (KeyboardInterrupt, SystemExit):
                raise
            except OSError as exc:
                log.warning("accept failed: %s", exc)
                continue
            _handle_one(conn, handler)
    except (KeyboardInterrupt, SystemExit):
        log.info("ipc shutting down")
    finally:
        try:
            server.close()
        except OSError:
            pass
        try:
            if os.path.exists(path):
                os.unlink(path)
        except OSError as exc:  # pragma: no cover - defensive
            log.debug("unlink on shutdown failed: %s", exc)


def _handle_one(conn: socket.socket, handler: Callable[[dict], dict]) -> None:
    try:
        conn.settimeout(2.0)
        buf = bytearray()
        while True:
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                _send(conn, {"ok": False, "error": "request timeout"})
                return
            except OSError as exc:
                log.debug("recv failed: %s", exc)
                return
            if not chunk:
                break
            buf.extend(chunk)
            if b"\n" in chunk or len(buf) >= _RECV_LIMIT:
                break

        line, _, _ = buf.partition(b"\n")
        if not line.strip():
            _send(conn, {"ok": False, "error": "empty request"})
            return

        try:
            req = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            _send(conn, {"ok": False, "error": f"invalid JSON: {exc}"})
            return

        if not isinstance(req, dict):
            _send(conn, {"ok": False, "error": "request must be a JSON object"})
            return

        try:
            reply = handler(req)
        except Exception as exc:
            log.exception("handler raised")
            reply = {"ok": False, "error": str(exc) or exc.__class__.__name__}

        if not isinstance(reply, dict):
            reply = {"ok": False, "error": "handler returned non-dict"}

        _send(conn, reply)
    finally:
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            conn.close()
        except OSError:
            pass


def _send(conn: socket.socket, reply: dict[str, Any]) -> None:
    try:
        payload = (json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        payload = (
            json.dumps({"ok": False, "error": f"cannot encode reply: {exc}"})
            + "\n"
        ).encode("utf-8")
    try:
        conn.sendall(payload)
    except OSError as exc:
        log.debug("send reply failed: %s", exc)


def _unlink_if_stale(path: str) -> None:
    """Remove a stale socket file if nothing is listening on it."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        log.warning("stat %s failed: %s", path, exc)
        return

    if not stat.S_ISSOCK(st.st_mode):
        # Not a socket; refuse to clobber.
        raise OSError(f"socket path exists and is not a socket: {path}")

    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        probe.connect(path)
    except (ConnectionRefusedError, FileNotFoundError):
        # Nothing listening — safe to unlink.
        try:
            os.unlink(path)
            log.info("removed stale socket %s", path)
        except OSError as exc:
            raise OSError(f"cannot unlink stale socket {path}: {exc}") from exc
    except OSError:
        # Some other error connecting; assume something is alive.
        raise OSError(f"socket {path} already in use")
    else:
        # Connect succeeded — another daemon is live.
        raise OSError(f"socket {path} already in use by another listener")
    finally:
        try:
            probe.close()
        except OSError:
            pass
