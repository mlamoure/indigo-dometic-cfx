from __future__ import annotations

import logging

from cfx.config import CoolerConfig
from cfx.link import (
    BACKOFF_SECONDS,
    ECHO_TIMEOUT,
    IDENTITY_MISMATCH_BACKOFF,
    CoolerLink,
    RequestStatus,
    SetBatteryProtection,
    SetCoolerPower,
    SetPower,
    SetSetpoint,
)
from ddmp.discovery import Discovered

from .fake_client import Factory


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def _link(factory, clock, resolver=None, **cfg):
    config = CoolerConfig(
        device_id=1, host="10.66.40.129", cooler_id="14335c34f12c", **cfg
    )
    return CoolerLink(
        config,
        client_factory=factory,
        resolver=resolver or (lambda cooler_id, **kw: None),
        clock=clock,
    )


def _texts(outcome):
    return [t for _, t in outcome.messages]


def test_connects_and_publishes_state():
    factory, clock = Factory(), Clock()
    link = _link(factory, clock)
    out = link.tick()
    assert out.connected and out.updated
    assert out.state.product_name == "CFX525"
    assert out.error_changed and out.error_state is None
    assert "connected to 10.66.40.129:13143" in _texts(out)
    assert factory.last.subscribe_calls == 1
    out2 = link.tick()
    assert out2.connected and not out2.updated and out2.messages == []


def test_backoff_while_cooler_is_off_then_recovers():
    factory, clock = Factory(fail_connect=True), Clock()
    link = _link(factory, clock)
    out = link.tick()
    assert not out.connected
    assert (
        out.error_state is None and not out.error_changed
    )  # off is normal, not an error
    assert any("probably switched off" in t for t in _texts(out))
    assert link.next_attempt == clock() + BACKOFF_SECONDS[0]
    # no retry before the deadline, no repeated error transitions
    clock.advance(1)
    assert link.tick().messages == []
    delays = [BACKOFF_SECONDS[0]]
    for expected in BACKOFF_SECONDS[1:] + (BACKOFF_SECONDS[-1],):
        clock.advance(delays[-1])
        out = link.tick()
        assert (
            not out.error_changed and out.messages[0][0] == logging.DEBUG
        )  # quiet retries
        delays.append(link.next_attempt - clock())
        assert delays[-1] == expected
    assert len(factory.clients) == len(delays)
    # cooler comes back
    factory.defaults["fail_connect"] = False
    clock.advance(delays[-1])
    out = link.tick()
    assert out.connected and out.error_changed and out.error_state is None
    assert link.failures == 0


def test_re_resolves_address_after_repeated_failures():
    factory, clock = Factory(fail_connect=True), Clock()
    calls = []

    def resolver(cooler_id, **kw):
        calls.append(cooler_id)
        return Discovered(
            cooler_id, "MC1_34f12c", "MC1_34f12c.local", "10.66.40.77", 13143
        )

    link = _link(factory, clock, resolver=resolver)
    for _ in range(3):
        link.tick()
        clock.advance(link.next_attempt - clock())
    assert calls == []  # not before the third failure has been counted
    out = link.tick()
    assert calls == ["14335c34f12c"]
    assert link.config.host == "10.66.40.77"
    assert any("moved from 10.66.40.129 to 10.66.40.77" in t for t in _texts(out))
    assert factory.last.host == "10.66.40.77"


def test_setpoint_confirmed_by_echo():
    factory, clock = Factory(), Clock()
    link = _link(factory, clock)
    link.tick()
    link.request(SetSetpoint(2.0))
    out = link.tick()
    sent = factory.last.sent
    assert sent[-1].encode() == bytes.fromhex("11 05 00 00 1a d0 07 00 00")
    assert any("sent set-point 2 °C" in t for t in _texts(out))
    out = link.tick()  # the echo is processed on the next tick
    assert any("confirmed set-point 2 °C" in t for t in _texts(out))
    assert out.state.compartment(0).setpoint_c == 2.0


def test_write_timeout_and_refusal():
    factory, clock = Factory(echo_sets=False), Clock()
    link = _link(factory, clock)
    link.tick()
    link.request(SetBatteryProtection("HIGH"))
    link.tick()
    clock.advance(ECHO_TIMEOUT + 0.1)
    out = link.tick()
    assert any(
        "did not confirm battery protection HIGH" in t and "reports" in t
        for t in _texts(out)
    )
    levels = [lvl for lvl, _ in out.messages]
    assert logging.ERROR in levels
    # the cooler re-publishes the old value first: not a refusal, the link keeps waiting
    link.request(SetCoolerPower(False))
    link.tick()
    factory.last.publish("coolerpow", True)
    out = link.tick()
    assert not any("refused" in t for t in _texts(out))
    factory.last.publish("coolerpow", False)
    out = link.tick()
    assert any("confirmed cooler power off" in t for t in _texts(out))
    # a NAK is a refusal
    link.request(SetBatteryProtection("LOW"))
    link.tick()
    from ddmp.protocol import Action, Address, Frame

    factory.last.feed(Frame(Action.NAK, Address(0x0D, 0, 0, 0x1A)).encode_line())
    out = link.tick()
    assert any("refused battery protection LOW (NAK)" in t for t in _texts(out))


def test_power_job_uses_compartment_topic_and_request_status_resubscribes():
    factory, clock = Factory(), Clock()
    link = _link(factory, clock)
    link.tick()
    link.request(SetPower(False))
    link.request(RequestStatus())
    link.tick()
    sent = factory.last.sent
    assert sent[0].encode() == bytes.fromhex("11 03 00 00 1a 00 00 00 00")
    assert factory.last.subscribe_calls == 2


def test_connection_loss_reconnects_and_drops_queued_jobs():
    factory, clock = Factory(), Clock()
    link = _link(factory, clock)
    link.tick()
    link.request(SetSetpoint(3.0))
    factory.last.drop(OSError("reset"))
    out = link.tick()
    assert not out.connected
    assert not out.error_changed
    assert any("connection lost" in t for t in _texts(out))
    assert any("discarded 1 pending command" in t for t in _texts(out))
    assert factory.last.closed
    clock.advance(BACKOFF_SECONDS[0])
    out = link.tick()
    assert out.connected and len(factory.clients) == 2


def test_identity_mismatch_disconnects():
    factory, clock = Factory(), Clock()
    link = _link(factory, clock)
    link.config = CoolerConfig(
        device_id=1, host="10.66.40.129", cooler_id="000000000001"
    )  # not this cooler
    out = link.tick()
    assert not out.connected
    assert out.error_state == "wrong cooler at address"
    assert any("is cooler 14335c34f12c, not 000000000001" in t for t in _texts(out))
    assert link.next_attempt == clock() + IDENTITY_MISMATCH_BACKOFF
    assert factory.last.closed


def test_not_configured_and_stop():
    factory, clock = Factory(), Clock()
    link = CoolerLink(
        CoolerConfig(device_id=1, host=""), client_factory=factory, clock=clock
    )
    out = link.tick()
    assert out.error_state == "not configured"
    assert factory.clients == []
    link2 = _link(factory, clock)
    link2.tick()
    link2.stop()
    assert factory.last.closed
    assert link2.tick().connected is False


def test_write_before_state_known_is_reported_not_raised():
    factory, clock = Factory(auto_publish=False), Clock()
    link = _link(factory, clock)
    link.tick()
    link.request(SetSetpoint(1.0))
    out = link.tick()
    assert any("could not send" in t and "not been published" in t for t in _texts(out))
