"""Test-only helpers for the integration tests."""

import threading
from collections.abc import Callable
from typing import Any

from plc_vision_handshake.vision_client import VisionClient

StartVision = Callable[..., VisionClient]  # the `start_vision` fixture


class GatedVisionClient(VisionClient):
    """A VisionClient whose loop doesn't poll until `gate` is set.

    start() still connects and does its first write (that happens in `_open`),
    so between start() and `gate.set()` DB11 shows exactly what that first write
    produced, with no loop poll that could have overwritten it.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.gate = threading.Event()

    def _run(self) -> None:
        while not self.gate.wait(0.01):
            if self._stop_requested.is_set():
                return  # stopped before the gate opened
        super()._run()
