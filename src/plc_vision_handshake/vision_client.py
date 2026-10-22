"""Vision side of the handshake: a snap7 client that answers inspection triggers.

Split like the PLC side: ``VisionLogic`` is pure (DB10 in, DB11 out, injected
time) and ``VisionClient`` only moves bytes over the S7 connection.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable

import snap7

from plc_vision_handshake import protocol
from plc_vision_handshake.plc_logic import Clock
from plc_vision_handshake.protocol import PlcToVision, Result, VisionToPlc
from plc_vision_handshake.worker import CyclicWorker

Inspector = Callable[[int], Result]  # part_id -> result


def random_inspector(seed: int | None = None, nok_rate: float = 0.2) -> Inspector:
    """OK/NOK at random; pass a seed for a repeatable sequence."""
    rng = random.Random(seed)

    def inspect(part_id: int) -> Result:
        return Result.NOK if rng.random() < nok_rate else Result.OK

    return inspect


class VisionLogic:
    def __init__(self, inspect: Inspector, latency_s: float = 0.05) -> None:
        self._inspect = inspect
        self._latency_s = latency_s
        self._last_started_seq = 0  # PLC never uses seq 0 for a real request
        self._busy_seq: int | None = None
        self._busy_part = 0
        self._done_at = 0.0
        self._result_seq_id = 0
        self._result = Result.NONE

    def step(self, db10: PlcToVision, now: float) -> VisionToPlc:
        """One poll: take DB10 as read, return DB11 to write."""
        # Each seq_id is answered exactly once, however long the trigger stays high.
        if self._busy_seq is None and db10.trigger and db10.seq_id != self._last_started_seq:
            self._last_started_seq = db10.seq_id
            self._busy_seq = db10.seq_id
            self._busy_part = db10.part_id
            self._done_at = now + self._latency_s

        # Inspection time is a deadline, not a sleep: the heartbeat echo below
        # keeps running while we "inspect", so slow vision never looks dead.
        if self._busy_seq is not None and now >= self._done_at:
            self._result = self._inspect(self._busy_part)
            self._result_seq_id = self._busy_seq
            self._busy_seq = None

        return VisionToPlc(
            heartbeat_echo=db10.heartbeat,
            result_seq_id=self._result_seq_id,
            result=self._result,
            ready=True,
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
        clock: Clock = time.monotonic,
    ) -> None:
        super().__init__(poll_s)
        self._port = port
        self._host = host
        self._clock = clock
        self._logic = VisionLogic(inspect or random_inspector(), latency_s)
        self._client: snap7.client.Client | None = None

    def _open(self) -> None:
        client = snap7.client.Client()
        client.connect(self._host, protocol.RACK, protocol.SLOT, self._port)
        self._client = client

    def _step(self) -> None:
        assert self._client is not None
        # Whole-block read and write: one request each, so each side always sees
        # a consistent DB, never a mix of old and new fields.
        raw = self._client.db_read(protocol.PLC_TO_VISION_DB, 0, protocol.PLC_TO_VISION_SIZE)
        db11 = self._logic.step(protocol.decode_plc_to_vision(raw), self._clock())
        self._client.db_write(protocol.VISION_TO_PLC_DB, 0, protocol.encode_vision_to_plc(db11))

    def _close(self) -> None:
        if self._client is not None:
            self._client.disconnect()
            self._client.destroy()
            self._client = None
