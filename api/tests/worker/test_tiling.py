"""Tiling: where tiles go, how they map back to the original, and what is refused."""

import io
from itertools import pairwise

import pytest
from PIL import Image

from app.tiling import (
    LEVEL_FULL,
    LEVEL_OVERVIEW,
    ImageRejected,
    cut_tiles,
    open_oriented,
    plan_tiles,
    tile_image,
)
from tests.worker.imaging import encode, gradient, header_only_png, with_orientation

TILE, OVERLAP = 640, 128
LIMITS = {"tile_size": TILE, "overlap": OVERLAP, "max_tiles": 512}


def plan(width: int, height: int, **overrides):
    return plan_tiles(width, height, **{**LIMITS, **overrides})


def full_tiles(specs):
    return [s for s in specs if s.level == LEVEL_FULL]


# Planning ----------------------------------------------------------------------


def test_a_photo_no_bigger_than_a_tile_is_one_tile_with_no_overview():
    specs = plan(640, 480)

    assert len(specs) == 1
    only = specs[0]
    assert (only.level, only.x, only.y, only.width, only.height, only.scale) == (
        0,
        0,
        0,
        640,
        480,
        1,
    )


def test_a_photo_exactly_one_tile_square_is_one_tile():
    assert len(plan(640, 640)) == 1


def test_a_larger_photo_gets_a_grid_and_one_overview():
    specs = plan(1000, 700)

    assert sorted({s.x for s in full_tiles(specs)}) == [0, 360]
    assert sorted({s.y for s in full_tiles(specs)}) == [0, 60]
    assert [s.level for s in specs].count(LEVEL_OVERVIEW) == 1


def test_the_last_tile_on_each_axis_is_aligned_to_the_edge():
    specs = full_tiles(plan(2000, 1500))

    assert max(s.x for s in specs) == 2000 - TILE
    assert max(s.y for s in specs) == 1500 - TILE
    assert all(s.x + s.src_width <= 2000 and s.y + s.src_height <= 1500 for s in specs)


def test_neighbouring_tiles_overlap_by_at_least_the_configured_amount():
    for width in (700, 1000, 1280, 1281, 4000):
        xs = sorted({s.x for s in full_tiles(plan(width, 640))})
        gaps = [(a + TILE) - b for a, b in pairwise(xs)]
        assert all(gap >= OVERLAP for gap in gaps), (width, xs)


@pytest.mark.parametrize(("width", "height"), [(641, 641), (1000, 700), (1300, 640), (777, 1999)])
def test_every_pixel_is_inside_at_least_one_full_resolution_tile(width, height):
    covered = bytearray(width * height)
    for s in full_tiles(plan(width, height)):
        for row in range(s.y, s.y + s.src_height):
            start = row * width + s.x
            covered[start : start + s.src_width] = b"\x01" * s.src_width

    assert all(covered)


def test_the_overview_fits_the_whole_photo_inside_one_tile():
    overview = next(s for s in plan(2000, 1000) if s.level == LEVEL_OVERVIEW)

    assert (overview.x, overview.y, overview.src_width, overview.src_height) == (0, 0, 2000, 1000)
    assert overview.scale == pytest.approx(TILE / 2000)
    assert (overview.width, overview.height) == (640, 320)


def test_a_tall_photos_overview_is_fitted_by_its_height():
    overview = next(s for s in plan(500, 2000) if s.level == LEVEL_OVERVIEW)

    assert (overview.width, overview.height) == (160, 640)


def test_a_thin_strip_still_has_a_one_pixel_overview():
    overview = next(s for s in plan(100_000, 1, max_tiles=10_000) if s.level == LEVEL_OVERVIEW)

    assert overview.height == 1


def test_too_many_tiles_is_refused_before_any_are_planned():
    with pytest.raises(ImageRejected) as caught:
        plan(40_000, 1_000, max_tiles=100)

    assert caught.value.code == "too_many_tiles"


@pytest.mark.parametrize("overlap", [-1, 640, 700])
def test_an_overlap_that_leaves_no_stride_is_a_programming_error(overlap):
    with pytest.raises(ValueError, match="overlap"):
        plan(1000, 1000, overlap=overlap)


def test_a_smaller_tile_size_and_overlap_are_honoured():
    specs = full_tiles(plan(500, 500, tile_size=256, overlap=32))

    assert {s.src_width for s in specs} == {256}
    assert sorted({s.x for s in specs}) == [0, 224, 244]


# Mapping tiles back to the original ---------------------------------------------


def decode(jpeg: bytes) -> Image.Image:
    return Image.open(io.BytesIO(jpeg)).convert("RGB")


def test_a_full_resolution_tile_is_the_region_it_says_it_is():
    original = gradient(1000, 700)
    specs = plan(1000, 700)

    tiles = dict(cut_tiles(original, specs, quality=95))
    spec = next(s for s in tiles if (s.level, s.x, s.y) == (0, 360, 60))
    picture = decode(tiles[spec])

    assert picture.size == (640, 640)
    for tx, ty in ((0, 0), (100, 200), (639, 639)):
        expected = original.getpixel((spec.x + tx, spec.y + ty))
        got = picture.getpixel((tx, ty))
        assert all(abs(a - b) <= 4 for a, b in zip(expected, got, strict=True)), (tx, ty)


def test_a_point_in_the_overview_maps_back_with_x_plus_tx_over_scale():
    original = gradient(2000, 1200)
    specs = plan(2000, 1200)

    tiles = dict(cut_tiles(original, specs, quality=95))
    spec = next(s for s in tiles if s.level == LEVEL_OVERVIEW)
    picture = decode(tiles[spec])

    assert picture.size == (spec.width, spec.height)
    for tx, ty in ((10, 10), (320, 200), (600, 380)):
        original_x = round(spec.x + tx / spec.scale)
        original_y = round(spec.y + ty / spec.scale)
        expected = original.getpixel((original_x, original_y))
        got = picture.getpixel((tx, ty))
        assert abs(expected[0] - got[0]) <= 4 and abs(expected[1] - got[1]) <= 4, (tx, ty)


# Orientation -------------------------------------------------------------------


def marked(width: int, height: int) -> Image.Image:
    """Mid-grey with a red square near the top-left corner of the stored image."""
    image = Image.new("RGB", (width, height), (128, 128, 128))
    image.paste((255, 0, 0), (20, 30, 80, 90))
    return image


def is_red(pixel) -> bool:
    return pixel[0] > 200 and pixel[1] < 60 and pixel[2] < 60


def test_exif_orientation_is_applied_before_tiling():
    stored = with_orientation(marked(1200, 700), 6, quality=95)  # rotate 90 degrees clockwise

    image = open_oriented(stored, max_pixels=50_000_000)

    assert image.size == (700, 1200)  # width and height swap
    # The stored (20..80, 30..90) square lands at x 610..670, y 20..80.
    assert is_red(image.getpixel((640, 50)))
    assert not is_red(image.getpixel((50, 640)))


def test_a_tile_of_a_rotated_photo_shows_the_marker_where_its_position_says():
    stored = with_orientation(marked(1200, 700), 6, quality=95)
    recorded = []

    width, height, _ = tile_image(
        stored,
        tile_size=TILE,
        overlap=OVERLAP,
        quality=95,
        max_pixels=50_000_000,
        max_tiles=512,
        store=lambda spec, jpeg: recorded.append((spec, jpeg)),
    )

    assert (width, height) == (700, 1200)
    spec, jpeg = next((s, j) for s, j in recorded if (s.level, s.x, s.y) == (0, 0, 0))
    # Point (630, 50) of the oriented original is (630 - x, 50 - y) in this tile.
    assert is_red(decode(jpeg).getpixel((630 - spec.x, 50 - spec.y)))


@pytest.mark.parametrize("orientation", [2, 3, 4, 5, 6, 7, 8])
def test_every_exif_orientation_decodes(orientation):
    image = open_oriented(with_orientation(marked(300, 200), orientation), max_pixels=10**7)

    assert image.size in {(300, 200), (200, 300)}


def test_an_unrotated_photo_keeps_its_size():
    image = open_oriented(encode(marked(300, 200)), max_pixels=10**7)

    assert image.size == (300, 200)


# Formats and modes -------------------------------------------------------------


def test_a_transparent_png_is_flattened_onto_white():
    clear = Image.new("RGBA", (50, 50), (255, 0, 0, 0))

    image = open_oriented(encode(clear, "PNG"), max_pixels=10**7)

    assert image.mode == "RGB"
    assert image.getpixel((10, 10)) == (255, 255, 255)


@pytest.mark.parametrize("mode", ["L", "P", "1"])
def test_other_modes_become_rgb(mode):
    picture = Image.new(mode, (40, 40))

    assert open_oriented(encode(picture, "PNG"), max_pixels=10**7).mode == "RGB"


def test_webp_and_png_are_accepted():
    for fmt in ("WEBP", "PNG"):
        assert open_oriented(encode(marked(64, 64), fmt), max_pixels=10**7).size == (64, 64)


# What is refused ---------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not an image at all",
        b"<html><script>alert(1)</script></html>",
        b"GIF89a" + b"\x00" * 40,
        encode(Image.new("RGB", (8, 8)), "GIF"),
        encode(Image.new("RGB", (8, 8)), "BMP"),
        encode(Image.new("RGB", (64, 64), (9, 9, 9)))[:200],  # truncated JPEG
    ],
    ids=["empty", "text", "html", "gif-header", "gif", "bmp", "truncated"],
)
def test_a_file_that_is_not_an_accepted_image_is_unreadable(data):
    with pytest.raises(ImageRejected) as caught:
        open_oriented(data, max_pixels=10**7)

    assert caught.value.code == "unreadable_image"


def test_an_image_over_the_pixel_limit_is_refused_from_its_header_alone():
    bomb = header_only_png(9_000, 9_000)  # 81 megapixels, a few dozen bytes

    with pytest.raises(ImageRejected) as caught:
        open_oriented(bomb, max_pixels=50_000_000)

    assert caught.value.code == "too_many_pixels"
    assert len(bomb) < 200


def test_an_enormous_claimed_size_is_refused_too():
    with pytest.raises(ImageRejected) as caught:
        open_oriented(header_only_png(100_000, 100_000), max_pixels=50_000_000)

    assert caught.value.code == "too_many_pixels"


def test_an_image_just_inside_the_limit_is_accepted():
    assert open_oriented(encode(Image.new("RGB", (100, 100))), max_pixels=10_000).size == (100, 100)
    with pytest.raises(ImageRejected):
        open_oriented(encode(Image.new("RGB", (100, 101))), max_pixels=10_000)


def test_nothing_is_stored_for_a_refused_image():
    stored = []

    with pytest.raises(ImageRejected):
        tile_image(
            b"garbage",
            tile_size=TILE,
            overlap=OVERLAP,
            quality=85,
            max_pixels=10**7,
            max_tiles=512,
            store=lambda spec, jpeg: stored.append(spec),
        )

    assert stored == []


def test_a_photo_that_would_need_too_many_tiles_stores_nothing():
    stored = []
    wide = encode(Image.new("RGB", (4000, 100)), "PNG")

    with pytest.raises(ImageRejected) as caught:
        tile_image(
            wide,
            tile_size=TILE,
            overlap=OVERLAP,
            quality=85,
            max_pixels=10**7,
            max_tiles=3,
            store=lambda spec, jpeg: stored.append(spec),
        )

    assert caught.value.code == "too_many_tiles"
    assert stored == []


# What the tiles carry ----------------------------------------------------------


def test_tiles_carry_no_metadata_from_the_original():
    exif = Image.Exif()
    exif[0x0112] = 1
    exif[0x010F] = "Synthetic Camera Maker"
    exif[0x0132] = "2026:01:01 00:00:00"
    stored = encode(marked(900, 700), "JPEG", exif=exif)
    assert Image.open(io.BytesIO(stored)).getexif().get(0x010F) == "Synthetic Camera Maker"
    produced = []

    tile_image(
        stored,
        tile_size=TILE,
        overlap=OVERLAP,
        quality=85,
        max_pixels=10**7,
        max_tiles=512,
        store=lambda spec, jpeg: produced.append(jpeg),
    )

    assert produced
    for jpeg in produced:
        image = Image.open(io.BytesIO(jpeg))
        assert image.format == "JPEG"
        assert dict(image.getexif()) == {}
        assert b"Exif" not in jpeg[:64]
        assert b"Synthetic Camera Maker" not in jpeg
