"""The integration ships brand images that Home Assistant serves locally."""

from __future__ import annotations

from pathlib import Path
import struct

import pytest

BRAND = Path(__file__).parent.parent / "custom_components" / "au_energy_tariff" / "brand"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _png_info(path: Path) -> tuple[int, int, int]:
    """Return (width, height, colour type) from a PNG header."""
    data = path.read_bytes()
    assert data[:8] == PNG_SIGNATURE, f"{path.name} is not a PNG"
    width, height = struct.unpack(">II", data[16:24])
    return width, height, data[25]


@pytest.mark.parametrize(("name", "size"), [("icon.png", 256), ("icon@2x.png", 512)])
def test_brand_icons_are_square_pngs_with_transparency(name, size):
    width, height, colour_type = _png_info(BRAND / name)
    assert (width, height) == (size, size)
    assert colour_type == 6  # RGBA, so the rounded corners stay transparent


def test_only_names_home_assistant_serves():
    allowed = {
        "icon.png", "logo.png", "icon@2x.png", "logo@2x.png",
        "dark_icon.png", "dark_logo.png", "dark_icon@2x.png", "dark_logo@2x.png",
    }
    assert {p.name for p in BRAND.iterdir()} <= allowed
