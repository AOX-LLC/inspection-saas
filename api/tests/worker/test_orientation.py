"""All eight EXIF orientations: thumbnail and tiles agree with the photo's true orientation.

The expected positions come from the EXIF specification, written out here, not from Pillow:
a pixel at (sx, sy) of a stored image W wide and H high is displayed at the position below.
Two markers (red near the stored top-left, blue near the stored bottom-right) are placed in a
grey photo, and each must be found, in the oriented photo, in the thumbnail, and in the tile
(and the overview) that covers it, at the position the specification gives.
"""

import io

import pytest
from PIL import Image

from app.tiling import LEVEL_FULL, LEVEL_OVERVIEW, open_oriented, tile_image
from tests.worker.imaging import with_orientation

STORED = (1500, 900)  # wider than a tile each way, so there are several tiles and an overview
TILE, OVERLAP = 640, 128
RED, BLUE = (255, 0, 0), (0, 0, 255)
RED_AT = (50, 60)
BLUE_AT = (STORED[0] - 70, STORED[1] - 40)
HALF = 30  # markers are 60 x 60

# orientation -> (displayed width, height) -> where a stored point (sx, sy) is displayed
DISPLAY = {
    1: lambda w, h, x, y: (x, y),
    2: lambda w, h, x, y: (w - 1 - x, y),  # mirrored left to right
    3: lambda w, h, x, y: (w - 1 - x, h - 1 - y),  # turned 180 degrees
    4: lambda w, h, x, y: (x, h - 1 - y),  # mirrored top to bottom
    5: lambda w, h, x, y: (y, x),  # transposed
    6: lambda w, h, x, y: (h - 1 - y, x),  # turned 90 degrees clockwise
    7: lambda w, h, x, y: (h - 1 - y, w - 1 - x),  # transversed
    8: lambda w, h, x, y: (y, w - 1 - x),  # turned 90 degrees counter-clockwise
}
SWAPS_AXES = {5, 6, 7, 8}


def stored_photo() -> Image.Image:
    image = Image.new("RGB", STORED, (128, 128, 128))
    for colour, (x, y) in ((RED, RED_AT), (BLUE, BLUE_AT)):
        image.paste(colour, (x - HALF, y - HALF, x + HALF, y + HALF))
    return image


def displayed_markers(orientation: int) -> tuple[tuple[int, int], dict[str, tuple[int, int]]]:
    w, h = STORED
    size = (h, w) if orientation in SWAPS_AXES else (w, h)
    where = DISPLAY[orientation]
    return size, {
        "red": where(w, h, *RED_AT),
        "blue": where(w, h, *BLUE_AT),
    }


def is_colour(pixel, colour) -> bool:
    return all(abs(a - b) < 40 for a, b in zip(pixel[:3], colour, strict=True))


def decode(jpeg: bytes) -> Image.Image:
    return Image.open(io.BytesIO(jpeg)).convert("RGB")


@pytest.fixture(params=range(1, 9), ids=lambda o: f"orientation-{o}")
def orientation(request) -> int:
    return request.param


def run(orientation: int):
    data = with_orientation(stored_photo(), orientation, quality=95)
    tiles, thumbnails = [], []
    width, height, specs = tile_image(
        data,
        tile_size=TILE,
        overlap=OVERLAP,
        quality=95,
        max_pixels=10**8,
        max_tiles=512,
        store=lambda spec, jpeg: tiles.append((spec, jpeg)),
        thumbnail_size=300,
        store_thumbnail=thumbnails.append,
    )
    return data, (width, height), specs, tiles, thumbnails


def test_the_recorded_size_is_the_displayed_size(orientation):
    _, size, _, _, _ = run(orientation)

    assert size == displayed_markers(orientation)[0]


def test_the_oriented_photo_has_each_marker_where_the_specification_says(orientation):
    data, _, _, _, _ = run(orientation)
    image = open_oriented(data, max_pixels=10**8)
    size, markers = displayed_markers(orientation)

    assert image.size == size
    assert is_colour(image.getpixel(markers["red"]), RED)
    assert is_colour(image.getpixel(markers["blue"]), BLUE)


def test_the_thumbnail_agrees(orientation):
    _, size, _, _, thumbnails = run(orientation)
    (jpeg,) = thumbnails
    thumbnail = decode(jpeg)
    _, markers = displayed_markers(orientation)

    scale = 300 / max(size)
    assert thumbnail.size == (round(size[0] * scale), round(size[1] * scale))
    for name, colour in (("red", RED), ("blue", BLUE)):
        x, y = markers[name]
        assert is_colour(thumbnail.getpixel((round(x * scale), round(y * scale))), colour), name


def test_each_marker_is_in_its_tiles_at_the_recorded_coordinates(orientation):
    """original = origin + tile_px / scale, for every tile that covers the marker."""
    _, _, _, tiles, _ = run(orientation)
    _, markers = displayed_markers(orientation)

    for name, colour in (("red", RED), ("blue", BLUE)):
        x, y = markers[name]
        covering = [
            (spec, jpeg)
            for spec, jpeg in tiles
            if spec.x <= x < spec.x + spec.src_width and spec.y <= y < spec.y + spec.src_height
        ]
        assert any(spec.level == LEVEL_FULL for spec, _ in covering), name
        assert any(spec.level == LEVEL_OVERVIEW for spec, _ in covering), name
        for spec, jpeg in covering:
            tile = decode(jpeg)
            point = (round((x - spec.x) * spec.scale), round((y - spec.y) * spec.scale))
            assert is_colour(tile.getpixel(point), colour), (name, spec)
            # And back: the tile pixel maps to the marker's centre, to within a pixel of scale.
            assert abs(spec.x + point[0] / spec.scale - x) <= 1 / spec.scale
            assert abs(spec.y + point[1] / spec.scale - y) <= 1 / spec.scale


def test_every_tile_lies_inside_the_displayed_photo(orientation):
    _, size, specs, _, _ = run(orientation)

    assert all(s.x + s.src_width <= size[0] and s.y + s.src_height <= size[1] for s in specs)
