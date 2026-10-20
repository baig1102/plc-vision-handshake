from dataclasses import replace

import pytest

from plc_vision_handshake.plc_logic import (
    PlcConfig,
    PlcLogic,
    RejectReason,
    State,
    Verdict,
)
from plc_vision_handshake.protocol import PlcToVision, Result, VisionToPlc

CYCLE_S = 0.25  # exact in binary floating point, so time comparisons stay exact


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Rig:
    """The PLC logic plus a scripted vision side that echoes the heartbeat."""

    def __init__(self, config: PlcConfig | None = None) -> None:
        self.clock = FakeClock()
        self.plc = PlcLogic(config, clock=self.clock)
        self.vision = VisionToPlc(heartbeat_echo=0, result_seq_id=0, result=Result.NONE, ready=True)
        self.out = PlcToVision(heartbeat=0, trigger=False, seq_id=0, part_id=0)

    def cycle(self) -> PlcToVision:
        self.clock.now += CYCLE_S
        self.out = self.plc.cycle(self.vision)
        self.vision = replace(self.vision, heartbeat_echo=self.out.heartbeat)
        return self.out

    def state(self) -> State:
        # A method, not a property: mypy would otherwise keep a narrowed
        # `rig.plc.state` from an earlier assert across later cycles.
        return self.plc.state

    def answer(self, result: Result, seq_id: int | None = None) -> None:
        """Vision writes a result, by default for the request currently on DB10."""
        seq = self.out.seq_id if seq_id is None else seq_id
        self.vision = replace(self.vision, result_seq_id=seq, result=result)

    def trigger(self, part_id: int = 100) -> None:
        """Request an inspection and run cycles until the PLC waits for a result."""
        assert self.plc.request_inspection(part_id)
        self.cycle()
        self.cycle()
        assert self.state() is State.WAIT_RESULT


def test_heartbeat_increments_every_cycle_and_wraps() -> None:
    rig = Rig()
    assert [rig.cycle().heartbeat for _ in range(3)] == [1, 2, 3]
    for _ in range(0xFFFF - 3):
        rig.cycle()
    assert rig.out.heartbeat == 0xFFFF
    assert rig.cycle().heartbeat == 0


def test_ok_result_is_accepted_and_trigger_follows_states() -> None:
    rig = Rig()
    assert rig.plc.request_inspection(part_id=4711)

    out = rig.cycle()
    assert rig.state() is State.TRIGGERED
    assert (out.trigger, out.seq_id, out.part_id) == (True, 1, 4711)

    assert rig.cycle().trigger
    assert rig.state() is State.WAIT_RESULT

    rig.answer(Result.OK)
    out = rig.cycle()
    assert rig.state() is State.ACCEPT
    assert out.trigger is False  # dropped in the verdict cycle itself
    assert rig.plc.last_verdict == Verdict(seq_id=1, part_id=4711, accepted=True)

    rig.cycle()
    assert rig.state() is State.IDLE


@pytest.mark.parametrize(
    ("result", "reason"),
    [
        (Result.NOK, RejectReason.NOK),
        (Result.ERROR, RejectReason.VISION_ERROR),
        (Result.INVALID, RejectReason.INVALID_RESULT),
    ],
)
def test_non_ok_results_are_rejected(result: Result, reason: RejectReason) -> None:
    rig = Rig()
    rig.trigger(part_id=7)
    rig.answer(result)
    out = rig.cycle()
    assert rig.state() is State.REJECT
    assert out.trigger is False
    assert rig.plc.last_verdict == Verdict(seq_id=1, part_id=7, accepted=False, reason=reason)


def test_none_result_for_our_seq_keeps_waiting() -> None:
    rig = Rig()
    rig.trigger()
    rig.answer(Result.NONE)
    rig.cycle()
    assert rig.state() is State.WAIT_RESULT


def test_no_result_times_out_exactly_at_deadline() -> None:
    rig = Rig(PlcConfig(result_timeout_s=1.0))
    rig.plc.request_inspection(part_id=1)
    rig.cycle()  # TRIGGERED: the deadline starts here
    triggered_at = rig.clock.now

    while rig.clock.now + CYCLE_S < triggered_at + 1.0:
        rig.cycle()
        assert rig.state() is State.WAIT_RESULT  # 0.75 s after trigger: still waiting

    rig.cycle()  # exactly 1.0 s after trigger
    assert rig.state() is State.REJECT
    assert rig.plc.last_verdict is not None
    assert rig.plc.last_verdict.reason is RejectReason.TIMEOUT


def test_previous_parts_result_is_never_accepted() -> None:
    rig = Rig()
    rig.trigger()
    rig.answer(Result.OK)
    rig.cycle()  # seq 1 accepted
    rig.cycle()  # back to IDLE; DB11 still says (seq 1, OK)

    rig.trigger()  # seq 2
    for _ in range(10):  # bounded, so a broken timeout fails instead of hanging
        if rig.state() is not State.WAIT_RESULT:
            break
        rig.cycle()  # the old (seq 1, OK) is on DB11 every cycle; it must never count
    assert rig.plc.last_verdict == Verdict(
        seq_id=2, part_id=100, accepted=False, reason=RejectReason.TIMEOUT
    )


def test_request_is_refused_while_busy() -> None:
    rig = Rig()
    assert rig.plc.request_inspection(part_id=1)
    assert not rig.plc.request_inspection(part_id=2)  # one already pending
    rig.cycle()
    assert not rig.plc.request_inspection(part_id=2)  # TRIGGERED
    rig.cycle()
    assert not rig.plc.request_inspection(part_id=2)  # WAIT_RESULT
    rig.answer(Result.OK)
    rig.cycle()
    assert not rig.plc.request_inspection(part_id=2)  # ACCEPT
    rig.cycle()
    assert rig.plc.request_inspection(part_id=2)  # IDLE again


def test_seq_id_wraps_to_one_and_skips_zero() -> None:
    rig = Rig()
    # Set the counter directly: walking 65 535 real inspections would only slow the test.
    rig.plc._seq_id = 0xFFFF
    rig.plc.request_inspection(part_id=1)
    assert rig.cycle().seq_id == 1
