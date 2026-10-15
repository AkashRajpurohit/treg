"""Re-draw an uploaded picture from its pixels (Pillow).

The only module that decodes user images. Decoding is bounded (`domain.images.MAX_PIXELS` before the
pixels are loaded), only PNG, JPEG and WebP decoders are allowed, and the output is a fresh file
written from the pixels: metadata, colour profiles and anything appended to the original are gone.
Square kinds are fitted onto a transparent square (never stretched); wide kinds are cropped to their
shape from the top, the way the /showcase gallery frames a page.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps

from ..domain.images import (CONTENT_TYPES, MAX_PIXELS, ImageRejected, Kind, Output,
                             check_dimensions, check_upload, key_for)

Image.MAX_IMAGE_PIXELS = MAX_PIXELS          # Pillow refuses (DecompressionBombError) past this
_FORMATS = ("PNG", "JPEG", "WEBP")


@dataclass(frozen=True)
class Rendered:
    sha256: str
    files: list[tuple[str, bytes, str]]      # (key, bytes, content type), primary first


def _fit_square(img: Image.Image, size: int) -> Image.Image:
    img = img.copy()
    img.thumbnail((size, size), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(img, ((size - img.width) // 2, (size - img.height) // 2), img)
    return canvas


def _cover_top(img: Image.Image, width: int, height: int) -> Image.Image:
    return ImageOps.fit(img, (width, height), Image.Resampling.LANCZOS, centering=(0.5, 0.0))


def _encode(img: Image.Image, fmt: str) -> bytes:
    out = BytesIO()
    if fmt == "png":
        img.save(out, "PNG", optimize=True)
    else:
        img.save(out, "WEBP", quality=82, method=6)
    return out.getvalue()


def render(kind: Kind, data: bytes) -> Rendered:
    """Check, decode and re-draw `data` into every output of `kind`. Raises ImageRejected."""
    check_upload(kind, data)
    try:
        with Image.open(BytesIO(data), formats=_FORMATS) as src:
            check_dimensions(kind, *src.size)          # before the pixels are decoded
            src.load()
            img = ImageOps.exif_transpose(src).convert("RGBA")
    except ImageRejected:
        raise
    except Exception as exc:  # corrupt, truncated, a decompression bomb, an unsupported variant
        raise ImageRejected("unreadable", "the image could not be read") from exc
    rendered: list[tuple[Output, bytes]] = []
    for out in kind.outputs:
        square = out.width == out.height
        drawn = _fit_square(img, out.width) if square else _cover_top(img, out.width, out.height)
        if out.fmt != "png":   # no alpha in the lossy output: flatten onto white, never onto black
            flat = Image.new("RGBA", drawn.size, (255, 255, 255, 255))
            drawn = Image.alpha_composite(flat, drawn).convert("RGB")
        rendered.append((out, _encode(drawn, out.fmt)))
    sha = hashlib.sha256(rendered[0][1]).hexdigest()
    return Rendered(sha, [(key_for(kind, sha, out), body, CONTENT_TYPES[out.fmt]) for out, body in rendered])
