"""Connection manager for one cooler: connect, back off while it is switched off, re-find it
by id when its address changes, forward writes and confirm them by the cooler's echo.

Runs on the plugin's concurrent thread via :meth:`CoolerLink.tick`; Indigo callbacks only
enqueue :class:`Job` objects through :meth:`CoolerLink.request`. No ``indigo`` imports.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ddmp.discovery import Discovered, find_cooler, normalize_id
from ddmp.errors import DdmpError
from ddmp.models import CoolerState
from ddmp.session import Event, Nak, Publish, WriteExpectation
from ddmp.sync_client import SyncClient

from .config import CoolerConfig
from .statemap import HVAC_POWER_TOPIC

log = logging.getLogger("Plugin")

BACKOFF_SECONDS = (
    5,
    10,
    20,
    30,
    60,
)  # a switched-off cooler is normal; keep retrying every minute
RESOLVE_AFTER_FAILURES = 3
RESOLVE_INTERVAL = 600.0
ECHO_TIMEOUT = 10.0  # the cooler publishes the new value ~3 s after a SET
IDENTITY_MISMATCH_BACKOFF = 300.0


# ----------------------------------------------------------------------------- jobs


@dataclass(frozen=True)
class SetSetpoint:
    celsius: float


@dataclass(frozen=True)
class SetPower:  # the thermostat mode (HVAC_POWER_TOPIC = the cooler's master switch)
    on: bool


@dataclass(frozen=True)
class SetCoolerPower:  # the master switch
    on: bool


@dataclass(frozen=True)
class SetBatteryProtection:
    level: str  # LOW / MEDIUM / HIGH


@dataclass(frozen=True)
class RequestStatus:
    pass


Job = SetSetpoint | SetPower | SetCoolerPower | SetBatteryProtection | RequestStatus


@dataclass
class LinkOutcome:
    connected: bool
    state: CoolerState | None
    messages: list[tuple[int, str]] = field(
        default_factory=list
    )  # (logging level, text)
    error_state: str | None = None  # value for setErrorStateOnServer when error_changed
    error_changed: bool = False
    updated: bool = False  # new publishes arrived this tick


class _Pending:
    __slots__ = ("deadline", "expectation", "label")

    def __init__(
        self, expectation: WriteExpectation, label: str, deadline: float
    ) -> None:
        self.expectation = expectation
        self.label = label
        self.deadline = deadline


class CoolerLink:
    """State machine: IDLE -> CONNECTING -> ONLINE -> BACKOFF -> CONNECTING ..."""

    def __init__(
        self,
        config: CoolerConfig,
        *,
        client_factory: Callable[..., Any] = SyncClient,
        resolver: Callable[..., Discovered | None] = find_cooler,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._factory = client_factory
        self._resolver = resolver
        self._clock = clock
        self.state_name = "IDLE"
        self.client: Any = None
        self.failures = 0
        self.next_attempt = 0.0
        self.last_resolve = -RESOLVE_INTERVAL
        self._events: queue.Queue[Event] = queue.Queue()
        self._jobs: queue.Queue[Job] = queue.Queue()
        self._closed = threading.Event()
        self._close_reason: BaseException | None = None
        self._pending: list[_Pending] = []
        self._error_state: str | None = None
        self._identity_checked = False
        self._stopped = False

    # ---- API used by the plugin (any thread)

    def request(self, job: Job) -> None:
        self._jobs.put(job)

    def stop(self) -> None:
        self._stopped = True
        self._disconnect()

    def update_config(self, config: CoolerConfig) -> None:
        self.config = config
        self._disconnect()
        self.failures = 0
        self.next_attempt = 0.0
        self.state_name = "IDLE"

    @property
    def connected(self) -> bool:
        return (
            self.state_name == "ONLINE"
            and self.client is not None
            and self.client.connected
        )

    # ---- driven from runConcurrentThread

    def tick(self, now: float | None = None) -> LinkOutcome:
        now = self._clock() if now is None else now
        outcome = LinkOutcome(connected=False, state=None)
        if self._stopped:
            return outcome
        if self.state_name in ("IDLE", "BACKOFF") and now >= self.next_attempt:
            self._connect(now, outcome)
        if self.state_name == "ONLINE":
            self._service(now, outcome)
        outcome.connected = self.connected
        if self.client is not None:
            outcome.state = self.client.state()
        return outcome

    # ---- internals

    def _set_error(self, outcome: LinkOutcome, value: str | None) -> None:
        if value != self._error_state:
            self._error_state = value
            outcome.error_state = value
            outcome.error_changed = True

    def _connect(self, now: float, outcome: LinkOutcome) -> None:
        cfg = self.config
        if not cfg.host:
            self.state_name = "BACKOFF"
            self.next_attempt = now + BACKOFF_SECONDS[-1]
            outcome.messages.append((logging.WARNING, "no address configured"))
            self._set_error(outcome, "not configured")
            return
        if (
            self.failures >= RESOLVE_AFTER_FAILURES
            and cfg.cooler_id
            and now - self.last_resolve >= RESOLVE_INTERVAL
        ):
            self.last_resolve = now
            self._resolve(outcome)
            cfg = self.config
        self.state_name = "CONNECTING"
        self._closed.clear()
        self._close_reason = None
        self._identity_checked = False
        self._pending.clear()
        client = self._factory(
            cfg.host,
            cfg.port,
            on_event=self._events.put,
            on_close=self._on_close,
            connect_timeout=cfg.connect_timeout,
        )
        try:
            client.connect()
        except OSError as exc:
            self.failures += 1
            delay = BACKOFF_SECONDS[min(self.failures, len(BACKOFF_SECONDS)) - 1]
            self.next_attempt = now + delay
            self.state_name = "BACKOFF"
            if self.failures == 1:
                outcome.messages.append(
                    (
                        logging.INFO,
                        f"{cfg.host}:{cfg.port} is not answering ({exc}); the cooler is "
                        "probably switched off, will keep trying quietly",
                    )
                )
            else:
                outcome.messages.append(
                    (
                        logging.DEBUG,
                        f"{cfg.host}:{cfg.port} still unreachable; retry in {delay}s",
                    )
                )
            self._set_error(outcome, None)  # being switched off is normal, not an error
            return
        self.client = client
        self.state_name = "ONLINE"
        self.failures = 0
        outcome.messages.append((logging.INFO, f"connected to {cfg.host}:{cfg.port}"))
        self._set_error(outcome, None)
        outcome.error_changed = True  # clear any stale error shown for this device

    def _resolve(self, outcome: LinkOutcome) -> None:
        try:
            # also probe the last known address directly: cheaper and works without mDNS
            found = self._resolver(
                self.config.cooler_id, timeout=3.0, hosts=[self.config.host]
            )
        except Exception as exc:  # discovery must never kill the loop
            outcome.messages.append((logging.DEBUG, f"re-discovery failed: {exc}"))
            return
        if found is None or not found.ip:
            outcome.messages.append(
                (
                    logging.DEBUG,
                    f"cooler {self.config.cooler_id} not found by mDNS/probe",
                )
            )
            return
        if found.ip != self.config.host:
            outcome.messages.append(
                (
                    logging.INFO,
                    f"cooler {found.name} moved from {self.config.host} to {found.ip}",
                )
            )
            self.config = self.config.with_host(found.ip)

    def _on_close(self, exc: BaseException | None) -> None:
        self._close_reason = exc
        self._closed.set()

    def _disconnect(self) -> None:
        client, self.client = self.client, None
        if client is not None:
            client.close()
        self._pending.clear()

    def _service(self, now: float, outcome: LinkOutcome) -> None:
        # 1. inbound events
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                break
            if isinstance(event, Publish):
                outcome.updated = True
            elif isinstance(event, Nak):
                outcome.messages.append(
                    (logging.WARNING, f"cooler sent NAK for {event.address}")
                )
            self._match_pending(event, outcome)
        # 2. expired writes
        for p in list(self._pending):
            if now >= p.deadline:
                self._pending.remove(p)
                outcome.messages.append(
                    (
                        logging.ERROR,
                        f"cooler did not confirm {p.label} within {ECHO_TIMEOUT:g}s "
                        f"(it reports {p.expectation.observed!r})",
                    )
                )
        # 3. identity check, once per connection
        if not self._identity_checked and self.client is not None:
            mac = self.client.value("gw_mac")
            if mac:
                self._identity_checked = True
                if self.config.cooler_id and normalize_id(mac) != self.config.cooler_id:
                    outcome.messages.append(
                        (
                            logging.ERROR,
                            f"{self.config.host} is cooler {normalize_id(mac)}, not "
                            f"{self.config.cooler_id}; disconnecting",
                        )
                    )
                    self._disconnect()
                    self.state_name = "BACKOFF"
                    self.failures = RESOLVE_AFTER_FAILURES
                    self.last_resolve = -RESOLVE_INTERVAL
                    self.next_attempt = now + IDENTITY_MISMATCH_BACKOFF
                    self._set_error(outcome, "wrong cooler at address")
                    return
        # 4. lost connection?
        if self._closed.is_set() or (
            self.client is not None and not self.client.connected
        ):
            reason = self._close_reason or "connection closed"
            outcome.messages.append(
                (
                    logging.INFO,
                    f"connection lost ({reason}); cooler switched off? reconnecting",
                )
            )
            self._disconnect()
            self.state_name = "BACKOFF"
            self.failures = 1
            self.next_attempt = now + BACKOFF_SECONDS[0]
            self._set_error(outcome, None)
            self._drop_jobs(outcome)
            return
        # 5. jobs
        while True:
            try:
                job = self._jobs.get_nowait()
            except queue.Empty:
                break
            self._run_job(job, now, outcome)

    def _drop_jobs(self, outcome: LinkOutcome) -> None:
        dropped = 0
        while True:
            try:
                self._jobs.get_nowait()
                dropped += 1
            except queue.Empty:
                break
        if dropped:
            outcome.messages.append(
                (logging.WARNING, f"discarded {dropped} pending command(s)")
            )

    def _run_job(self, job: Job, now: float, outcome: LinkOutcome) -> None:
        client = self.client
        if client is None:
            return
        try:
            if isinstance(job, RequestStatus):
                client.subscribe_all()
                outcome.messages.append(
                    (logging.DEBUG, "status requested (re-subscribed)")
                )
                return
            if isinstance(job, SetSetpoint):
                label = f"set-point {job.celsius:g} °C"
                exp = client.set(
                    "csettemp", job.celsius, compartment=self.config.compartment
                )
            elif isinstance(job, SetPower):
                label = f"{HVAC_POWER_TOPIC} {'on' if job.on else 'off'}"
                comp = self.config.compartment if HVAC_POWER_TOPIC == "cpow" else None
                exp = client.set(HVAC_POWER_TOPIC, job.on, compartment=comp)
            elif isinstance(job, SetCoolerPower):
                label = f"cooler power {'on' if job.on else 'off'}"
                exp = client.set("coolerpow", job.on)
            elif isinstance(job, SetBatteryProtection):
                label = f"battery protection {job.level}"
                exp = client.set("batprotlvl", job.level)
            else:
                outcome.messages.append((logging.ERROR, f"unknown job {job!r}"))
                return
        except (DdmpError, ValueError, OSError) as exc:
            outcome.messages.append((logging.ERROR, f"could not send {job!r}: {exc}"))
            return
        outcome.messages.append(
            (logging.INFO, f"sent {label}; waiting for the cooler's echo")
        )
        self._pending.append(_Pending(exp, label, now + ECHO_TIMEOUT))

    def _match_pending(self, event: Event, outcome: LinkOutcome) -> None:
        for p in list(self._pending):
            verdict = p.expectation.matches(event)
            if verdict is True:
                self._pending.remove(p)
                outcome.messages.append((logging.INFO, f"cooler confirmed {p.label}"))
            elif verdict is False:  # NAK
                self._pending.remove(p)
                outcome.messages.append(
                    (logging.ERROR, f"cooler refused {p.label} (NAK)")
                )
