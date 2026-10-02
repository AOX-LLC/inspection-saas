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


def render_demo_image(caption: str, seed: int) -> bytes:
    """A JPEG of grey concrete-like noise with a diagonal crack line and a stamp."""
    rng = random.Random(seed)  # noqa: S311 (texture, not security)
    noise = Image.frombytes("L", (WIDTH, HEIGHT), rng.randbytes(WIDTH * HEIGHT))
    texture = noise.filter(ImageFilter.GaussianBlur(radius=1.5)).convert("RGB")
    tint = Image.new("RGB", (WIDTH, HEIGHT), (120, 118, 112))
    image = Image.blend(texture, tint, 0.6)

    draw = ImageDraw.Draw(image)
    x, y = rng.randint(40, 200), 0
    points = [(x, y)]
    while y < HEIGHT:
        x += rng.randint(-14, 22)
        y += rng.randint(18, 40)
        points.append((x, min(y, HEIGHT)))
    draw.line(points, fill=(35, 33, 31), width=3)

    big = ImageFont.load_default(size=56)
    small = ImageFont.load_default(size=22)
    draw.text(
        (WIDTH // 2, HEIGHT // 2),
        STAMP,
        font=big,
        fill=(255, 255, 255),
        anchor="mm",
        stroke_width=3,
        stroke_fill=(0, 0, 0),
    )
    draw.rectangle((0, HEIGHT - 36, WIDTH, HEIGHT), fill=(0, 0, 0))
    draw.text(
        (12, HEIGHT - 18), f"{STAMP} - {caption}", font=small, fill=(255, 255, 255), anchor="lm"
    )

    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY)
    return buffer.getvalue()
