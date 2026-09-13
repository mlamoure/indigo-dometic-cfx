"""Enumerations and the decoded cooler state. No protocol or I/O code lives here."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class BatteryProtection(IntEnum):
    LOW = 0
    MEDIUM = 1
    HIGH = 2


class PowerSource(IntEnum):
    AC = 0
    DC = 1


class ProductType(IntEnum):
    UNCONFIGURED = 0
    SINGLE_ZONE = 1
    SINGLE_ZONE_ICEMAKER = 2
    DUAL_ZONE = 3


#: Error codes published on ``errst`` (uint16 each), with the app's own wording.
ERROR_TEXT: dict[int, str] = {
    16: "DC input undervoltage",
    17: "DC input overvoltage",
    23: "Door has been opened too long",
    26: "Faulty solenoid valve",
    27: "Temperature out of range",
    512: "Compressor fan over current",
    513: "Compressor didn't start",
    514: "Compressor speed low",
    515: "Compressor over temperature",
    516: "Compressor fan speed low",
    517: "NTC sensor open circuit (compartment 1)",
    518: "NTC sensor short circuit (compartment 1)",
    519: "NTC sensor open circuit (compartment 2)",
    520: "NTC sensor short circuit (compartment 2)",
    521: "NTC sensor open circuit (compartment 3)",
    522: "NTC sensor short circuit (compartment 3)",
    523: "Temperature out of range",
    524: "Controller over temperature",
}


def error_text(code: int) -> str:
    return ERROR_TEXT.get(code, f"Unknown error {code}")


@dataclass(slots=True)
class Compartment:
    index: int
    powered: bool | None = None
    temperature_c: float | None = None
    setpoint_c: float | None = None
    door_open: bool | None = None
    range_c: tuple[float, float] | None = None
    recommended_range_c: tuple[float, float] | None = None
    offset_c: float | None = None


@dataclass(slots=True)
class CoolerState:
    """Latest published values. Every field is ``None`` until the cooler has published it."""

    product_name: str | None = None
    product_type: ProductType | int | None = None
    compartment_count: int | None = None
    serial: str | None = None
    article: str | None = None
    cms_sku: str | None = None
    firmware: str | None = None
    firmware_id: str | None = None
    controller_firmware: str | None = None
    mac: str | None = None
    compartments: list[Compartment] = field(default_factory=list)
    active_compartment: int | None = None
    cooler_on: bool | None = None
    compressor_on: bool | None = None
    voltage_v: float | None = None
    current_a: float | None = None
    power_source: PowerSource | int | None = None
    battery_protection: BatteryProtection | int | None = None
    ice_maker_on: bool | None = None
    error_codes: tuple[int, ...] = ()

    @property
    def error_text(self) -> str:
        return "; ".join(error_text(c) for c in self.error_codes)

    def compartment(self, index: int = 0) -> Compartment | None:
        return self.compartments[index] if index < len(self.compartments) else None

    @classmethod
    def from_values(cls, values: Mapping[str, Any]) -> CoolerState:
        """Build a state from ``{topic name: decoded value}``."""
        s = cls()
        s.product_name = values.get("product_name")
        s.product_type = values.get("ptype")
        s.compartment_count = values.get("nocpt")
        s.serial = values.get("sn")
        s.article = values.get("sku")
        s.cms_sku = values.get("cms_sku")
        s.firmware = values.get("gw_fwver")
        s.firmware_id = values.get("cfg_fwid")
        s.controller_firmware = values.get("fwver")
        s.mac = values.get("gw_mac")
        s.active_compartment = values.get("acpt")
        s.cooler_on = values.get("coolerpow")
        s.compressor_on = values.get("comppow")
        s.voltage_v = values.get("v")
        s.current_a = values.get("i")
        s.power_source = values.get("powsrc")
        s.battery_protection = values.get("batprotlvl")
        s.ice_maker_on = values.get("icepow")
        errs = values.get("errst")
        s.error_codes = tuple(errs) if errs else ()

        arrays = {
            "powered": values.get("cpow") or [],
            "temperature_c": values.get("ctemp") or [],
            "setpoint_c": values.get("csettemp") or [],
            "door_open": values.get("cdoor") or [],
            "range_c": values.get("ctemprng") or [],
            "recommended_range_c": values.get("crecdrng") or [],
            "offset_c": values.get("ctempofs") or [],
        }
        count = max([len(v) for v in arrays.values()] + [s.compartment_count or 0])
        s.compartments = [Compartment(i) for i in range(count)]
        for attr, arr in arrays.items():
            for i, v in enumerate(arr):
                if i < count:
                    setattr(s.compartments[i], attr, v)
        return s
