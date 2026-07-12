from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from scripts.verify_webengine_surface import (
    SurfaceError,
    png_luminance,
    validate_surface,
)


def _png(path: Path, width: int, height: int, *, varied: bool) -> None:
    rows = bytearray()
    for y_position in range(height):
        rows.append(0)
        for x_position in range(width):
            value = 180 if varied and (x_position // 32 + y_position // 32) % 2 else 12
            rows.extend((value, value // 2, 255 - value))

    def chunk(kind: bytes, value: bytes) -> bytes:
        return (
            struct.pack("!I", len(value))
            + kind
            + value
            + struct.pack("!I", zlib.crc32(kind + value) & 0xFFFFFFFF)
        )

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def test_rendered_webengine_png_has_visible_varied_pixels(tmp_path: Path) -> None:
    screenshot = tmp_path / "rendered.png"
    _png(screenshot, 900, 620, varied=True)
    stats = png_luminance(screenshot)
    validate_surface(stats)
    assert stats["nonblack_fraction"] == 1


def test_uniform_webengine_png_cannot_pass_surface_validation(tmp_path: Path) -> None:
    screenshot = tmp_path / "blank.png"
    _png(screenshot, 900, 620, varied=False)
    stats = png_luminance(screenshot)
    with pytest.raises(SurfaceError, match="blank or uniform"):
        validate_surface(stats)
