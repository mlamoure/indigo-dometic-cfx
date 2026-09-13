"""Pytest configuration: stub the `indigo` module before any plugin import.

The plugin runs inside Indigo's embedded Python where `indigo` is injected by the host.
Tests stub just the surface the plugin touches (same approach as the Roomie plugin).
"""

from __future__ import annotations

import logging
import os
import sys
import types

indigo_stub = types.SimpleNamespace()


class Devices(dict):
    def __iter__(self):
        return iter(self.values())

    def iter(self, _filter=""):
        return iter(list(self.values()))


class Device:
    _next_id = 100

    def __init__(
        self, dev_id=None, name="", deviceTypeId="cfxCooler", pluginProps=None
    ):
        if dev_id is None:
            Device._next_id += 1
            dev_id = Device._next_id
        self.id = dev_id
        self.name = name or f"device-{dev_id}"
        self.deviceTypeId = deviceTypeId
        self.pluginProps = pluginProps or {}
        self.states = {}
        self.errorState = None
        self.state_updates = []
        self.error_state_calls = []
        self.state_list_changed = 0

    @property
    def coolSetpoint(self):
        return float(self.states.get("setpointCool", 0.0))

    def updateStatesOnServer(self, state_list):
        self.state_updates.append(state_list)
        for item in state_list:
            self.states[item["key"]] = item["value"]

    def updateStateOnServer(self, key, value, uiValue=None, decimalPlaces=None):
        self.updateStatesOnServer([{"key": key, "value": value}])

    def setErrorStateOnServer(self, message):
        self.errorState = message
        self.error_state_calls.append(message)

    def stateListOrDisplayStateIdChanged(self):
        self.state_list_changed += 1

    def replacePluginPropsOnServer(self, props):
        self.pluginProps = props


class IndigoDict(dict):
    pass


class _DummyHandler(logging.Handler):
    def __init__(self, baseFilename="/tmp/Logs/plugin.log"):
        super().__init__()
        self.baseFilename = baseFilename

    def emit(self, record):
        pass


class _StopThread(Exception):
    pass


class PluginBase:
    StopThread = _StopThread

    def __init__(self, plugin_id, plugin_display_name, plugin_version, plugin_prefs):
        self.pluginId = plugin_id
        self.pluginDisplayName = plugin_display_name
        self.pluginVersion = plugin_version
        self.pluginPrefs = plugin_prefs
        self.logger = logging.getLogger("Plugin")
        self.indigo_log_handler = _DummyHandler()
        self.plugin_file_handler = _DummyHandler()

    def sleep(self, seconds):
        raise _StopThread()

    def savePluginPrefs(self):
        pass


indigo_stub.devices = Devices()
indigo_stub.Device = Device
indigo_stub.Dict = IndigoDict
indigo_stub.PluginBase = PluginBase
indigo_stub.kHvacMode = types.SimpleNamespace(
    Cool="Cool", Off="Off", Heat="Heat", HeatCool="HeatCool"
)
indigo_stub.kThermostatAction = types.SimpleNamespace(
    SetCoolSetpoint="SetCoolSetpoint",
    IncreaseCoolSetpoint="IncreaseCoolSetpoint",
    DecreaseCoolSetpoint="DecreaseCoolSetpoint",
    SetHeatSetpoint="SetHeatSetpoint",
    SetHvacMode="SetHvacMode",
    SetFanMode="SetFanMode",
    RequestStatusAll="RequestStatusAll",
    RequestMode="RequestMode",
    RequestEquipmentState="RequestEquipmentState",
    RequestTemperatures="RequestTemperatures",
    RequestSetpoints="RequestSetpoints",
    RequestHumidities="RequestHumidities",
    RequestDeadbands="RequestDeadbands",
)
indigo_stub.kUniversalAction = types.SimpleNamespace(
    RequestStatus="RequestStatus", Beep="Beep"
)
indigo_stub.server = types.SimpleNamespace(broadcastToSubscribers=lambda *a: None)

sys.modules["indigo"] = indigo_stub

SERVER_PLUGIN = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        os.pardir,
        "Dometic CFX.indigoPlugin",
        "Contents",
        "Server Plugin",
    )
)
sys.path.insert(0, SERVER_PLUGIN)

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def fake_indigo():
    indigo_stub.devices.clear()
    yield indigo_stub


class ThermostatAction:
    def __init__(self, thermostatAction, actionValue=0.0, actionMode=None, deviceId=0):
        self.thermostatAction = thermostatAction
        self.actionValue = actionValue
        self.actionMode = actionMode
        self.deviceId = deviceId


class PluginAction:
    def __init__(self, props=None, deviceAction=None):
        self.props = props or {}
        self.deviceAction = deviceAction


@pytest.fixture
def thermostat_action():
    return ThermostatAction


@pytest.fixture
def plugin_action():
    return PluginAction
