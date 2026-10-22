import pytest
from watcher import PlcWatcher

from plc_vision_handshake.fake_plc import FakePlc
from plc_vision_handshake.plc_logic import State, StateChanged, Verdict
from plc_vision_handshake.vision_client import VisionClient

pytestmark = pytest.mark.integration


def test_normal_ok_parts_are_accepted_over_snap7(
    plc: FakePlc, vision: VisionClient, watcher: PlcWatcher
) -> None:
    for part_id in (101, 102, 103):
        plc.submit_part(part_id)

    verdicts = [watcher.verdict() for _ in range(3)]

    assert verdicts == [
        Verdict(seq_id=1, part_id=101, accepted=True),
        Verdict(seq_id=2, part_id=102, accepted=True),
        Verdict(seq_id=3, part_id=103, accepted=True),
    ]
    assert not any(isinstance(e, StateChanged) and e.new is State.FAULT for e in watcher.seen)
