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
from typing import TypeAlias

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
    WATCHDOG = "WATCHDOG"


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
class StateChanged:
    time: float
    old: State
    new: State


@dataclass(frozen=True)
class VerdictMade:
    time: float
    verdict: Verdict


Event: TypeAlias = StateChanged | VerdictMade


@dataclass(frozen=True)
class PlcConfig:
    result_timeout_s: float = 1.0
    watchdog_timeout_s: float = 0.5


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
        self._last_echo: int | None = None
        self._echo_changed_at = clock()  # vision gets one watchdog window from startup
        self._events: list[Event] = []
        self.last_verdict: Verdict | None = None

    @property
    def state(self) -> State:
        return self._state

    def request_inspection(self, part_id: int) -> bool:
        """Latch a request to inspect ``part_id``. It is triggered on the next cycle.

        Returns False and changes nothing unless the PLC is IDLE with no request
        pending. That includes FAULT: no new triggers until ``reset()``. The
        caller decides what happens to a part that was refused.

        Every request that returns True gets exactly one ``Verdict``.
        """
        if not 0 <= part_id <= UINT32_MAX:
            raise ValueError(f"part_id={part_id} out of range 0..{UINT32_MAX}")
        if self._state is not State.IDLE or self._requested_part is not None:
            return False
        self._requested_part = part_id
        return True

    def reset(self) -> bool:
        """Operator acknowledge: leave FAULT and give vision a fresh watchdog window.

        ``seq_id`` is deliberately kept, so whatever result is still in DB11 from
        before the fault can never match the next request. If the heartbeat echo
        is still frozen, the watchdog faults again after one window.
        Returns False if the PLC was not in FAULT.
        """
        if self._state is not State.FAULT:
            return False
        now = self._clock()
        self._echo_changed_at = now
        self._state = State.IDLE
        self._events.append(StateChanged(now, State.FAULT, State.IDLE))
        return True

    def drain_events(self) -> list[Event]:
        """Return everything that happened since the last call, oldest first.

        This is the only place a verdict is guaranteed to show up: a WATCHDOG
        verdict goes straight to FAULT and never passes through REJECT. The
        caller must drain regularly (the fake PLC does it every cycle);
        otherwise the list keeps growing.
        """
        events, self._events = self._events, []
        return events

    def cycle(self, inputs: VisionToPlc) -> PlcToVision:
        """Run one scan cycle: take DB11 as read, return DB10 to write."""
        now = self._clock()
        old_state = self._state
        self._heartbeat = (self._heartbeat + 1) & UINT16_MAX

        # Watchdog before anything else: if vision is dead, a result read in the
        # same cycle can't be trusted, so FAULT/WATCHDOG wins over it.
        self._check_watchdog(inputs, now)

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

        # At most one transition per cycle, so comparing start and end is enough.
        # Any verdict event from this cycle is already in the list, before it.
        if self._state is not old_state:
            self._events.append(StateChanged(now, old_state, self._state))

        return PlcToVision(
            heartbeat=self._heartbeat,
            # Level, not pulse: high for the whole request, low again in the
            # same cycle the verdict is made.
            trigger=self._state in (State.TRIGGERED, State.WAIT_RESULT),
            seq_id=self._seq_id,
            part_id=self._part_id,
        )

    def _check_watchdog(self, inputs: VisionToPlc, now: float) -> None:
        # The echo always lags our heartbeat by at least a cycle, so we don't
        # compare it to the heartbeat: we only require that it keeps *changing*.
        if inputs.heartbeat_echo != self._last_echo:
            self._last_echo = inputs.heartbeat_echo
            self._echo_changed_at = now
        if self._state is State.FAULT:
            return
        if now - self._echo_changed_at >= self._config.watchdog_timeout_s:
            self._enter_fault(now)

    def _enter_fault(self, now: float) -> None:
        if self._state in (State.TRIGGERED, State.WAIT_RESULT):
            self._record_verdict(now, self._seq_id, self._part_id, RejectReason.WATCHDOG)
        elif self._requested_part is not None:  # accepted but never triggered
            self._record_verdict(now, 0, self._requested_part, RejectReason.WATCHDOG)
        self._requested_part = None
        self._state = State.FAULT

    def _start_inspection(self, part_id: int, inputs: VisionToPlc, now: float) -> None:
        self._requested_part = None
        if not inputs.ready:
            # Don't trigger a vision system that says it can't inspect. The part
            # can't wait on the table forever, so it is rejected right away.
            self._record_verdict(now, 0, part_id, RejectReason.NOT_READY)
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
        # Stale = vision *wrote a result* during our wait, but for a different
        # request. A cleared DB11 (e.g. after a vision restart) carries no result,
        # so it is not stale. Only used to name the reject reason; a stale result
        # is never evaluated, and a later matching result still counts.
        db11 = (inputs.result_seq_id, inputs.result)
        if (
            db11 != self._db11_at_trigger
            and inputs.result_seq_id != self._seq_id
            and inputs.result is not Result.NONE
        ):
            self._saw_stale = True

    def _evaluate_result(self, inputs: VisionToPlc, now: float) -> None:
        # Deadline first: a result read after the deadline is too late (fail-to-reject).
        if now - self._trigger_time >= self._config.result_timeout_s:
            reason = RejectReason.STALE_RESULT if self._saw_stale else RejectReason.TIMEOUT
            self._finish(now, reason)
            return
        if inputs.result_seq_id != self._seq_id or inputs.result is Result.NONE:
            return  # no answer for *this* request yet
        if inputs.result is Result.OK:
            self._finish(now, None)
        else:
            self._finish(now, _REJECT_REASON_FOR_RESULT[inputs.result])

    def _finish(self, now: float, reason: RejectReason | None) -> None:
        self._record_verdict(now, self._seq_id, self._part_id, reason)
        self._state = State.ACCEPT if reason is None else State.REJECT

    def _record_verdict(
        self, now: float, seq_id: int, part_id: int, reason: RejectReason | None
    ) -> None:
        verdict = Verdict(seq_id=seq_id, part_id=part_id, accepted=reason is None, reason=reason)
        self.last_verdict = verdict
        self._events.append(VerdictMade(now, verdict))
