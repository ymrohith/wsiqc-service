"""Tissue detection.

Most of a pathology slide is empty glass. Finding the tissue first is what
lets the pipeline skip 80-90% of the pixels, and that ratio is the whole
performance story.

Everything here is plain NumPy on purpose: no OpenCV, no scikit-image.
Fewer install failures, and every step is explainable in an interview.
"""

from __future__ import annotations

import numpy as np

from .reader import SlideReader


def saturation(rgb: np.ndarray) -> np.ndarray:
    """HSV saturation, 0-255, without a colour-space library.

    Glass is grey or white, so R, G and B are nearly equal and saturation is
    near zero. Haematoxylin and eosin are strongly coloured, so they are not.
    """
    a = rgb.astype(np.float32)
    mx = a.max(axis=-1)
    mn = a.min(axis=-1)
    with np.errstate(divide="ignore", invalid="ignore"):
        sat = np.where(mx > 0, (mx - mn) / mx, 0.0)
    return (sat * 255).astype(np.uint8)


def otsu_threshold(gray: np.ndarray) -> int:
    """Otsu's method: the cut that minimises within-class variance.

    Equivalent to maximising between-class variance, which is cheaper to
    compute -- one pass over a 256-bin histogram.
    """
    hist = np.bincount(gray.ravel(), minlength=256).astype(np.float64)
    total = hist.sum()
    if total == 0:
        return 0

    prob = hist / total
    omega = np.cumsum(prob)                      # class-0 weight per threshold
    mu = np.cumsum(prob * np.arange(256))        # class-0 cumulative mean
    mu_total = mu[-1]

    denom = omega * (1.0 - omega)
    with np.errstate(divide="ignore", invalid="ignore"):
        between = np.where(denom > 0, (mu_total * omega - mu) ** 2 / denom, 0.0)

    # On a clean bimodal image every cut between the two peaks scores
    # identically, so argmax would return the low edge of that plateau and sit
    # right against the darker mode. The centre is the stable choice.
    best = between.max()
    plateau = np.flatnonzero(between >= best - 1e-12)
    return int(round(float(plateau.mean())))


def _binary_morph(mask: np.ndarray, size: int, op: str) -> np.ndarray:
    """Square-kernel erode/dilate as a max/min over shifted views.

    A separable sliding window would be faster; this version is short enough
    to read, and the mask level is small.
    """
    if size < 2:
        return mask
    pad = size // 2
    padded = np.pad(mask, pad, mode="constant",
                    constant_values=(op == "erode"))
    out = np.empty_like(mask)
    h, w = mask.shape
    stack = np.empty((size * size, h, w), dtype=bool)
    k = 0
    for dy in range(size):
        for dx in range(size):
            stack[k] = padded[dy:dy + h, dx:dx + w]
            k += 1
    if op == "erode":
        np.all(stack, axis=0, out=out)
    else:
        np.any(stack, axis=0, out=out)
    return out


def open_close(mask: np.ndarray, size: int = 5) -> np.ndarray:
    """Opening removes speckle; closing fills pinholes inside tissue."""
    opened = _binary_morph(_binary_morph(mask, size, "erode"), size, "dilate")
    closed = _binary_morph(_binary_morph(opened, size, "dilate"), size, "erode")
    return closed


class TissueMask:
    """A boolean mask plus the level it was computed at.

    Holding the level is what lets callers translate between mask pixels and
    level-0 coordinates without guessing.
    """

    def __init__(self, mask: np.ndarray, level: int, downsample: float):
        self.mask = mask
        self.level = level
        self.downsample = downsample

    @property
    def coverage(self) -> float:
        return float(self.mask.mean())

    def fraction_in(self, x: int, y: int, size: int) -> float:
        """Tissue fraction of a level-0 box, looked up in mask space."""
        mx0 = int(x / self.downsample)
        my0 = int(y / self.downsample)
        step = max(1, int(size / self.downsample))
        window = self.mask[my0:my0 + step, mx0:mx0 + step]
        if window.size == 0:
            return 0.0
        return float(window.mean())


def tissue_mask(
    reader: SlideReader,
    target_downsample: float = 32.0,
    min_saturation: int = 12,
    morph_size: int = 5,
) -> TissueMask:
    """Build a tissue mask from a low-magnification view of the slide.

    target_downsample is roughly 1.25x for a 40x slide. Reading the mask at
    full resolution would defeat the point of having a mask.
    """
    level = reader.best_level_for_downsample(target_downsample)
    w, h = reader.level_dimensions[level]
    ds = reader.level_downsamples[level]

    thumb = reader.read_region(0, 0, level, w, h)
    sat = saturation(thumb)

    t = otsu_threshold(sat)
    # Otsu on a slide that is almost entirely glass can settle on a
    # meaningless cut in the noise, so hold a sanity floor.
    threshold = max(t, min_saturation)
    mask = sat > threshold
    mask = open_close(mask, morph_size)
    return TissueMask(mask=mask, level=level, downsample=ds)
