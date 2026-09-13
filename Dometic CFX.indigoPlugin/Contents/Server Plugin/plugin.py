"""Dometic CFX Indigo plugin: thin adapter between Indigo and the ddmp library.

Everything protocol-related lives in the vendored ``ddmp`` package and in ``cfx/`` (both
Indigo-free). This module owns device lifecycle, config UIs, actions, menus, the concurrent
thread and pushing state to the Indigo server.
"""

try:
    import indigo
except ImportError:  # unit tests inject a stub
    pass

import logging
import threading
import time

from cfx.config import CoolerConfig, validate_device_props, validate_prefs
from cfx.link import (
    CoolerLink,
    LinkOutcome,
    RequestStatus,
    SetBatteryProtection,
    SetCoolerPower,
    SetPower,
    SetSetpoint,
)
from cfx.statemap import HVAC_COOL, StateValue, diff, display_to_c, to_indigo_states
from ddmp import __version__ as DDMP_VERSION
from ddmp.discovery import Discovered, discover, probe_ddmd

DEVICE_TYPE = "cfxCooler"
MANUAL_ENTRY = "manual"


class Plugin(indigo.PluginBase):
    def __init__(self, plugin_id, plugin_display_name, plugin_version, plugin_prefs):
        super().__init__(plugin_id, plugin_display_name, plugin_version, plugin_prefs)
        self.log_level = int(plugin_prefs.get("log_level", logging.INFO))
        self.indigo_log_handler.setLevel(self.log_level)
        self.plugin_file_handler.setLevel(logging.DEBUG)

        self._lock = threading.Lock()
        self._links: dict[int, CoolerLink] = {}  # device id -> link
        self._last_states: dict[int, dict[str, StateValue]] = {}
        self._discovery_cache: list[Discovered] = []

    ########################################
    # Lifecycle
    ########################################

    def startup(self):
        self.logger.debug(f"startup (ddmp {DDMP_VERSION})")

    def shutdown(self):
        self.logger.debug("shutdown")
        with self._lock:
            links = list(self._links.values())
        for link in links:
            link.stop()

    def runConcurrentThread(self):
        try:
            while True:
                with self._lock:
                    links = list(self._links.items())
                for dev_id, link in links:
                    try:
                        outcome = link.tick()
                        self._apply_outcome(dev_id, outcome)
                    except Exception:
                        self.logger.exception(f"cooler link {dev_id} failed")
                self.sleep(0.5)
        except self.StopThread:
            pass

    ########################################
    # Devices
    ########################################

    def _connect_timeout(self) -> float:
        try:
            return float(self.pluginPrefs.get("connectTimeout", 5))
        except (TypeError, ValueError):
            return 5.0

    def deviceStartComm(self, dev):
        if dev.deviceTypeId != DEVICE_TYPE:
            return
        dev.stateListOrDisplayStateIdChanged()
        config = CoolerConfig.from_props(
            dev.id, dev.pluginProps, connect_timeout=self._connect_timeout()
        )
        self.logger.debug(f"deviceStartComm {dev.name}: {config}")
        link = CoolerLink(config)
        with self._lock:
            old = self._links.pop(dev.id, None)
            self._links[dev.id] = link
            self._last_states.pop(dev.id, None)
        if old is not None:
            old.stop()
        if not config.host:
            dev.setErrorStateOnServer("not configured")

    def deviceStopComm(self, dev):
        with self._lock:
            link = self._links.pop(dev.id, None)
            self._last_states.pop(dev.id, None)
        if link is not None:
            link.stop()

    def _apply_outcome(self, dev_id: int, outcome: LinkOutcome) -> None:
        for level, text in outcome.messages:
            self.logger.log(level, text)
        try:
            dev = indigo.devices[dev_id]
        except KeyError:
            return
        if outcome.error_changed:
            dev.setErrorStateOnServer(outcome.error_state)
        with self._lock:
            link = self._links.get(dev_id)
            prev = self._last_states.get(dev_id, {})
        if link is None:
            return
        cfg = link.config
        now_text = time.strftime("%Y-%m-%d %H:%M:%S") if outcome.updated else None
        new = to_indigo_states(
            outcome.state,
            compartment=cfg.compartment,
            unit=cfg.unit,
            connected=outcome.connected,
            now_text=now_text,
        )
        if now_text is None and "lastUpdate" in prev:
            new["lastUpdate"] = prev["lastUpdate"]
        updates = diff(prev, new)
        if not updates:
            return
        for item in updates:
            if item["key"] == "hvacOperationMode":
                item["value"] = (
                    indigo.kHvacMode.Cool
                    if item["value"] == HVAC_COOL
                    else indigo.kHvacMode.Off
                )
        dev.updateStatesOnServer(updates)
        with self._lock:
            self._last_states[dev_id] = new

    def _link_for(self, dev):
        with self._lock:
            return self._links.get(dev.id)

    ########################################
    # Config UIs
    ########################################

    def validatePrefsConfigUi(self, values_dict):
        errors, cleaned = validate_prefs(values_dict)
        values_dict.update(cleaned)
        if errors:
            error_dict = indigo.Dict()
            for key, text in errors.items():
                error_dict[key] = text
            return (False, values_dict, error_dict)
        return (True, values_dict)

    def closedPrefsConfigUi(self, values_dict, user_cancelled):
        if user_cancelled:
            return
        self.log_level = int(values_dict.get("log_level", logging.INFO))
        self.indigo_log_handler.setLevel(self.log_level)

    def validateDeviceConfigUi(self, values_dict, type_id, dev_id):
        selected = str(values_dict.get("coolerId", "") or "")
        if selected and selected != MANUAL_ENTRY:
            match = next(
                (d for d in self._discovery_cache if d.cooler_id == selected), None
            )
            if match is not None:
                if match.ip and not str(values_dict.get("host", "")).strip():
                    values_dict["host"] = match.ip
                elif match.ip and values_dict.get("host") != match.ip:
                    values_dict["host"] = match.ip
                values_dict["coolerName"] = match.name
        errors, cleaned = validate_device_props(values_dict)
        values_dict["host"] = cleaned["host"]
        values_dict["port"] = cleaned["port"]
        values_dict["coolerId"] = cleaned["coolerId"]
        if errors:
            error_dict = indigo.Dict()
            for key, text in errors.items():
                error_dict[key] = text
            return (False, values_dict, error_dict)
        if not cleaned["coolerId"]:
            # Manual address: learn the cooler id so the plugin can re-find it later.
            try:
                replies = probe_ddmd(
                    cleaned["host"], int(cleaned["port"]), timeout=1.5, first_only=True
                )
            except OSError:
                replies = []
            if replies:
                values_dict["coolerId"] = replies[0].cooler_id
                values_dict["coolerName"] = replies[0].name
            else:
                self.logger.warning(
                    f"no cooler answered at {cleaned['host']}:{cleaned['port']} "
                    "(it may be switched off); saving anyway"
                )
        return (True, values_dict)

    def get_cooler_list(self, filter="", values_dict=None, type_id="", target_id=0):
        items = [
            (
                d.cooler_id or MANUAL_ENTRY,
                f"{d.instance or d.name} ({d.name}, {d.ip or '?'})",
            )
            for d in self._discovery_cache
            if d.cooler_id
        ]
        current = str((values_dict or {}).get("coolerId", "") or "")
        if current and current != MANUAL_ENTRY and current not in [i[0] for i in items]:
            name = str((values_dict or {}).get("coolerName", "") or current)
            items.insert(0, (current, f"{name} (cached)"))
        items.append((MANUAL_ENTRY, "Enter the address manually"))
        return items

    def refresh_cooler_list(self, values_dict, type_id, dev_id):
        self._run_discovery()
        return values_dict

    def _run_discovery(self) -> list[Discovered]:
        try:
            found = discover(timeout=3.0)
        except OSError as exc:
            self.logger.warning(f"discovery failed: {exc}")
            found = []
        self._discovery_cache = found
        if found:
            for d in found:
                self.logger.info(
                    f"found {d.instance or d.name}: {d.name} id={d.cooler_id or '?'} "
                    f"at {d.ip or '?'}:{d.port} firmware {d.firmware or '?'}"
                )
        else:
            self.logger.info("no coolers found (they only answer while switched on)")
        return found

    ########################################
    # Menu items
    ########################################

    def menu_discover_coolers(self):
        self._run_discovery()

    def menu_toggle_debug(self):
        if self.log_level > logging.DEBUG:
            self.log_level = logging.DEBUG
            self.logger.info("debug logging on")
        else:
            self.log_level = logging.INFO
            self.logger.info("debug logging off")
        self.indigo_log_handler.setLevel(self.log_level)
        self.pluginPrefs["log_level"] = str(self.log_level)

    ########################################
    # Actions
    ########################################

    def actionControlThermostat(self, action, dev):
        link = self._link_for(dev)
        if link is None:
            self.logger.error(f"{dev.name}: device is not started")
            return
        unit = link.config.unit
        kind = action.thermostatAction
        if kind == indigo.kThermostatAction.SetCoolSetpoint:
            self._request_setpoint(link, dev, float(action.actionValue), unit)
        elif kind == indigo.kThermostatAction.IncreaseCoolSetpoint:
            self._request_setpoint(
                link, dev, dev.coolSetpoint + float(action.actionValue), unit
            )
        elif kind == indigo.kThermostatAction.DecreaseCoolSetpoint:
            self._request_setpoint(
                link, dev, dev.coolSetpoint - float(action.actionValue), unit
            )
        elif kind == indigo.kThermostatAction.SetHvacMode:
            mode = action.actionMode
            if mode == indigo.kHvacMode.Cool:
                link.request(SetPower(True))
                self.logger.info(f"{dev.name}: turning cooling on")
            elif mode == indigo.kHvacMode.Off:
                link.request(SetPower(False))
                self.logger.info(f"{dev.name}: turning cooling off")
            else:
                self.logger.warning(
                    f"{dev.name}: mode {mode} is not supported (use Cool or Off)"
                )
        elif kind in (
            indigo.kThermostatAction.RequestStatusAll,
            indigo.kThermostatAction.RequestMode,
            indigo.kThermostatAction.RequestEquipmentState,
            indigo.kThermostatAction.RequestTemperatures,
            indigo.kThermostatAction.RequestSetpoints,
        ):
            link.request(RequestStatus())
        else:
            self.logger.warning(
                f"{dev.name}: thermostat action {kind} is not supported"
            )

    def _request_setpoint(self, link, dev, display_value: float, unit: str) -> None:
        celsius = round(display_to_c(display_value, unit), 1)
        lo = dev.states.get("setpointMin")
        hi = dev.states.get("setpointMax")
        if lo not in (None, "") and hi not in (None, ""):
            clamped = min(max(display_value, float(lo)), float(hi))
            if clamped != display_value:
                self.logger.warning(
                    f"{dev.name}: set-point {display_value:g} clamped to {clamped:g} "
                    f"(cooler range {float(lo):g}..{float(hi):g})"
                )
                celsius = round(display_to_c(clamped, unit), 1)
        self.logger.info(f"{dev.name}: requesting set-point {celsius:g} °C")
        link.request(SetSetpoint(celsius))

    def actionControlGeneral(self, action, dev):
        if action.deviceAction == indigo.kUniversalAction.RequestStatus:
            link = self._link_for(dev)
            if link is not None:
                link.request(RequestStatus())
        else:
            self.logger.warning(
                f"{dev.name}: action {action.deviceAction} is not supported"
            )

    def set_battery_protection(self, action, dev):
        link = self._link_for(dev)
        level = str(action.props.get("level", "MEDIUM")).upper()
        if link is None:
            self.logger.error(f"{dev.name}: device is not started")
            return
        if level not in ("LOW", "MEDIUM", "HIGH"):
            self.logger.error(f"{dev.name}: invalid battery protection level {level!r}")
            return
        link.request(SetBatteryProtection(level))

    def set_cooler_power(self, action, dev):
        link = self._link_for(dev)
        if link is None:
            self.logger.error(f"{dev.name}: device is not started")
            return
        link.request(
            SetCoolerPower(str(action.props.get("power", "on")).lower() == "on")
        )

    def request_status(self, action, dev):
        link = self._link_for(dev)
        if link is not None:
            link.request(RequestStatus())
