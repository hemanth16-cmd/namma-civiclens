"""Do two photos show the same place? ORB feature matching plus a RANSAC homography (OpenCV).

ORB features are robust to moderate changes of camera angle, scale, lighting and resolution. Matches are
filtered with Lowe's ratio test, then a homography is fitted with RANSAC; the number and share of matches
that agree with it (inliers) measure how strongly the scenes correspond. The repaired spot itself usually
looks different, so the match relies on the surroundings (kerbs, walls, markings). A homography whose
shape is implausible (collapsed, mirrored or extremely stretched) is rejected.

The homography maps original-image pixels to repair-image pixels, so the original damage boxes can be
projected into the repair photo. Without OpenCV the result is {"available": False}, which the verifier
treats as "location not verified".
"""

import logging

from PIL import Image

logger = logging.getLogger("civiclens.ai")

WORK_SIDE = 800
MIN_MATCHES = 8


def _gray(img: Image.Image, np):
    g = img.convert("L")
    scale = min(1.0, WORK_SIDE / max(g.size))
    if scale < 1.0:
        g = g.resize((max(1, round(g.size[0] * scale)), max(1, round(g.size[1] * scale))), Image.Resampling.BILINEAR)
    return np.asarray(g, dtype=np.uint8), scale


def align(original: Image.Image, repair: Image.Image) -> dict:
    try:
        import cv2
        import numpy as np
    except ImportError:
        return {"available": False, "reason": "opencv_missing"}
    try:
        a, sa = _gray(original, np)
        b, sb = _gray(repair, np)
        orb = cv2.ORB_create(nfeatures=2000)
        ka, da = orb.detectAndCompute(a, None)
        kb, db = orb.detectAndCompute(b, None)
        base = {"available": True, "keypointsOriginal": len(ka or ()), "keypointsRepair": len(kb or ()),
                "goodMatches": 0, "inliers": 0, "inlierRatio": 0.0, "homography": None}
        if da is None or db is None or len(ka) < MIN_MATCHES or len(kb) < MIN_MATCHES:
            return base | {"reason": "too_few_features"}
        pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(da, db, k=2)
        good = [p[0] for p in pairs if len(p) == 2 and p[0].distance < 0.75 * p[1].distance]
        base["goodMatches"] = len(good)
        if len(good) < MIN_MATCHES:
            return base | {"reason": "too_few_matches"}
        src = np.float32([ka[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
        dst = np.float32([kb[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
        H, mask = cv2.findHomography(src, dst, cv2.RANSAC, 5.0)
        if H is None or mask is None:
            return base | {"reason": "no_homography"}
        inliers = int(mask.sum())
        base |= {"inliers": inliers, "inlierRatio": round(inliers / len(good), 3)}
        det = float(np.linalg.det(H[:2, :2]))
        if not 0.1 <= det <= 10 or abs(H[2, 0]) > 0.005 or abs(H[2, 1]) > 0.005:
            return base | {"reason": "implausible_geometry", "inliers": 0, "inlierRatio": 0.0}
        # Full-resolution mapping: original px -> work px (scale sa) -> repair work px (H) -> repair px (1/sb).
        full = np.diag([1 / sb, 1 / sb, 1.0]) @ H @ np.diag([sa, sa, 1.0])
        return base | {"homography": [[round(float(v), 6) for v in row] for row in full]}
    except cv2.error:
        logger.warning("Image alignment failed inside OpenCV")
        return {"available": False, "reason": "opencv_error"}


def project_box(bbox: dict, H: list) -> dict | None:
    """The axis-aligned box around `bbox` (original px) projected into the repair photo, or None."""
    corners = [(bbox["x"], bbox["y"]), (bbox["x"] + bbox["width"], bbox["y"]),
               (bbox["x"], bbox["y"] + bbox["height"]), (bbox["x"] + bbox["width"], bbox["y"] + bbox["height"])]
    pts = []
    for x, y in corners:
        w = H[2][0] * x + H[2][1] * y + H[2][2]
        if abs(w) < 1e-9:
            return None
        pts.append(((H[0][0] * x + H[0][1] * y + H[0][2]) / w, (H[1][0] * x + H[1][1] * y + H[1][2]) / w))
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return {"x": min(xs), "y": min(ys), "width": max(xs) - min(xs), "height": max(ys) - min(ys)}
