from __future__ import annotations

import pathlib
import struct

import pytest

import pyflp

TEMPO = bytes([0x9C]) + (130_000).to_bytes(4, "little")


def _flp(tmp_path: pathlib.Path, version: str, *events: bytes) -> pathlib.Path:
    ver = version.encode() + b"\0"
    data = bytes([0xC7, len(ver)]) + ver + b"".join(events)
    path = tmp_path / "synthetic.flp"
    header = struct.pack("4sIh2H", b"FLhd", 6, 0, 0, 96)
    path.write_bytes(header + b"FLdt" + len(data).to_bytes(4, "little") + data)
    return path


@pytest.mark.parametrize(
    ("version", "ac_event"),
    [
        pytest.param("25.2.5.5055", bytes([0xAC, 0x05, 0x00, 0x00, 0x00]), id="fl25-dword"),
        pytest.param("25.2.5.5055", bytes([0xAC, 0x01, 0x01, 0x00]), id="fl25-varint-value"),
        pytest.param("26.1.1.5100", bytes([0xAC, 0x01, 0x01]), id="fl26-varint-length"),
        pytest.param("25.2.5.5055", b"", id="no-ac"),
    ],
)
def test_ac_layouts_keep_stream_aligned(tmp_path, version, ac_event):
    path = _flp(tmp_path, version, ac_event, TEMPO)
    assert pyflp.parse(path).tempo == 130.0

