"""Pictures: the airport image for the map, an annotated copy with boxes, and per-aircraft chips."""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .detect import Detection

log = logging.getLogger(__name__)

BOX_COLOURS = {
    "very large": (255, 59, 48),
    "large": (255, 149, 0),
    "medium": (255, 214, 10),
    "small": (76, 201, 240),
}
CHIP_PX = 240
CHIP_HALF = 24  # source pixels each side of the aircraft (240 m)


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def to_rgba(bands: dict[str, np.ndarray], sensor: str) -> np.ndarray:
    """8-bit RGBA picture of a pass (transparent where there is no data)."""
    if sensor == "S2":
        rgb = np.stack([bands["red"], bands["green"], bands["blue"]], axis=-1)
        valid = np.isfinite(rgb).all(axis=-1)
        vals = rgb[valid]
        if vals.size:
            lo, hi = np.percentile(vals, [1, 99.5])
        else:
            lo, hi = 0.0, 0.3
        hi = max(hi, lo + 1e-3)
        img = np.clip((np.nan_to_num(rgb) - lo) / (hi - lo), 0, 1) ** (1 / 1.4)
    else:
        vv = bands["vv"]
        valid = np.isfinite(vv) & (vv > 0)
        with np.errstate(divide="ignore", invalid="ignore"):
            db = 10 * np.log10(np.where(valid, vv, 1e-6))
        g = np.clip((db + 25) / 25, 0, 1)
        img = np.repeat(g[..., None], 3, axis=-1)
    rgba = np.zeros(img.shape[:2] + (4,), np.uint8)
    rgba[..., :3] = (img * 255).astype(np.uint8)
    rgba[..., 3] = np.where(valid, 255, 0)
    return rgba


def _draw_box(draw: ImageDraw.ImageDraw, d: Detection, scale: float, ox: float = 0, oy: float = 0,
              width: int = 2, pad: float = 1.0) -> tuple[float, float, float, float]:
    colour = BOX_COLOURS.get(d.size_class, (255, 255, 255))
    x0 = (d.box_c0 - pad + 0.5 - ox) * scale
    y0 = (d.box_r0 - pad + 0.5 - oy) * scale
    x1 = (d.box_c1 + pad + 0.5 - ox) * scale
    y1 = (d.box_r1 + pad + 0.5 - oy) * scale
    draw.rectangle([x0 - 1, y0 - 1, x1 + 1, y1 + 1], outline=(0, 0, 0), width=width + 2)
    draw.rectangle([x0, y0, x1, y1], outline=colour, width=width)
    return x0, y0, x1, y1


def save_overlay(rgba: np.ndarray, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgba, "RGBA").save(path, optimize=True)
    return path


def save_annotated(rgba: np.ndarray, dets: list[Detection], path: Path, title: str,
                   pixel_m: float = 10.0, max_px: int = 1600) -> Path:
    """The airport image with a numbered box round every aircraft."""
    h, w = rgba.shape[:2]
    scale = max(1.0, min(2.0, max_px / max(h, w)))
    img = Image.fromarray(rgba[..., :3], "RGB").resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    draw = ImageDraw.Draw(img)
    font = _font(14)
    for i, d in enumerate(dets, 1):
        x0, y0, x1, y1 = _draw_box(draw, d, scale, width=2)
        tb = draw.textbbox((x1 + 3, y0), str(i), font=font)
        draw.rectangle([tb[0] - 2, tb[1] - 1, tb[2] + 2, tb[3] + 1], fill=(0, 0, 0))
        draw.text((x1 + 3, y0), str(i), fill=BOX_COLOURS.get(d.size_class, (255, 255, 255)), font=font)
    W, H = img.size
    draw.rectangle([0, H - 26, W, H], fill=(0, 0, 0))
    draw.text((8, H - 13), f"{title}   {len(dets)} aircraft", fill=(255, 255, 255), font=_font(15), anchor="lm")
    bar = 1000 / pixel_m * scale
    draw.line([(W - 14 - bar, 16), (W - 14, 16)], fill=(0, 0, 0), width=6)
    draw.line([(W - 14 - bar, 16), (W - 14, 16)], fill=(255, 255, 255), width=3)
    draw.text((W - 14 - bar / 2, 30), "1 km", fill=(255, 255, 255), font=_font(13), anchor="mm",
              stroke_width=2, stroke_fill=(0, 0, 0))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, optimize=True)
    return path


def save_chip(rgba: np.ndarray, d: Detection, path: Path, label: str = "") -> Path:
    """Close-up of one aircraft with its box."""
    h, w = rgba.shape[:2]
    r, c = int(round(d.row)), int(round(d.col))
    r0, c0 = r - CHIP_HALF, c - CHIP_HALF
    crop = np.zeros((2 * CHIP_HALF, 2 * CHIP_HALF, 3), np.uint8)
    sr0, sc0 = max(r0, 0), max(c0, 0)
    sr1, sc1 = min(r0 + 2 * CHIP_HALF, h), min(c0 + 2 * CHIP_HALF, w)
    crop[sr0 - r0:sr1 - r0, sc0 - c0:sc1 - c0] = rgba[sr0:sr1, sc0:sc1, :3]
    scale = CHIP_PX / (2 * CHIP_HALF)
    img = Image.fromarray(crop, "RGB").resize((CHIP_PX, CHIP_PX), Image.LANCZOS)
    draw = ImageDraw.Draw(img)
    _draw_box(draw, d, scale, ox=c0, oy=r0, width=2, pad=0.7)
    small = _font(12)
    draw.rectangle([0, CHIP_PX - 18, CHIP_PX, CHIP_PX], fill=(0, 0, 0))
    draw.text((5, CHIP_PX - 9), f"~{d.size_m:.0f} m  {d.size_class}  {label}".strip(),
              fill=(255, 255, 255), font=small, anchor="lm")
    bar = 50 / 10 * scale  # 50 m
    draw.line([(CHIP_PX - 8 - bar, 10), (CHIP_PX - 8, 10)], fill=(255, 255, 255), width=3)
    draw.text((CHIP_PX - 8 - bar / 2, 21), "50 m", fill=(255, 255, 255), font=small, anchor="mm",
              stroke_width=2, stroke_fill=(0, 0, 0))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, optimize=True)
    return path
