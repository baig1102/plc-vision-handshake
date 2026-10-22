import signal
import subprocess
import sys
import threading

import pytest

from plc_vision_handshake import cli


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
