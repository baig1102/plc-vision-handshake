"""One test per fault scenario, over a real S7 connection.

Timing (conftest): result timeout 1.5 s, PLC watchdog 1.0 s, 10 ms cycles.
Every wait blocks on an event or queue with a deadline; nothing sleeps.
"""

import pytest
from watcher import PlcWatcher, StartVision

from plc_vision_handshake.fake_plc import FakePlc
from plc_vision_handshake.plc_logic import Event, RejectReason, State, StateChanged, Verdict
from plc_vision_handshake.vision_client import VisionFaults

pytestmark = pytest.mark.integration


def is_fault(event: Event) -> bool:
    return isinstance(event, StateChanged) and event.new is State.FAULT


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
