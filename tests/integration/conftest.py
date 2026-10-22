import contextlib
from collections.abc import Iterator
from typing import Any

import pytest
from watcher import PlcWatcher, StartVision

from plc_vision_handshake.fake_plc import FakePlc
from plc_vision_handshake.plc_logic import PlcConfig
from plc_vision_handshake.protocol import Result
from plc_vision_handshake.vision_client import VisionClient

# Wider watchdog window than the default 0.5 s: vision has to connect within it
# from startup, and CI runners can be slow.
CONFIG = PlcConfig(result_timeout_s=1.0, watchdog_timeout_s=1.0)
STARTUP_TIMEOUT_S = 5.0


def always_ok(part_id: int) -> Result:
    return Result.OK


@pytest.fixture
def plc() -> Iterator[FakePlc]:
    fake_plc = FakePlc(config=CONFIG)  # free port on 127.0.0.1
    fake_plc.start()
    try:
        yield fake_plc
    finally:
        fake_plc.stop()  # re-raises if the PLC loop crashed during the test


@pytest.fixture
def watcher(plc: FakePlc) -> PlcWatcher:
    return PlcWatcher(plc)


@pytest.fixture
def start_vision(plc: FakePlc, watcher: PlcWatcher) -> Iterator[StartVision]:
    """Factory: start a VisionClient on the PLC's port (always OK, 20 ms by default)
    and wait until the PLC has seen ready=True, so a part submitted right after
    can't get NOT_READY.

    Depends on `plc`, so pytest tears it down first: every client disconnects
    while the server is still there. ExitStack stops all of them even if one
    stop() raises.
    """
    with contextlib.ExitStack() as stack:

        def start(**kwargs: Any) -> VisionClient:
            kwargs.setdefault("inspect", always_ok)
            kwargs.setdefault("latency_s", 0.02)
            client = VisionClient(plc.port, **kwargs)
            client.start()
            stack.callback(client.stop)
            watcher.workers.append(client)
            plc.wait_until_vision_ready(STARTUP_TIMEOUT_S)
            return client

        yield start


@pytest.fixture
def vision(start_vision: StartVision) -> VisionClient:
    return start_vision()
