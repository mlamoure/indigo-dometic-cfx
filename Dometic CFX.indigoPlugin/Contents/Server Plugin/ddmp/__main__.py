"""Command-line tool: ``python -m ddmp`` / ``ddmp``.

Read-only commands: ``discover``, ``watch``, ``state``. Write commands (``set-temp``, ``set``)
print the exact frame and refuse to send it unless ``--yes`` is given. Exit codes for writes:
0 confirmed by the cooler's publish, 3 NAK, 4 not confirmed within the echo timeout.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from dataclasses import asdict
from typing import Any

from . import __version__
from .discovery import discover, probe_ddmd, query_mdns
from .models import CoolerState
from .protocol import DEFAULT_PORT, WRITABLE, topic
from .session import Event, Nak, Publish, Unhandled, WriteExpectation
from .sync_client import SyncClient


def _fmt_value(value: Any) -> str:
    if hasattr(value, "name") and hasattr(value, "value"):
        return f"{value.name} ({int(value)})"
    if isinstance(value, bytes):
        return value.hex(" ")
    return repr(value)


def _print_event(event: Event, *, raw: bool, as_json: bool) -> None:
    stamp = time.strftime("%H:%M:%S")
    if isinstance(event, Publish):
        if as_json:
            print(
                json.dumps(
                    {
                        "t": stamp,
                        "topic": event.name,
                        "address": str(event.address),
                        "value": event.value,
                        "raw": event.raw.hex(),
                    },
                    default=str,
                ),
                flush=True,
            )
            return
        line = f"{stamp}  {event.name:14s} = {_fmt_value(event.value)}"
        if raw:
            line += f"    [{event.address} {event.raw.hex(' ')}]"
        print(line, flush=True)
    elif isinstance(event, Nak):
        print(f"{stamp}  NAK {event.address} {event.raw.hex(' ')}", flush=True)
    elif isinstance(event, Unhandled):
        print(f"{stamp}  ?? {event.frame}", flush=True)


def _print_state(state: CoolerState) -> None:
    data = asdict(state)
    data["error_text"] = state.error_text
    print(json.dumps(data, indent=2, default=lambda o: getattr(o, "name", str(o))))


def cmd_discover(args: argparse.Namespace) -> int:
    if args.mdns_only:
        for svc in query_mdns(timeout=args.timeout):
            print(
                f"{svc.instance!r} -> {svc.hostname}:{svc.port} {svc.addresses} {svc.txt}"
            )
        return 0
    if args.host:
        for r in probe_ddmd(args.host, args.port, timeout=args.timeout):
            print(json.dumps(asdict(r)))
        return 0
    found = discover(timeout=args.timeout, broadcast=not args.no_broadcast)
    if not found:
        print("no coolers found", file=sys.stderr)
        return 1
    for d in found:
        print(
            f"{d.name:14s} id={d.cooler_id or '?':12s} ip={d.ip or '?':15s} port={d.port} "
            f"fw={d.firmware or '?'} sku={d.sku or '?'} via={','.join(sorted(d.sources))}"
        )
    return 0


def _connect(args: argparse.Namespace, on_event) -> SyncClient:
    closed = threading.Event()

    def on_close(exc: BaseException | None) -> None:
        if exc is not None:
            print(f"connection closed: {exc}", file=sys.stderr)
        closed.set()

    client = SyncClient(
        args.host,
        args.port,
        on_event=on_event,
        on_close=on_close,
        connect_timeout=args.timeout,
    )
    client.connect()
    client.closed_event = closed  # type: ignore[attr-defined]
    return client


def cmd_watch(args: argparse.Namespace) -> int:
    client = _connect(args, lambda e: _print_event(e, raw=args.raw, as_json=args.json))
    print(f"connected to {args.host}:{args.port}; Ctrl-C to stop", file=sys.stderr)
    try:
        while not client.closed_event.wait(0.5):  # type: ignore[attr-defined]
            pass
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        client.close()


def cmd_state(args: argparse.Namespace) -> int:
    client = _connect(args, None)
    try:
        time.sleep(args.wait)
        _print_state(client.state())
        return 0
    finally:
        client.close()


def _write(
    args: argparse.Namespace, name: str, value: Any, compartment: int | None
) -> int:
    events: list[Event] = []
    lock = threading.Lock()

    def on_event(e: Event) -> None:
        with lock:
            events.append(e)
        if not args.quiet:
            _print_event(e, raw=True, as_json=False)

    client = _connect(args, on_event)
    try:
        time.sleep(args.wait)  # let the subscribe burst populate the session
        frame, expectation = client.session.set_frame(
            name, value, compartment=compartment
        )
        print(f"frame: {frame}    line: {frame.encode_line()!r}", file=sys.stderr)
        if not args.yes:
            print("dry run: pass --yes to send", file=sys.stderr)
            return 2
        with lock:
            events.clear()
        client.send(frame)
        deadline = time.monotonic() + args.echo_timeout
        while time.monotonic() < deadline:
            with lock:
                pending = list(events)
                events.clear()
            for e in pending:
                verdict: bool | None = expectation.matches(e)
                if verdict is True:
                    print(f"confirmed: {name} = {_fmt_value(expectation.observed)}")
                    return 0
                if verdict is False:
                    print("REFUSED: the cooler answered NAK", file=sys.stderr)
                    return 3
            time.sleep(0.1)
        print(
            f"NOT CONFIRMED within {args.echo_timeout}s; cooler reports "
            f"{_fmt_value(expectation.observed)}",
            file=sys.stderr,
        )
        return 4
    finally:
        client.close()


def cmd_set_temp(args: argparse.Namespace) -> int:
    return _write(args, "csettemp", float(args.celsius), args.compartment)


def cmd_set(args: argparse.Namespace) -> int:
    tp = topic(args.topic)
    if tp.name not in WRITABLE:
        print(
            f"{tp.name} is not writable (allowed: {', '.join(sorted(WRITABLE))})",
            file=sys.stderr,
        )
        return 2
    return _write(args, tp.name, args.value, args.compartment)


def _expectation_str(exp: WriteExpectation) -> str:
    return f"{exp.topic.name} -> {exp.expected!r}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ddmp", description=__doc__)
    parser.add_argument("--version", action="version", version=f"ddmp {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("discover", help="find coolers via mDNS and the DDMD probe")
    p.add_argument("--timeout", type=float, default=3.0)
    p.add_argument("--host", help="probe one address instead of browsing")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--mdns-only", action="store_true")
    p.add_argument("--no-broadcast", action="store_true")
    p.set_defaults(func=cmd_discover)

    def conn_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("host")
        sp.add_argument("--port", type=int, default=DEFAULT_PORT)
        sp.add_argument("--timeout", type=float, default=5.0, help="connect timeout")

    p = sub.add_parser("watch", help="subscribe and print every publish (read-only)")
    conn_args(p)
    p.add_argument("--raw", action="store_true", help="show address and payload bytes")
    p.add_argument("--json", action="store_true", help="one JSON object per line")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser(
        "state", help="subscribe, wait, print the decoded state (read-only)"
    )
    conn_args(p)
    p.add_argument("--wait", type=float, default=3.0)
    p.set_defaults(func=cmd_state)

    def write_args(sp: argparse.ArgumentParser) -> None:
        conn_args(sp)
        sp.add_argument("--compartment", type=int, default=None)
        sp.add_argument(
            "--wait", type=float, default=3.0, help="seconds to collect state first"
        )
        sp.add_argument(
            "--echo-timeout",
            type=float,
            default=10.0,
            help="seconds to wait for the cooler to publish the new value (it takes ~3 s)",
        )
        sp.add_argument("--yes", action="store_true", help="actually send the SET")
        sp.add_argument("--quiet", action="store_true")

    p = sub.add_parser(
        "set-temp", help="set the set-point in °C (dry run without --yes)"
    )
    write_args(p)
    p.add_argument("celsius", type=float)
    p.set_defaults(func=cmd_set_temp)

    p = sub.add_parser("set", help="set a writable topic (dry run without --yes)")
    write_args(p)
    p.add_argument("topic", help=f"one of: {', '.join(sorted(WRITABLE))}")
    p.add_argument("value", help="on/off, LOW/MEDIUM/HIGH, or a number")
    p.set_defaults(func=cmd_set)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    try:
        return int(args.func(args))
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
