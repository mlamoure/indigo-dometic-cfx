"""Device configuration parsed from Indigo pluginProps. Never raises on bad input."""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Mapping
from dataclasses import dataclass, replace

DEFAULT_PORT = 13143
_HEX12 = re.compile(r"^[0-9a-f]{12}$")


def normalize_id(value: str) -> str:
    return (value or "").replace(":", "").replace("-", "").strip().lower()


@dataclass(frozen=True)
class CoolerConfig:
    device_id: int
    host: str
    port: int = DEFAULT_PORT
    cooler_id: str = ""  # 12 hex chars (MAC) or "" when unknown
    unit: str = "F"
    compartment: int = 0
    connect_timeout: float = 5.0
    name: str = ""

    @classmethod
    def from_props(
        cls,
        device_id: int,
        props: Mapping[str, object],
        *,
        connect_timeout: float = 5.0,
    ) -> CoolerConfig:
        def text(key: str, default: str = "") -> str:
            value = props.get(key, default)
            return str(value).strip() if value is not None else default

        try:
            port = int(text("port", str(DEFAULT_PORT)) or DEFAULT_PORT)
        except ValueError:
            port = DEFAULT_PORT
        try:
            compartment = int(text("compartment", "0") or 0)
        except ValueError:
            compartment = 0
        unit = text("unit", "F").upper()
        if unit not in ("F", "C"):
            unit = "F"
        return cls(
            device_id=device_id,
            host=text("host"),
            port=port if 0 < port < 65536 else DEFAULT_PORT,
            cooler_id=normalize_id(text("coolerId")),
            unit=unit,
            compartment=max(compartment, 0),
            connect_timeout=connect_timeout,
            name=text("coolerName"),
        )

    def with_host(self, host: str) -> CoolerConfig:
        return replace(self, host=host)


def looks_like_host(value: str) -> bool:
    """IP address or a resolvable hostname (a short DNS lookup, no connection)."""
    value = value.strip()
    if not value:
        return False
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        pass
    try:
        socket.getaddrinfo(value, None, proto=socket.IPPROTO_TCP)
        return True
    except OSError:
        return False


def validate_device_props(
    values: Mapping[str, object],
) -> tuple[dict[str, str], dict[str, str]]:
    """Return ``(errors, cleaned)`` for the device ConfigUI values."""
    errors: dict[str, str] = {}
    cleaned: dict[str, str] = {}

    host = str(values.get("host", "") or "").strip()
    if not host:
        errors["host"] = "Enter the cooler's IP address or hostname (or discover it)."
    elif not looks_like_host(host):
        errors["host"] = f"'{host}' is not an IP address or a resolvable hostname."
    cleaned["host"] = host

    port_text = str(values.get("port", "") or "").strip() or str(DEFAULT_PORT)
    try:
        port = int(port_text)
        if not 0 < port < 65536:
            raise ValueError
    except ValueError:
        errors["port"] = "Port must be a number between 1 and 65535."
        port = DEFAULT_PORT
    cleaned["port"] = str(port)

    cooler_id = normalize_id(str(values.get("coolerId", "") or ""))
    if cooler_id in ("manual", "none"):
        cooler_id = ""
    if cooler_id and not _HEX12.match(cooler_id):
        errors["coolerId"] = (
            "Cooler id must be the 12-hex-digit MAC address (or blank)."
        )
    cleaned["coolerId"] = cooler_id

    compartment = str(values.get("compartment", "0") or "0").strip()
    if compartment not in ("0", "1"):
        errors["compartment"] = "Compartment must be 0 or 1."
    cleaned["compartment"] = compartment

    unit = str(values.get("unit", "F") or "F").strip().upper()
    cleaned["unit"] = unit if unit in ("F", "C") else "F"
    return errors, cleaned


def validate_prefs(
    values: Mapping[str, object],
) -> tuple[dict[str, str], dict[str, str]]:
    errors: dict[str, str] = {}
    cleaned: dict[str, str] = {}
    raw = str(values.get("connectTimeout", "5") or "5").strip()
    try:
        timeout = float(raw)
        if not 2 <= timeout <= 30:
            raise ValueError
    except ValueError:
        errors["connectTimeout"] = "Connect timeout must be between 2 and 30 seconds."
        timeout = 5.0
    cleaned["connectTimeout"] = f"{timeout:g}"
    return errors, cleaned
