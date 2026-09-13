from __future__ import annotations

from cfx.statemap import (
    HVAC_COOL,
    HVAC_OFF,
    StateValue,
    c_to_display,
    diff,
    display_to_c,
    to_indigo_states,
)
from ddmp.models import BatteryProtection, CoolerState, PowerSource
from ddmp.session import Session

from .fake_client import capture_stream


def _state() -> CoolerState:
    s = Session()
    s.feed(capture_stream())
    return s.state()


def test_unit_conversion_roundtrip():
    assert c_to_display(0.0, "F") == 32.0
    assert c_to_display(-18.0, "C") == -18.0
    assert round(display_to_c(35.0, "F"), 3) == 1.667
    assert display_to_c(4.0, "C") == 4.0


def test_states_in_fahrenheit_from_live_capture():
    states = to_indigo_states(
        _state(), compartment=0, unit="F", connected=True, now_text="t"
    )
    assert states["temperatureInput1"] == StateValue(35.6, "35.6 °F", 1)
    assert states["setpointCool"] == StateValue(33.8, "33.8 °F", 1)
    assert states["hvacOperationMode"].value == HVAC_COOL
    assert states["hvacCoolerIsOn"].value is True
    assert states["compressorOn"].value is True
    assert states["coolerPowerOn"].value is True
    assert states["compartmentPowerOn"].value is True
    assert states["doorOpen"].value is False
    assert states["voltage"] == StateValue(13.5, "13.5 V", 2)
    assert states["current"] == StateValue(2.1, "2.10 A", 2)
    assert states["powerSource"].value == "DC"
    assert states["batteryProtection"].value == "Medium"
    assert states["errorCodes"].value == "" and states["hasError"].value is False
    assert states["setpointMin"].value == -7.6 and states["setpointMax"].value == 68.0
    assert states["productName"].value == "CFX525"
    assert states["serialNumber"].value == "52402647"
    assert states["firmwareVersion"].value == "1.0.2"
    assert states["connected"].value is True
    assert states["lastUpdate"].value == "t"
    assert "iceMakerOn" not in states  # never published in the capture


def test_states_in_celsius_and_off_mode():
    st = _state()
    st.compartments[0].powered = False
    st.error_codes = (23, 16)
    st.power_source = 7
    states = to_indigo_states(st, compartment=0, unit="C", connected=True)
    assert states["temperatureInput1"] == StateValue(2.0, "2.0 °C", 1)
    assert states["hvacOperationMode"].value == HVAC_OFF
    assert states["errorCodes"].value == "23,16"
    assert "Door has been opened too long" in states["errorText"].value
    assert states["hasError"].value is True
    assert states["powerSource"].value == "UNKNOWN (7)"
    assert isinstance(st.battery_protection, BatteryProtection)
    assert PowerSource.DC.name == "DC"


def test_unknown_state_and_missing_compartment():
    assert to_indigo_states(None, compartment=0, unit="F", connected=False) == {
        "connected": StateValue(False)
    }
    states = to_indigo_states(_state(), compartment=1, unit="F", connected=True)
    assert "temperatureInput1" not in states and "hvacOperationMode" not in states
    assert states["voltage"].value == 13.5


def test_diff_only_emits_changes():
    a = {"x": StateValue(1), "y": StateValue(2.0, "2.0 V", 1)}
    b = {"x": StateValue(1), "y": StateValue(2.5, "2.5 V", 1), "z": StateValue("new")}
    updates = diff(a, b)
    assert updates == [
        {"key": "y", "value": 2.5, "uiValue": "2.5 V", "decimalPlaces": 1},
        {"key": "z", "value": "new"},
    ]
    assert diff(b, b) == []
