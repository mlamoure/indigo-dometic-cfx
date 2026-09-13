"""Pure (socket-free) protocol session: bytes in, events out, frames to send.

A :class:`Session` remembers the last published value of every topic, decodes inbound
frames into :class:`Publish` / :class:`Nak` / :class:`Unhandled` events, builds the
SUBSCRIBE burst and SET frames, and produces a :class:`WriteExpectation` that tells the
caller when the cooler has confirmed a write (the cooler echoes every accepted SET as a
PUBLISH; there is no ACK on this transport).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import DecodeError, StateUnknown
from .models import CoolerState
from .protocol import (
    SUBSCRIBE_TOPICS,
    TOPIC_BY_ADDRESS,
    Action,
    Address,
    Frame,
    LineDecoder,
    Topic,
    assert_writable,
)


@dataclass(frozen=True, slots=True)
class Publish:
    address: Address
    topic: Topic | None
    value: Any
    raw: bytes

    @property
    def name(self) -> str:
        return self.topic.name if self.topic else str(self.address)


@dataclass(frozen=True, slots=True)
class Nak:
    address: Address
    raw: bytes


@dataclass(frozen=True, slots=True)
class Unhandled:
    frame: Frame


Event = Publish | Nak | Unhandled


@dataclass(slots=True)
class WriteExpectation:
    """What the cooler should publish back after a SET."""

    topic: Topic
    expected: Any
    tolerance: float = 0.0
    observed: Any = None

    def matches(self, event: Event) -> bool | None:
        """``True`` when a publish confirms the write, ``False`` when the cooler refused it
        with a NAK, ``None`` otherwise.

        The cooler answers a SET by re-publishing the *old* value first and publishes the new
        value a few seconds later, so a publish that does not match yet is not a refusal; the
        last value seen is kept in :attr:`observed` for the timeout message.
        """
        if isinstance(event, Nak):
            return False if event.address == self.topic.address else None
        if not isinstance(event, Publish) or event.address != self.topic.address:
            return None
        self.observed = event.value
        return True if _close(event.value, self.expected, self.tolerance) else None


def _close(a: Any, b: Any, tol: float) -> bool:
    if isinstance(a, list | tuple) and isinstance(b, list | tuple):
        return len(a) == len(b) and all(
            _close(x, y, tol) for x, y in zip(a, b, strict=True)
        )
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    if isinstance(a, int | float) and isinstance(b, int | float):
        return abs(float(a) - float(b)) <= tol
    return a == b


class Session:
    def __init__(self, topics: tuple[Topic, ...] = SUBSCRIBE_TOPICS) -> None:
        self.topics = topics
        self.values: dict[str, Any] = {}
        self.raw: dict[Address, bytes] = {}
        self._decoder = LineDecoder()

    # ---- inbound

    def feed(self, data: bytes) -> list[Event]:
        events: list[Event] = []
        for frame in self._decoder.feed(data):
            if frame.action == Action.PUBLISH:
                events.append(self._publish(frame))
            elif frame.action == Action.NAK:
                events.append(Nak(frame.address, frame.payload))
            else:
                events.append(Unhandled(frame))
        return events

    def _publish(self, frame: Frame) -> Publish:
        tp = TOPIC_BY_ADDRESS.get(frame.address)
        self.raw[frame.address] = frame.payload
        value: Any = frame.payload
        if tp is not None:
            try:
                value = tp.codec.decode(frame.payload)
            except DecodeError:
                value = frame.payload
            self.values[tp.name] = value
        return Publish(frame.address, tp, value, frame.payload)

    @property
    def dropped(self) -> int:
        return self._decoder.dropped

    # ---- state

    def value(self, name: str) -> Any | None:
        return self.values.get(name)

    def state(self) -> CoolerState:
        return CoolerState.from_values(self.values)

    # ---- outbound

    def subscribe_frames(self) -> list[Frame]:
        return [Frame(Action.SUBSCRIBE, t.address) for t in self.topics]

    def set_frame(
        self, name: str | Address | Topic, value: Any, *, compartment: int | None = None
    ) -> tuple[Frame, WriteExpectation]:
        """Build a SET frame for a writable topic.

        Per-compartment topics are written as the full array (the other compartments keep
        their last published value), so the current value must be known first.
        """
        tp = assert_writable(name)
        # the cooler stores temperatures with 0.1 °C granularity (2.2222 -> 2.2)
        tolerance = 0.051 if tp.codec.kind.startswith("milli") else 0.0
        if tp.per_compartment:
            current = self.values.get(tp.name)
            if not current:
                raise StateUnknown(
                    f"{tp.name} has not been published yet; subscribe first"
                )
            idx = 0 if compartment is None else compartment
            if idx < 0 or idx >= len(current):
                raise ValueError(
                    f"compartment {idx} out of range for {tp.name} ({len(current)})"
                )
            new = list(current)
            new[idx] = _coerce(tp, value)
            if tp.name == "csettemp":
                self._check_range(idx, new[idx])
            payload = tp.codec.encode(new)
            return Frame(Action.SET, tp.address, payload), WriteExpectation(
                tp, new, tolerance
            )
        if compartment not in (None, 0):
            raise ValueError(f"{tp.name} is not a per-compartment topic")
        coerced = _coerce(tp, value)
        payload = tp.codec.encode(coerced)
        expected = tp.codec.decode(payload)
        return Frame(Action.SET, tp.address, payload), WriteExpectation(
            tp, expected, tolerance
        )

    def _check_range(self, idx: int, setpoint_c: float) -> None:
        ranges = self.values.get("ctemprng") or []
        if idx < len(ranges):
            lo, hi = ranges[idx]
            if not lo <= setpoint_c <= hi:
                raise ValueError(
                    f"set-point {setpoint_c} °C outside cooler range {lo}..{hi} °C"
                )


def _coerce(tp: Topic, value: Any) -> Any:
    kind = tp.codec.kind
    if kind in ("bool", "bool[]"):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "on", "yes")
        return bool(value)
    if kind in ("milli", "milli[]"):
        return float(value)
    if kind == "enum":
        return tp.codec.decode(tp.codec.encode(value))
    return value
