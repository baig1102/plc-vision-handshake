"""Background loop with a clean, checked shutdown.

Both the fake PLC and the vision client are "do one step, wait, repeat" loops
on a thread. The start/stop/error handling lives here once:

* ``stop()`` takes effect within one period (``Event.wait``, not ``sleep``).
* A thread that doesn't stop in time raises instead of hanging silently.
* An exception inside the loop ends the loop, is stored, and is re-raised by
  ``stop()`` / ``raise_if_failed()``, so a crash can't look like a timeout.
"""

from __future__ import annotations

import threading
from types import TracebackType
from typing import Self


class CyclicWorker:
    name = "worker"

    def __init__(self, period_s: float) -> None:
        self._period_s = period_s
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self.error: BaseException | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError(f"{self.name} already started")
        self._open()
        # daemon=True is only a last-resort net for a hung thread at interpreter
        # exit; stop() is what actually ends the thread.
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()

    def stop(self, timeout_s: float = 2.0) -> None:
        self._stop_requested.set()
        if self._thread is not None:
            self._thread.join(timeout_s)
            if self._thread.is_alive():
                raise RuntimeError(f"{self.name} thread did not stop within {timeout_s} s")
        self._close()
        self.raise_if_failed()

    def raise_if_failed(self) -> None:
        if self.error is not None:
            raise RuntimeError(f"{self.name} loop crashed") from self.error

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    def _run(self) -> None:
        try:
            while not self._stop_requested.is_set():
                self._step()
                self._stop_requested.wait(self._period_s)
        except BaseException as exc:
            self.error = exc

    # --- hooks for subclasses ---------------------------------------------------
    def _open(self) -> None:
        """Acquire resources (runs in the caller's thread, before the loop)."""

    def _step(self) -> None:
        raise NotImplementedError

    def _close(self) -> None:
        """Release resources (runs in the caller's thread, after the loop ended)."""
