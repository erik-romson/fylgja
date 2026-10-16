"""``scripts/gen_favicon_attention.py`` draws the attention dot on the tab icon."""

from __future__ import annotations

import importlib.util
import sys
import zlib
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = ROOT / "scripts" / "gen_favicon_attention.py"
STATIC = ROOT / "src" / "fylgja" / "api" / "static"


@pytest.fixture(scope="module")
def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_favicon_attention", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["gen_favicon_attention"] = module
    spec.loader.exec_module(module)
    return module


def _pixels(script: ModuleType, png: bytes) -> list[bytearray]:
    rows: list[bytearray] = script._decode(png)
    return rows


def test_the_committed_icon_is_the_generators_output(script: ModuleType) -> None:
    source = (STATIC / "favicon.png").read_bytes()
    assert script.render(source) == (STATIC / "favicon-attention.png").read_bytes()


def test_the_output_is_a_64_by_64_rgba_png(script: ModuleType) -> None:
    out = script.render((STATIC / "favicon.png").read_bytes())
    assert out.startswith(b"\x89PNG\r\n\x1a\n")
    rows = _pixels(script, out)  # the decoder accepts only 64x64, 8-bit RGBA, non-interlaced
    assert len(rows) == 64
    assert all(len(row) == 64 * 4 for row in rows)
    assert zlib.decompress(out[out.index(b"IDAT") + 4 : out.index(b"IEND") - 8])


def test_only_the_dot_changes(script: ModuleType) -> None:
    source = _pixels(script, (STATIC / "favicon.png").read_bytes())
    out = _pixels(script, script.render((STATIC / "favicon.png").read_bytes()))
    cx, cy = script.CENTRE
    for y in range(64):
        for x in range(64):
            if (x - cx) ** 2 + (y - cy) ** 2 > 16**2:
                assert out[y][x * 4 : x * 4 + 4] == source[y][x * 4 : x * 4 + 4], (x, y)


def test_the_dot_is_opaque_orange_with_a_white_ring(script: ModuleType) -> None:
    out = _pixels(script, script.render((STATIC / "favicon.png").read_bytes()))
    cx, cy = script.CENTRE
    assert bytes(out[cy][cx * 4 : cx * 4 + 4]) == bytes((0xC7, 0x77, 0x00, 0xFF))
    ring = cx + script.RADIUS
    assert bytes(out[cy][ring * 4 : ring * 4 + 4]) == bytes((0xFF, 0xFF, 0xFF, 0xFF))
