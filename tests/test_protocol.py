import pytest

from plc_vision_handshake.protocol import (
    PLC_TO_VISION_SIZE,
    VISION_TO_PLC_SIZE,
    PlcToVision,
    Result,
    VisionToPlc,
    decode_plc_to_vision,
    decode_vision_to_plc,
    encode_plc_to_vision,
    encode_vision_to_plc,
)


@pytest.mark.parametrize(
    "msg",
    [
        PlcToVision(heartbeat=0, trigger=False, seq_id=0, part_id=0),
        PlcToVision(heartbeat=1234, trigger=True, seq_id=42, part_id=987_654),
        PlcToVision(heartbeat=0xFFFF, trigger=True, seq_id=0xFFFF, part_id=0xFFFF_FFFF),
    ],
)
def test_plc_to_vision_round_trip(msg: PlcToVision) -> None:
    assert decode_plc_to_vision(encode_plc_to_vision(msg)) == msg


@pytest.mark.parametrize("result", list(Result))
def test_vision_to_plc_round_trip_for_every_result(result: Result) -> None:
    msg = VisionToPlc(heartbeat_echo=7, result_seq_id=3, result=result, ready=True)
    assert decode_vision_to_plc(encode_vision_to_plc(msg)) == msg


def test_plc_to_vision_byte_layout() -> None:
    # Pins offsets and big-endian byte order, which a round trip alone can't catch.
    data = encode_plc_to_vision(
        PlcToVision(heartbeat=0x0102, trigger=True, seq_id=0x0304, part_id=0x05060708)
    )
    assert data == bytearray([0x01, 0x02, 0x01, 0x00, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08])


def test_vision_to_plc_byte_layout() -> None:
    data = encode_vision_to_plc(
        VisionToPlc(heartbeat_echo=0x0102, result_seq_id=0x0304, result=Result.NOK, ready=True)
    )
    assert data == bytearray([0x01, 0x02, 0x03, 0x04, 0x02, 0x01])


def test_unknown_result_byte_decodes_to_invalid() -> None:
    data = bytearray(VISION_TO_PLC_SIZE)
    data[4] = 0x7E
    assert decode_vision_to_plc(data).result is Result.INVALID


@pytest.mark.parametrize(
    "msg",
    [
        PlcToVision(heartbeat=0x1_0000, trigger=False, seq_id=0, part_id=0),
        PlcToVision(heartbeat=0, trigger=False, seq_id=-1, part_id=0),
        PlcToVision(heartbeat=0, trigger=False, seq_id=0, part_id=0x1_0000_0000),
    ],
)
def test_encode_rejects_out_of_range_values(msg: PlcToVision) -> None:
    with pytest.raises(ValueError, match="out of range"):
        encode_plc_to_vision(msg)


def test_decode_rejects_wrong_buffer_size() -> None:
    with pytest.raises(ValueError, match="expected"):
        decode_plc_to_vision(bytearray(PLC_TO_VISION_SIZE - 1))
    with pytest.raises(ValueError, match="expected"):
        decode_vision_to_plc(bytearray(VISION_TO_PLC_SIZE + 1))
