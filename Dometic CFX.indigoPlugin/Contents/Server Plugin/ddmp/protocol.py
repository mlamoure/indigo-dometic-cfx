"""Sans-I/O core of Dometic's DDMP v2 / DDM2 protocol as spoken by CFX5 coolers over Wi-Fi.

Wire format (decoded from Mobile Cooling 2.0.30, verified live 2026-09-13):

* One TCP connection to port 13143. Every frame is ``base64(bytes) + "\\r"``; the inbound
  stream is split on ``"\\r"`` and each piece is base64-decoded.
* Frame bytes are ``action, p0, p1, p2, p3, payload...``. The four address bytes are
  parameter, instance, subclass and class.
* Values are little-endian int32 (booleans 0/1, enums as integers, temperatures, volts and
  amps scaled by 1000), one int32 per compartment for per-compartment topics, UTF-8 strings
  padded with NUL bytes, and uint16 arrays for error lists.

Nothing in this module touches a socket; see :mod:`ddmp.session`, :mod:`ddmp.sync_client`
and :mod:`ddmp.aio` for the I/O layers.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, NamedTuple, Protocol

from .errors import DecodeError, NotWritable
from .models import BatteryProtection, PowerSource, ProductType

DEFAULT_PORT = 13143
LINE_TERMINATOR = b"\r"


class Action(IntEnum):
    """DDM2 action byte (first byte of every frame)."""

    HELLO = 0x03
    ACK = 0x04
    NAK = 0x05
    NOP = 0x06
    PUBLISH = 0x10  # cooler -> client: current value, on subscribe and on every change
    SET = 0x11  # client -> cooler: write a value
    SUBSCRIBE = 0x12  # client -> cooler: 5-byte frame, no payload
    FRAGMENT = 0x14  # client -> cooler only (long uploads); never seen inbound


class Address(NamedTuple):
    """Four-byte parameter address in wire order."""

    param: int
    instance: int
    subclass: int
    cls: int

    def to_bytes(self) -> bytes:
        return bytes(self)

    @classmethod
    def from_bytes(cls, raw: bytes) -> Address:
        if len(raw) < 4:
            raise DecodeError(f"address needs 4 bytes, got {len(raw)}")
        return cls(raw[0], raw[1], raw[2], raw[3])

    @classmethod
    def parse(cls, text: str) -> Address:
        """Parse ``"04 00 00 1A"`` / ``"0400001a"`` / ``"04:00:00:1a"``."""
        cleaned = text.replace(":", "").replace(" ", "").strip()
        if len(cleaned) != 8:
            raise ValueError(f"address must be 4 hex bytes: {text!r}")
        return cls.from_bytes(bytes.fromhex(cleaned))

    def __str__(self) -> str:  # "04 00 00 1A"
        return " ".join(f"{b:02X}" for b in self)


@dataclass(frozen=True, slots=True)
class Frame:
    """A decoded DDM2 frame."""

    action: int
    address: Address
    payload: bytes = b""

    def encode(self) -> bytes:
        return bytes([self.action]) + self.address.to_bytes() + self.payload

    def encode_line(self) -> bytes:
        """The exact bytes written to the TCP socket."""
        return base64.b64encode(self.encode()) + LINE_TERMINATOR

    @classmethod
    def decode(cls, raw: bytes) -> Frame:
        if len(raw) < 5:
            raise DecodeError(f"frame needs at least 5 bytes, got {len(raw)}")
        action: int = raw[0]
        with contextlib.suppress(ValueError):  # keep unknown actions as plain ints
            action = Action(action)
        return cls(action, Address.from_bytes(raw[1:5]), bytes(raw[5:]))

    @property
    def action_name(self) -> str:
        try:
            return Action(self.action).name
        except ValueError:
            return f"0x{self.action:02X}"

    def __str__(self) -> str:
        payload = self.payload.hex(" ").upper()
        return f"{self.action_name} {self.address}" + (f" {payload}" if payload else "")


class LineDecoder:
    """Turns the inbound byte stream into frames; tolerates split chunks and garbage lines."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self.dropped = 0

    def feed(self, data: bytes) -> list[Frame]:
        self._buf += data
        frames: list[Frame] = []
        while True:
            end = self._buf.find(LINE_TERMINATOR)
            if end < 0:
                break
            line = bytes(self._buf[:end]).strip()
            del self._buf[: end + 1]
            if not line:
                continue
            try:
                raw = base64.b64decode(line, validate=True)
            except (binascii.Error, ValueError):
                self.dropped += 1
                continue
            try:
                frames.append(Frame.decode(raw))
            except DecodeError:
                self.dropped += 1
        return frames

    @property
    def pending(self) -> bytes:
        return bytes(self._buf)


# --------------------------------------------------------------------------- codecs


class Codec(Protocol):
    kind: str

    def decode(self, payload: bytes) -> Any: ...

    def encode(self, value: Any) -> bytes: ...


def _need(payload: bytes, n: int, what: str) -> None:
    if len(payload) < n:
        raise DecodeError(f"{what}: need {n} bytes, got {len(payload)}")


def _i32s(payload: bytes) -> list[int]:
    usable = len(payload) - len(payload) % 4
    return [v[0] for v in struct.iter_unpack("<i", payload[:usable])]


class Int32Codec:
    kind = "int32"

    def decode(self, payload: bytes) -> int:
        _need(payload, 4, "int32")
        return struct.unpack_from("<i", payload)[0]

    def encode(self, value: Any) -> bytes:
        return struct.pack("<i", int(value))


class Int32ArrayCodec:
    kind = "int32[]"

    def decode(self, payload: bytes) -> list[int]:
        return _i32s(payload)

    def encode(self, value: Any) -> bytes:
        return b"".join(struct.pack("<i", int(v)) for v in value)


class BoolCodec:
    kind = "bool"

    def decode(self, payload: bytes) -> bool:
        _need(payload, 4, "bool")
        return struct.unpack_from("<i", payload)[0] != 0

    def encode(self, value: Any) -> bytes:
        return struct.pack("<i", 1 if value else 0)


class BoolArrayCodec:
    kind = "bool[]"

    def decode(self, payload: bytes) -> list[bool]:
        return [v != 0 for v in _i32s(payload)]

    def encode(self, value: Any) -> bytes:
        return b"".join(struct.pack("<i", 1 if v else 0) for v in value)


class MilliCodec:
    """int32 scaled by 1000 (°C, V, A)."""

    kind = "milli"

    def decode(self, payload: bytes) -> float:
        _need(payload, 4, "milli")
        return struct.unpack_from("<i", payload)[0] / 1000

    def encode(self, value: Any) -> bytes:
        return struct.pack("<i", round(float(value) * 1000))


class MilliArrayCodec:
    kind = "milli[]"

    def decode(self, payload: bytes) -> list[float]:
        return [v / 1000 for v in _i32s(payload)]

    def encode(self, value: Any) -> bytes:
        return b"".join(struct.pack("<i", round(float(v) * 1000)) for v in value)


class MilliPairArrayCodec:
    """Pairs of scaled int32 (min, max) per compartment."""

    kind = "milli-pair[]"

    def decode(self, payload: bytes) -> list[tuple[float, float]]:
        values = _i32s(payload)
        return [
            (values[i] / 1000, values[i + 1] / 1000)
            for i in range(0, len(values) - 1, 2)
        ]

    def encode(self, value: Any) -> bytes:
        out = b""
        for lo, hi in value:
            out += struct.pack("<ii", round(float(lo) * 1000), round(float(hi) * 1000))
        return out


class EnumCodec:
    kind = "enum"

    def __init__(self, enum_cls: type[IntEnum]) -> None:
        self.enum_cls = enum_cls

    def decode(self, payload: bytes) -> Any:
        _need(payload, 4, "enum")
        raw = struct.unpack_from("<i", payload)[0]
        try:
            return self.enum_cls(raw)
        except ValueError:
            return raw

    def encode(self, value: Any) -> bytes:
        if isinstance(value, str):
            value = self.enum_cls[value.upper()]
        return struct.pack("<i", int(value))


class StrCodec:
    kind = "str"

    def decode(self, payload: bytes) -> str:
        return payload.split(b"\x00", 1)[0].decode("utf-8", errors="replace").strip()

    def encode(self, value: Any) -> bytes:
        return str(value).encode("utf-8")


class Mac6Codec:
    kind = "mac"

    def decode(self, payload: bytes) -> str:
        _need(payload, 6, "mac")
        return ":".join(f"{b:02x}" for b in payload[:6])

    def encode(self, value: Any) -> bytes:
        return bytes.fromhex(str(value).replace(":", ""))


class U16ArrayCodec:
    kind = "uint16[]"

    def decode(self, payload: bytes) -> list[int]:
        usable = len(payload) - len(payload) % 2
        return [v[0] for v in struct.iter_unpack("<H", payload[:usable])]

    def encode(self, value: Any) -> bytes:
        return b"".join(struct.pack("<H", int(v)) for v in value)


# --------------------------------------------------------------------------- topics


@dataclass(frozen=True, slots=True)
class Topic:
    """One addressable parameter."""

    name: str
    address: Address
    codec: Codec
    description: str
    writable: bool = False
    per_compartment: bool = False
    unit: str = ""


def _mccc(param: int) -> Address:  # class 0x1A "Mobile Cooling Controller Class"
    return Address(param, 0, 0, 0x1A)


def _prod(param: int) -> Address:  # class 0x1C product info
    return Address(param, 0, 0, 0x1C)


_INT32 = Int32Codec()
_BOOL = BoolCodec()
_BOOLS = BoolArrayCodec()
_MILLI = MilliCodec()
_MILLIS = MilliArrayCodec()
_PAIRS = MilliPairArrayCodec()
_STR = StrCodec()

TOPICS: tuple[Topic, ...] = (
    # gateway / config (class 0x00)
    Topic("gw_fwver", Address(0x02, 0, 0, 0), _STR, "Gateway firmware version"),
    Topic("gw_mac", Address(0x11, 0, 0, 0), Mac6Codec(), "Wi-Fi MAC address"),
    Topic("cfg_fwid", Address(0x07, 0, 1, 0), _STR, "Firmware family id (MC1 = CFX5)"),
    # product info (class 0x1C)
    Topic("product_name", _prod(0x01), _STR, "Exact product name (e.g. CFX525)"),
    Topic("cms_sku", _prod(0x03), _STR, "CMS SKU"),
    # cooler (class 0x1A)
    Topic("ptype", _mccc(0x01), EnumCodec(ProductType), "Product type"),
    Topic("nocpt", _mccc(0x02), _INT32, "Number of compartments"),
    Topic(
        "cpow",
        _mccc(0x03),
        _BOOLS,
        "Compartment power",
        writable=True,
        per_compartment=True,
    ),
    Topic(
        "ctemp",
        _mccc(0x04),
        _MILLIS,
        "Measured temperature",
        per_compartment=True,
        unit="°C",
    ),
    Topic(
        "csettemp",
        _mccc(0x05),
        _MILLIS,
        "Set-point temperature",
        writable=True,
        per_compartment=True,
        unit="°C",
    ),
    Topic("acpt", _mccc(0x06), _INT32, "Active compartment on the display"),
    Topic("cdoor", _mccc(0x07), _BOOLS, "Door open", per_compartment=True),
    Topic(
        "ctemprng",
        _mccc(0x08),
        _PAIRS,
        "Allowed set-point range",
        per_compartment=True,
        unit="°C",
    ),
    Topic(
        "crecdrng",
        _mccc(0x09),
        _PAIRS,
        "Recommended range",
        per_compartment=True,
        unit="°C",
    ),
    Topic(
        "ctempofs",
        _mccc(0x0A),
        _MILLIS,
        "Temperature offset",
        per_compartment=True,
        unit="°C",
    ),
    Topic("coolerpow", _mccc(0x0B), _BOOL, "Cooler master power", writable=True),
    Topic("v", _mccc(0x0C), _MILLI, "Supply voltage", unit="V"),
    Topic(
        "batprotlvl",
        _mccc(0x0D),
        EnumCodec(BatteryProtection),
        "Battery protection level",
        writable=True,
    ),
    Topic("comppow", _mccc(0x0E), _BOOL, "Compressor running"),
    Topic("i", _mccc(0x0F), _MILLI, "Current draw", unit="A"),
    Topic("powsrc", _mccc(0x10), EnumCodec(PowerSource), "Power source"),
    Topic("icepow", _mccc(0x11), _BOOL, "Ice maker power", writable=True),
    Topic("errst", _mccc(0x12), U16ArrayCodec(), "Active error codes"),
    Topic("sn", _mccc(0x13), _STR, "Serial number"),
    Topic("sku", _mccc(0x14), _STR, "Article number"),
    Topic("fwver", _mccc(0x15), _STR, "Controller firmware version"),
)

TOPIC_BY_NAME: dict[str, Topic] = {t.name: t for t in TOPICS}
TOPIC_BY_ADDRESS: dict[Address, Topic] = {t.address: t for t in TOPICS}

#: Topics subscribed by default: identity first, then live state (the app's own list plus
#: the three diagnostics the live session confirmed).
SUBSCRIBE_TOPICS: tuple[Topic, ...] = tuple(TOPIC_BY_NAME[n] for n in (
    "gw_fwver", "gw_mac", "cfg_fwid", "sku", "sn", "product_name", "cms_sku",
    "ptype", "nocpt", "cpow", "ctemp", "csettemp", "acpt", "cdoor", "ctemprng",
    "coolerpow", "icepow", "v", "batprotlvl", "comppow", "i", "powsrc", "errst",
))  # fmt: skip

#: The write allow-list. Everything else is refused (default deny).
WRITABLE: frozenset[str] = frozenset(t.name for t in TOPICS if t.writable)

#: Addresses that must never be written, kept as documentation (the allow-list is the guard).
NEVER_WRITE: tuple[tuple[Address, str], ...] = (
    (
        Address(0x14, 0, 0, 0),
        "gw.cupd command word: 11223344 = Restart, 19181716 = Factory reset",
    ),
    (Address(0x03, 0, 0, 0), "gw.ota firmware update trigger"),
    (
        Address(0x06, 0, 0, 0),
        "gw.awssc AWS certificate slot (06-09 are AWS, 0A-0D are OTA certs)",
    ),
    (Address(0x01, 0, 1, 2), "wifi.scan / wifi class (subclass 1, class 2)"),
    (Address(0x01, 0, 2, 2), "wfwl Wi-Fi whitelist (ssid/pw/del)"),
    (Address(0x01, 0, 3, 2), "bt / btwl Bluetooth classes"),
    (_mccc(0x01), "ptype factory product type"),
    (_mccc(0x02), "nocpt factory compartment count"),
    (_mccc(0x08), "ctemprng factory range"),
    (_mccc(0x09), "crecdrng factory range"),
    (_mccc(0x0A), "ctempofs calibration offset"),
    (_mccc(0x13), "sn factory serial"),
    (_mccc(0x14), "sku factory article"),
    (_mccc(0x15), "fwver firmware string"),
)


def topic(name_or_address: str | Address | Topic) -> Topic:
    """Resolve a topic by name, address or ``"04 00 00 1A"`` text."""
    if isinstance(name_or_address, Topic):
        return name_or_address
    if isinstance(name_or_address, Address):
        try:
            return TOPIC_BY_ADDRESS[name_or_address]
        except KeyError:
            raise KeyError(f"unknown topic address {name_or_address}") from None
    if name_or_address in TOPIC_BY_NAME:
        return TOPIC_BY_NAME[name_or_address]
    try:
        return TOPIC_BY_ADDRESS[Address.parse(name_or_address)]
    except (ValueError, KeyError):
        raise KeyError(f"unknown topic {name_or_address!r}") from None


def assert_writable(name_or_address: str | Address | Topic) -> Topic:
    """Return the topic if it may be written, else raise :class:`NotWritable`."""
    t = topic(name_or_address)
    if t.name not in WRITABLE:
        raise NotWritable(f"{t.name} ({t.address}) is not writable")
    return t


def subscribe_frame(t: Topic | str | Address) -> Frame:
    return Frame(Action.SUBSCRIBE, topic(t).address)


def publish_frame(t: Topic | str | Address, value: Any) -> Frame:
    """Build a PUBLISH frame (used by tests and the fake cooler)."""
    tp = topic(t)
    return Frame(Action.PUBLISH, tp.address, tp.codec.encode(value))
