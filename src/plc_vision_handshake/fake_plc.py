"""Fake PLC: a snap7 server that owns DB10/DB11 and runs PlcLogic in a scan loop.

Only the loop thread ever touches ``PlcLogic``. Other threads talk to it
through queues: ``submit_part()`` / ``reset()`` in, ``events`` out. So the state
machine needs no locks.

The two DBs are plain byte buffers shared with the native snap7 server, which
reads and writes them for the vision client. Every access from this side is
wrapped in the server's area lock, so a half-written DB11 is never decoded.
"""

from __future__ import annotations

import contextlib
import ctypes
import queue
import socket
import threading
import time

import snap7
from snap7.type import SrvArea

from plc_vision_handshake import protocol
from plc_vision_handshake.plc_logic import (
    Clock,
    Event,
    PlcConfig,
    PlcLogic,
    State,
    StateChanged,
)
from plc_vision_handshake.worker import CyclicWorker

_START_ATTEMPTS = 3


def free_port(host: str = protocol.LOCALHOST) -> int:
    """Ask the OS for a currently free TCP port.

    The native server can't report the port it got when started on port 0, so
    we pick one here and pass it in. Another process could take it between
    here and ``start_to()``; ``FakePlc`` retries with a fresh port if so.
    """
    with socket.socket() as s:
        s.bind((host, 0))
        port: int = s.getsockname()[1]
        return port


def _port_in_use(host: str, port: int) -> bool:
    # The native server binds with address reuse, and on Windows that lets it
    # "start" on a port another server is already listening on, with no error.
    # A plain socket without reuse does get "address in use", so probe first.
    with socket.socket() as s:
        try:
            s.bind((host, port))
        except OSError:
            return True
        return False


class FakePlc(CyclicWorker):
    name = "fake-plc"

    def __init__(
        self,
        port: int | None = None,
        *,
        host: str = protocol.LOCALHOST,
        config: PlcConfig | None = None,
        cycle_s: float = 0.01,
        clock: Clock = time.monotonic,
    ) -> None:
        """``port=None`` picks a free port (see ``free_port``); read it from ``.port``.

        ``host`` defaults to 127.0.0.1: never reachable from the network, and no
        firewall prompt on Windows.
        """
        super().__init__(cycle_s)
        self._requested_port = port
        self.port = port or 0
        self._host = host
        self._config = config
        self._clock = clock
        self._server: snap7.server.Server | None = None
        self._logic: PlcLogic | None = None
        self._db10 = (ctypes.c_uint8 * protocol.PLC_TO_VISION_SIZE)()
        self._db11 = (ctypes.c_uint8 * protocol.VISION_TO_PLC_SIZE)()
        self._parts: queue.Queue[int] = queue.Queue()
        self._pending_part: int | None = None
        self._reset_requested = threading.Event()
        self.events: queue.Queue[Event] = queue.Queue()
        self.vision_ready = threading.Event()  # set once DB11 has shown ready=True
        self.fault_seen = threading.Event()  # set on the first entry into FAULT

    # --- called from other threads ------------------------------------------------
    def submit_part(self, part_id: int) -> None:
        """Queue a part for inspection. Parts are requested one at a time, in order.

        Parts queued while the PLC is in FAULT wait until ``reset()``; each part
        that gets requested receives exactly one verdict (see ``events``).
        """
        if not 0 <= part_id <= protocol.UINT32_MAX:
            raise ValueError(f"part_id={part_id} out of range 0..{protocol.UINT32_MAX}")
        self._parts.put(part_id)

    def reset(self) -> None:
        """Operator acknowledge; applied by the loop at the start of the next cycle."""
        self._reset_requested.set()

    def wait_until_vision_ready(self, timeout_s: float) -> None:
        """Block until the PLC has read ready=True from DB11.

        Fails with a clear message if the PLC faults first, which means vision
        didn't connect within the watchdog window from startup.
        """
        deadline = time.monotonic() + timeout_s
        while not self.vision_ready.wait(0.01):
            self.raise_if_failed()
            if self.fault_seen.is_set():
                raise RuntimeError(
                    "PLC watchdog faulted before vision reported ready: vision did "
                    "not connect within the watchdog window from startup"
                )
            if time.monotonic() > deadline:
                raise TimeoutError(f"vision did not report ready within {timeout_s} s")

    # --- worker hooks ---------------------------------------------------------------
    def _open(self) -> None:
        last_error: RuntimeError | None = None
        for _ in range(1 if self._requested_port else _START_ATTEMPTS):
            port = self._requested_port or free_port(self._host)
            if _port_in_use(self._host, port):
                last_error = RuntimeError(f"port {port} is already in use")
                continue
            server = snap7.server.Server(log=False)
            server.register_area(SrvArea.DB, protocol.PLC_TO_VISION_DB, self._db10)
            server.register_area(SrvArea.DB, protocol.VISION_TO_PLC_DB, self._db11)
            try:
                server.start_to(self._host, port)
            except RuntimeError as exc:
                server.destroy()
                last_error = exc
                continue
            self._server = server
            self.port = port
            # Created last, so vision's first watchdog window starts when we listen.
            self._logic = PlcLogic(self._config, self._clock)
            return
        raise RuntimeError(f"could not start snap7 server on {self._host}") from last_error

    def _step(self) -> None:
        assert self._logic is not None
        logic = self._logic

        inputs = protocol.decode_vision_to_plc(self._read(protocol.VISION_TO_PLC_DB, self._db11))
        if inputs.ready:
            self.vision_ready.set()

        if self._reset_requested.is_set():
            self._reset_requested.clear()
            logic.reset()

        if self._pending_part is None:
            with contextlib.suppress(queue.Empty):
                self._pending_part = self._parts.get_nowait()
        # Refused (busy or FAULT) -> keep it and try again next cycle.
        if self._pending_part is not None and logic.request_inspection(self._pending_part):
            self._pending_part = None

        outputs = logic.cycle(inputs)
        self._write(protocol.PLC_TO_VISION_DB, self._db10, protocol.encode_plc_to_vision(outputs))

        for event in logic.drain_events():
            if isinstance(event, StateChanged) and event.new is State.FAULT:
                self.fault_seen.set()
            self.events.put(event)

    def _close(self) -> None:
        if self._server is not None:
            self._server.stop()
            self._server.destroy()
            self._server = None

    # --- buffer access under the server's area lock ---------------------------------
    def _read(self, db: int, buffer: ctypes.Array[ctypes.c_uint8]) -> bytes:
        assert self._server is not None
        self._server.lock_area(SrvArea.DB, db)
        try:
            return bytes(buffer)
        finally:
            self._server.unlock_area(SrvArea.DB, db)

    def _write(self, db: int, buffer: ctypes.Array[ctypes.c_uint8], data: bytearray) -> None:
        assert self._server is not None
        self._server.lock_area(SrvArea.DB, db)
        try:
            ctypes.memmove(buffer, bytes(data), len(data))
        finally:
            self._server.unlock_area(SrvArea.DB, db)
