"""Read PLC events against a deadline. Shared by the CLI and the integration tests.

Every wait blocks on the event queue in short slices (0.1 s), never sleeps, and
checks the workers for crashes in between, so a crashed loop fails with its own
error instead of looking like a timeout. The short slices also keep the main
thread responsive to Ctrl+C on Windows.
"""

from __future__ import annotations

import queue
import time
from collections.abc import Callable

from plc_vision_handshake.fake_plc import FakePlc
from plc_vision_handshake.plc_logic import Event, Verdict, VerdictMade
from plc_vision_handshake.worker import CyclicWorker

WAIT_TIMEOUT_S = 5.0
_SLICE_S = 0.1


class PlcWatcher:
    def __init__(self, plc: FakePlc, on_event: Callable[[Event], None] | None = None) -> None:
        self.plc = plc
        self.seen: list[Event] = []
        self.workers: list[CyclicWorker] = [plc]
        self._on_event = on_event

    def wait_for(
        self, predicate: Callable[[Event], bool], what: str, timeout_s: float = WAIT_TIMEOUT_S
    ) -> Event:
        """Read events until one matches ``predicate``; return it."""
        deadline = time.monotonic() + timeout_s
        while True:
            event = self._next(deadline, what, timeout_s)
            if event is not None and predicate(event):
                return event

    def verdict(self, timeout_s: float = WAIT_TIMEOUT_S) -> Verdict:
        event = self.wait_for(lambda e: isinstance(e, VerdictMade), "verdict", timeout_s)
        assert isinstance(event, VerdictMade)
        return event.verdict

    def pump_until(
        self, check: Callable[[], bool], what: str, timeout_s: float = WAIT_TIMEOUT_S
    ) -> None:
        """Keep reading events until ``check()`` is true (for conditions that aren't
        PLC events, e.g. "vision crashed")."""
        deadline = time.monotonic() + timeout_s
        while not check():
            self._next(deadline, what, timeout_s)

    def _next(self, deadline: float, what: str, timeout_s: float) -> Event | None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"no {what} within {timeout_s} s; events: {self.seen}")
        try:
            event = self.plc.events.get(timeout=min(remaining, _SLICE_S))
        except queue.Empty:
            for worker in self.workers:
                worker.raise_if_failed()
            return None
        self.seen.append(event)
        if self._on_event is not None:
            self._on_event(event)
        return event
