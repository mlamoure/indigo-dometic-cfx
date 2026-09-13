"""asyncio client with the same semantics as :class:`ddmp.sync_client.SyncClient`."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

from .errors import ConnectionClosed, WriteRejected, WriteTimeout
from .models import CoolerState
from .protocol import DEFAULT_PORT, Frame
from .session import Event, Session, WriteExpectation
from .sync_client import SUBSCRIBE_SPACING, apply_keepalive

log = logging.getLogger(__name__)


class AsyncClient:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        session: Session | None = None,
        subscribe_spacing: float = SUBSCRIBE_SPACING,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self.session = session or Session()
        self.subscribe_spacing = subscribe_spacing
        self._events: asyncio.Queue[Event | None] = asyncio.Queue()
        self._pending: list[tuple[WriteExpectation, asyncio.Future[Any]]] = []
        self._closed = False
        self.close_reason: BaseException | None = None
        self._task = asyncio.create_task(self._read_loop(), name="ddmp-reader")

    @classmethod
    async def connect(
        cls,
        host: str,
        port: int = DEFAULT_PORT,
        *,
        timeout: float = 5.0,
        session: Session | None = None,
        subscribe: bool = True,
        keepalive: bool = True,
    ) -> AsyncClient:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout
        )
        if keepalive:
            sock = writer.get_extra_info("socket")
            if sock is not None:
                apply_keepalive(sock)
        client = cls(reader, writer, session=session, subscribe_spacing=0.0)
        if subscribe:
            await client.subscribe_all()
        return client

    @property
    def connected(self) -> bool:
        return not self._closed

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task
        with contextlib.suppress(Exception):
            sock = self._writer.get_extra_info("socket")
            if sock is not None:
                import socket as _socket

                sock.shutdown(_socket.SHUT_RDWR)
        self._writer.close()
        with contextlib.suppress(Exception):
            await self._writer.wait_closed()
        await self._events.put(None)

    async def __aenter__(self) -> AsyncClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ---- outbound

    async def send(self, frame: Frame) -> None:
        if self._closed:
            raise ConnectionClosed("client is closed")
        self._writer.write(frame.encode_line())
        await self._writer.drain()
        log.debug("-> %s", frame)

    async def subscribe_all(self) -> None:
        for frame in self.session.subscribe_frames():
            await self.send(frame)
            if self.subscribe_spacing:
                await asyncio.sleep(self.subscribe_spacing)

    async def set(
        self,
        name: str,
        value: Any,
        *,
        compartment: int | None = None,
        timeout: float = 10.0,
    ) -> Any:
        """Write a value and wait for the cooler to publish it; returns the published value.

        The cooler re-publishes the old value first and the new one a few seconds later.
        """
        frame, expectation = self.session.set_frame(
            name, value, compartment=compartment
        )
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending.append((expectation, fut))
        try:
            await self.send(frame)
            return await asyncio.wait_for(fut, timeout)
        except TimeoutError as exc:
            raise WriteTimeout(
                f"{expectation.topic.name} not confirmed within {timeout}s "
                f"(cooler reports {expectation.observed!r})"
            ) from exc
        finally:
            with contextlib.suppress(ValueError):
                self._pending.remove((expectation, fut))

    # ---- state / events

    def state(self) -> CoolerState:
        return self.session.state()

    def value(self, name: str) -> Any | None:
        return self.session.value(name)

    async def events(self) -> AsyncIterator[Event]:
        """Yield every inbound event until the connection closes."""
        while True:
            event = await self._events.get()
            if event is None:
                return
            yield event

    async def _read_loop(self) -> None:
        error: BaseException | None = None
        try:
            while True:
                data = await self._reader.read(4096)
                if not data:
                    error = ConnectionClosed("cooler closed the connection")
                    break
                for event in self.session.feed(data):
                    log.debug("<- %s", event)
                    self._resolve(event)
                    await self._events.put(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = exc
        finally:
            self.close_reason = error
            self._closed = True
            for _, fut in self._pending:
                if not fut.done():
                    fut.set_exception(ConnectionClosed("connection lost"))
            await self._events.put(None)

    def _resolve(self, event: Event) -> None:
        for expectation, fut in list(self._pending):
            if fut.done():
                continue
            verdict = expectation.matches(event)
            if verdict is True:
                fut.set_result(expectation.observed)
            elif verdict is False:
                fut.set_exception(
                    WriteRejected(f"cooler refused {expectation.topic.name} (NAK)")
                )
