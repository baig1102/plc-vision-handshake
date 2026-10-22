"""Turn PLC events into log lines an operator can read without knowing the code.

Every line: time since start, source, event, seq/part, and the reason in plain
words. The RejectReason name stays in the line, so log and code can be matched.
Pure formatting: no I/O, no clock.
"""

from __future__ import annotations

from collections import Counter

from plc_vision_handshake.plc_logic import (
    Event,
    RejectReason,
    State,
    Triggered,
    Verdict,
    VerdictMade,
)

_REASON_TEXT = {
    RejectReason.NOK: "inspection found a defect",
    RejectReason.VISION_ERROR: "vision reported an error",
    RejectReason.INVALID_RESULT: "unknown result code",
    RejectReason.TIMEOUT: "no answer within {timeout:g} s",
    RejectReason.STALE_RESULT: "vision answered a different request",
    RejectReason.NOT_READY: "vision not ready",
    RejectReason.WATCHDOG: "vision heartbeat stopped",
}


class EventLog:
    def __init__(self, t0: float, result_timeout_s: float, verbose: bool = False) -> None:
        self._t0 = t0
        self._result_timeout_s = result_timeout_s
        self._verbose = verbose
        self.verdicts: list[Verdict] = []

    def line(
        self,
        time: float,
        source: str,
        text: str,
        seq_id: int | None = None,
        part_id: int | None = None,
        note: str = "",
    ) -> str:
        where = ""
        if part_id is not None:
            seq = str(seq_id) if seq_id else "-"  # seq 0 = never sent to vision
            where = f"seq {seq:<3} part {part_id}"
        return f"{time - self._t0:7.3f} s  {source:<8} {text:<22} {where:<20} {note}".rstrip()

    def format(self, event: Event) -> str | None:
        """One log line for the event, or None if it's only shown with verbose."""
        if isinstance(event, Triggered):
            return self.line(event.time, "PLC", "trigger", event.seq_id, event.part_id)
        if isinstance(event, VerdictMade):
            v = event.verdict
            self.verdicts.append(v)
            if v.accepted:
                return self.line(event.time, "VERDICT", "ACCEPT", v.seq_id, v.part_id)
            assert v.reason is not None
            note = _REASON_TEXT[v.reason].format(timeout=self._result_timeout_s)
            return self.line(
                event.time, "VERDICT", f"REJECT  {v.reason.value}", v.seq_id, v.part_id, note
            )
        if event.new is State.FAULT:
            return self.line(event.time, "PLC", "FAULT", note="no new triggers until reset")
        if (event.old, event.new) == (State.FAULT, State.IDLE):
            return self.line(event.time, "PLC", "reset", note="FAULT acknowledged")
        if self._verbose:
            return self.line(event.time, "PLC", f"{event.old.value} -> {event.new.value}")
        return None

    def summary(self) -> str:
        n = len(self.verdicts)
        accepted = sum(v.accepted for v in self.verdicts)
        reasons = Counter(v.reason.value for v in self.verdicts if v.reason is not None)
        text = f"{n} part{'s' if n != 1 else ''}: {accepted} accepted, {n - accepted} rejected"
        if reasons:
            text += " (" + ", ".join(f"{r}: {c}" for r, c in sorted(reasons.items())) + ")"
        return text
