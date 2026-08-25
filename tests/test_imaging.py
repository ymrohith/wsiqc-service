"""Imaging tests.

These use synthetic images with known answers, so a failure points at the
algorithm rather than at the data.
"""

from __future__ import annotations

import numpy as np
import pytest

from wsiqc.imaging.analyze import analyze_slide
from wsiqc.imaging.focus import blur_threshold, focus_score, laplacian
from wsiqc.imaging.mask import otsu_threshold, saturation, tissue_mask
from wsiqc.imaging.reader import open_slide
from wsiqc.imaging.tiles import count_tiles, iter_tiles


def test_otsu_splits_a_two_peak_histogram():
    dark = np.full(500, 30, dtype=np.uint8)
    bright = np.full(500, 200, dtype=np.uint8)
    t = otsu_threshold(np.concatenate([dark, bright]))
    assert 30 < t < 200


def test_saturation_is_zero_for_grey():
    grey = np.full((8, 8, 3), 128, dtype=np.uint8)
    assert saturation(grey).max() == 0


def test_saturation_is_high_for_a_strong_colour():
    pink = np.zeros((8, 8, 3), dtype=np.uint8)
    pink[..., 0] = 220
    pink[..., 1] = 60
    pink[..., 2] = 150
    assert saturation(pink).min() > 100


def test_laplacian_is_zero_on_a_flat_field():
    flat = np.full((16, 16), 100.0)
    assert np.allclose(laplacian(flat), 0.0)


def test_sharp_scores_higher_than_blurred():
    rng = np.random.default_rng(0)
    sharp = rng.integers(0, 255, (64, 64, 3), dtype=np.uint8)
    # Averaging neighbours is a low-pass filter: this is blur, by definition.
    blurred = sharp.astype(np.float32)
    for _ in range(4):
        blurred = (
            blurred
            + np.roll(blurred, 1, 0)
            + np.roll(blurred, -1, 0)
            + np.roll(blurred, 1, 1)
            + np.roll(blurred, -1, 1)
        ) / 5.0
    assert focus_score(sharp) > focus_score(blurred.astype(np.uint8)) * 5


def test_blur_threshold_respects_its_floor():
    assert blur_threshold(np.array([], dtype=np.float32)) == pytest.approx(5.0)
    assert blur_threshold(np.array([0.1, 0.2, 0.3], dtype=np.float32)) >= 5.0


def test_reader_reports_a_pyramid(sample_slide):
    with open_slide(sample_slide) as r:
        assert r.level_count >= 2
        assert r.level_dimensions[0] == (1024, 768)
        assert r.level_downsamples[0] == 1.0
        assert r.level_downsamples[1] > 1.0


def test_read_region_shape_and_off_slide_padding(sample_slide):
    with open_slide(sample_slide) as r:
        tile = r.read_region(0, 0, 0, 128, 128)
        assert tile.shape == (128, 128, 3)
        assert tile.dtype == np.uint8

        # Entirely past the right edge: should come back as white glass.
        far = r.read_region(10_000, 10_000, 0, 64, 64)
        assert far.shape == (64, 64, 3)
        assert far.min() == 255


def test_tile_grid_skips_background(sample_slide):
    with open_slide(sample_slide) as r:
        mask = tissue_mask(r)
        total = count_tiles(r, 0, 128)
        kept = list(iter_tiles(r, mask, level=0, tile_size=128, min_tissue=0.10))
        assert 0 < len(kept) < total       # some tissue, but not the whole slide
        assert all(t.tissue_fraction >= 0.10 for t in kept)


def test_grid_is_lazy(sample_slide):
    """iter_tiles must be a generator -- materialising the grid is the bug
    this test exists to prevent."""
    with open_slide(sample_slide) as r:
        mask = tissue_mask(r)
        gen = iter_tiles(r, mask, level=0, tile_size=128)
        assert hasattr(gen, "__next__")
        next(gen)


def test_analyze_finds_the_blurred_band(sample_slide):
    report = analyze_slide(sample_slide, tile_size=128, target_downsample=1.0)
    assert report.tiles_analyzed > 0
    assert report.tiles_blurred > 0

    blurred_rows = [t.row for t in report.tiles if t.blurred]
    sharp_rows = [t.row for t in report.tiles if not t.blurred]
    # The synthetic slide blurs a band at 55-72% of its height, so blurred
    # tiles must sit lower down the grid than the typical sharp tile.
    assert np.mean(blurred_rows) > np.mean(sharp_rows)


def test_report_serialises_without_numpy_types(sample_slide):
    import json

    report = analyze_slide(sample_slide, tile_size=128, target_downsample=1.0)
    json.dumps(report.to_dict())  # raises if a float32 leaked through
