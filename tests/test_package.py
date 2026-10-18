import plc_vision_handshake


def test_package_exposes_version() -> None:
    assert isinstance(plc_vision_handshake.__version__, str)
