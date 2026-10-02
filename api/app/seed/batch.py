"""A batch of synthetic photos for exercising the whole pipeline.

Fifty by default, each stamped "SYNTHETIC DEMO" and numbered, generated from a
fixed seed so every run produces the same bytes. They are large enough to be
cut into several tiles (1600 x 1200, about the size of a phone photo shrunk for
a demo); every fifth is portrait, and every tenth is stored sideways with an
EXIF orientation tag, as phones do, so the orientation step is exercised too.
"""

from collections.abc import Iterator
from dataclasses import dataclass

from app.seed.images import encode_jpeg, render_demo_photo

BATCH_SIZE = 50
LANDSCAPE = (1600, 1200)
PORTRAIT = (1200, 1600)
# Stored landscape, displayed portrait: EXIF orientation 6 means "rotate 90 degrees clockwise".
ROTATE_CLOCKWISE = 6


@dataclass(frozen=True)
class BatchPhoto:
    index: int
    filename: str
    data: bytes
    # Size as displayed, after the EXIF orientation is applied.
    width: int
    height: int


def batch_photos(count: int = BATCH_SIZE, *, seed: int = 0) -> Iterator[BatchPhoto]:
    for index in range(count):
        portrait = index % 5 == 0
        sideways = index % 10 == 3
        # A sideways photo is stored landscape and shown portrait. As a phone does, the
        # displayed (upright) picture is turned back by the inverse of the tag, so that
        # applying the tag restores it: rotating 90 degrees counter-clockwise undoes
        # EXIF orientation 6, "rotate 90 degrees clockwise to display".
        stored = PORTRAIT if portrait and not sideways else LANDSCAPE
        drawn = (stored[1], stored[0]) if sideways else stored
        image = render_demo_photo(
            f"batch photo {index:02d}", seed * 1000 + index, width=drawn[0], height=drawn[1]
        )
        if sideways:
            image = image.rotate(90, expand=True)
        data = encode_jpeg(image, orientation=ROTATE_CLOCKWISE if sideways else None)
        shown = (stored[1], stored[0]) if sideways else stored
        yield BatchPhoto(index, f"synthetic-batch-{index:02d}.jpg", data, *shown)
