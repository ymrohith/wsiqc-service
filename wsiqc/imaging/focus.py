"""Focus scoring.

A sharp image has strong high-frequency content: sudden intensity changes at
cell and nucleus boundaries. Blur is a low-pass filter, so it removes exactly
that. The Laplacian is a second-derivative operator, and the variance of its
response is the standard cheap proxy for sharpness.

The score is not absolute. It depends on stain, tissue type and
magnification, which is why the threshold is derived from this slide's own
score distribution rather than hard-coded.
"""

from __future__ import annotations

import numpy as np


def to_gray(rgb: np.ndarray) -> np.ndarray:
    """Luminance weights, matching how the eye weights the channels."""
    a = rgb.astype(np.float32)
    return 0.299 * a[..., 0] + 0.587 * a[..., 1] + 0.114 * a[..., 2]


def laplacian(gray: np.ndarray) -> np.ndarray:
    """4-neighbour Laplacian by array shifting -- no convolution library.

    kernel:  0  1  0
             1 -4  1
             0  1  0
    """
    p = np.pad(gray, 1, mode="edge")
    return (
        p[:-2, 1:-1]
        + p[2:, 1:-1]
        + p[1:-1, :-2]
        + p[1:-1, 2:]
        - 4.0 * p[1:-1, 1:-1]
    )


def focus_score(rgb: np.ndarray) -> float:
    """Variance of the Laplacian. Higher is sharper."""
    return float(laplacian(to_gray(rgb)).var())


def blur_threshold(scores: np.ndarray, percentile: float = 15.0,
                   floor: float = 5.0) -> float:
    """Pick a blur cut-off from this slide's own distribution.

    Flags the worst `percentile` of tiles, but never calls a tile blurred
    when its absolute sharpness is comfortably high -- otherwise a perfectly
    scanned slide would still report 15% failures.
    """
    if scores.size == 0:
        return floor
    return float(max(np.percentile(scores, percentile), floor))
