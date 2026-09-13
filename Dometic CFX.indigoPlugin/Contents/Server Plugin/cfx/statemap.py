"""Pure mapping from :class:`ddmp.CoolerState` to Indigo device states, plus unit conversion.

The library works in °C; this is the only place values are converted for display.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ddmp.models import BatteryProtection, CoolerState, PowerSource

#: Which topic the thermostat mode (Cool / Off) drives. ``cpow`` (compartment power) matches
#: the community BLE climate mapping and makes a second-compartment device identical; switch
#: to ``coolerpow`` if the supervised write test shows the front-panel button toggles the
#: master topic instead.
HVAC_POWER_TOPIC = "cpow"

#: Marker values for the built-in ``hvacOperationMode`` state; plugin.py maps them to
#: ``indigo.kHvacMode.Cool`` / ``indigo.kHvacMode.Off``.
HVAC_COOL = "cool"
HVAC_OFF = "off"


def c_to_display(celsius: float, unit: str) -> float:
    return celsius * 9 / 5 + 32 if unit.upper() == "F" else celsius


def display_to_c(value: float, unit: str) -> float:
    return (value - 32) * 5 / 9 if unit.upper() == "F" else value


def unit_symbol(unit: str) -> str:
    return "°F" if unit.upper() == "F" else "°C"


@dataclass(frozen=True)
class StateValue:
    value: Any
    ui_value: str | None = None
    decimal_places: int | None = None

    def as_update(self, key: str) -> dict[str, Any]:
        item: dict[str, Any] = {"key": key, "value": self.value}
        if self.ui_value is not None:
            item["uiValue"] = self.ui_value
        if self.decimal_places is not None:
            item["decimalPlaces"] = self.decimal_places
        return item


def _temp(celsius: float, unit: str) -> StateValue:
    shown = round(c_to_display(celsius, unit), 1)
    return StateValue(shown, f"{shown:.1f} {unit_symbol(unit)}", 1)


def _enum_name(value: Any, enum_cls: type) -> str:
    if isinstance(value, enum_cls):
        return value.name.title()
    return f"Unknown ({value})"


def to_indigo_states(
    state: CoolerState | None,
    *,
    compartment: int,
    unit: str,
    connected: bool,
    now_text: str | None = None,
) -> dict[str, StateValue]:
    """Indigo state key -> value for one device. Unknown values are omitted."""
    out: dict[str, StateValue] = {"connected": StateValue(connected)}
    if state is None:
        return out
    comp = state.compartment(compartment)
    if comp is not None:
        if comp.temperature_c is not None:
            out["temperatureInput1"] = _temp(comp.temperature_c, unit)
        if comp.setpoint_c is not None:
            out["setpointCool"] = _temp(comp.setpoint_c, unit)
        if comp.powered is not None:
            out["compartmentPowerOn"] = StateValue(comp.powered)
        if comp.door_open is not None:
            out["doorOpen"] = StateValue(comp.door_open)
        if comp.range_c is not None:
            lo, hi = comp.range_c
            out["setpointMin"] = StateValue(round(c_to_display(lo, unit), 1), None, 1)
            out["setpointMax"] = StateValue(round(c_to_display(hi, unit), 1), None, 1)
    power_topic_value = (
        state.cooler_on
        if HVAC_POWER_TOPIC == "coolerpow"
        else (comp.powered if comp else None)
    )
    if power_topic_value is not None:
        out["hvacOperationMode"] = StateValue(
            HVAC_COOL if power_topic_value else HVAC_OFF
        )
    if state.compressor_on is not None:
        out["hvacCoolerIsOn"] = StateValue(state.compressor_on)
        out["compressorOn"] = StateValue(state.compressor_on)
    if state.cooler_on is not None:
        out["coolerPowerOn"] = StateValue(state.cooler_on)
    if state.voltage_v is not None:
        out["voltage"] = StateValue(
            round(state.voltage_v, 2), f"{state.voltage_v:.1f} V", 2
        )
    if state.current_a is not None:
        out["current"] = StateValue(
            round(state.current_a, 2), f"{state.current_a:.2f} A", 2
        )
    if state.power_source is not None:
        out["powerSource"] = StateValue(
            _enum_name(state.power_source, PowerSource).upper()
        )
    if state.battery_protection is not None:
        out["batteryProtection"] = StateValue(
            _enum_name(state.battery_protection, BatteryProtection)
        )
    if state.ice_maker_on is not None:
        out["iceMakerOn"] = StateValue(state.ice_maker_on)
    codes = state.error_codes
    out["errorCodes"] = StateValue(",".join(str(c) for c in codes))
    out["errorText"] = StateValue(state.error_text)
    out["hasError"] = StateValue(bool(codes))
    if state.product_name:
        out["productName"] = StateValue(state.product_name)
    if state.serial:
        out["serialNumber"] = StateValue(state.serial)
    if state.firmware:
        out["firmwareVersion"] = StateValue(state.firmware)
    if now_text:
        out["lastUpdate"] = StateValue(now_text)
    return out


def diff(
    prev: dict[str, StateValue], new: dict[str, StateValue]
) -> list[dict[str, Any]]:
    """Only the changed entries, ready for ``dev.updateStatesOnServer``."""
    return [sv.as_update(key) for key, sv in new.items() if prev.get(key) != sv]
