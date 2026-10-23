"""Command line: ``python -m plc_vision_handshake demo --scenario <name>``.

Or in two terminals: ``... plc`` (Enter = next part, r = reset, q = quit), then
``... vision [--slow] [--stale-seq] [--freeze-heartbeat] [--crash-after N]``.

Exit codes:
  0  the scenario ran to the end. That includes rejected parts: rejecting is the
     correct outcome of a fault scenario, not an error.
  1  something went wrong (port in use, a worker crashed, a wait timed out).
  2  bad command line (argparse).
  130  interrupted with Ctrl+C; PLC and vision were stopped the normal way first.

Only the main thread prints. The PLC and vision threads never do; their events
reach the main thread through the PLC's event queue.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import queue
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from plc_vision_handshake import protocol
from plc_vision_handshake.eventlog import EventLog
from plc_vision_handshake.fake_plc import FakePlc
from plc_vision_handshake.plc_logic import Event, PlcConfig, State, StateChanged
from plc_vision_handshake.protocol import Result
from plc_vision_handshake.vision_client import (
    Inspector,
    VisionClient,
    VisionFaults,
    random_inspector,
)
from plc_vision_handshake.watch import WAIT_TIMEOUT_S, PlcWatcher

FIRST_PART_ID = 1001


def always_ok(part_id: int) -> Result:
    return Result.OK


def every_second_nok(part_id: int) -> Result:
    return Result.NOK if part_id % 2 == 0 else Result.OK


def is_fault(event: Event) -> bool:
    return isinstance(event, StateChanged) and event.new is State.FAULT


def is_reset(event: Event) -> bool:
    return isinstance(event, StateChanged) and (event.old, event.new) == (State.FAULT, State.IDLE)


def print_line(line: str) -> None:
    print(line, flush=True)


class Demo:
    """One demo run: PLC, vision clients and the log. Used from the main thread only."""

    def __init__(
        self,
        stack: contextlib.ExitStack,
        port: int,
        verbose: bool,
        out: Callable[[str], None],
        fault_hint: str = "no new triggers until reset",
    ) -> None:
        self._stack = stack
        self._out = out
        config = PlcConfig()
        self.log = EventLog(time.monotonic(), config.result_timeout_s, verbose, fault_hint)
        self.plc = FakePlc(port or None, config=config)  # port 0 -> any free port
        self.plc.start()
        stack.callback(self.plc.stop)  # ExitStack is LIFO: vision clients stop first
        self.watcher = PlcWatcher(self.plc, on_event=self._print_event)
        self._next_part = FIRST_PART_ID
        self.say("PLC", f"listening on {protocol.LOCALHOST}:{self.plc.port}")

    def say(self, source: str, text: str, part_id: int | None = None, note: str = "") -> None:
        self._out(self.log.line(time.monotonic(), source, text, part_id=part_id, note=note))

    def _print_event(self, event: Event) -> None:
        line = self.log.format(event)
        if line is not None:
            self._out(line)

    def start_vision(
        self, inspect: Inspector = always_ok, faults: VisionFaults | None = None, note: str = ""
    ) -> VisionClient:
        vision = VisionClient(self.plc.port, inspect=inspect, faults=faults)
        vision.start()
        self._stack.callback(vision.stop)
        self.watcher.workers.append(vision)
        self.plc.wait_until_vision_ready(WAIT_TIMEOUT_S)
        self.say("VISION", "connected, ready", note=note)
        return vision

    def run_parts(self, count: int) -> None:
        """One part at a time: queue it, wait for its verdict."""
        for _ in range(count):
            part_id = self._next_part
            self._next_part += 1
            self.say("PART", "queued", part_id=part_id)
            self.plc.submit_part(part_id)
            self.watcher.verdict()


# --- scenarios ------------------------------------------------------------------


def _parts_with(
    inspect: Inspector = always_ok, faults: VisionFaults | None = None, note: str = ""
) -> Callable[[Demo, int], None]:
    def run(demo: Demo, parts: int) -> None:
        demo.start_vision(inspect, faults, note)
        demo.run_parts(parts)

    return run


def _freeze(demo: Demo, parts: int) -> None:
    demo.start_vision(
        faults=VisionFaults(freeze_heartbeat=True), note="fault: hangs at the first part"
    )
    demo.run_parts(1)
    demo.watcher.wait_for(is_fault, "FAULT")


def _restart(demo: Demo, parts: int) -> None:
    first = demo.start_vision(
        faults=VisionFaults(crash_after=1), note="fault: crashes after one part"
    )
    demo.run_parts(1)
    demo.watcher.pump_until(first.crashed.is_set, "vision crash")
    demo.say("VISION", "crashed", note="connection dropped, heartbeat echo stops")
    demo.watcher.wait_for(is_fault, "FAULT")
    demo.say("OPERATOR", "reset")
    demo.plc.reset()
    demo.watcher.wait_for(is_reset, "reset")
    demo.start_vision(note="new instance, DB11 overwritten with a clean state")
    demo.run_parts(1)


def _plc_stop(demo: Demo, parts: int) -> None:
    vision = demo.start_vision()
    demo.run_parts(1)
    demo.say("PLC", "halted", note="CPU in STOP: heartbeat frozen, S7 server still answers")
    demo.plc.halt()
    demo.watcher.pump_until(vision.plc_lost.is_set, "vision noticing the halted PLC")
    demo.say("VISION", "PLC heartbeat lost", note="ready=False, no new inspections")


@dataclass(frozen=True)
class Scenario:
    description: str
    run: Callable[[Demo, int], None]
    parts: int | None = None  # None: fixed flow, --parts not allowed


SCENARIOS = {
    "normal": Scenario("every part is OK", _parts_with(), parts=3),
    "nok": Scenario(
        "every second part has a defect",
        _parts_with(every_second_nok, note="every second part NOK"),
        parts=4,
    ),
    "slow": Scenario(
        "vision answers after the result timeout",
        _parts_with(faults=VisionFaults(slow=True), note="fault: 2.0 s per inspection"),
        parts=1,
    ),
    "stale": Scenario(
        "vision answers with the wrong seq_id",
        _parts_with(faults=VisionFaults(stale_seq=True), note="fault: answers the wrong seq_id"),
        parts=1,
    ),
    "freeze": Scenario("vision hangs during an inspection", _freeze),
    "restart": Scenario("vision crashes and is restarted after a reset", _restart),
    "plc-stop": Scenario("the PLC goes to STOP while vision keeps running", _plc_stop),
}


def run_demo(args: argparse.Namespace) -> int:
    scenario = SCENARIOS[args.scenario]
    print_line(f"scenario {args.scenario}: {scenario.description}")
    with contextlib.ExitStack() as stack:
        demo = Demo(stack, args.port, args.verbose, print_line)
        scenario.run(demo, args.parts or scenario.parts or 1)
        demo.watcher.drain()
        print_line("------")
        print_line(demo.log.summary())
    return 0


# --- two terminals ----------------------------------------------------------------


def _read_commands(commands: queue.Queue[str]) -> None:
    """stdin reader thread: only reads, never prints. End of input means quit."""
    for line in sys.stdin:
        commands.put(line.strip().lower())
    commands.put("q")


def run_plc(args: argparse.Namespace) -> int:
    commands: queue.Queue[str] = queue.Queue()
    threading.Thread(target=_read_commands, args=(commands,), name="stdin", daemon=True).start()
    with contextlib.ExitStack() as stack:
        # Vision usually starts after the PLC, so the first FAULT is expected.
        demo = Demo(stack, args.port, args.verbose, print_line, "start vision, then press r")
        print_line("Enter = next part, r = reset, q = quit")
        part_id = FIRST_PART_ID
        vision_seen = False
        while True:
            demo.watcher.drain()
            demo.plc.raise_if_failed()
            if not vision_seen and demo.plc.vision_ready.is_set():
                vision_seen = True
                demo.say("VISION", "ready", note="DB11 says ready=True")
            try:
                command = commands.get(timeout=0.1)  # short wait: Ctrl+C gets through
            except queue.Empty:
                continue
            if command == "q":
                return 0
            if command == "r":
                demo.plc.reset()
            elif command == "":
                demo.say("PART", "queued", part_id=part_id)
                demo.plc.submit_part(part_id)
                part_id += 1
            else:
                print_line(f"unknown command {command!r}: Enter, r or q")


def faults_from_args(args: argparse.Namespace) -> VisionFaults:
    return VisionFaults(args.slow, args.stale_seq, args.freeze_heartbeat, args.crash_after)


def run_vision(args: argparse.Namespace) -> int:
    log = EventLog(time.monotonic(), result_timeout_s=0.0)
    vision = VisionClient(
        args.port, inspect=random_inspector(args.seed), faults=faults_from_args(args)
    )
    vision.start()  # raises if no PLC is listening on the port
    print_line(log.line(time.monotonic(), "VISION", f"connected to port {args.port}"))
    try:
        plc_lost = False
        while not vision.crashed.wait(0.1):  # short wait: Ctrl+C gets through
            vision.raise_if_failed()
            if vision.plc_lost.is_set() != plc_lost:
                plc_lost = not plc_lost
                text = "PLC heartbeat lost, ready=False" if plc_lost else "PLC heartbeat back"
                print_line(log.line(time.monotonic(), "VISION", text))
        print_line(log.line(time.monotonic(), "VISION", "crashed (simulated)"))
        return 0
    except RuntimeError:  # the loop died: the PLC went away
        print(f"error: lost the connection to the PLC on port {args.port}", file=sys.stderr)
        return 1
    finally:
        with contextlib.suppress(RuntimeError):  # loop error already reported above
            vision.stop()


# --- command line ---------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m plc_vision_handshake")
    parser.add_argument("--debug", action="store_true", help="show full tracebacks")
    commands = parser.add_subparsers(dest="command", required=True)

    demo = commands.add_parser("demo", help="run PLC and vision together on one scenario")
    demo.add_argument("--scenario", choices=SCENARIOS, default="normal")
    demo.add_argument("--parts", type=int, help="parts to inspect (normal/nok/slow/stale)")
    demo.add_argument("--port", type=int, default=protocol.DEFAULT_PORT, help="0 = any free")
    demo.add_argument("--verbose", action="store_true", help="also show every state change")
    demo.set_defaults(handler=run_demo)

    plc = commands.add_parser("plc", help="fake PLC only, driven from the keyboard")
    plc.add_argument("--port", type=int, default=protocol.DEFAULT_PORT)
    plc.add_argument("--verbose", action="store_true")
    plc.set_defaults(handler=run_plc)

    vision = commands.add_parser("vision", help="vision client only, connects to a running plc")
    vision.add_argument("--port", type=int, default=protocol.DEFAULT_PORT)
    vision.add_argument("--seed", type=int, help="seed for the random OK/NOK results")
    vision.add_argument("--slow", action="store_true", help="answer after the result timeout")
    vision.add_argument("--stale-seq", action="store_true", help="answer with the wrong seq_id")
    vision.add_argument("--freeze-heartbeat", action="store_true", help="hang at the first part")
    vision.add_argument("--crash-after", type=int, metavar="N", help="crash after N answers")
    vision.set_defaults(handler=run_vision)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    # python-snap7 logs connection errors from the worker threads; our own
    # messages already report them, and only the main thread should print.
    logging.getLogger("snap7").setLevel(logging.CRITICAL)
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "demo" and args.parts is not None:
        if SCENARIOS[args.scenario].parts is None:
            parser.error(f"--parts can't be used with --scenario {args.scenario}")
        if args.parts < 1:
            parser.error("--parts must be at least 1")
    try:
        result: int = args.handler(args)
        return result
    except KeyboardInterrupt:
        # By now the handler's ExitStack has unwound: vision and PLC went through
        # the same stop() as in the tests. Nothing left to clean up, no traceback.
        print_line("interrupted, PLC and vision stopped")
        return 130
    except Exception as exc:
        if args.debug:
            raise
        cause = f" ({exc.__cause__})" if exc.__cause__ else ""
        print(f"error: {exc}{cause}", file=sys.stderr)
        return 1
