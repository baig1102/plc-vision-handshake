import queue
import time

import pytest

from plc_vision_handshake.fake_plc import FakePlc
from plc_vision_handshake.plc_logic import Event, State, StateChanged, Verdict, VerdictMade
from plc_vision_handshake.vision_client import VisionClient

pytestmark = pytest.mark.integration


def collect_verdicts(
    plc: FakePlc, vision: VisionClient, count: int, timeout_s: float = 5.0
) -> tuple[list[Verdict], list[Event]]:
    """Read PLC events until `count` verdicts arrived. No sleeps: blocks on the queue."""
    deadline = time.monotonic() + timeout_s
    verdicts: list[Verdict] = []
    events: list[Event] = []
    while len(verdicts) < count:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(
                f"got {len(verdicts)}/{count} verdicts within {timeout_s} s; events: {events}"
            )
        try:
            event = plc.events.get(timeout=min(remaining, 0.1))
        except queue.Empty:
            # A crashed loop should fail here with its own error, not as a timeout.
            plc.raise_if_failed()
            vision.raise_if_failed()
            continue
        events.append(event)
        if isinstance(event, VerdictMade):
            verdicts.append(event.verdict)
    return verdicts, events


def test_normal_ok_parts_are_accepted_over_snap7(plc: FakePlc, vision: VisionClient) -> None:
    for part_id in (101, 102, 103):
        plc.submit_part(part_id)

    verdicts, events = collect_verdicts(plc, vision, count=3)

    assert verdicts == [
        Verdict(seq_id=1, part_id=101, accepted=True),
        Verdict(seq_id=2, part_id=102, accepted=True),
        Verdict(seq_id=3, part_id=103, accepted=True),
    ]
    assert not any(isinstance(e, StateChanged) and e.new is State.FAULT for e in events)
