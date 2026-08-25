"""Generate a synthetic pyramidal 'slide' so the service runs offline.

Real whole-slide images are gigabytes and behind registration walls. This
produces a small pyramidal TIFF with the same shape of problem: mostly empty
glass, irregular stained tissue, and a deliberately out-of-focus band so the
QC step has something true to find.

    python scripts/make_sample_slide.py data/sample.tif --width 8192

Swap it for a real .svs (OpenSlide's CMU-1 sample) whenever one is available.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import tifffile


def _blur(a: np.ndarray, radius: int) -> np.ndarray:
    """Box blur by repeated cumulative sums -- cheap and dependency-free."""
    if radius < 1:
        return a
    out = a.astype(np.float32)
    for axis in (0, 1):
        pad = [(0, 0)] * out.ndim
        pad[axis] = (radius, radius)
        p = np.pad(out, pad, mode="edge")
        c = np.cumsum(p, axis=axis)
        sl_hi = [slice(None)] * out.ndim
        sl_lo = [slice(None)] * out.ndim
        sl_hi[axis] = slice(2 * radius, None)
        sl_lo[axis] = slice(0, -2 * radius)
        out = (c[tuple(sl_hi)] - c[tuple(sl_lo)]) / (2 * radius)
    return out


def synth_slide(width: int, height: int, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    img = np.full((height, width, 3), 244, dtype=np.float32)
    img += rng.normal(0, 2.0, img.shape)  # glass is never perfectly clean

    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    yn, xn = yy / height, xx / width

    # A few soft tissue blobs, off-centre so the mask has real work to do.
    tissue = np.zeros((height, width), dtype=np.float32)
    for cy, cx, r in [(0.38, 0.34, 0.22), (0.62, 0.58, 0.26), (0.30, 0.70, 0.15)]:
        d = np.sqrt((yn - cy) ** 2 + (xn - cx) ** 2)
        tissue += np.clip(1.0 - d / r, 0, 1) ** 1.5
    tissue = np.clip(tissue, 0, 1)
    tissue *= 0.75 + 0.25 * np.sin(xn * 47) * np.sin(yn * 41)
    tissue = np.clip(tissue, 0, 1)

    # H&E-ish: eosin pink cytoplasm with haematoxylin nuclei scattered through.
    eosin = np.stack([
        np.full_like(tissue, 226.0),
        np.full_like(tissue, 132.0),
        np.full_like(tissue, 170.0),
    ], axis=-1)
    nuclei = (rng.random((height, width)) < 0.010).astype(np.float32)
    nuclei = np.clip(_blur(nuclei, 3) * 22, 0, 1) * tissue
    hema = np.stack([
        np.full_like(tissue, 70.0),
        np.full_like(tissue, 48.0),
        np.full_like(tissue, 120.0),
    ], axis=-1)

    t3 = tissue[..., None]
    n3 = nuclei[..., None]
    img = img * (1 - t3) + eosin * t3
    img = img * (1 - n3) + hema * n3

    # A horizontal band that the scanner focused badly -- the thing QC exists
    # to catch. Roughly a fifth of the slide height.
    y0, y1 = int(height * 0.55), int(height * 0.72)
    img[y0:y1] = _blur(img[y0:y1], max(2, width // 400))

    return np.clip(img, 0, 255).astype(np.uint8)


def write_pyramid(arr: np.ndarray, path: Path, tile: int = 256, levels: int = 4) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tifffile.TiffWriter(str(path), bigtiff=True) as tif:
        opts = dict(tile=(tile, tile), compression="zlib", photometric="rgb")
        tif.write(arr, subifds=levels - 1, **opts)
        cur = arr
        for _ in range(levels - 1):
            cur = cur[::2, ::2]
            tif.write(cur, subfiletype=1, **opts)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("output", type=Path, nargs="?", default=Path("data/sample.tif"))
    ap.add_argument("--width", type=int, default=6144)
    ap.add_argument("--height", type=int, default=4608)
    args = ap.parse_args()

    arr = synth_slide(args.width, args.height)
    write_pyramid(arr, args.output)
    mb = args.output.stat().st_size / 1e6
    print(f"wrote {args.output}  {args.width}x{args.height}  {mb:.1f} MB")


if __name__ == "__main__":
    main()
