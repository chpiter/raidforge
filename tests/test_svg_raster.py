"""Unit tests for the built-in Rich/Textual SVG -> PNG rasteriser.

The rasteriser must never raise: screenshots are evidence, and one odd SVG
(Textual changes its writer between releases) must not stop the report or the
upload.
"""

from __future__ import annotations

import io

import pytest

pytest.importorskip("PIL", reason="needs Pillow to rasterise screenshots")

from PIL import Image  # noqa: E402

from raidforge.core import svg_raster  # noqa: E402


SAMPLE_SVG = """<?xml version="1.0" encoding="UTF-8"?>
<svg class="rich-terminal" viewBox="0 0 100 50" xmlns="http://www.w3.org/2000/svg">
  <style>
    .terminal-r1 { fill: #c5c8c6 }
    .terminal-r2 { fill: #ffffff; font-weight: bold }
    .terminal-matrix { font-size: 10px }
  </style>
  <rect class="terminal-r1" x="0" y="0" width="100" height="50" fill="#000000"/>
  <rect class="terminal-r2" x="2" y="2" width="30" height="12" rx="2" fill="#ffffff"/>
  <g transform="translate(4, 8)">
    <text class="terminal-r1" x="0" y="0" textLength="40">AB&#160;CD</text>
    <text class="terminal-r2" x="50" y="0" text-anchor="middle" textLength="20">X</text>
  </g>
  <circle cx="80" cy="40" r="3" fill="#ffffff"/>
</svg>
"""


def _png_size(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as image:
        assert image.format == "PNG"
        return image.size


def test_empty_and_broken_svg_return_none():
    assert svg_raster.svg_to_png("") is None
    assert svg_raster.svg_to_png("   ") is None
    assert svg_raster.svg_to_png("<svg") is None
    assert svg_raster.svg_to_png("not xml at all") is None


def test_rich_svg_renders_at_scale():
    data = svg_raster.svg_to_png(SAMPLE_SVG, scale=2.0)
    assert data is not None and data.startswith(b"\x89PNG\r\n\x1a\n")
    assert _png_size(data) == (200, 100)


def test_scale_is_clamped_to_max_side():
    # A 100x50 viewBox at scale 1000 would be 100000px wide; the renderer must
    # clamp instead of allocating gigabytes.
    data = svg_raster.svg_to_png(SAMPLE_SVG, scale=1000.0)
    assert data is not None
    width, height = _png_size(data)
    assert max(width, height) <= int(svg_raster.MAX_IMAGE_SIDE)


def test_missing_fonts_fall_back_without_raising(monkeypatch):
    monkeypatch.setattr(svg_raster, "_FONT_CANDIDATES", ())
    monkeypatch.setattr(svg_raster, "_BOLD_FONT_CANDIDATES", ())
    monkeypatch.setattr(svg_raster, "_ITALIC_FONT_CANDIDATES", ())
    svg_raster._font.cache_clear()
    try:
        data = svg_raster.svg_to_png(SAMPLE_SVG)
    finally:
        svg_raster._font.cache_clear()
    assert data is not None and data.startswith(b"\x89PNG")


def test_missing_file_returns_none(tmp_path):
    assert svg_raster.svg_file_to_png(tmp_path / "nope.svg") is None
