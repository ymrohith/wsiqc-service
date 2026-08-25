"""Tile geometry.

The grid is a generator, never a list. A 40x slide at 512px tiles can be
hundreds of thousands of positions; materialising that list is the first
thing that blows up memory on a real slide.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

from .mask import TissueMask
from .reader import SlideReader


@dataclass(frozen=True)
class Tile:
    """One tile position. Coordinates are level 0, like OpenSlide's API."""

    col: int
    row: int
    x: int
    y: int
    level: int
    size: int
    tissue_fraction: float


def grid_shape(reader: SlideReader, level: int, tile_size: int) -> tuple[int, int]:
    w, h = reader.level_dimensions[level]
    cols = (w + tile_size - 1) // tile_size
    rows = (h + tile_size - 1) // tile_size
    return cols, rows


def iter_tiles(
    reader: SlideReader,
    mask: TissueMask,
    level: int = 0,
    tile_size: int = 512,
    min_tissue: float = 0.10,
) -> Iterator[Tile]:
    """Yield tiles that contain at least `min_tissue` tissue.

    Yields lazily so the caller controls memory: one tile in flight at a
    time unless it deliberately batches.
    """
    ds = reader.level_downsamples[level]
    cols, rows = grid_shape(reader, level, tile_size)
    step0 = int(tile_size * ds)  # tile edge measured in level-0 pixels

    for row in range(rows):
        for col in range(cols):
            x0 = int(col * step0)
            y0 = int(row * step0)
            frac = mask.fraction_in(x0, y0, step0)
            if frac < min_tissue:
                continue
            yield Tile(
                col=col, row=row, x=x0, y=y0,
                level=level, size=tile_size, tissue_fraction=frac,
            )


def count_tiles(reader: SlideReader, level: int, tile_size: int) -> int:
    cols, rows = grid_shape(reader, level, tile_size)
    return cols * rows
