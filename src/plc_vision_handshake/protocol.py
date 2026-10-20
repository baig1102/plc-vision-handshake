"""Byte layout of the two data blocks shared by the PLC and the vision system.

Each side writes exactly one DB and only reads the other:

* DB10 "PLC -> Vision" is written by the PLC only.
* DB11 "Vision -> PLC" is written by the vision system only.

One writer per block means no write ever overwrites the other side's data.

Offsets follow the rules S7 uses for a non-optimized DB: multi-byte values
start on an even byte, a Bool occupies one bit of a byte, and the block size
is rounded up to an even number of bytes. All values are big-endian, which
``snap7.util`` handles.

Encode/decode functions are pure: bytes in, dataclass out (and back).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from snap7 import util

UINT16_MAX = 0xFFFF
UINT32_MAX = 0xFFFF_FFFF

# --- DB10: PLC -> Vision -----------------------------------------------------
PLC_TO_VISION_DB = 10
PLC_HEARTBEAT = 0  # UInt   counter, incremented by the PLC every cycle
PLC_TRIGGER = 2  # Bool   bit 0: "please inspect the part now"
PLC_SEQ_ID = 4  # UInt   request number, new value per trigger
PLC_PART_ID = 6  # UDInt  part being inspected (for logging/traceability)
PLC_TO_VISION_SIZE = 10

# --- DB11: Vision -> PLC -----------------------------------------------------
VISION_TO_PLC_DB = 11
VIS_HEARTBEAT_ECHO = 0  # UInt   last PLC heartbeat the vision system saw
VIS_RESULT_SEQ_ID = 2  # UInt   seq_id the result belongs to
VIS_RESULT = 4  # USInt  Result code
VIS_READY = 5  # Bool   bit 0: vision system is up and accepting triggers
VISION_TO_PLC_SIZE = 6


class Result(IntEnum):
    """Inspection result codes in DB11."""

    NONE = 0
    OK = 1
    NOK = 2
    ERROR = 3
    # Never written on purpose. Decoding maps any byte outside the codes
    # above to INVALID, so a corrupt value still reaches the PLC logic
    # (which rejects the part) instead of crashing the decoder.
    INVALID = 0xFF


@dataclass(frozen=True)
class PlcToVision:
    heartbeat: int
    trigger: bool
    seq_id: int
    part_id: int


@dataclass(frozen=True)
class VisionToPlc:
    heartbeat_echo: int
    result_seq_id: int
    result: Result
    ready: bool


def _check_range(name: str, value: int, maximum: int) -> None:
    if not 0 <= value <= maximum:
        raise ValueError(f"{name}={value} out of range 0..{maximum}")


def _check_size(data: bytes | bytearray, expected: int) -> None:
    if len(data) != expected:
        raise ValueError(f"expected {expected} bytes, got {len(data)}")


def encode_plc_to_vision(msg: PlcToVision) -> bytearray:
    _check_range("heartbeat", msg.heartbeat, UINT16_MAX)
    _check_range("seq_id", msg.seq_id, UINT16_MAX)
    _check_range("part_id", msg.part_id, UINT32_MAX)
    data = bytearray(PLC_TO_VISION_SIZE)
    util.set_uint(data, PLC_HEARTBEAT, msg.heartbeat)
    util.set_bool(data, PLC_TRIGGER, 0, msg.trigger)
    util.set_uint(data, PLC_SEQ_ID, msg.seq_id)
    util.set_udint(data, PLC_PART_ID, msg.part_id)
    return data


def decode_plc_to_vision(data: bytes | bytearray) -> PlcToVision:
    _check_size(data, PLC_TO_VISION_SIZE)
    buf = bytearray(data)
    return PlcToVision(
        heartbeat=util.get_uint(buf, PLC_HEARTBEAT),
        trigger=util.get_bool(buf, PLC_TRIGGER, 0),
        seq_id=util.get_uint(buf, PLC_SEQ_ID),
        part_id=util.get_udint(buf, PLC_PART_ID),
    )


def encode_vision_to_plc(msg: VisionToPlc) -> bytearray:
    _check_range("heartbeat_echo", msg.heartbeat_echo, UINT16_MAX)
    _check_range("result_seq_id", msg.result_seq_id, UINT16_MAX)
    data = bytearray(VISION_TO_PLC_SIZE)
    util.set_uint(data, VIS_HEARTBEAT_ECHO, msg.heartbeat_echo)
    util.set_uint(data, VIS_RESULT_SEQ_ID, msg.result_seq_id)
    util.set_usint(data, VIS_RESULT, int(msg.result))
    util.set_bool(data, VIS_READY, 0, msg.ready)
    return data


def decode_vision_to_plc(data: bytes | bytearray) -> VisionToPlc:
    _check_size(data, VISION_TO_PLC_SIZE)
    buf = bytearray(data)
    raw_result = util.get_usint(buf, VIS_RESULT)
    try:
        result = Result(raw_result)
    except ValueError:
        result = Result.INVALID
    return VisionToPlc(
        heartbeat_echo=util.get_uint(buf, VIS_HEARTBEAT_ECHO),
        result_seq_id=util.get_uint(buf, VIS_RESULT_SEQ_ID),
        result=result,
        ready=util.get_bool(buf, VIS_READY, 0),
    )
