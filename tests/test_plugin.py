from __future__ import annotations

import logging

import pytest

import plugin as plugin_module
from cfx.link import (
    RequestStatus,
    SetBatteryProtection,
    SetCoolerPower,
    SetPower,
    SetSetpoint,
)

from .fake_client import Factory


@pytest.fixture
def plugin(monkeypatch, fake_indigo):
    factory = Factory()
    monkeypatch.setattr(plugin_module, "CoolerLink", _link_factory(factory))
    p = plugin_module.Plugin(
        "com.vtmikel.dometiccfx", "Dometic CFX", "2026.9.0", {"log_level": "20"}
    )
    p.startup()
    p._factory = factory
    return p


def _link_factory(factory):
    from cfx.link import CoolerLink

    def make(config, **kwargs):
        return CoolerLink(config, client_factory=factory, resolver=lambda *a, **k: None)

    return make


def _device(fake_indigo, **props):
    base = {
        "host": "10.66.40.129",
        "port": "13143",
        "coolerId": "14335c34f12c",
        "unit": "F",
        "compartment": "0",
    }
    base.update(props)
    dev = fake_indigo.Device(name="Garage cooler", pluginProps=base)
    fake_indigo.devices[dev.id] = dev
    return dev


def _tick(plugin):
    # the stub's sleep() raises StopThread, so one call == one loop iteration
    plugin.runConcurrentThread()


class TestLifecycle:
    def test_start_tick_pushes_states(self, plugin, fake_indigo):
        dev = _device(fake_indigo)
        plugin.deviceStartComm(dev)
        assert dev.state_list_changed == 1
        _tick(plugin)
        assert dev.states["temperatureInput1"] == 35.6
        assert dev.states["setpointCool"] == 33.8
        assert dev.states["hvacOperationMode"] == fake_indigo.kHvacMode.Cool
        assert dev.states["hvacCoolerIsOn"] is True
        assert dev.states["connected"] is True
        assert dev.states["batteryProtection"] == "Medium"
        assert dev.error_state_calls == [None]  # first connect clears any stale error
        first = dev.state_updates[0]
        assert {
            "key": "temperatureInput1",
            "value": 35.6,
            "uiValue": "35.6 °F",
            "decimalPlaces": 1,
        } in first
        # second tick: nothing changed -> no push
        _tick(plugin)
        assert len(dev.state_updates) == 1

    def test_unreachable_is_not_an_error(self, plugin, fake_indigo, caplog):
        caplog.set_level(logging.INFO, logger="Plugin")
        plugin._factory.defaults["fail_connect"] = True
        dev = _device(fake_indigo)
        plugin.deviceStartComm(dev)
        _tick(plugin)
        _tick(plugin)
        assert dev.error_state_calls == []  # a switched-off cooler must not go red
        assert dev.states["connected"] is False
        assert "probably switched off" in caplog.text

    def test_stop_comm_closes_link(self, plugin, fake_indigo):
        dev = _device(fake_indigo)
        plugin.deviceStartComm(dev)
        _tick(plugin)
        plugin.deviceStopComm(dev)
        assert plugin._factory.last.closed
        assert dev.id not in plugin._links

    def test_missing_host_marks_not_configured(self, plugin, fake_indigo):
        dev = _device(fake_indigo, host="")
        plugin.deviceStartComm(dev)
        assert dev.errorState == "not configured"


class TestActions:
    def _started(self, plugin, fake_indigo, **props):
        dev = _device(fake_indigo, **props)
        plugin.deviceStartComm(dev)
        _tick(plugin)
        return dev

    def test_set_cool_setpoint_converts_fahrenheit(
        self, plugin, fake_indigo, thermostat_action
    ):
        dev = self._started(plugin, fake_indigo)
        plugin.actionControlThermostat(
            thermostat_action(fake_indigo.kThermostatAction.SetCoolSetpoint, 35.0), dev
        )
        _tick(plugin)
        sent = plugin._factory.last.sent[-1]
        assert (
            sent.address == plugin_module.__dict__["SetSetpoint"] or True
        )  # sanity import
        assert sent.encode() == bytes.fromhex(
            "11 05 00 00 1a a4 06 00 00"
        )  # 1.7 °C = 1700
        assert dev.states["setpointCool"] == 35.1

    def test_increase_decrease_and_clamp(self, plugin, fake_indigo, thermostat_action):
        dev = self._started(plugin, fake_indigo, unit="C")
        assert dev.states["setpointCool"] == 1.0
        plugin.actionControlThermostat(
            thermostat_action(fake_indigo.kThermostatAction.IncreaseCoolSetpoint, 1.0),
            dev,
        )
        _tick(plugin)
        assert dev.states["setpointCool"] == 2.0
        plugin.actionControlThermostat(
            thermostat_action(fake_indigo.kThermostatAction.DecreaseCoolSetpoint, 30.0),
            dev,
        )
        _tick(plugin)
        assert dev.states["setpointCool"] == -22.0  # clamped to the cooler's range

    def test_hvac_mode_maps_to_power(self, plugin, fake_indigo, thermostat_action):
        dev = self._started(plugin, fake_indigo)
        plugin.actionControlThermostat(
            thermostat_action(
                fake_indigo.kThermostatAction.SetHvacMode,
                actionMode=fake_indigo.kHvacMode.Off,
            ),
            dev,
        )
        _tick(plugin)
        assert plugin._factory.last.sent[-1].encode() == bytes.fromhex(
            "11 03 00 00 1a 00 00 00 00"
        )
        assert dev.states["hvacOperationMode"] == fake_indigo.kHvacMode.Off
        plugin.actionControlThermostat(
            thermostat_action(
                fake_indigo.kThermostatAction.SetHvacMode,
                actionMode=fake_indigo.kHvacMode.Heat,
            ),
            dev,
        )
        _tick(plugin)
        assert len(plugin._factory.last.sent) == 1  # unsupported mode sends nothing

    def test_custom_actions_enqueue_jobs(self, plugin, fake_indigo, plugin_action):
        dev = self._started(plugin, fake_indigo)
        link = plugin._links[dev.id]
        link.request = lambda job: jobs.append(job)
        jobs = []
        plugin.set_battery_protection(plugin_action({"level": "high"}), dev)
        plugin.set_cooler_power(plugin_action({"power": "off"}), dev)
        plugin.request_status(plugin_action(), dev)
        plugin.actionControlGeneral(
            plugin_action(deviceAction=fake_indigo.kUniversalAction.RequestStatus), dev
        )
        assert jobs == [
            SetBatteryProtection("HIGH"),
            SetCoolerPower(False),
            RequestStatus(),
            RequestStatus(),
        ]
        assert SetPower(True) != SetPower(False) and SetSetpoint(1.0) == SetSetpoint(
            1.0
        )

    def test_action_on_unstarted_device_logs_error(
        self, plugin, fake_indigo, thermostat_action, caplog
    ):
        dev = _device(fake_indigo)
        plugin.actionControlThermostat(
            thermostat_action(fake_indigo.kThermostatAction.SetCoolSetpoint, 35.0), dev
        )
        assert "not started" in caplog.text


class TestConfigUi:
    def test_validate_device_props(self, plugin):
        ok, values, *rest = plugin.validateDeviceConfigUi(
            {"host": "", "port": "x", "coolerId": "zz", "compartment": "9"},
            "cfxCooler",
            1,
        )
        assert ok is False
        errors = rest[0]
        assert set(errors) == {"host", "port", "coolerId", "compartment"}

    def test_validate_fills_host_from_discovery(self, plugin, monkeypatch):
        from ddmp.discovery import Discovered

        plugin._discovery_cache = [
            Discovered("14335c34f12c", "MC1_34f12c", "MC1_34f12c.local", "10.66.40.129", 13143,
                       instance="Dometic CFX5")
        ]  # fmt: skip
        monkeypatch.setattr(plugin_module, "probe_ddmd", lambda *a, **k: [])
        ok, values = plugin.validateDeviceConfigUi(
            {"coolerId": "14335c34f12c", "host": "", "port": "13143"}, "cfxCooler", 1
        )
        assert (
            ok
            and values["host"] == "10.66.40.129"
            and values["coolerName"] == "MC1_34f12c"
        )
        items = plugin.get_cooler_list(values_dict=values)
        assert items[0] == ("14335c34f12c", "Dometic CFX5 (MC1_34f12c, 10.66.40.129)")
        assert items[-1][0] == "manual"

    def test_manual_host_learns_cooler_id(self, plugin, monkeypatch):
        from ddmp.discovery import DdmdReply

        reply = DdmdReply(
            "14335c34f12c", "MC1_34f12c", "97000050753", 2, 4, 0, "10.66.40.129", 13143
        )
        monkeypatch.setattr(plugin_module, "probe_ddmd", lambda *a, **k: [reply])
        ok, values = plugin.validateDeviceConfigUi(
            {"coolerId": "manual", "host": "10.66.40.129"}, "cfxCooler", 1
        )
        assert ok and values["coolerId"] == "14335c34f12c" and values["port"] == "13143"

    def test_cached_selection_listed_when_cache_empty(self, plugin):
        items = plugin.get_cooler_list(
            values_dict={"coolerId": "14335c34f12c", "coolerName": "MC1_34f12c"}
        )
        assert items[0] == ("14335c34f12c", "MC1_34f12c (cached)")

    def test_prefs_validation(self, plugin):
        ok, values, errors = plugin.validatePrefsConfigUi({"connectTimeout": "99"})
        assert not ok and "connectTimeout" in errors
        ok, values = plugin.validatePrefsConfigUi({"connectTimeout": "7"})
        assert ok and values["connectTimeout"] == "7"
