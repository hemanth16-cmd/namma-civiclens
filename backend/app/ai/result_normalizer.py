"""Provider responses -> the internal detection format stored in ai_inferences.predictions.

Roboflow's hosted model API returns {"image": {"width", "height"}, "predictions": [...]}. A workflow
returns {"outputs": [ {<output name>: ...}, ... ]} (one entry per input image) and the detections sit
under whichever output name the workflow defines, in the same serialised form. Rather than assume an
output name, the first object that has a "predictions" list is used. A response with no such object is
malformed (bad_response); an empty list is a valid "nothing found".

Each prediction is a box given by its centre (x, y), width and height in the coordinates of the image the
provider saw; segmentation results add "points". The internal format uses the top-left corner and the
size, in pixels of the original (orientation-corrected) evidence image:

    {"className": "pothole", "category": "Potholes", "confidence": 0.91,
     "bbox": {"x": 250.0, "y": 180.0, "width": 140.0, "height": 95.0}, "polygon": [[x, y], ...]}
"""

import math

from .model_registry import AiSettings
from .roboflow_service import ProviderError

MAX_DETECTIONS = 200
MAX_POLYGON_POINTS = 100
MAX_DEPTH = 6


def _find_detections(node, depth: int = 0):
    """(predictions list, coordinate space {"width","height"} or None), or None when absent."""
    if depth > MAX_DEPTH:
        return None
    if isinstance(node, dict):
        preds = node.get("predictions")
        if isinstance(preds, list):
            image = node.get("image") if isinstance(node.get("image"), dict) else None
            return preds, image
        if isinstance(preds, dict):
            found = _find_detections(preds, depth + 1)
            if found:
                return found
        for key, value in node.items():
            if key in ("predictions", "visualization", "image") or isinstance(value, str):
                continue
            found = _find_detections(value, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for item in node[:20]:
            found = _find_detections(item, depth + 1)
            if found:
                return found
    return None


def _num(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _polygon(points, fx: float, fy: float) -> list | None:
    if not isinstance(points, list) or len(points) < 3:
        return None
    step = max(1, math.ceil(len(points) / MAX_POLYGON_POINTS))
    out = []
    for p in points[::step]:
        x, y = (_num(p.get("x")), _num(p.get("y"))) if isinstance(p, dict) else (None, None)
        if x is None or y is None:
            return None
        out.append([round(x * fx, 1), round(y * fy, 1)])
    return out


def normalise(raw, settings: AiSettings, sent_size: tuple[int, int], original_size: tuple[int, int]) -> dict:
    """`sent_size` is the (width, height) uploaded to the provider; `original_size` the evidence image's.
    Raises ProviderError("bad_response") for a malformed response."""
    if isinstance(raw, dict) and isinstance(raw.get("outputs"), list):
        raw = raw["outputs"]
    found = _find_detections(raw)
    if found is None:
        raise ProviderError("bad_response")
    predictions, space = found
    sw, sh = sent_size
    if space:
        pw, ph = _num(space.get("width")), _num(space.get("height"))
        if pw and ph and pw > 0 and ph > 0:
            sw, sh = pw, ph
    ow, oh = original_size
    fx, fy = ow / sw, oh / sh

    detections, invalid = [], 0
    for p in predictions:
        if not isinstance(p, dict):
            invalid += 1
            continue
        x, y, w, h, conf = (_num(p.get(k)) for k in ("x", "y", "width", "height", "confidence"))
        name = p.get("class")
        if None in (x, y, w, h, conf) or not isinstance(name, str) or not name.strip() or w < 0 or h < 0 or not 0 <= conf <= 1:
            invalid += 1
            continue
        left, top = max(0.0, (x - w / 2) * fx), max(0.0, (y - h / 2) * fy)
        det = {"className": name.strip()[:100], "category": settings.category_for(name), "confidence": round(conf, 4),
               "bbox": {"x": round(left, 1), "y": round(top, 1),
                        "width": round(min(w * fx, ow - left), 1), "height": round(min(h * fy, oh - top), 1)}}
        poly = _polygon(p.get("points"), fx, fy)
        if poly:
            det["polygon"] = poly
        detections.append(det)
    if predictions and not detections:
        raise ProviderError("bad_response")
    detections.sort(key=lambda d: d["confidence"], reverse=True)
    detections = detections[:MAX_DETECTIONS]

    mapped = [d for d in detections if d["category"]]
    present = [d for d in mapped if d["confidence"] >= settings.min_confidence]
    top = mapped[0] if mapped else (detections[0] if detections else None)
    confidences = [d["confidence"] for d in detections]
    return {
        "detections": detections,
        "imageWidth": ow, "imageHeight": oh,
        "topClass": top["className"] if top else None,
        "topCategory": top["category"] if top else None,
        "topConfidence": top["confidence"] if top else None,
        "presentCount": len(present),
        "confidenceMeta": {
            "max": max(confidences) if confidences else None,
            "mean": round(sum(confidences) / len(confidences), 4) if confidences else None,
            "returned": len(detections), "mapped": len(mapped), "present": len(present),
            "skippedInvalid": invalid,
            "presentThreshold": settings.min_confidence, "lowThreshold": settings.low_confidence,
        },
    }
