"""Push-style event broadcast socket for the local-dictation daemon.

Subscriber model: opens a Unix-socket connection. The server immediately
sends one ``hello`` event with the current daemon state, then pushes one
newline-delimited UTF-8 JSON object per subsequent event until the
subscriber closes the connection. The subscriber never writes.

Wire protocol (each line is a complete JSON object)::

    {"type":"hello","version":1,"daemon_state":"<state>","ts":<float>}
    {"type":"state","state":"idle"|"recording"|"transcribing"|"error","ts":<float>}
    {"type":"meter","peak_db":<float>,"rms_db":<float>,"ts":<float>}

``peak_db`` and ``rms_db`` are dBFS in the range ``(-120, 0]``. ``-inf`` is
clamped to ``-120.0`` so consumers never need to handle JSON ``Infinity``.

Slow-subscriber policy: each subscriber has a bounded ``queue.Queue``
(``maxsize=64``). Publishing uses ``put_nowait``; on ``queue.Full`` we
increment a per-subscriber drop counter, and after **5 consecutive drops**
that subscriber is disconnected. The producer (the daemon) never blocks.

The server socket lives at ``Config.events_socket_path`` (default
``$XDG_RUNTIME_DIR/local-dictation-events.sock``) with mode ``0600``.
"""

from __future__ import annotations

import json
import logging
import math
import os
import socket
import stat
import threading
import time
from pathlib import Path
from queue import Empty, Full, Queue
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)


# Sentinel placed on a subscriber's queue by stop() to wake the sender
# thread so it can exit cleanly.
_SENTINEL: dict[str, Any] = {"__sentinel__": True}

_QUEUE_MAX = 64
_DROP_DISCONNECT_THRESHOLD = 5


def clamp_db(value: float) -> float:
    """Clamp a dBFS value into the inclusive range [-120, 0].

    ``-inf`` and NaN map to -120.0. Values above 0 are clipped to 0.
    """
    if value is None:
        return -120.0
    try:
        v = float(value)
    except (TypeError, ValueError):
        return -120.0
    if math.isnan(v) or v < -120.0 or math.isinf(v) and v < 0:
        return -120.0
    if v > 0.0:
        return 0.0
    return v


def encode_event(event: dict[str, Any]) -> bytes:
    """Encode a single event as a newline-terminated UTF-8 JSON line."""
    return (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")


class _Subscriber:
    __slots__ = ("conn", "queue", "drops", "sender", "addr")

    def __init__(self, conn: socket.socket, addr: str) -> None:
        self.conn = conn
        self.addr = addr
        self.queue: Queue[dict[str, Any]] = Queue(maxsize=_QUEUE_MAX)
        self.drops = 0
        self.sender: threading.Thread | None = None


class EventsServer:
    """Multi-subscriber broadcast server.

    Lifecycle:
        * ``start()`` binds the socket and spawns an accept thread. Idempotent.
        * ``publish(event)`` fans the event out to every connected subscriber's
          queue. Never blocks; drops on slow consumers.
        * ``stop()`` closes the listening socket, drops all subscribers, and
          joins worker threads.
    """

    def __init__(
        self,
        socket_path: Path,
        get_state: Callable[[], Any],
    ) -> None:
        self._socket_path = Path(socket_path)
        self._get_state = get_state

        self._server: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._running = False
        self._failed = False  # set if bind() fails; publish() becomes a no-op
        self._lock = threading.Lock()
        self._subs: list[_Subscriber] = []

    # ---- lifecycle ----

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._failed = False
            try:
                self._bind_locked()
            except OSError as exc:
                log.error(
                    "events server: cannot bind %s: %s; events disabled",
                    self._socket_path,
                    exc,
                )
                self._failed = True
                self._running = False
                self._server = None
                return

            t = threading.Thread(
                target=self._accept_loop,
                name="ld-events-accept",
                daemon=True,
            )
            self._accept_thread = t
            t.start()
            log.info("events server listening on %s", self._socket_path)

    def stop(self) -> None:
        with self._lock:
            if not self._running and self._server is None:
                # Either never started or already stopped.
                self._failed = False
                return
            self._running = False
            server = self._server
            self._server = None
            subs = list(self._subs)
            self._subs.clear()

        # Closing the server socket makes accept() return, unwinding the
        # accept thread.
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
            try:
                if os.path.exists(self._socket_path):
                    os.unlink(self._socket_path)
            except OSError as exc:  # pragma: no cover - defensive
                log.debug("events: unlink on stop failed: %s", exc)

        accept_thread = self._accept_thread
        self._accept_thread = None
        if accept_thread is not None and accept_thread.is_alive():
            accept_thread.join(timeout=1.0)

        # Wake every subscriber's sender thread and let it exit.
        for sub in subs:
            self._shutdown_subscriber(sub, join=True)

    # ---- publishing ----

    def publish(self, event: dict[str, Any]) -> None:
        """Fan ``event`` out to every connected subscriber. Never blocks."""
        with self._lock:
            if self._failed or not self._running:
                return
            subs = list(self._subs)

        if not subs:
            return

        dropped: list[_Subscriber] = []
        for sub in subs:
            try:
                sub.queue.put_nowait(event)
            except Full:
                sub.drops += 1
                log.debug(
                    "events: subscriber %s queue full (drops=%d)",
                    sub.addr,
                    sub.drops,
                )
                if sub.drops >= _DROP_DISCONNECT_THRESHOLD:
                    dropped.append(sub)
            else:
                # Successful enqueue resets the consecutive drop counter.
                sub.drops = 0

        for sub in dropped:
            log.info(
                "events: disconnecting slow subscriber %s after %d drops",
                sub.addr,
                sub.drops,
            )
            self._remove_subscriber(sub)
            self._shutdown_subscriber(sub, join=False)

    # ---- internals ----

    def _bind_locked(self) -> None:
        path = str(self._socket_path)
        parent = self._socket_path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise OSError(f"cannot create events socket dir {parent}: {exc}") from exc

        _unlink_if_stale(path)

        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            srv.bind(path)
        except OSError:
            try:
                srv.close()
            except OSError:
                pass
            raise
        try:
            os.chmod(path, 0o600)
        except OSError as exc:  # pragma: no cover - defensive
            log.warning("events: chmod 0600 %s failed: %s", path, exc)
        srv.listen(8)
        srv.settimeout(0.5)  # so accept() can notice _running going False
        self._server = srv

    def _accept_loop(self) -> None:
        while True:
            with self._lock:
                if not self._running:
                    return
                server = self._server
            if server is None:
                return
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                # Server socket was closed (stop() called) or fatal error.
                return
            self._handle_new_connection(conn)

    def _handle_new_connection(self, conn: socket.socket) -> None:
        try:
            conn.setblocking(True)
        except OSError:
            pass
        addr = _conn_repr(conn)
        sub = _Subscriber(conn, addr)
        sender = threading.Thread(
            target=self._sender_loop,
            args=(sub,),
            name=f"ld-events-send-{addr}",
            daemon=True,
        )
        sub.sender = sender

        with self._lock:
            if not self._running:
                try:
                    conn.close()
                except OSError:
                    pass
                return
            self._subs.append(sub)
            current_state = _state_str(self._get_state)

        # Hello event first, before any state/meter that arrives after.
        hello: dict[str, Any] = {
            "type": "hello",
            "version": 1,
            "daemon_state": current_state,
            "ts": time.time(),
        }
        try:
            sub.queue.put_nowait(hello)
        except Full:  # pragma: no cover - fresh queue
            pass

        sender.start()
        log.debug("events: subscriber %s connected", addr)

    def _sender_loop(self, sub: _Subscriber) -> None:
        conn = sub.conn
        try:
            while True:
                try:
                    event = sub.queue.get(timeout=0.5)
                except Empty:
                    # Periodic wake-up; check if we should exit.
                    if not self._running:
                        return
                    continue
                if event is _SENTINEL or "__sentinel__" in event:
                    return
                try:
                    conn.sendall(encode_event(event))
                except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                    log.debug("events: send to %s failed: %s", sub.addr, exc)
                    self._remove_subscriber(sub)
                    return
        finally:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass

    def _remove_subscriber(self, sub: _Subscriber) -> None:
        with self._lock:
            try:
                self._subs.remove(sub)
            except ValueError:
                pass

    def _shutdown_subscriber(self, sub: _Subscriber, *, join: bool) -> None:
        # Wake the sender thread by enqueuing the sentinel.
        try:
            sub.queue.put_nowait(_SENTINEL)
        except Full:
            # Queue full and the sender isn't draining; close the socket
            # to force it out.
            try:
                sub.conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sub.conn.close()
            except OSError:
                pass
        if join and sub.sender is not None and sub.sender.is_alive():
            sub.sender.join(timeout=1.0)


# ---- helpers ----


def _state_str(get_state: Callable[[], Any]) -> str:
    """Resolve the daemon's current state into a wire-format string."""
    try:
        s = get_state()
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("events: get_state raised: %s", exc)
        return "idle"
    if s is None:
        return "idle"
    val = getattr(s, "value", None)
    if isinstance(val, str):
        return val
    if isinstance(s, str):
        return s
    return str(s)


def _conn_repr(conn: socket.socket) -> str:
    try:
        fd = conn.fileno()
    except OSError:
        fd = -1
    return f"fd={fd}"


def _unlink_if_stale(path: str) -> None:
    """Mirror of ipc._unlink_if_stale; duplicated to avoid editing ipc.py."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        log.warning("events: stat %s failed: %s", path, exc)
        return

    if not stat.S_ISSOCK(st.st_mode):
        raise OSError(f"events socket path exists and is not a socket: {path}")

    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        probe.connect(path)
    except (ConnectionRefusedError, FileNotFoundError):
        try:
            os.unlink(path)
            log.info("events: removed stale socket %s", path)
        except OSError as exc:
            raise OSError(
                f"events: cannot unlink stale socket {path}: {exc}"
            ) from exc
    except OSError:
        raise OSError(f"events socket {path} already in use")
    else:
        raise OSError(f"events socket {path} already in use by another listener")
    finally:
        try:
            probe.close()
        except OSError:
            pass


__all__: Iterable[str] = (
    "EventsServer",
    "clamp_db",
    "encode_event",
)
