"""A scripted stand-in for ddmp.sync_client.SyncClient used by the link tests."""

from __future__ import annotations

import base64
from pathlib import Path

from ddmp.protocol import Frame, publish_frame
from ddmp.session import Session

CAPTURE = Path(__file__).parent / "fixtures" / "capture-2026-09-13.b64"


def capture_stream() -> bytes:
    lines = [
        ln.strip().encode()
        for ln in CAPTURE.read_text().splitlines()
        if ln.strip() and not ln.startswith("#")
    ]
    return b"".join(ln + b"\r" for ln in lines)


class FakeClient:
    """Mimics SyncClient's surface without sockets. Instances are recorded on the factory."""

    def __init__(
        self,
        host,
        port=13143,
        *,
        on_event=None,
        on_close=None,
        connect_timeout=5.0,
        fail_connect=False,
        auto_publish=True,
        echo_sets=True,
    ):
        self.host = host
        self.port = port
        self.on_event = on_event
        self.on_close = on_close
        self.connect_timeout = connect_timeout
        self.fail_connect = fail_connect
        self.auto_publish = auto_publish
        self.echo_sets = echo_sets
        self.session = Session()
        self.connected = False
        self.closed = False
        self.sent: list[Frame] = []
        self.subscribe_calls = 0

    def connect(self, *, subscribe=True):
        if self.fail_connect:
            raise TimeoutError("timed out")
        self.connected = True
        if subscribe:
            self.subscribe_all()

    def subscribe_all(self):
        self.subscribe_calls += 1
        if self.auto_publish:
            self.feed(capture_stream())

    def feed(self, data: bytes):
        for event in self.session.feed(data):
            if self.on_event is not None:
                self.on_event(event)

    def publish(self, name, value):
        self.feed(publish_frame(name, value).encode_line())

    def set(self, name, value, *, compartment=None):
        frame, expectation = self.session.set_frame(
            name, value, compartment=compartment
        )
        self.sent.append(frame)
        if self.echo_sets:
            self.feed(Frame(0x10, frame.address, frame.payload).encode_line())
        return expectation

    def send(self, frame):
        self.sent.append(frame)

    def state(self):
        return self.session.state()

    def value(self, name):
        return self.session.value(name)

    def drop(self, exc=None):
        """Simulate the cooler going away."""
        self.connected = False
        if self.on_close is not None:
            self.on_close(exc)

    def close(self):
        self.closed = True
        self.connected = False


class Factory:
    def __init__(self, **defaults):
        self.defaults = defaults
        self.clients: list[FakeClient] = []

    def __call__(self, host, port=13143, **kwargs):
        opts = {**self.defaults, **kwargs}
        client = FakeClient(host, port, **opts)
        self.clients.append(client)
        return client

    @property
    def last(self) -> FakeClient:
        return self.clients[-1]


def b64(frame: Frame) -> str:
    return base64.b64encode(frame.encode()).decode()
