from collections.abc import Iterator

import pytest

from plc_vision_handshake.fake_plc import FakePlc
from plc_vision_handshake.plc_logic import PlcConfig
from plc_vision_handshake.protocol import Result
from plc_vision_handshake.vision_client import VisionClient

# Wider watchdog window than the default 0.5 s: vision has to connect within it
# from startup, and CI runners can be slow.
CONFIG = PlcConfig(result_timeout_s=1.0, watchdog_timeout_s=1.0)
STARTUP_TIMEOUT_S = 5.0


@pytest.fixture
def plc() -> Iterator[FakePlc]:
    fake_plc = FakePlc(config=CONFIG)  # free port on 127.0.0.1
    fake_plc.start()
    try:
        yield fake_plc
    finally:
        fake_plc.stop()  # re-raises if the PLC loop crashed during the test


@pytest.fixture
def vision(plc: FakePlc) -> Iterator[VisionClient]:
    # Depends on `plc`, so pytest tears it down first: the client disconnects
    # while the server is still there.
    client = VisionClient(plc.port, inspect=lambda part_id: Result.OK, latency_s=0.02)
    client.start()
    try:
        # Submitting parts before the PLC has seen ready=True would give NOT_READY.
        plc.wait_until_vision_ready(STARTUP_TIMEOUT_S)
        yield client
    finally:
        client.stop()
