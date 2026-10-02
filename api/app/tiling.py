"""Cut a photo into tiles for the detector, and say where each one came from.

Coordinates are pixels of the oriented original: the photo as a person sees it,
after the EXIF orientation is applied. A tile records the region it covers
(`x`, `y`, `src_width`, `src_height`) and the `scale` it was resized by. A point
at (`tx`, `ty`) in a tile is at

    original_x = x + tx / scale
    original_y = y + ty / scale

in the original, which is how a detected box is mapped back. The stored tile is
`src_* * scale` pixels, rounded, so the mapping is exact to under a pixel.

Level 0 tiles are full resolution (`scale` is 1) on a grid with a fixed overlap;
the last tile on each axis is aligned to the edge rather than left a sliver.
Level 1 is one overview of the whole photo, fitted inside a tile, so a defect
larger than a tile can still be seen whole. A photo no bigger than one tile has
only its single level 0 tile.
"""

import io
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from PIL import Image, ImageChops, ImageOps

ACCEPTED_FORMATS = ("JPEG", "PNG", "WEBP")
EXIF_ORIENTATION = 0x0112
LEVEL_FULL, LEVEL_OVERVIEW = 0, 1


class ImageRejected(Exception):
    """The file cannot be tiled, and trying again will not change that."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class TileSpec:
    level: int
    x: int
    y: int
    src_width: int
    src_height: int
    scale: float
    width: int
    height: int


def _axis_starts(length: int, tile_size: int, stride: int) -> list[int]:
    if length <= tile_size:
        return [0]
    starts = list(range(0, length - tile_size + 1, stride))
    if starts[-1] != length - tile_size:
        starts.append(length - tile_size)
    return starts


def plan_tiles(
    width: int, height: int, *, tile_size: int, overlap: int, max_tiles: int
) -> list[TileSpec]:
    """Where every tile goes, before any pixels are touched. Raises ImageRejected if too many."""
    if not 0 <= overlap < tile_size:
        raise ValueError("overlap must be at least 0 and smaller than the tile size")
    stride = tile_size - overlap
    xs = _axis_starts(width, tile_size, stride)
    ys = _axis_starts(height, tile_size, stride)
    # Counted before it is built: a 50000 x 1000 strip would otherwise plan thousands.
    overview = max(width, height) > tile_size
    if len(xs) * len(ys) + overview > max_tiles:
        raise ImageRejected("too_many_tiles")

    tile_width, tile_height = min(width, tile_size), min(height, tile_size)
    specs = [
        TileSpec(LEVEL_FULL, x, y, tile_width, tile_height, 1.0, tile_width, tile_height)
        for y in ys
        for x in xs
    ]
    if overview:
        scale = tile_size / max(width, height)
        specs.append(
            TileSpec(
                LEVEL_OVERVIEW,
                0,
                0,
                width,
                height,
                scale,
                max(1, round(width * scale)),
                max(1, round(height * scale)),
            )
        )
    return specs


def open_oriented(data: bytes, *, max_pixels: int) -> Image.Image:
    """Decode `data` as an upright RGB image, refusing anything over `max_pixels`.

    The pixel count is read from the header and checked before any decoding, so
    a decompression bomb costs a few kilobytes, not gigabytes. Only the formats
    uploads may have are tried. Raises ImageRejected.
    """
    # Pillow's own guard is a second line: it warns past this and errors at twice it.
    Image.MAX_IMAGE_PIXELS = max_pixels
    try:
        image = Image.open(io.BytesIO(data), formats=ACCEPTED_FORMATS)
        width, height = image.size
        if width * height > max_pixels:
            raise ImageRejected("too_many_pixels")
        image.load()
        if image.getexif().get(EXIF_ORIENTATION, 1) not in (None, 1):
            # Only here, because the transposed copy doubles the image's memory.
            image = ImageOps.exif_transpose(image)
        return _as_rgb(image)
    except ImageRejected:
        raise
    except Image.DecompressionBombError as error:
        raise ImageRejected("too_many_pixels") from error
    except (OSError, SyntaxError, ValueError) as error:
        # UnidentifiedImageError, truncated and corrupt files all land here.
        raise ImageRejected("unreadable_image") from error


def _as_rgb(image: Image.Image) -> Image.Image:
    if image.mode == "RGB":
        return image
    has_alpha_band = "A" in image.getbands()
    if has_alpha_band or "transparency" in image.info:
        # Flatten onto white without a full-size RGBA copy where the image already
        # has an alpha band: the colours, then white painted back through the
        # inverted alpha. Memory is the cost that matters here.
        alpha = image.getchannel("A") if has_alpha_band else image.convert("RGBA").getchannel("A")
        flattened = image.convert("RGB")
        flattened.paste((255, 255, 255), mask=ImageChops.invert(alpha))
        return flattened
    return image.convert("RGB")


def cut_tiles(
    image: Image.Image, specs: list[TileSpec], *, quality: int
) -> Iterator[tuple[TileSpec, bytes]]:
    """Each tile as JPEG bytes. Nothing from the original's metadata is carried over."""
    for spec in specs:
        region = image.crop((spec.x, spec.y, spec.x + spec.src_width, spec.y + spec.src_height))
        if (spec.width, spec.height) != (spec.src_width, spec.src_height):
            region = region.resize(
                (spec.width, spec.height), Image.Resampling.LANCZOS, reducing_gap=3.0
            )
        # crop and resize copy the original's `info`, which can hold a JPEG comment.
        region.info = {}
        buffer = io.BytesIO()
        region.save(buffer, format="JPEG", quality=quality)
        yield spec, buffer.getvalue()


def make_thumbnail(image: Image.Image, *, size: int, quality: int) -> bytes:
    """The whole photo fitted inside `size` pixels, as JPEG. Never enlarges, carries no metadata."""
    scale = min(1.0, size / max(image.size))
    target = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    # resize builds only the small result; the reducing gap lets a JPEG decode at reduced size.
    small = image.resize(target, Image.Resampling.LANCZOS, reducing_gap=3.0)
    small.info = {}
    buffer = io.BytesIO()
    small.save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


def tile_image(
    data: bytes,
    *,
    tile_size: int,
    overlap: int,
    quality: int,
    max_pixels: int,
    max_tiles: int,
    store: Callable[[TileSpec, bytes], None],
    thumbnail_size: int | None = None,
    store_thumbnail: Callable[[bytes], None] | None = None,
) -> tuple[int, int, list[TileSpec]]:
    """Decode, plan and cut `data`, handing each tile to `store`. Returns (width, height, specs).

    With `store_thumbnail`, one small preview of the whole photo goes to it as well,
    from the same decode, before the tiles.
    """
    image = open_oriented(data, max_pixels=max_pixels)
    specs = plan_tiles(*image.size, tile_size=tile_size, overlap=overlap, max_tiles=max_tiles)
    if store_thumbnail is not None and thumbnail_size is not None:
        store_thumbnail(make_thumbnail(image, size=thumbnail_size, quality=quality))
    for spec, jpeg in cut_tiles(image, specs, quality=quality):
        store(spec, jpeg)
    return image.size[0], image.size[1], specs
