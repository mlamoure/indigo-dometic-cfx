"""Blocking client: one TCP connection, a daemon reader thread, callbacks.

Designed for hosts without an event loop (Indigo plugins). A ``SyncClient`` is single-use:
``connect()`` once, then ``close()``; reconnect policy belongs to the caller.
"""

from __future__ import annotations

import contextlib
import logging
import socket
import threading
import time
from collections.abc import Callable
from typing import Any

from .errors import ConnectionClosed
from .models import CoolerState
from .protocol import DEFAULT_PORT, Frame
from .session import Event, Session, WriteExpectation

log = logging.getLogger(__name__)

#: Seconds between SUBSCRIBE frames in the initial burst (the firmware reads slowly).
SUBSCRIBE_SPACING = 0.05


def apply_keepalive(
    sock: socket.socket, idle: int = 30, interval: int = 10, count: int = 3
) -> None:
    """Enable TCP keepalive so a powered-off cooler is noticed within ~idle + interval*count."""
    with contextlib.suppress(OSError):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    tcp = socket.IPPROTO_TCP
    idle_opt = getattr(socket, "TCP_KEEPIDLE", None) or getattr(
        socket, "TCP_KEEPALIVE", None
    )
    if idle_opt is not None:
        with contextlib.suppress(OSError):
            sock.setsockopt(tcp, idle_opt, idle)
    for name, value in (("TCP_KEEPINTVL", interval), ("TCP_KEEPCNT", count)):
        opt = getattr(socket, name, None)
        if opt is not None:
            with contextlib.suppress(OSError):
                sock.setsockopt(tcp, opt, value)


class SyncClient:
    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        *,
        on_event: Callable[[Event], None] | None = None,
        on_close: Callable[[BaseException | None], None] | None = None,
        connect_timeout: float = 5.0,
        session: Session | None = None,
        keepalive: bool = True,
        subscribe_spacing: float = SUBSCRIBE_SPACING,
    ) -> None:
        self.host = host
        self.port = port
        self.on_event = on_event
        self.on_close = on_close
        self.connect_timeout = connect_timeout
        self.session = session or Session()
        self.keepalive = keepalive
        self.subscribe_spacing = subscribe_spacing
        self._sock: socket.socket | None = None
        self._lock = threading.RLock()
        self._closed = threading.Event()
        self._reader: threading.Thread | None = None
        self.close_reason: BaseException | None = None

    # ---- lifecycle

    @property
    def connected(self) -> bool:
        return self._sock is not None and not self._closed.is_set()

    def connect(self, *, subscribe: bool = True) -> None:
        """Open the connection and (by default) send the subscribe burst.

        Raises ``OSError`` (including ``TimeoutError``) when the cooler is unreachable.
        """
        if self._sock is not None:
            raise ConnectionClosed(
                "SyncClient is single-use; create a new one to reconnect"
            )
        sock = socket.create_connection(
            (self.host, self.port), timeout=self.connect_timeout
        )
        if self.keepalive:
            apply_keepalive(sock)
        sock.settimeout(1.0)
        self._sock = sock
        self._reader = threading.Thread(
            target=self._read_loop, name=f"ddmp-reader-{self.host}", daemon=True
        )
        self._reader.start()
        if subscribe:
            self.subscribe_all()

    def close(self) -> None:
        """Idempotent. Frees the cooler's connection slot (shutdown before close)."""
        if self._closed.is_set():
            return
        self._closed.set()
        sock = self._sock
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                sock.close()
        reader = self._reader
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=1.5)

    def __enter__(self) -> SyncClient:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- outbound

    def send(self, frame: Frame) -> None:
        sock = self._sock
        if sock is None or self._closed.is_set():
            raise ConnectionClosed(f"not connected to {self.host}:{self.port}")
        with self._lock:
            try:
                sock.sendall(frame.encode_line())
            except OSError as exc:
                raise ConnectionClosed(str(exc)) from exc
        log.debug("-> %s", frame)

    def subscribe_all(self) -> None:
        for frame in self.session.subscribe_frames():
            self.send(frame)
            if self.subscribe_spacing:
                time.sleep(self.subscribe_spacing)

    def set(
        self, name: str, value: Any, *, compartment: int | None = None
    ) -> WriteExpectation:
        """Send a SET for a writable topic; returns what the cooler is expected to echo."""
        with self._lock:
            frame, expectation = self.session.set_frame(
                name, value, compartment=compartment
            )
        self.send(frame)
        return expectation

    # ---- state

    def state(self) -> CoolerState:
        with self._lock:
            return self.session.state()

    def value(self, name: str) -> Any | None:
        with self._lock:
            return self.session.value(name)

    # ---- reader thread

    def _read_loop(self) -> None:
        sock = self._sock
        error: BaseException | None = None
        assert sock is not None
        try:
            while not self._closed.is_set():
                try:
                    data = sock.recv(4096)
                except TimeoutError:
                    continue
                except OSError as exc:
                    if not self._closed.is_set():
                        error = exc
                    break
                if not data:
                    if not self._closed.is_set():
                        error = ConnectionClosed("cooler closed the connection")
                    break
                with self._lock:
                    events = self.session.feed(data)
                for event in events:
                    log.debug("<- %s", event)
                    if self.on_event is not None:
                        try:
                            self.on_event(event)
                        except Exception:
                            log.exception("on_event callback failed")
        finally:
            self.close_reason = error
            self._closed.set()
            with contextlib.suppress(OSError):
                sock.close()
            if self.on_close is not None:
                try:
                    self.on_close(error)
                except Exception:
                    log.exception("on_close callback failed")
