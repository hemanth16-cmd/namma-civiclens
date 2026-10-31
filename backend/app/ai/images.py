"""Evidence images for AI analysis.

Files are read only through the private storage abstraction (EvidenceStorage.open_read), never through a
URL. Only JPEG photos are analysed; a video is never treated as an image. Before anything is sent to a
provider the photo is decoded, rotated upright (EXIF orientation), downscaled and re-encoded, which also
drops every EXIF field, including GPS: the provider receives pixels only.
"""

import io
import math
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

from ..evidence_access import open_evidence
from ..store import StoreError

MAX_PIXELS = 60_000_000          # refuse decompression bombs well before Pillow's own limit
READ_CHUNK = 256 * 1024


class ImageProblem(Exception):
    """The evidence cannot be analysed. `code` is stored as the inference error code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class PreparedImage:
    image: Image.Image            # upright RGB, full resolution
    width: int
    height: int
    upload_jpeg: bytes            # downscaled, EXIF-free JPEG for the provider
    upload_size: tuple[int, int]
    gps: tuple[float, float] | None   # from EXIF, when the file carries it
    quality: dict


def read_evidence(storages: dict, evidence: dict, max_bytes: int) -> bytes:
    """The stored file's bytes, from the backend that stored it. `evidence` is store._evidence_dict output."""
    if evidence.get("contentType") != "image/jpeg" or evidence.get("mediaType") != "photo":
        raise ImageProblem("unsupported_media")
    if evidence.get("byteSize", 0) > max_bytes:
        raise ImageProblem("image_too_large")
    try:  # the same backend selection and opening as staff and owner viewing (evidence_access.py)
        handle = open_evidence(storages, evidence)
    except StoreError:
        raise ImageProblem("evidence_unavailable") from None
    if handle is None:
        raise ImageProblem("evidence_unavailable")
    try:
        buf = bytearray()
        while chunk := handle.read(READ_CHUNK):
            buf.extend(chunk)
            if len(buf) > max_bytes:
                raise ImageProblem("image_too_large")
        return bytes(buf)
    except StoreError:
        raise ImageProblem("evidence_unavailable") from None
    finally:
        handle.close()


def _rational(value) -> float:
    if isinstance(value, tuple) and len(value) == 2:
        return value[0] / value[1] if value[1] else math.nan
    return float(value)


def exif_gps(img: Image.Image) -> tuple[float, float] | None:
    try:
        gps = img.getexif().get_ifd(0x8825)
        if not gps or 2 not in gps or 4 not in gps:
            return None
        def deg(dms, ref, neg):
            d, m, s = (_rational(v) for v in dms)
            val = d + m / 60 + s / 3600
            return -val if str(ref).upper().startswith(neg) else val
        lat, lng = deg(gps[2], gps.get(1, "N"), "S"), deg(gps[4], gps.get(3, "E"), "W")
    except Exception:  # malformed EXIF is common and never fatal
        return None
    if not (math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180) or (lat == 0 and lng == 0):
        return None
    return lat, lng


def quality_metrics(img: Image.Image) -> dict:
    """Simple, explainable quality signals: size, brightness and sharpness (variance of the Laplacian)."""
    import numpy as np

    gray = img.convert("L")
    gray.thumbnail((800, 800))
    a = np.asarray(gray, dtype=np.float32)
    lap = a[1:-1, 1:-1] * -4 + a[:-2, 1:-1] + a[2:, 1:-1] + a[1:-1, :-2] + a[1:-1, 2:]
    brightness = float(a.mean())
    sharpness = float(lap.var())
    problems = []
    if min(img.size) < 320:
        problems.append("low_resolution")
    if brightness < 35:
        problems.append("too_dark")
    elif brightness > 225:
        problems.append("overexposed")
    if sharpness < 20:
        problems.append("blurry")
    return {"width": img.size[0], "height": img.size[1], "brightness": round(brightness, 1),
            "sharpness": round(sharpness, 1), "problems": problems, "acceptable": not problems}


def prepare(data: bytes, max_side: int) -> PreparedImage:
    if data[:3] != b"\xff\xd8\xff":
        raise ImageProblem("unsupported_media")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            if probe.format != "JPEG":
                raise ImageProblem("unsupported_media")
            if probe.size[0] * probe.size[1] > MAX_PIXELS:
                raise ImageProblem("image_too_large")
            probe.verify()
        img = Image.open(io.BytesIO(data))
        gps = exif_gps(img)
        img = ImageOps.exif_transpose(img).convert("RGB")
    except ImageProblem:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise ImageProblem("invalid_image") from None
    small = img.copy()
    small.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    small.save(out, "JPEG", quality=90)  # no exif= argument: the copy carries no metadata
    return PreparedImage(image=img, width=img.size[0], height=img.size[1], upload_jpeg=out.getvalue(),
                         upload_size=small.size, gps=gps, quality=quality_metrics(img))


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in metres (haversine)."""
    lat1, lng1, lat2, lng2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(h))
