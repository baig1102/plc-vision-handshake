import signal
import subprocess
import sys
import threading

import pytest

from plc_vision_handshake import cli
from plc_vision_handshake.vision_client import VisionFaults

# For each scenario: lines that must appear in this order (each line contains all
# of its fragments), fragments that must not appear, and the summary line.
ExpectedOutput = tuple[list[tuple[str, ...]], list[str], str]
SCENARIO_OUTPUT: dict[str, ExpectedOutput] = {
    "normal": (
        [("VERDICT", "ACCEPT", "seq 1", "part 1001"), ("VERDICT", "ACCEPT", "seq 3", "part 1003")],
        ["REJECT", "FAULT"],
        "3 parts: 3 accepted, 0 rejected",
    ),
    "nok": (
        [
            ("VERDICT", "ACCEPT", "seq 1", "part 1001"),
            ("REJECT  NOK", "seq 2", "part 1002", "inspection found a defect"),
        ],
        ["FAULT"],
        "4 parts: 2 accepted, 2 rejected (NOK: 2)",
    ),
    "slow": (
        [("REJECT  TIMEOUT", "seq 1", "part 1001", "no answer within 1 s")],
        ["FAULT", "WATCHDOG"],
        "1 part: 0 accepted, 1 rejected (TIMEOUT: 1)",
    ),
    "stale": (
        [("REJECT  STALE_RESULT", "seq 1", "part 1001", "vision answered a different request")],
        ["ACCEPT", "FAULT"],
        "1 part: 0 accepted, 1 rejected (STALE_RESULT: 1)",
    ),
    "freeze": (
        [
            ("REJECT  WATCHDOG", "seq 1", "part 1001", "vision heartbeat stopped"),
            ("PLC", "FAULT", "no new triggers until reset"),
        ],
        ["ACCEPT"],
        "1 part: 0 accepted, 1 rejected (WATCHDOG: 1)",
    ),
    "restart": (
        [
            ("VERDICT", "ACCEPT", "seq 1", "part 1001"),
            ("VISION", "crashed"),
            ("PLC", "FAULT"),
            ("OPERATOR", "reset"),
            ("PLC", "reset", "FAULT acknowledged"),
            ("VISION", "connected, ready", "new instance"),
            ("VERDICT", "ACCEPT", "seq 2", "part 1002"),
        ],
        ["REJECT"],
        "2 parts: 2 accepted, 0 rejected",
    ),
    "plc-stop": (
        [
            ("VERDICT", "ACCEPT", "seq 1", "part 1001"),
            ("PLC", "halted"),
            ("VISION", "PLC heartbeat lost", "ready=False"),
        ],
        ["REJECT", "FAULT"],
        "1 part: 1 accepted, 0 rejected",
    ),
}


def test_every_scenario_has_an_output_test() -> None:
    assert SCENARIO_OUTPUT.keys() == cli.SCENARIOS.keys()


@pytest.mark.integration
@pytest.mark.parametrize("scenario", SCENARIO_OUTPUT)
def test_demo_scenario_output(scenario: str, capsys: pytest.CaptureFixture[str]) -> None:
    expected_lines, absent, summary = SCENARIO_OUTPUT[scenario]

    code = cli.main(["demo", "--scenario", scenario, "--port", "0"])

    out, err = capsys.readouterr()
    assert (code, err) == (0, "")  # 0 even with rejects: that's the expected outcome
    lines = out.splitlines()
    position = -1
    for fragments in expected_lines:
        matches = [i for i, line in enumerate(lines) if all(f in line for f in fragments)]
        assert matches, f"no line with {fragments} in:\n{out}"
        assert matches[0] > position, f"{fragments} out of order in:\n{out}"
        position = matches[0]
    for fragment in absent:
        assert fragment not in out
    assert lines[-1] == summary


def test_unknown_scenario_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["demo", "--scenario", "nope"])
    assert exit_info.value.code == 2


def test_vision_command_maps_fault_flags() -> None:
    args = cli.build_parser().parse_args(
        ["vision", "--port", "1103", "--slow", "--stale-seq", "--crash-after", "2"]
    )
    assert args.port == 1103
    assert cli.faults_from_args(args) == VisionFaults(slow=True, stale_seq=True, crash_after=2)


def worker_threads_alive() -> list[str]:
    return [t.name for t in threading.enumerate() if t.name in ("fake-plc", "vision")]


@pytest.mark.integration
def test_ctrl_c_stops_plc_and_vision_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupted(demo: cli.Demo, count: int) -> None:
        raise KeyboardInterrupt  # as if Ctrl+C hit while waiting for a verdict

    monkeypatch.setattr(cli.Demo, "run_parts", interrupted)

    code = cli.main(["demo", "--scenario", "normal", "--port", "0"])

    out, err = capsys.readouterr()
    assert code == 130
    assert "interrupted, PLC and vision stopped" in out
    assert err == ""  # no traceback
    assert worker_threads_alive() == []


@pytest.mark.integration
@pytest.mark.skipif(
    sys.platform == "win32", reason="Ctrl+C can't be sent reliably to a child process on Windows"
)
def test_real_sigint_stops_the_demo_cleanly() -> None:
    proc = subprocess.Popen(
        [sys.executable, "-m", "plc_vision_handshake", "demo", "--scenario", "slow", "--port", "0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        for _ in range(10):  # wait until a part is in flight
            if "trigger" in proc.stdout.readline():
                break
        else:
            pytest.fail("demo never triggered a part")
        proc.send_signal(signal.SIGINT)
        out, err = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 130
    assert "interrupted, PLC and vision stopped" in out
    assert "Traceback" not in err
