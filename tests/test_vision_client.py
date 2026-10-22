from plc_vision_handshake.protocol import PlcToVision, Result
from plc_vision_handshake.vision_client import VisionFaults, VisionLogic, random_inspector


class CountingInspector:
    def __init__(self, result: Result = Result.OK) -> None:
        self.calls: list[int] = []
        self._result = result

    def __call__(self, part_id: int) -> Result:
        self.calls.append(part_id)
        return self._result


def db10(heartbeat: int, trigger: bool, seq_id: int, part_id: int = 0) -> PlcToVision:
    return PlcToVision(heartbeat=heartbeat, trigger=trigger, seq_id=seq_id, part_id=part_id)


def test_each_seq_id_is_answered_exactly_once() -> None:
    inspector = CountingInspector()
    vision = VisionLogic(inspector, latency_s=0.0)

    # The PLC holds the trigger high with the same seq for many polls
    # (e.g. it hasn't read the result yet). Only one inspection may run.
    for hb in range(1, 21):
        out = vision.step(db10(hb, trigger=True, seq_id=1, part_id=42), now=hb * 0.25)
        assert (out.result_seq_id, out.result) == (1, Result.OK)
    assert inspector.calls == [42]

    vision.step(db10(21, trigger=False, seq_id=1), now=5.25)
    vision.step(db10(22, trigger=True, seq_id=2, part_id=43), now=5.5)
    assert inspector.calls == [42, 43]  # a new seq is a new inspection


def test_heartbeat_echo_keeps_running_during_inspection() -> None:
    vision = VisionLogic(CountingInspector(), latency_s=0.5)

    out = vision.step(db10(1, trigger=True, seq_id=1), now=0.0)  # inspection starts
    assert (out.heartbeat_echo, out.result) == (1, Result.NONE)
    out = vision.step(db10(2, trigger=True, seq_id=1), now=0.25)  # still busy
    assert (out.heartbeat_echo, out.result) == (2, Result.NONE)
    out = vision.step(db10(3, trigger=True, seq_id=1), now=0.5)  # latency elapsed
    assert (out.heartbeat_echo, out.result_seq_id, out.result) == (3, 1, Result.OK)


def test_random_inspector_is_repeatable_with_a_seed() -> None:
    a, b = random_inspector(seed=7), random_inspector(seed=7)
    assert [a(i) for i in range(50)] == [b(i) for i in range(50)]


def test_frozen_plc_heartbeat_drops_ready_and_blocks_new_inspections() -> None:
    inspector = CountingInspector()
    vision = VisionLogic(inspector, latency_s=0.0, plc_watchdog_s=0.5)

    assert vision.step(db10(1, trigger=False, seq_id=0), now=0.0).ready  # window starts
    assert vision.step(db10(1, trigger=False, seq_id=0), now=0.25).ready

    out = vision.step(db10(1, trigger=True, seq_id=1, part_id=9), now=0.5)  # 0.5 s frozen
    assert out.ready is False
    assert (out.result_seq_id, out.result) == (0, Result.NONE)
    assert inspector.calls == []  # no new inspection while the PLC looks dead

    out = vision.step(db10(2, trigger=True, seq_id=1, part_id=9), now=0.75)  # PLC is back
    assert out.ready is True
    assert inspector.calls == [9]


def test_stale_seq_fault_answers_with_the_previous_seq_id() -> None:
    vision = VisionLogic(CountingInspector(), latency_s=0.0, faults=VisionFaults(stale_seq=True))
    out = vision.step(db10(1, trigger=True, seq_id=5), now=0.0)
    assert (out.result_seq_id, out.result) == (4, Result.OK)


def test_freeze_heartbeat_fault_hangs_at_the_first_inspection() -> None:
    inspector = CountingInspector()
    vision = VisionLogic(inspector, latency_s=0.0, faults=VisionFaults(freeze_heartbeat=True))
    assert vision.step(db10(1, trigger=False, seq_id=0), now=0.0).heartbeat_echo == 1

    frozen = vision.step(db10(2, trigger=True, seq_id=1), now=0.25)  # hangs here
    for hb in range(3, 10):
        assert vision.step(db10(hb, trigger=True, seq_id=1), now=hb * 0.25) == frozen
    assert frozen.heartbeat_echo == 2
    assert (frozen.result_seq_id, frozen.result) == (0, Result.NONE)  # never answers
    assert inspector.calls == []
