"""One test per fault scenario, over a real S7 connection.

Timing (conftest): result timeout 1.5 s, PLC watchdog 1.0 s, 10 ms cycles.
Every wait blocks on an event or queue with a deadline; nothing sleeps.
"""

import pytest
from watcher import GatedVisionClient, StartVision

from plc_vision_handshake.fake_plc import FakePlc
from plc_vision_handshake.plc_logic import Event, RejectReason, State, StateChanged, Verdict
from plc_vision_handshake.protocol import Result
from plc_vision_handshake.vision_client import VisionFaults
from plc_vision_handshake.watch import WAIT_TIMEOUT_S, PlcWatcher

pytestmark = pytest.mark.integration


def is_fault(event: Event) -> bool:
    return isinstance(event, StateChanged) and event.new is State.FAULT


def is_reset(event: Event) -> bool:
    return isinstance(event, StateChanged) and (event.old, event.new) == (State.FAULT, State.IDLE)


class RecordingInspector:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def __call__(self, part_id: int) -> Result:
        self.calls.append(part_id)
        return Result.OK


def test_slow_vision_times_out_instead_of_tripping_the_watchdog(
    plc: FakePlc, start_vision: StartVision, watcher: PlcWatcher
) -> None:
    start_vision(faults=VisionFaults(slow=True))  # 2.0 s per inspection
    plc.submit_part(201)

    # The echo keeps running while vision "inspects", so the PLC knows vision is
    # alive and the part runs into the result deadline instead.
    assert watcher.verdict() == Verdict(
        seq_id=1, part_id=201, accepted=False, reason=RejectReason.TIMEOUT
    )
    assert not any(is_fault(e) for e in watcher.seen)


def test_answer_for_the_wrong_seq_is_rejected_as_stale(
    plc: FakePlc, start_vision: StartVision, watcher: PlcWatcher
) -> None:
    start_vision(faults=VisionFaults(stale_seq=True))  # answers seq 1 as seq 0
    plc.submit_part(202)

    assert watcher.verdict() == Verdict(
        seq_id=1, part_id=202, accepted=False, reason=RejectReason.STALE_RESULT
    )


def test_frozen_heartbeat_faults_and_rejects_the_part_in_flight(
    plc: FakePlc, start_vision: StartVision, watcher: PlcWatcher
) -> None:
    start_vision(faults=VisionFaults(freeze_heartbeat=True))  # hangs at the first part
    plc.submit_part(203)

    assert watcher.verdict() == Verdict(
        seq_id=1, part_id=203, accepted=False, reason=RejectReason.WATCHDOG
    )
    watcher.wait_for(is_fault, "FAULT after the WATCHDOG verdict")


def test_vision_restart_recovers_after_reset_without_reusing_the_old_result(
    plc: FakePlc, start_vision: StartVision, watcher: PlcWatcher
) -> None:
    first = start_vision(faults=VisionFaults(crash_after=1))
    plc.submit_part(301)
    assert watcher.verdict() == Verdict(seq_id=1, part_id=301, accepted=True)

    assert first.crashed.wait(WAIT_TIMEOUT_S), "vision did not crash after one part"
    watcher.wait_for(is_fault, "FAULT after the vision crash")
    leftover = plc.read_db11()
    assert (leftover.result_seq_id, leftover.result) == (1, Result.OK)  # still in DB11

    plc.reset()
    watcher.wait_for(is_reset, "FAULT -> IDLE after reset")
    inspector = RecordingInspector()
    second = start_vision(client_class=GatedVisionClient, inspect=inspector)
    assert isinstance(second, GatedVisionClient)

    # Its loop hasn't polled yet, so only the first write inside start() can have
    # replaced the leftover: DB11 must already be clean at this point.
    clean = plc.read_db11()
    assert (clean.result_seq_id, clean.result, clean.ready) == (0, Result.NONE, True)
    second.gate.set()

    plc.submit_part(302)
    assert watcher.verdict() == Verdict(seq_id=2, part_id=302, accepted=True)
    assert inspector.calls == [302]  # accepted because the new instance inspected it


def test_halted_plc_makes_vision_drop_ready(plc: FakePlc, start_vision: StartVision) -> None:
    vision = start_vision(plc_watchdog_s=0.5)
    assert plc.read_db11().ready

    plc.halt()  # CPU in STOP: heartbeat freezes, the S7 server keeps answering

    assert vision.plc_lost.wait(WAIT_TIMEOUT_S), "vision did not notice the halted PLC"
    assert plc.read_db11().ready is False
