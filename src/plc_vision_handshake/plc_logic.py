"""PLC side of the handshake as pure logic.

No sockets, no threads, no sleeping. Once per scan cycle the caller passes in
what the PLC read from DB11 and gets back what to write into DB10: the same
read inputs -> compute -> write outputs rhythm as an S7 OB1 cycle.

Time comes from an injected clock (monotonic seconds), so tests can step it
exactly instead of sleeping.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from plc_vision_handshake.protocol import (
    UINT16_MAX,
    UINT32_MAX,
    PlcToVision,
    Result,
    VisionToPlc,
)

Clock = Callable[[], float]


class State(Enum):
    IDLE = "IDLE"
    TRIGGERED = "TRIGGERED"
    WAIT_RESULT = "WAIT_RESULT"
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    FAULT = "FAULT"


class RejectReason(Enum):
    NOK = "NOK"
    VISION_ERROR = "VISION_ERROR"
    INVALID_RESULT = "INVALID_RESULT"
    TIMEOUT = "TIMEOUT"
    STALE_RESULT = "STALE_RESULT"
    NOT_READY = "NOT_READY"


_REJECT_REASON_FOR_RESULT = {
    Result.NOK: RejectReason.NOK,
    Result.ERROR: RejectReason.VISION_ERROR,
    Result.INVALID: RejectReason.INVALID_RESULT,
}


@dataclass(frozen=True)
class Verdict:
    """Final decision for one inspection request.

    ``seq_id`` is 0 when the request was rejected before a trigger was sent.
    """

    seq_id: int
    part_id: int
    accepted: bool
    reason: RejectReason | None = None  # None when accepted


@dataclass(frozen=True)
class PlcConfig:
    result_timeout_s: float = 1.0


class PlcLogic:
    def __init__(self, config: PlcConfig | None = None, clock: Clock = time.monotonic) -> None:
        self._config = config or PlcConfig()
        self._clock = clock
        self._state = State.IDLE
        self._heartbeat = 0
        self._seq_id = 0  # 0 = "no request yet"; real requests use 1..65535
        self._part_id = 0
        self._requested_part: int | None = None
        self._trigger_time = 0.0
        self._db11_at_trigger: tuple[int, Result] = (0, Result.NONE)
        self._saw_stale = False
        self.last_verdict: Verdict | None = None

    @property
    def state(self) -> State:
        return self._state

    def request_inspection(self, part_id: int) -> bool:
        """Latch a request to inspect ``part_id``. It is triggered on the next cycle.

        Returns False and changes nothing unless the PLC is IDLE with no request
        pending. The caller decides what happens to a part that was refused.
        """
        if not 0 <= part_id <= UINT32_MAX:
            raise ValueError(f"part_id={part_id} out of range 0..{UINT32_MAX}")
        if self._state is not State.IDLE or self._requested_part is not None:
            return False
        self._requested_part = part_id
        return True

    def cycle(self, inputs: VisionToPlc) -> PlcToVision:
        """Run one scan cycle: take DB11 as read, return DB10 to write."""
        now = self._clock()
        self._heartbeat = (self._heartbeat + 1) & UINT16_MAX

        if self._state is State.IDLE:
            if self._requested_part is not None:
                self._start_inspection(self._requested_part, inputs, now)
        elif self._state is State.TRIGGERED:
            # The trigger has been written once; from now on we wait for the answer.
            self._watch_for_stale(inputs)
            self._state = State.WAIT_RESULT
        elif self._state is State.WAIT_RESULT:
            self._watch_for_stale(inputs)
            self._evaluate_result(inputs, now)
        elif self._state in (State.ACCEPT, State.REJECT):
            self._state = State.IDLE

        return PlcToVision(
            heartbeat=self._heartbeat,
            # Level, not pulse: high for the whole request, low again in the
            # same cycle the verdict is made.
            trigger=self._state in (State.TRIGGERED, State.WAIT_RESULT),
            seq_id=self._seq_id,
            part_id=self._part_id,
        )

    def _start_inspection(self, part_id: int, inputs: VisionToPlc, now: float) -> None:
        self._requested_part = None
        if not inputs.ready:
            # Don't trigger a vision system that says it can't inspect. The part
            # can't wait on the table forever, so it is rejected right away.
            self.last_verdict = Verdict(
                seq_id=0, part_id=part_id, accepted=False, reason=RejectReason.NOT_READY
            )
            self._state = State.REJECT
            return
        # Wrap 65535 -> 1 and skip 0: DB11 starts all-zero, so seq 0 must never
        # match a real request.
        self._seq_id = self._seq_id + 1 if self._seq_id < UINT16_MAX else 1
        self._part_id = part_id
        self._trigger_time = now  # the result deadline counts from the trigger
        # What DB11 holds right now is the previous part's answer: expected, not stale.
        self._db11_at_trigger = (inputs.result_seq_id, inputs.result)
        self._saw_stale = False
        self._state = State.TRIGGERED

    def _watch_for_stale(self, inputs: VisionToPlc) -> None:
        # Stale = vision *wrote something new* during our wait, but for a different
        # request. Only used to name the reject reason; a stale result is never
        # evaluated, and a later matching result still counts.
        db11 = (inputs.result_seq_id, inputs.result)
        if db11 != self._db11_at_trigger and inputs.result_seq_id != self._seq_id:
            self._saw_stale = True

    def _evaluate_result(self, inputs: VisionToPlc, now: float) -> None:
        # Deadline first: a result read after the deadline is too late (fail-to-reject).
        if now - self._trigger_time >= self._config.result_timeout_s:
            self._finish(RejectReason.STALE_RESULT if self._saw_stale else RejectReason.TIMEOUT)
            return
        if inputs.result_seq_id != self._seq_id or inputs.result is Result.NONE:
            return  # no answer for *this* request yet
        if inputs.result is Result.OK:
            self._finish(None)
        else:
            self._finish(_REJECT_REASON_FOR_RESULT[inputs.result])

    def _finish(self, reason: RejectReason | None) -> None:
        self.last_verdict = Verdict(
            seq_id=self._seq_id,
            part_id=self._part_id,
            accepted=reason is None,
            reason=reason,
        )
        self._state = State.ACCEPT if reason is None else State.REJECT
