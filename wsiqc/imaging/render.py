"""Rendering the QC result as something a human can look at.

The heatmap is the artefact that makes the whole service legible in one
glance, so it is worth the small amount of code.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from .analyze import QCReport
from .reader import open_slide
from .tiles import grid_shape


def _colourise(hm: np.ndarray, threshold: float) -> np.ndarray:
    """Sharp tiles green, blurred tiles red, no tissue pale grey."""
    rows, cols = hm.shape
    out = np.full((rows, cols, 3), 235, dtype=np.uint8)

    valid = ~np.isnan(hm)
    if not valid.any():
        return out

    vals = hm[valid]
    lo, hi = float(np.nanmin(vals)), float(np.nanmax(vals))
    span = max(hi - lo, 1e-6)
    norm = np.zeros_like(hm)
    norm[valid] = (hm[valid] - lo) / span

    blurred = valid & (hm < threshold)
    sharp = valid & ~blurred

    out[..., 0] = np.where(sharp, (60 + 60 * (1 - norm)).astype(np.uint8), out[..., 0])
    out[..., 1] = np.where(sharp, (120 + 100 * norm).astype(np.uint8), out[..., 1])
    out[..., 2] = np.where(sharp, 90, out[..., 2])

    out[..., 0] = np.where(blurred, 205, out[..., 0])
    out[..., 1] = np.where(blurred, (60 + 80 * norm).astype(np.uint8), out[..., 1])
    out[..., 2] = np.where(blurred, 90, out[..., 2])
    return out


def write_heatmap(report: QCReport, out_path: str | Path, cell: int = 12) -> Path:
    """Write the focus heatmap as a PNG, one cell per tile."""
    with open_slide(report.slide_path) as reader:
        cols, rows = grid_shape(reader, report.level_used, report.tile_size)

    rgb = _colourise(report.heatmap(cols, rows), report.blur_threshold)
    img = Image.fromarray(rgb, mode="RGB").resize(
        (cols * cell, rows * cell), Image.NEAREST
    )
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    return out_path


def write_thumbnail(slide_path: str | Path, out_path: str | Path,
                    max_edge: int = 900) -> Path:
    """Low-magnification overview, for putting beside the heatmap."""
    with open_slide(slide_path) as reader:
        level = reader.level_count - 1
        w, h = reader.level_dimensions[level]
        arr = reader.read_region(0, 0, level, w, h)

    img = Image.fromarray(arr, mode="RGB")
    img.thumbnail((max_edge, max_edge))
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    return out_path
