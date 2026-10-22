from plc_vision_handshake.eventlog import EventLog
from plc_vision_handshake.plc_logic import (
    RejectReason,
    State,
    StateChanged,
    Triggered,
    Verdict,
    VerdictMade,
)


def make_log(verbose: bool = False) -> EventLog:
    return EventLog(t0=100.0, result_timeout_s=1.0, verbose=verbose)


def test_trigger_line_has_time_source_event_and_seq_part() -> None:
    line = make_log().format(Triggered(100.021, seq_id=1, part_id=1001))
    assert line == "  0.021 s  PLC      trigger                seq 1   part 1001"


def test_reject_line_names_the_reason_in_code_and_in_words() -> None:
    log = make_log()
    stale = Verdict(seq_id=1, part_id=1001, accepted=False, reason=RejectReason.STALE_RESULT)
    timeout = Verdict(seq_id=2, part_id=1002, accepted=False, reason=RejectReason.TIMEOUT)
    not_ready = Verdict(seq_id=0, part_id=1003, accepted=False, reason=RejectReason.NOT_READY)

    assert log.format(VerdictMade(101.0, stale)) == (
        "  1.000 s  VERDICT  REJECT  STALE_RESULT   seq 1   part 1001"
        "    vision answered a different request"
    )
    assert str(log.format(VerdictMade(102.0, timeout))).endswith("no answer within 1 s")
    assert "seq -   part 1003" in str(log.format(VerdictMade(103.0, not_ready)))


def test_fault_and_reset_are_shown_but_routine_transitions_only_with_verbose() -> None:
    routine = StateChanged(100.5, State.TRIGGERED, State.WAIT_RESULT)
    assert make_log().format(routine) is None
    assert "TRIGGERED -> WAIT_RESULT" in str(make_log(verbose=True).format(routine))

    fault = make_log().format(StateChanged(100.5, State.WAIT_RESULT, State.FAULT))
    reset = make_log().format(StateChanged(101.0, State.FAULT, State.IDLE))
    assert "FAULT" in str(fault)
    assert "reset" in str(reset)


def test_summary_counts_verdicts_and_reasons() -> None:
    log = make_log()
    log.format(VerdictMade(100.1, Verdict(seq_id=1, part_id=1, accepted=True)))
    log.format(VerdictMade(100.2, Verdict(2, 2, False, RejectReason.NOK)))
    log.format(VerdictMade(100.3, Verdict(3, 3, False, RejectReason.NOK)))
    assert log.summary() == "3 parts: 1 accepted, 2 rejected (NOK: 2)"
