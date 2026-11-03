"""Rasterise the monochrome SVG screenshots produced by Textual/Rich.

Textual 0.89 only offers ``export_screenshot()``, which returns SVG.  A DOCX
needs raster images and the live image ships no system rasteriser, so this
module implements the deliberately small subset Rich emits:

* ``<rect>`` and ``<circle>`` backgrounds (the terminal frame and highlights);
* one ``<text>`` element per styled span, with absolute ``x``/``y`` and a
  ``textLength`` that lets us derive the monospace cell width;
* ``<style>`` classes for fill/bold/italic and the global font size;
* ``<g transform="translate(x, y)">`` offsets.

Everything else (clip paths, strokes, embedded fonts) is ignored.  The TUI is
English-only and pure black/white, so DejaVu Sans Mono covers all glyphs it can
emit.  Any parse or rendering failure returns ``None``: screenshots are
evidence, not a control path.
"""

from __future__ import annotations

import io
import logging
import re
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

DEFAULT_WIDTH = 1200.0
DEFAULT_HEIGHT = 600.0
MAX_IMAGE_SIDE = 4000.0
DEFAULT_FONT_SIZE = 20.0
DEFAULT_TEXT_FILL = "#c5c8c6"
DEFAULT_BACKGROUND = "#000000"

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    "C:/Windows/Fonts/consola.ttf",
    "C:/Windows/Fonts/DejaVuSansMono.ttf",
)
_BOLD_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
    "C:/Windows/Fonts/consolab.ttf",
)
_ITALIC_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Oblique.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Italic.ttf",
    "C:/Windows/Fonts/consolai.ttf",
)

_RULE_RE = re.compile(r"\.([A-Za-z0-9_-]+)\s*\{([^}]*)\}")
_FILL_RE = re.compile(r"fill\s*:\s*([^;\s]+)", re.IGNORECASE)
_FONT_SIZE_RE = re.compile(r"font-size\s*:\s*([0-9.]+)px", re.IGNORECASE)
_FONT_WEIGHT_RE = re.compile(r"font-weight\s*:\s*(bold|[0-9]+)", re.IGNORECASE)
_FONT_STYLE_RE = re.compile(r"font-style\s*:\s*(italic|oblique)", re.IGNORECASE)
_NUMBER_RE = re.compile(r"[-+]?[0-9]*\.?[0-9]+")
_TRANSLATE_RE = re.compile(
    r"translate\(\s*([-+]?[0-9]*\.?[0-9]+)(?:[\s,]+([-+]?[0-9]*\.?[0-9]+))?\s*\)"
)
_NAMED_COLORS = {
    "black": "#000000",
    "white": "#ffffff",
    "gray": "#808080",
    "grey": "#808080",
    "silver": "#c0c0c0",
    "red": "#ff0000",
    "green": "#00ff00",
    "blue": "#0000ff",
    "yellow": "#ffff00",
    "cyan": "#00ffff",
    "magenta": "#ff00ff",
}


def _local(tag: Any) -> str:
    """Strip the XML namespace from an ElementTree tag."""

    return str(tag).rsplit("}", 1)[-1]


def _number(value: Optional[str], default: float = 0.0) -> float:
    """Parse the leading number from an SVG attribute (``12.5px`` -> 12.5)."""

    if value is None:
        return default
    match = _NUMBER_RE.search(str(value))
    return float(match.group(0)) if match else default


def _color(value: Optional[str], default: Optional[str] = None) -> Optional[str]:
    """Convert an SVG paint value into something Pillow accepts."""

    if not value:
        return default
    text = value.strip()
    lowered = text.lower()
    if lowered in ("none", "transparent"):
        return None
    if text.startswith("#"):
        if len(text) in (4, 5):  # #rgb / #rgba
            return "#" + "".join(char * 2 for char in text[1:4])
        if len(text) in (7, 9):  # #rrggbb / #rrggbbaa
            return text[:7]
        return default
    match = re.match(r"rgba?\(([^)]*)\)", lowered)
    if match:
        parts = [part.strip() for part in match.group(1).split(",")]
        if len(parts) >= 3:
            channels: List[str] = []
            for part in parts[:3]:
                try:
                    number = float(part.rstrip("%"))
                except ValueError:
                    return default
                if part.endswith("%"):
                    number = number * 255.0 / 100.0
                channels.append(str(max(0, min(255, int(round(number))))))
            return "rgb(" + ",".join(channels) + ")"
        return default
    return _NAMED_COLORS.get(lowered, default)


def _style_rules(root: ET.Element) -> Dict[str, Dict[str, Any]]:
    """Parse the classes Rich writes into the screenshot's ``<style>`` block."""

    rules: Dict[str, Dict[str, Any]] = {}
    style_text = ""
    for element in root.iter():
        if _local(element.tag) == "style" and element.text:
            style_text += element.text
    for class_name, body in _RULE_RE.findall(style_text):
        fill = _FILL_RE.search(body)
        size = _FONT_SIZE_RE.search(body)
        weight = _FONT_WEIGHT_RE.search(body)
        italic = _FONT_STYLE_RE.search(body)
        rules[class_name] = {
            "fill": _color(fill.group(1)) if fill else None,
            "font_size": float(size.group(1)) if size else None,
            "bold": bool(weight and (weight.group(1).lower() == "bold" or int(weight.group(1) or 0) >= 600)),
            "italic": bool(italic),
        }
    return rules


def _class_style(node: ET.Element, rules: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Merge every class referenced by an element (later classes win)."""

    merged: Dict[str, Any] = {}
    for class_name in (node.get("class") or "").split():
        merged.update(rules.get(class_name, {}))
    return merged


def _translate(node: ET.Element) -> Tuple[float, float]:
    """Return the translation encoded in a ``transform`` attribute."""

    transform = node.get("transform") or ""
    match = _TRANSLATE_RE.search(transform)
    if not match:
        return 0.0, 0.0
    return float(match.group(1)), float(match.group(2) or 0.0)


def _viewbox(root: ET.Element) -> Tuple[float, float]:
    """Extract the drawing size, preferring ``viewBox``."""

    viewbox = root.get("viewBox")
    if viewbox:
        parts = [part for part in re.split(r"[\s,]+", viewbox.strip()) if part]
        if len(parts) >= 4:
            width, height = _number(parts[2]), _number(parts[3])
            if width > 0 and height > 0:
                return width, height
    width = _number(root.get("width"), DEFAULT_WIDTH)
    height = _number(root.get("height"), DEFAULT_HEIGHT)
    return (width if width > 0 else DEFAULT_WIDTH, height if height > 0 else DEFAULT_HEIGHT)


@lru_cache(maxsize=32)
def _font(kind: str, size: int):
    """Load a TrueType font, falling back to Pillow's built-in bitmap font."""

    from PIL import ImageFont

    candidates = {
        "regular": _FONT_CANDIDATES,
        "bold": _BOLD_FONT_CANDIDATES,
        "italic": _ITALIC_FONT_CANDIDATES,
        "bolditalic": _BOLD_FONT_CANDIDATES + _ITALIC_FONT_CANDIDATES,
    }.get(kind, _FONT_CANDIDATES)
    for candidate in candidates:
        try:
            if Path(candidate).is_file():
                return ImageFont.truetype(candidate, size=size)
        except OSError:  # pragma: no cover - corrupt font file
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # pragma: no cover - Pillow < 10.1
        return ImageFont.load_default()


def _font_kind(style: Dict[str, Any]) -> str:
    bold = bool(style.get("bold"))
    italic = bool(style.get("italic"))
    if bold and italic:
        return "bolditalic"
    if bold:
        return "bold"
    if italic:
        return "italic"
    return "regular"


def _iter_drawables(
    node: ET.Element,
    dx: float,
    dy: float,
    rules: Dict[str, Dict[str, Any]],
    rects: List[Dict[str, Any]],
    circles: List[Dict[str, Any]],
    texts: List[Dict[str, Any]],
) -> None:
    """Collect shapes and text runs, accumulating group translations."""

    tag = _local(node.tag)
    if tag in ("defs", "style", "clipPath", "title", "desc", "metadata"):
        return
    offset_x, offset_y = _translate(node)
    x = dx + offset_x
    y = dy + offset_y

    if tag == "rect":
        fill = _color(node.get("fill")) or _class_style(node, rules).get("fill")
        width = _number(node.get("width"))
        height = _number(node.get("height"))
        if fill and width > 0 and height > 0:
            rects.append(
                {
                    "x": x + _number(node.get("x")),
                    "y": y + _number(node.get("y")),
                    "width": width,
                    "height": height,
                    "rx": _number(node.get("rx"), _number(node.get("ry"))),
                    "fill": fill,
                }
            )
    elif tag == "circle":
        fill = _color(node.get("fill")) or _class_style(node, rules).get("fill")
        radius = _number(node.get("r"))
        if fill and radius > 0:
            circles.append(
                {
                    "x": x + _number(node.get("cx")),
                    "y": y + _number(node.get("cy")),
                    "r": radius,
                    "fill": fill,
                }
            )
    elif tag == "text":
        content = "".join(node.itertext()).replace("\xa0", " ")
        if content.strip():
            style = _class_style(node, rules)
            texts.append(
                {
                    "text": content,
                    "x": x + _number(node.get("x")),
                    "y": y + _number(node.get("y")),
                    "text_length": _number(node.get("textLength"), -1.0),
                    "anchor": (node.get("text-anchor") or "").lower(),
                    "fill": _color(node.get("fill")) or style.get("fill") or DEFAULT_TEXT_FILL,
                    "font_size": style.get("font_size"),
                    "font_kind": _font_kind(style),
                }
            )

    for child in list(node):
        _iter_drawables(child, x, y, rules, rects, circles, texts)


def svg_to_png(svg_text: str, scale: float = 2.0) -> Optional[bytes]:
    """Render one Textual/Rich SVG to PNG bytes.

    Args:
        svg_text: SVG markup.
        scale: Raster scale relative to the SVG's user units (2.0 by default).

    Returns:
        PNG bytes, or ``None`` when Pillow is unavailable or the SVG cannot be
        parsed/rendered.  Never raises.
    """

    if not svg_text or not svg_text.strip():
        return None
    try:
        from PIL import Image, ImageDraw

        root = ET.fromstring(svg_text)
        width, height = _viewbox(root)
        scale = max(0.1, min(float(scale), MAX_IMAGE_SIDE / max(width, height, 1.0)))
        scale = float(scale)
        if scale < 0.1:
            return None
        out_width = max(1, int(round(width * scale)))
        out_height = max(1, int(round(height * scale)))

        rules = _style_rules(root)
        rects: List[Dict[str, Any]] = []
        circles: List[Dict[str, Any]] = []
        texts: List[Dict[str, Any]] = []
        _iter_drawables(root, 0.0, 0.0, rules, rects, circles, texts)

        image = Image.new("RGB", (out_width, out_height), _color(DEFAULT_BACKGROUND, "#000000"))
        draw = ImageDraw.Draw(image)

        for rect in rects:
            box = (
                rect["x"] * scale,
                rect["y"] * scale,
                (rect["x"] + rect["width"]) * scale,
                (rect["y"] + rect["height"]) * scale,
            )
            radius = max(0.0, rect["rx"] * scale)
            if radius > 0:
                draw.rounded_rectangle(box, radius=radius, fill=rect["fill"])
            else:
                draw.rectangle(box, fill=rect["fill"])

        for circle in circles:
            x = circle["x"] * scale
            y = circle["y"] * scale
            radius = circle["r"] * scale
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=circle["fill"])

        global_size_match = re.search(r"font-size\s*:\s*([0-9.]+)px", "".join(
            element.text or "" for element in root.iter() if _local(element.tag) == "style"
        ))
        global_size = float(global_size_match.group(1)) if global_size_match else DEFAULT_FONT_SIZE

        for run in texts:
            content: str = run["text"]
            pixel_size = max(6, int(round(float(run["font_size"] or global_size) * scale)))
            font = _font(run["font_kind"], pixel_size)
            x = run["x"] * scale
            y = run["y"] * scale
            if run["anchor"] in ("middle", "end"):
                anchor = "ms" if run["anchor"] == "middle" else "rs"
                draw.text((x, y), content, font=font, fill=run["fill"], anchor=anchor)
                continue
            text_length = run["text_length"]
            if text_length and text_length > 0 and len(content) > 0:
                cell = (text_length / len(content)) * scale
                for index, character in enumerate(content):
                    if character != " ":
                        draw.text((x + index * cell, y), character, font=font, fill=run["fill"], anchor="ls")
            else:
                draw.text((x, y), content, font=font, fill=run["fill"], anchor="ls")

        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()
    except Exception as exc:  # pragma: no cover - defensive by design
        log.debug("cannot rasterise screenshot: %s", exc)
        return None


def svg_file_to_png(path: Path, scale: float = 2.0) -> Optional[bytes]:
    """Read an SVG file and return PNG bytes, or ``None`` on any failure."""

    try:
        return svg_to_png(Path(path).read_text(encoding="utf-8"), scale=scale)
    except OSError as exc:  # pragma: no cover - missing/unreadable evidence file
        log.debug("cannot read screenshot %s: %s", path, exc)
        return None
