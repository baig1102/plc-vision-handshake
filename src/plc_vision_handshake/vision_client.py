"""Vision side of the handshake: a snap7 client that answers inspection triggers.

Split like the PLC side: ``VisionLogic`` is pure (DB10 in, DB11 out, injected
time) and ``VisionClient`` only moves bytes over the S7 connection.
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import snap7

from plc_vision_handshake import protocol
from plc_vision_handshake.plc_logic import Clock
from plc_vision_handshake.protocol import PlcToVision, Result, VisionToPlc
from plc_vision_handshake.worker import CyclicWorker

Inspector = Callable[[int], Result]  # part_id -> result

SLOW_LATENCY_S = 2.0  # longer than the default 1.0 s PLC result timeout


@dataclass(frozen=True)
class VisionFaults:
    """Fault injection for the demo scenarios. All off by default."""

    slow: bool = False  # inspect for SLOW_LATENCY_S instead of latency_s
    stale_seq: bool = False  # answer with the previous seq_id instead of the current one
    freeze_heartbeat: bool = False  # hang at the first inspection: echo stops, no result
    crash_after: int | None = None  # after N answers, disconnect as if the process died


def random_inspector(seed: int | None = None, nok_rate: float = 0.2) -> Inspector:
    """OK/NOK at random; pass a seed for a repeatable sequence."""
    rng = random.Random(seed)

    def inspect(part_id: int) -> Result:
        return Result.NOK if rng.random() < nok_rate else Result.OK

    return inspect


class VisionLogic:
    def __init__(
        self,
        inspect: Inspector,
        latency_s: float = 0.05,
        plc_watchdog_s: float = 0.5,
        faults: VisionFaults | None = None,
    ) -> None:
        self._faults = faults or VisionFaults()
        self._inspect = inspect
        self._latency_s = SLOW_LATENCY_S if self._faults.slow else latency_s
        self._plc_watchdog_s = plc_watchdog_s
        self._last_plc_heartbeat: int | None = None
        self._plc_heartbeat_changed_at = 0.0
        self.plc_alive = True
        self._last_started_seq = 0  # PLC never uses seq 0 for a real request
        self._busy_seq: int | None = None
        self._busy_part = 0
        self._done_at = 0.0
        self._result_seq_id = 0
        self._result = Result.NONE
        self._hung_output: VisionToPlc | None = None
        self.answered = 0

    def step(self, db10: PlcToVision, now: float) -> VisionToPlc:
        """One poll: take DB10 as read, return DB11 to write."""
        if self._hung_output is not None:
            return self._hung_output  # a hung process keeps showing its last state
        # Second half of the bidirectional watchdog: the PLC's heartbeat must keep
        # changing. If it stops (PLC in STOP, program hung), say not ready and
        # start nothing new. Recovers by itself once the heartbeat moves again;
        # the FAULT/acknowledge decision belongs to the PLC, not to vision.
        if db10.heartbeat != self._last_plc_heartbeat:
            self._last_plc_heartbeat = db10.heartbeat
            self._plc_heartbeat_changed_at = now
        self.plc_alive = now - self._plc_heartbeat_changed_at < self._plc_watchdog_s

        # Each seq_id is answered exactly once, however long the trigger stays high.
        if (
            self.plc_alive
            and self._busy_seq is None
            and db10.trigger
            and db10.seq_id != self._last_started_seq
        ):
            self._last_started_seq = db10.seq_id
            self._busy_seq = db10.seq_id
            self._busy_part = db10.part_id
            self._done_at = now + self._latency_s
            if self._faults.freeze_heartbeat:
                self._hung_output = self._output(db10.heartbeat)
                return self._hung_output

        # Inspection time is a deadline, not a sleep: the heartbeat echo below
        # keeps running while we "inspect", so slow vision never looks dead.
        if self._busy_seq is not None and now >= self._done_at:
            self._result = self._inspect(self._busy_part)
            self._result_seq_id = self._busy_seq
            if self._faults.stale_seq:
                self._result_seq_id = (self._busy_seq - 1) & protocol.UINT16_MAX
            self._busy_seq = None
            self.answered += 1

        return self._output(db10.heartbeat)

    def _output(self, heartbeat_echo: int) -> VisionToPlc:
        return VisionToPlc(
            heartbeat_echo=heartbeat_echo,
            result_seq_id=self._result_seq_id,
            result=self._result,
            ready=self.plc_alive,
        )


class VisionClient(CyclicWorker):
    name = "vision"

    def __init__(
        self,
        port: int = protocol.DEFAULT_PORT,
        *,
        host: str = protocol.LOCALHOST,
        inspect: Inspector | None = None,
        latency_s: float = 0.05,
        poll_s: float = 0.01,
        plc_watchdog_s: float = 0.5,
        faults: VisionFaults | None = None,
        clock: Clock = time.monotonic,
    ) -> None:
        super().__init__(poll_s)
        self._port = port
        self._host = host
        self._clock = clock
        self._faults = faults or VisionFaults()
        self._logic = VisionLogic(
            inspect or random_inspector(), latency_s, plc_watchdog_s, self._faults
        )
        self._client: snap7.client.Client | None = None
        self.plc_lost = threading.Event()  # set while the PLC heartbeat is frozen
        self.crashed = threading.Event()  # set when crash_after has ended the loop

    def _open(self) -> None:
        client = snap7.client.Client()
        client.connect(self._host, protocol.RACK, protocol.SLOT, self._port)
        self._client = client
        # One full poll before the loop starts: when start() returns, DB11 is
        # already overwritten with our clean state (seq 0, NONE). After a restart
        # the previous instance's result is gone before anything else happens.
        try:
            self._step()
        except BaseException:
            self._close()
            raise

    def _step(self) -> None:
        assert self._client is not None
        # Whole-block read and write: one request each, so each side always sees
        # a consistent DB, never a mix of old and new fields.
        raw = self._client.db_read(protocol.PLC_TO_VISION_DB, 0, protocol.PLC_TO_VISION_SIZE)
        db11 = self._logic.step(protocol.decode_plc_to_vision(raw), self._clock())
        self._client.db_write(protocol.VISION_TO_PLC_DB, 0, protocol.encode_vision_to_plc(db11))
        # Set only after ready=False is in DB11, so a waiter can rely on it.
        if self._logic.plc_alive:
            self.plc_lost.clear()
        else:
            self.plc_lost.set()

        crash_after = self._faults.crash_after
        if crash_after is not None and self._logic.answered >= crash_after:
            # Simulated crash: the connection drops and nothing is written any
            # more, so the PLC sees a frozen heartbeat echo.
            self._close()
            self._stop_requested.set()
            self.crashed.set()

    def _close(self) -> None:
        if self._client is not None:
            self._client.disconnect()
            self._client.destroy()
            self._client = None
