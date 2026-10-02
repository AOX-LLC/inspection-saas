"""Synthetic images for the tiling tests. Every pixel is generated; nothing is a photograph."""

import io
import struct
import zlib

from PIL import Image


def gradient(width: int, height: int) -> Image.Image:
    """Red rises left to right and green top to bottom, so a pixel's colour is its position."""
    red_row = bytes(x * 255 // max(width - 1, 1) for x in range(width))
    red = Image.frombytes("L", (width, height), red_row * height)
    green = Image.frombytes(
        "L",
        (width, height),
        b"".join(bytes([y * 255 // max(height - 1, 1)]) * width for y in range(height)),
    )
    return Image.merge("RGB", (red, green, Image.new("L", (width, height), 128)))


def encode(image: Image.Image, fmt: str = "JPEG", **options) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **options)
    return buffer.getvalue()


def with_orientation(image: Image.Image, orientation: int, **options) -> bytes:
    exif = Image.Exif()
    exif[0x0112] = orientation
    return encode(image, "JPEG", exif=exif, **options)


def header_only_png(width: int, height: int) -> bytes:
    """A PNG whose header claims a size and whose body is a few bytes: a decompression bomb."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"\x00" * 16))
        + chunk(b"IEND", b"")
    )
