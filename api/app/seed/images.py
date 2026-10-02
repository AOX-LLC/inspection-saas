"""Synthetic placeholder photos for the demo. Nothing here is a real photograph.

Each image is generated noise, softened and stamped "SYNTHETIC DEMO" so it can
never be mistaken for a site photo. Output is deterministic for a given seed.
"""

import io
import random

from PIL import Image, ImageDraw, ImageFilter, ImageFont

WIDTH, HEIGHT = 640, 480
STAMP = "SYNTHETIC DEMO"
JPEG_QUALITY = 70
EXIF_ORIENTATION = 0x0112


def render_demo_photo(
    caption: str, seed: int, *, width: int = WIDTH, height: int = HEIGHT
) -> Image.Image:
    """Grey concrete-like noise with a diagonal crack line and a stamp, at any size.

    Stamp, crack and footer scale with the width, so the 640 x 480 default is
    unchanged and a larger photo looks like a larger version of it.
    """
    scale = width / WIDTH
    rng = random.Random(seed)  # noqa: S311 (texture, not security)
    noise = Image.frombytes("L", (width, height), rng.randbytes(width * height))
    texture = noise.filter(ImageFilter.GaussianBlur(radius=1.5 * scale)).convert("RGB")
    tint = Image.new("RGB", (width, height), (120, 118, 112))
    image = Image.blend(texture, tint, 0.6)

    draw = ImageDraw.Draw(image)
    x, y = rng.randint(int(40 * scale), int(200 * scale)), 0
    points = [(x, y)]
    while y < height:
        x += rng.randint(int(-14 * scale), int(22 * scale))
        y += rng.randint(int(18 * scale), int(40 * scale))
        points.append((x, min(y, height)))
    draw.line(points, fill=(35, 33, 31), width=max(3, round(3 * scale)))

    big = ImageFont.load_default(size=round(56 * scale))
    small = ImageFont.load_default(size=round(22 * scale))
    footer = round(36 * scale)
    draw.text(
        (width // 2, height // 2),
        STAMP,
        font=big,
        fill=(255, 255, 255),
        anchor="mm",
        stroke_width=max(3, round(3 * scale)),
        stroke_fill=(0, 0, 0),
    )
    draw.rectangle((0, height - footer, width, height), fill=(0, 0, 0))
    draw.text(
        (round(12 * scale), height - footer // 2),
        f"{STAMP} - {caption}",
        font=small,
        fill=(255, 255, 255),
        anchor="lm",
    )
    return image


def encode_jpeg(image: Image.Image, *, orientation: int | None = None) -> bytes:
    """JPEG bytes, optionally tagged with an EXIF orientation as a phone would."""
    buffer = io.BytesIO()
    if orientation is None:
        image.save(buffer, format="JPEG", quality=JPEG_QUALITY)
    else:
        exif = Image.Exif()
        exif[EXIF_ORIENTATION] = orientation
        image.save(buffer, format="JPEG", quality=JPEG_QUALITY, exif=exif)
    return buffer.getvalue()


def render_demo_image(caption: str, seed: int) -> bytes:
    """A 640 x 480 JPEG for the seed data."""
    return encode_jpeg(render_demo_photo(caption, seed))
