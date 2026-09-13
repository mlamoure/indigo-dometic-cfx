"""Find coolers on the LAN: the ``DDMD`` UDP probe and a dependency-free one-shot mDNS query.

* ``DDMD``: four ASCII bytes sent to UDP 13143 (unicast, or broadcast on the local subnet)
  are answered with one JSON datagram such as
  ``{"version":2,"pid":4,"id":"14335c34f12c","name":"MC1_34f12c","f":0,"sku":"97000050753"}``.
* mDNS: the cooler advertises ``Dometic CFX5._ddmp._tcp.local`` with an SRV record pointing at
  ``MC1_<mac tail>.local:13143`` and TXT ``fwversion``/``sku``/``board``. A multicast (QM)
  query is used so that an mDNS repeater between VLANs can reflect the answer.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import struct
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from .protocol import DEFAULT_PORT

DDMD_PROBE = b"DDMD"
MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353
SERVICE_TYPE = "_ddmp._tcp.local"


def normalize_id(value: str) -> str:
    """``"14:33:5C:34:F1:2C"`` / ``"14335c34f12c"`` -> ``"14335c34f12c"``."""
    return value.replace(":", "").replace("-", "").strip().lower()


@dataclass(frozen=True, slots=True)
class DdmdReply:
    cooler_id: str
    name: str
    sku: str
    version: int
    pid: int
    f: int
    host: str
    port: int

    @classmethod
    def parse(cls, payload: bytes, host: str, port: int) -> DdmdReply | None:
        try:
            obj = json.loads(payload.decode("utf-8", errors="replace"))
        except ValueError:
            return None
        if not isinstance(obj, dict) or "id" not in obj:
            return None
        return cls(
            cooler_id=normalize_id(str(obj.get("id", ""))),
            name=str(obj.get("name", "")),
            sku=str(obj.get("sku", "")).strip(),
            version=int(obj.get("version", 0) or 0),
            pid=int(obj.get("pid", 0) or 0),
            f=int(obj.get("f", 0) or 0),
            host=host,
            port=port,
        )


def probe_ddmd(
    host: str = "255.255.255.255",
    port: int = DEFAULT_PORT,
    *,
    timeout: float = 2.0,
    first_only: bool = False,
) -> list[DdmdReply]:
    """Send ``DDMD`` and collect every JSON reply until ``timeout`` (or the first reply)."""
    replies: list[DdmdReply] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if host.endswith(".255") or host == "255.255.255.255":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.25)
        sock.sendto(DDMD_PROBE, (host, port))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(4096)
            except TimeoutError:
                continue
            except OSError:
                break
            if data == DDMD_PROBE:
                continue  # our own broadcast echo
            reply = DdmdReply.parse(data, addr[0], addr[1])
            if reply is not None and reply not in replies:
                replies.append(reply)
                if first_only:
                    break
    finally:
        sock.close()
    return replies


# --------------------------------------------------------------------------- mDNS


@dataclass(frozen=True, slots=True)
class MdnsService:
    instance: str
    hostname: str
    port: int
    addresses: tuple[str, ...]
    txt: dict[str, str] = field(default_factory=dict)


def _encode_name(name: str) -> bytes:
    out = b""
    for label in name.strip(".").split("."):
        raw = label.encode("utf-8")
        out += bytes([len(raw)]) + raw
    return out + b"\x00"


def _read_name(data: bytes, offset: int) -> tuple[str, int]:
    labels: list[str] = []
    jumped = False
    end = offset
    hops = 0
    while True:
        if offset >= len(data):
            raise ValueError("truncated name")
        length = data[offset]
        if length == 0:
            offset += 1
            break
        if length & 0xC0 == 0xC0:
            pointer = struct.unpack_from(">H", data, offset)[0] & 0x3FFF
            if not jumped:
                end = offset + 2
            jumped = True
            offset = pointer
            hops += 1
            if hops > 64:
                raise ValueError("name compression loop")
            continue
        labels.append(
            data[offset + 1 : offset + 1 + length].decode("utf-8", errors="replace")
        )
        offset += 1 + length
    if not jumped:
        end = offset
    return ".".join(labels), end


def _parse_txt(rdata: bytes) -> dict[str, str]:
    txt: dict[str, str] = {}
    i = 0
    while i < len(rdata):
        length = rdata[i]
        item = rdata[i + 1 : i + 1 + length].decode("utf-8", errors="replace")
        i += 1 + length
        if not item:
            continue
        key, _, value = item.partition("=")
        txt[key] = value.strip()
    return txt


def build_query(
    service: str = SERVICE_TYPE, *, unicast_response: bool = False
) -> bytes:
    qclass = 1 | (0x8000 if unicast_response else 0)
    return (
        struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0)
        + _encode_name(service)
        + struct.pack(">HH", 12, qclass)
    )


def parse_response(data: bytes) -> list[tuple[str, int, bytes, int]]:
    """Return ``[(name, rtype, rdata, rdata_offset)]`` for every record in a DNS message."""
    records: list[tuple[str, int, bytes, int]] = []
    if len(data) < 12:
        return records
    qd, an, ns, ar = struct.unpack_from(">HHHH", data, 4)
    offset = 12
    try:
        for _ in range(qd):
            _, offset = _read_name(data, offset)
            offset += 4
        for _ in range(an + ns + ar):
            name, offset = _read_name(data, offset)
            rtype, _rclass, _ttl, rdlen = struct.unpack_from(">HHIH", data, offset)
            offset += 10
            records.append((name, rtype, data[offset : offset + rdlen], offset))
            offset += rdlen
    except (ValueError, struct.error):
        pass
    return records


class _MdnsCollector:
    def __init__(self, service: str) -> None:
        self.service = service.strip(".").lower()
        self.instances: set[str] = set()
        self.srv: dict[str, tuple[int, str]] = {}
        self.txt: dict[str, dict[str, str]] = {}
        self.addresses: dict[str, list[str]] = {}

    def add(self, data: bytes) -> None:
        for name, rtype, rdata, rd_off in parse_response(data):
            key = name.lower()
            if rtype == 12 and key == self.service:  # PTR
                try:
                    instance, _ = _read_name(data, rd_off)
                except ValueError:
                    continue
                self.instances.add(instance)
            elif rtype == 33 and len(rdata) >= 6:  # SRV
                port = struct.unpack_from(">H", rdata, 4)[0]
                try:
                    target, _ = _read_name(data, rd_off + 6)
                except ValueError:
                    continue
                self.srv[name] = (port, target)
            elif rtype == 16:  # TXT
                self.txt[name] = _parse_txt(rdata)
            elif rtype == 1 and len(rdata) == 4:  # A
                self.addresses.setdefault(key, []).append(socket.inet_ntoa(rdata))

    def services(self) -> list[MdnsService]:
        out: list[MdnsService] = []
        for instance in sorted(self.instances):
            srv = self._lookup(self.srv, instance)
            if srv is None:
                continue
            port, target = srv
            addrs = tuple(dict.fromkeys(self.addresses.get(target.lower(), [])))
            out.append(
                MdnsService(
                    instance=instance,
                    hostname=target,
                    port=port,
                    addresses=addrs,
                    txt=self._lookup(self.txt, instance) or {},
                )
            )
        return out

    @staticmethod
    def _lookup(table: dict, name: str):
        if name in table:
            return table[name]
        lowered = name.lower()
        for key, value in table.items():
            if key.lower() == lowered:
                return value
        return None


def query_mdns(
    service: str = SERVICE_TYPE,
    *,
    timeout: float = 2.0,
    interface_ip: str | None = None,
) -> list[MdnsService]:
    """One-shot multicast DNS-SD browse using only the standard library."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    collector = _MdnsCollector(service)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        unicast = False
        try:
            sock.bind(("", MDNS_PORT))
        except OSError:
            # Port 5353 not available: fall back to an ephemeral port and ask for a unicast
            # reply (only works on the same layer-2 network).
            sock.bind(("", 0))
            unicast = True
        iface = (
            socket.inet_aton(interface_ip)
            if interface_ip
            else socket.inet_aton("0.0.0.0")
        )
        with contextlib.suppress(OSError):
            sock.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_ADD_MEMBERSHIP,
                socket.inet_aton(MDNS_GROUP) + iface,
            )
        if interface_ip:
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, iface)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.settimeout(0.25)
        query = build_query(service, unicast_response=unicast)
        deadline = time.monotonic() + timeout
        resend_at = time.monotonic() + timeout / 2
        sock.sendto(query, (MDNS_GROUP, MDNS_PORT))
        while time.monotonic() < deadline:
            if time.monotonic() >= resend_at:
                resend_at = float("inf")
                with contextlib.suppress(OSError):
                    sock.sendto(query, (MDNS_GROUP, MDNS_PORT))
            try:
                data, _ = sock.recvfrom(9000)
            except TimeoutError:
                continue
            except OSError:
                break
            collector.add(data)
    finally:
        sock.close()
    return collector.services()


# --------------------------------------------------------------------------- merged view


@dataclass(frozen=True, slots=True)
class Discovered:
    cooler_id: str  # 12 hex chars (MAC) when known, else ""
    name: str  # "MC1_34f12c"
    hostname: str | None  # "MC1_34f12c.local"
    ip: str | None
    port: int
    firmware: str | None = None
    sku: str | None = None
    instance: str | None = None  # "Dometic CFX5"
    sources: frozenset[str] = frozenset()

    @property
    def short_id(self) -> str:
        """The last three MAC bytes, which the cooler uses in its hostname."""
        if self.cooler_id:
            return self.cooler_id[-6:]
        return self.name.rpartition("_")[2].lower()

    def matches(self, cooler_id: str) -> bool:
        wanted = normalize_id(cooler_id)
        if self.cooler_id and wanted == self.cooler_id:
            return True
        return bool(wanted) and wanted[-6:] == self.short_id


def discover(
    *,
    timeout: float = 3.0,
    mdns: bool = True,
    broadcast: bool = True,
    hosts: Sequence[str] = (),
    probe_timeout: float = 1.0,
) -> list[Discovered]:
    """Browse mDNS and/or broadcast ``DDMD``, then confirm each hit with a unicast probe."""
    found: dict[str, Discovered] = {}

    def remember(d: Discovered) -> None:
        key = (
            d.short_id or f"name:{d.name.lower()}"
        )  # MAC tail is common to both sources
        prev = found.get(key)
        if prev is None:
            found[key] = d
            return
        found[key] = Discovered(
            cooler_id=prev.cooler_id or d.cooler_id,
            name=prev.name or d.name,
            hostname=prev.hostname or d.hostname,
            ip=prev.ip or d.ip,
            port=prev.port or d.port,
            firmware=prev.firmware or d.firmware,
            sku=prev.sku or d.sku,
            instance=prev.instance or d.instance,
            sources=prev.sources | d.sources,
        )

    def from_reply(r: DdmdReply, source: str) -> Discovered:
        return Discovered(
            cooler_id=r.cooler_id,
            name=r.name,
            hostname=f"{r.name}.local" if r.name else None,
            ip=r.host,
            port=r.port,
            sku=r.sku or None,
            sources=frozenset({source}),
        )

    if mdns:
        for svc in query_mdns(timeout=timeout):
            ip = svc.addresses[0] if svc.addresses else None
            name = svc.hostname.split(".")[0]
            entry = Discovered(
                cooler_id="",
                name=name,
                hostname=svc.hostname,
                ip=ip,
                port=svc.port,
                firmware=svc.txt.get("fwversion"),
                sku=svc.txt.get("sku"),
                instance=svc.instance,
                sources=frozenset({"mdns"}),
            )
            if ip:
                for r in probe_ddmd(
                    ip, svc.port, timeout=probe_timeout, first_only=True
                ):
                    remember(from_reply(r, "probe"))
            remember(entry)
    if broadcast:
        for r in probe_ddmd(timeout=min(timeout, 2.0)):
            remember(from_reply(r, "broadcast"))
    for host in hosts:
        for r in probe_ddmd(host, timeout=probe_timeout, first_only=True):
            remember(from_reply(r, "probe"))
    return sorted(found.values(), key=lambda d: (d.name, d.ip or ""))


def find_cooler(
    cooler_id: str, *, timeout: float = 3.0, hosts: Iterable[str] = ()
) -> Discovered | None:
    """Locate one cooler by id (MAC); ``hosts`` are extra addresses to probe directly."""
    for d in discover(timeout=timeout, hosts=tuple(hosts)):
        if d.matches(cooler_id):
            return d
    return None


async def async_discover(**kwargs) -> list[Discovered]:
    return await asyncio.to_thread(discover, **kwargs)


async def async_find_cooler(cooler_id: str, **kwargs) -> Discovered | None:
    return await asyncio.to_thread(find_cooler, cooler_id, **kwargs)
