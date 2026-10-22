import queue
import time
from collections.abc import Callable

from plc_vision_handshake.fake_plc import FakePlc
from plc_vision_handshake.plc_logic import Event, Verdict, VerdictMade
from plc_vision_handshake.worker import CyclicWorker

WAIT_TIMEOUT_S = 5.0


class PlcWatcher:
    """Waits for PLC events against a deadline. Blocks on the queue, never sleeps."""

    def __init__(self, plc: FakePlc) -> None:
        self.plc = plc
        self.seen: list[Event] = []
        self.workers: list[CyclicWorker] = [plc]

    def wait_for(
        self, predicate: Callable[[Event], bool], what: str, timeout_s: float = WAIT_TIMEOUT_S
    ) -> Event:
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"no {what} within {timeout_s} s; events: {self.seen}")
            try:
                event = self.plc.events.get(timeout=min(remaining, 0.1))
            except queue.Empty:
                # A crashed loop should fail here with its own error, not as a timeout.
                for worker in self.workers:
                    worker.raise_if_failed()
                continue
            self.seen.append(event)
            if predicate(event):
                return event

    def verdict(self, timeout_s: float = WAIT_TIMEOUT_S) -> Verdict:
        event = self.wait_for(lambda e: isinstance(e, VerdictMade), "verdict", timeout_s)
        assert isinstance(event, VerdictMade)
        return event.verdict
