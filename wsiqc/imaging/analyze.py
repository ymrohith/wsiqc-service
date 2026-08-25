"""The analysis entry point.

This module knows nothing about HTTP, databases or queues. That is
deliberate: it means the whole pipeline can be unit-tested with a synthetic
image and no server running, and it is the layering question most likely to
come up in review.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from .focus import blur_threshold, focus_score
from .mask import tissue_mask
from .reader import open_slide
from .tiles import count_tiles, iter_tiles


@dataclass
class TileResult:
    col: int
    row: int
    x: int
    y: int
    focus: float
    tissue_fraction: float
    blurred: bool = False


@dataclass
class QCReport:
    slide_path: str
    width: int
    height: int
    level_used: int
    tile_size: int
    tiles_total: int          # every position in the grid
    tiles_analyzed: int       # positions that held tissue
    tiles_blurred: int
    tissue_coverage: float
    blur_threshold: float
    median_focus: float
    duration_seconds: float
    tiles: list[TileResult] = field(default_factory=list)

    @property
    def blurred_fraction(self) -> float:
        if self.tiles_analyzed == 0:
            return 0.0
        return self.tiles_blurred / self.tiles_analyzed

    @property
    def passed(self) -> bool:
        """A slide fails QC if too much of its tissue is out of focus."""
        return self.blurred_fraction <= 0.10

    def to_dict(self, include_tiles: bool = True) -> dict:
        d = asdict(self)
        d["blurred_fraction"] = self.blurred_fraction
        d["passed"] = self.passed
        if not include_tiles:
            d.pop("tiles")
        return d

    def heatmap(self, grid_cols: int, grid_rows: int) -> np.ndarray:
        """Focus scores laid back out on the tile grid. NaN where no tissue."""
        hm = np.full((grid_rows, grid_cols), np.nan, dtype=np.float32)
        for t in self.tiles:
            hm[t.row, t.col] = t.focus
        return hm


def analyze_slide(
    path: str | Path,
    tile_size: int = 512,
    target_downsample: float = 4.0,
    min_tissue: float = 0.10,
    max_tiles: int | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> QCReport:
    """Tile a slide, score every tissue tile for focus, summarise.

    target_downsample selects the working magnification. 4.0 on a 40x slide
    is roughly 10x -- enough detail to judge focus, a sixteenth of the pixels.
    """
    started = time.perf_counter()
    path = Path(path)

    with open_slide(path) as reader:
        w0, h0 = reader.level_dimensions[0]
        level = reader.best_level_for_downsample(target_downsample)

        mask = tissue_mask(reader)
        total_positions = count_tiles(reader, level, tile_size)

        results: list[TileResult] = []
        for i, tile in enumerate(iter_tiles(reader, mask, level, tile_size, min_tissue)):
            if max_tiles is not None and i >= max_tiles:
                break
            region = reader.read_region(tile.x, tile.y, tile.level, tile.size, tile.size)
            results.append(
                TileResult(
                    col=tile.col, row=tile.row, x=tile.x, y=tile.y,
                    focus=focus_score(region),
                    tissue_fraction=tile.tissue_fraction,
                )
            )
            if progress and i % 50 == 0:
                progress(i, total_positions)

        scores = np.array([r.focus for r in results], dtype=np.float32)
        threshold = blur_threshold(scores)
        for r in results:
            r.blurred = r.focus < threshold

        return QCReport(
            slide_path=str(path),
            width=w0,
            height=h0,
            level_used=level,
            tile_size=tile_size,
            tiles_total=total_positions,
            tiles_analyzed=len(results),
            tiles_blurred=sum(r.blurred for r in results),
            tissue_coverage=mask.coverage,
            blur_threshold=threshold,
            median_focus=float(np.median(scores)) if scores.size else 0.0,
            duration_seconds=time.perf_counter() - started,
            tiles=results,
        )
