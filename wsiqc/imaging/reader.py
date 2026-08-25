"""Slide readers.

Two backends behind one interface. OpenSlide handles the real pathology
formats (.svs, .ndpi, .mrxs) but needs a system C library that is awkward to
install. TiffReader is pure Python and works anywhere, so the demo runs with
no native dependencies.

TiffReader decodes only the TIFF tiles a requested window actually touches.
That is the point of the whole design: peak memory is proportional to the
window, not to the slide. A 2 GB slide costs the same as a 200 MB one.

Nothing else in the codebase imports a backend directly -- callers use
open_slide() and receive something satisfying SlideReader.
"""

from __future__ import annotations

import abc
from pathlib import Path

import numpy as np
import tifffile

try:  # pragma: no cover - depends on the machine, not the code
    import openslide

    HAS_OPENSLIDE = True
except (ImportError, OSError):
    HAS_OPENSLIDE = False


class SlideReader(abc.ABC):
    """A pyramidal image that does not fit in memory."""

    @property
    @abc.abstractmethod
    def level_count(self) -> int:
        ...

    @property
    @abc.abstractmethod
    def level_dimensions(self) -> list[tuple[int, int]]:
        """(width, height) per level, level 0 being full resolution."""

    @property
    @abc.abstractmethod
    def level_downsamples(self) -> list[float]:
        ...

    @abc.abstractmethod
    def read_region(self, x: int, y: int, level: int, w: int, h: int) -> np.ndarray:
        """Read a window as uint8 RGB, shape (h, w, 3).

        x and y are LEVEL 0 coordinates while w and h count pixels at the
        requested level. That asymmetry comes from OpenSlide's API and is the
        most common source of off-by-a-downsample bugs, so it lives in one
        place and every caller obeys it.
        """

    @abc.abstractmethod
    def close(self) -> None:
        ...

    def best_level_for_downsample(self, target: float) -> int:
        """Coarsest level still at least as detailed as `target`."""
        best = 0
        for i, ds in enumerate(self.level_downsamples):
            if ds <= target:
                best = i
        return best

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class TiffReader(SlideReader):
    """Windowed reads from a pyramidal TIFF, decoding tile by tile."""

    def __init__(self, path: Path, cache_tiles: int = 64):
        self.path = Path(path)
        self._tif = tifffile.TiffFile(str(self.path))
        series = self._tif.series[0]
        levels = list(series.levels) if series.is_pyramidal else [series]
        self._pages = [lvl.pages[0] for lvl in levels]
        self._cache: dict[tuple[int, int], tuple[np.ndarray, int, int]] = {}
        self._cache_limit = cache_tiles

    @property
    def level_count(self) -> int:
        return len(self._pages)

    @property
    def level_dimensions(self) -> list[tuple[int, int]]:
        return [(int(p.imagewidth), int(p.imagelength)) for p in self._pages]

    @property
    def level_downsamples(self) -> list[float]:
        w0 = self.level_dimensions[0][0]
        return [w0 / w for (w, _h) in self.level_dimensions]

    def _decode_tile(self, level: int, index: int) -> tuple[np.ndarray, int, int]:
        """Decode one stored TIFF tile. Returns (tile, x, y) in level pixels."""
        key = (level, index)
        hit = self._cache.get(key)
        if hit is not None:
            return hit

        page = self._pages[level]
        fh = self._tif.filehandle
        fh.seek(page.dataoffsets[index])
        raw = fh.read(page.databytecounts[index])
        arr, indices, _shape = page.decode(raw, index)
        tile = _to_rgb_uint8(np.asarray(arr)[0])   # (depth, h, w, s) -> (h, w, 3)
        ty, tx = int(indices[2]), int(indices[3])

        if len(self._cache) >= self._cache_limit:
            self._cache.clear()   # crude but bounded; an LRU buys little here
        self._cache[key] = (tile, tx, ty)
        return tile, tx, ty

    def read_region(self, x: int, y: int, level: int, w: int, h: int) -> np.ndarray:
        page = self._pages[level]
        ds = self.level_downsamples[level]
        lx, ly = int(x / ds), int(y / ds)
        lw, lh = self.level_dimensions[level]

        out = np.full((h, w, 3), 255, dtype=np.uint8)  # off-slide reads as glass

        if not page.is_tiled:
            # Striped TIFF has no per-tile random access. Only the small
            # pyramid levels are usually striped, so a whole-level read is
            # acceptable here and nowhere else.
            full = _to_rgb_uint8(np.asarray(page.asarray()))
            y1, x1 = min(lh, ly + h), min(lw, lx + w)
            if y1 > ly and x1 > lx:
                crop = full[max(0, ly):y1, max(0, lx):x1]
                out[: crop.shape[0], : crop.shape[1]] = crop
            return out

        tw, th = int(page.tilewidth), int(page.tilelength)
        tiles_across = (lw + tw - 1) // tw
        tiles_down = (lh + th - 1) // th

        col0 = max(0, lx // tw)
        col1 = min(tiles_across - 1, (lx + w - 1) // tw)
        row0 = max(0, ly // th)
        row1 = min(tiles_down - 1, (ly + h - 1) // th)

        for row in range(row0, row1 + 1):
            for col in range(col0, col1 + 1):
                index = row * tiles_across + col
                if index >= len(page.dataoffsets):
                    continue
                tile, tx, ty = self._decode_tile(level, index)

                # Intersect stored tile with requested window in level pixels,
                # then copy into the output at the matching offset.
                sx0, sy0 = max(lx, tx), max(ly, ty)
                sx1 = min(lx + w, tx + tile.shape[1], lw)
                sy1 = min(ly + h, ty + tile.shape[0], lh)
                if sx1 <= sx0 or sy1 <= sy0:
                    continue
                out[sy0 - ly:sy1 - ly, sx0 - lx:sx1 - lx] = tile[
                    sy0 - ty:sy1 - ty, sx0 - tx:sx1 - tx
                ]
        return out

    def close(self) -> None:
        self._cache.clear()
        self._tif.close()


class OpenSlideReader(SlideReader):
    """Wraps openslide.OpenSlide in the same interface."""

    def __init__(self, path: Path):
        self._s = openslide.OpenSlide(str(path))

    @property
    def level_count(self) -> int:
        return self._s.level_count

    @property
    def level_dimensions(self) -> list[tuple[int, int]]:
        return [tuple(d) for d in self._s.level_dimensions]

    @property
    def level_downsamples(self) -> list[float]:
        return list(self._s.level_downsamples)

    def read_region(self, x: int, y: int, level: int, w: int, h: int) -> np.ndarray:
        # OpenSlide returns RGBA with transparent padding outside the slide.
        # Compositing onto white matches what a pathologist expects to see.
        img = self._s.read_region((x, y), level, (w, h)).convert("RGBA")
        arr = np.asarray(img)
        alpha = arr[..., 3:4].astype(np.float32) / 255.0
        rgb = arr[..., :3].astype(np.float32)
        white = np.full_like(rgb, 255.0)
        return (rgb * alpha + white * (1 - alpha)).astype(np.uint8)

    def close(self) -> None:
        self._s.close()


def _to_rgb_uint8(arr: np.ndarray) -> np.ndarray:
    arr = np.asarray(arr)
    if arr.ndim == 2:
        arr = np.stack([arr] * 3, axis=-1)
    if arr.shape[-1] == 1:
        arr = np.repeat(arr, 3, axis=-1)
    if arr.shape[-1] == 4:
        arr = arr[..., :3]
    if arr.dtype != np.uint8:
        arr = np.clip(arr, 0, 255).astype(np.uint8)
    return arr


OPENSLIDE_SUFFIXES = {".svs", ".ndpi", ".mrxs", ".scn", ".vms", ".bif"}


def open_slide(path: str | Path) -> SlideReader:
    """Choose a backend for this file, or say clearly why we cannot."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    if path.suffix.lower() in OPENSLIDE_SUFFIXES:
        if not HAS_OPENSLIDE:
            raise RuntimeError(
                f"{path.suffix} requires OpenSlide, which is not installed. "
                "Install the system library "
                "(conda install -c conda-forge openslide-python), "
                "or supply a pyramidal TIFF instead."
            )
        return OpenSlideReader(path)
    return TiffReader(path)
