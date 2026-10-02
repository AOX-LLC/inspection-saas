"""The synthetic batch looks upright once each photo's EXIF orientation is applied."""

import io

import pytest
from PIL import Image, ImageChops, ImageOps, ImageStat

from app.seed.batch import batch_photos
from app.seed.images import render_demo_photo

EXIF_ORIENTATION = 0x0112


@pytest.fixture(scope="module")
def photos():
    return list(batch_photos(10))


def oriented(data: bytes) -> Image.Image:
    return ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")


def test_every_photo_is_displayed_at_the_size_it_declares(photos):
    for photo in photos:
        assert oriented(photo.data).size == (photo.width, photo.height), photo.filename


def test_the_sideways_photo_is_tagged_and_stored_landscape(photos):
    photo = photos[3]

    stored = Image.open(io.BytesIO(photo.data))

    assert stored.getexif().get(EXIF_ORIENTATION) == 6
    assert stored.size == (1600, 1200)
    assert (photo.width, photo.height) == (1200, 1600)


def test_the_sideways_photo_is_upright_once_oriented(photos):
    """Applying the tag must give the picture a viewer should see, not a sideways one."""
    photo = photos[3]

    shown = oriented(photo.data)

    upright = render_demo_photo("batch photo 03", 3, width=1200, height=1600)
    difference = ImageStat.Stat(ImageChops.difference(shown, upright)).mean
    assert max(difference) < 6  # the same picture, to within JPEG loss
    # And the footer strip, which belongs at the bottom, is at the bottom.
    top, bottom = shown.crop((0, 0, 1200, 40)), shown.crop((0, 1560, 1200, 1600))
    assert (
        ImageStat.Stat(bottom.convert("L")).mean[0] < 40 < ImageStat.Stat(top.convert("L")).mean[0]
    )


@pytest.mark.parametrize("index", [0, 1, 5])
def test_untagged_photos_are_stored_as_displayed(photos, index):
    stored = Image.open(io.BytesIO(photos[index].data))

    assert EXIF_ORIENTATION not in stored.getexif()
    assert stored.size == (photos[index].width, photos[index].height)
