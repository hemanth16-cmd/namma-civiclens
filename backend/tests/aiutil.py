"""Deterministic helpers for the AI tests: synthetic street-like photos and a fake Roboflow provider.

Photos are random shapes (plenty of features for OpenCV) with a marker square in the top-left corner
drawn after any camera movement: white means "damage visible", black "no damage", grey "unclear". The fake
provider reads only that marker from the pixels it receives, so its answers do not depend on which
evidence row the image came from, just like a real model.
"""

import io

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

from app.ai.model_registry import AiSettings
from app.ai.roboflow_service import ProviderError

TEST_KEY = "rf_test_key_never_real_0123456789"
COMPLAINT_GPS = (13.100712, 77.596334)  # tests.test_complaints.payload()


def ai_settings(**env) -> AiSettings:
    return AiSettings.from_env({"CIVICLENS_AI_PROVIDER": "roboflow", "ROBOFLOW_API_KEY": TEST_KEY, **env})


def scene(seed: int, w: int = 1000, h: int = 750) -> Image.Image:
    rng = np.random.default_rng(seed)
    img = Image.new("RGB", (w, h), (120, 120, 120))
    d = ImageDraw.Draw(img)
    for _ in range(260):
        x, y, s = int(rng.integers(0, w)), int(rng.integers(0, h)), int(rng.integers(8, 60))
        c = tuple(int(v) for v in rng.integers(0, 255, 3))
        if rng.random() < 0.5:
            d.rectangle([x, y, x + s, y + s // 2], fill=c)
        else:
            d.ellipse([x, y, x + s, y + s], fill=c)
    return img


def photo(seed: int = 1, marker: str = "damage", moved: bool = False, dark: bool = False, gps: tuple | None = None) -> bytes:
    img = scene(seed)
    if moved:  # a second photo of the same place: shifted, slightly rotated, brighter
        img = img.transform(img.size, Image.AFFINE, (1, 0.03, 25, -0.02, 1, 15), resample=Image.BILINEAR)
        img = ImageEnhance.Brightness(img).enhance(1.2)
    if dark:
        img = ImageEnhance.Brightness(img.filter(ImageFilter.GaussianBlur(8))).enhance(0.15)
    ImageDraw.Draw(img).rectangle([0, 0, 79, 79], fill={"damage": (255, 255, 255), "clear": (0, 0, 0), "unclear": (128, 128, 128)}[marker])
    out = io.BytesIO()
    kwargs = {}
    if gps:
        exif = Image.Exif()
        g = exif.get_ifd(0x8825)

        def dms(v):
            v = abs(v)
            d = int(v)
            m = int((v - d) * 60)
            return (d, m, round(((v - d) * 60 - m) * 60, 4))
        g[1], g[2], g[3], g[4] = ("N" if gps[0] >= 0 else "S"), dms(gps[0]), ("E" if gps[1] >= 0 else "W"), dms(gps[1])
        kwargs["exif"] = exif
    img.save(out, "JPEG", quality=92, **kwargs)
    return out.getvalue()


def marker_of(jpeg: bytes) -> str:
    with Image.open(io.BytesIO(jpeg)) as img:
        scale = img.size[0] / 1000
        corner = np.asarray(img.convert("L").crop((5, 5, int(70 * scale), int(70 * scale))), dtype=np.float32).mean()
    return "damage" if corner > 200 else ("clear" if corner < 50 else "unclear")


class FakeProvider:
    """Answers like Roboflow: the 'pavement' detector in the hosted-model shape, others in the workflow shape."""

    name = "roboflow"

    def __init__(self, fail_with: list | None = None):
        self.calls = []          # (detector, marker, jpeg bytes)
        self.fail_with = list(fail_with or [])  # ProviderError codes to raise, one per call, before answering

    def detect(self, spec, jpeg, min_confidence: float = 0.1):
        marker = marker_of(jpeg)
        self.calls.append((spec.name, marker, jpeg))
        if self.fail_with:
            code = self.fail_with.pop(0)
            if code:
                raise ProviderError(code, retryable=code in ("timeout", "rate_limited", "provider_unavailable"))
        with Image.open(io.BytesIO(jpeg)) as img:
            w, h = img.size
        conf = {"damage": 0.91, "clear": None, "unclear": 0.35}[marker]
        cls = {"pothole": "potholes", "pavement": "pothole", "garbage": "garbage", "infrastructure": "pothole"}[spec.name]
        preds = [] if conf is None else [{"x": w * 0.5, "y": h * 0.55, "width": w * 0.15, "height": h * 0.12, "confidence": conf,
                                          "class": cls, "class_id": 0, "detection_id": "d1"}]
        body = {"image": {"width": w, "height": h}, "predictions": preds}
        if spec.mechanism == "model":
            return {"time": 0.05, **body}
        return {"outputs": [{"model_predictions": body, "count": len(preds)}]}


class NullExecutor:
    """Accepts jobs and never runs them (simulates a process that stopped before the queue drained)."""

    def __init__(self):
        self.jobs = []

    def submit(self, fn, *args):
        self.jobs.append((fn, args))

    def shutdown(self, wait=True, cancel_futures=False):
        pass
