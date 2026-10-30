"""AI building blocks without HTTP or a database: configuration and routing, the Roboflow client (against
a mocked transport, never the network), response normalisation, image handling, alignment and the
before/after decision rules."""

import base64
import io
import json
import logging

import httpx
import pytest
from PIL import Image

from app.ai.image_alignment import align, project_box
from app.ai.images import ImageProblem, prepare, read_evidence
from app.ai.model_registry import AiConfigError, AiSettings
from app.ai.repair_verifier import assess
from app.ai.result_normalizer import normalise
from app.ai.roboflow_service import ProviderError, RoboflowClient
from tests.aiutil import COMPLAINT_GPS, TEST_KEY, ai_settings, photo


# ---------- Configuration, model selection and class mapping ----------

def test_ai_is_off_by_default():
    s = AiSettings.from_env({})
    assert not s.enabled and s.disabled_reason == "disabled"


def test_missing_api_key_disables_ai_with_a_reason(caplog):
    with caplog.at_level(logging.WARNING):
        s = AiSettings.from_env({"CIVICLENS_AI_PROVIDER": "roboflow"})
    assert not s.enabled and s.disabled_reason == "not_configured"
    assert "ROBOFLOW_API_KEY" in caplog.text


def test_default_detectors_match_the_teams_tested_roboflow_calls():
    s = ai_settings()
    d = s.detectors
    assert set(d) == {"pothole", "pavement", "garbage", "infrastructure"}
    assert (d["pavement"].mechanism, d["pavement"].model_id, d["pavement"].version) == ("model", "pavement-gvib5/1", "1")
    assert (d["pothole"].mechanism, d["pothole"].identifier, d["pothole"].classes) == (
        "workflow", "mohammed-arshad-nb1p3/general-segmentation-api-18", "potholes")
    assert d["garbage"].identifier.endswith("/general-segmentation-api-20") and d["garbage"].classes == "box, garbage, z"
    assert d["infrastructure"].identifier.endswith("/general-segmentation-api-19")
    assert d["infrastructure"].classes == "pothole, graffiti, damaged-sign, electricity-pole-damage, fallen-trees"
    assert "api_key" not in repr(s) and TEST_KEY not in repr(s)


def test_category_routing():
    s = ai_settings()
    assert [x.name for x in s.detectors_for("Potholes")] == ["pothole", "pavement"]
    assert [x.name for x in s.detectors_for("Illegal dumping")] == ["garbage"]
    assert [x.name for x in s.detectors_for("Broken streetlights")] == ["infrastructure"]
    assert s.detectors_for("Water leakage") == [], "no detector can see water; left to people"


def test_routes_models_and_class_map_are_configurable():
    s = ai_settings(ROBOFLOW_GARBAGE_MODE="model", ROBOFLOW_GARBAGE_MODEL_ID="garbage-dumping-6avsy/3",
                    ROBOFLOW_POTHOLE_CLASSES="pothole", CIVICLENS_AI_DETECTORS="pothole,garbage",
                    CIVICLENS_AI_CATEGORY_ROUTES=json.dumps({"Water leakage": {"detectors": ["infrastructure"], "relevant": ["Water leakage"]}}),
                    CIVICLENS_AI_CLASS_MAP=json.dumps({"Box": "Garbage accumulation", "z": None}))
    assert s.detectors["garbage"].identifier == "garbage-dumping-6avsy/3" and s.detectors["pothole"].classes == "pothole"
    assert [x.name for x in s.detectors_for("Potholes")] == ["pothole"], "disabled detectors are dropped from routes"
    assert s.detectors_for("Water leakage") == [], "routes to a disabled detector run nothing"
    assert s.category_for("box") == "Garbage accumulation" and s.category_for("z") is None
    assert s.category_for("Alligator_Crack") == "Road cracks and damage" and s.category_for("car") is None


@pytest.mark.parametrize("env, message", [
    ({"CIVICLENS_AI_PROVIDER": "openai"}, "CIVICLENS_AI_PROVIDER"),
    ({"ROBOFLOW_PAVEMENT_MODEL_ID": "pavement-gvib5"}, "ROBOFLOW_PAVEMENT_MODEL_ID"),
    ({"ROBOFLOW_POTHOLE_MODE": "magic"}, "ROBOFLOW_POTHOLE_MODE"),
    ({"CIVICLENS_AI_DETECTORS": "pothole,lidar"}, "CIVICLENS_AI_DETECTORS"),
    ({"CIVICLENS_AI_CLASS_MAP": "{not json"}, "CIVICLENS_AI_CLASS_MAP"),
    ({"CIVICLENS_AI_CLASS_MAP": '{"x": "Volcanoes"}'}, "CIVICLENS_AI_CLASS_MAP"),
    ({"CIVICLENS_AI_CATEGORY_ROUTES": '{"Potholes": {"detectors": ["sonar"]}}'}, "CIVICLENS_AI_CATEGORY_ROUTES"),
    ({"ROBOFLOW_API_URL": "http://serverless.roboflow.com"}, "ROBOFLOW_API_URL"),
    ({"CIVICLENS_AI_MIN_CONFIDENCE": "2"}, "CIVICLENS_AI_MIN_CONFIDENCE"),
])
def test_invalid_configuration_is_refused_with_the_variable_named(env, message):
    with pytest.raises(AiConfigError, match=message) as exc:
        ai_settings(**env)
    assert TEST_KEY not in str(exc.value)


# ---------- Roboflow client (mocked HTTP) ----------

def client_with(handler, **env):
    sleeps = []
    return RoboflowClient(ai_settings(**env), transport=httpx.MockTransport(handler), sleep=sleeps.append), sleeps


def test_model_inference_request_uses_header_auth_and_base64_body():
    seen = {}

    def handler(request):
        seen.update(url=str(request.url), auth=request.headers.get("authorization"), ctype=request.headers.get("content-type"),
                    body=request.content)
        return httpx.Response(200, json={"predictions": []})

    client, _ = client_with(handler)
    jpeg = photo()
    assert client.detect(ai_settings().detectors["pavement"], jpeg) == {"predictions": []}
    assert seen["url"].startswith("https://serverless.roboflow.com/pavement-gvib5/1?")
    assert seen["auth"] == f"Bearer {TEST_KEY}"
    assert TEST_KEY not in seen["url"] and b"api_key" not in seen["body"]
    assert seen["ctype"] == "application/x-www-form-urlencoded" and base64.b64decode(seen["body"]) == jpeg


def test_workflow_request_sends_image_and_class_prompt():
    seen = {}

    def handler(request):
        seen.update(url=str(request.url), auth=request.headers.get("authorization"), body=json.loads(request.content))
        return httpx.Response(200, json={"outputs": [{"predictions": {"image": {"width": 10, "height": 10}, "predictions": []}}]})

    client, _ = client_with(handler)
    client.detect(ai_settings().detectors["infrastructure"], b"\xff\xd8\xffjpeg")
    assert seen["url"] == "https://serverless.roboflow.com/mohammed-arshad-nb1p3/workflows/general-segmentation-api-19"
    assert seen["auth"] == f"Bearer {TEST_KEY}" and "api_key" not in seen["body"]
    assert seen["body"]["inputs"]["image"]["type"] == "base64"
    assert seen["body"]["inputs"]["classes"] == "pothole, graffiti, damaged-sign, electricity-pole-damage, fallen-trees"
    assert seen["body"]["use_cache"] is True


@pytest.mark.parametrize("status, code, attempts", [
    (401, "auth_failed", 1), (403, "auth_failed", 1), (404, "model_not_found", 1), (422, "rejected_by_provider", 1),
    (503, "provider_unavailable", 2), (500, "provider_unavailable", 2),
])
def test_http_errors_map_to_codes_with_bounded_retries(status, code, attempts, caplog):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(status, text=f"error body mentioning {TEST_KEY}")

    client, _ = client_with(handler)
    with caplog.at_level(logging.WARNING), pytest.raises(ProviderError) as exc:
        client.detect(ai_settings().detectors["pavement"], b"\xff\xd8\xff")
    assert exc.value.code == code and len(calls) == attempts
    assert TEST_KEY not in str(exc.value) and TEST_KEY not in caplog.text


def test_rate_limit_is_retried_once_after_a_short_retry_after():
    responses = [httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(200, json={"predictions": []})]
    client, sleeps = client_with(lambda r: responses.pop(0))
    assert client.detect(ai_settings().detectors["pavement"], b"\xff\xd8\xff") == {"predictions": []}
    assert sleeps == [2.0]


def test_long_retry_after_fails_fast_as_rate_limited():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "120"})

    client, sleeps = client_with(handler)
    with pytest.raises(ProviderError) as exc:
        client.detect(ai_settings().detectors["pavement"], b"\xff\xd8\xff")
    assert exc.value.code == "rate_limited" and exc.value.retryable and len(calls) == 1 and sleeps == []


def test_timeout_and_unreachable_provider():
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    def refused(request):
        raise httpx.ConnectError("refused", request=request)

    for handler, code in ((timeout, "timeout"), (refused, "provider_unreachable")):
        client, sleeps = client_with(handler)
        with pytest.raises(ProviderError) as exc:
            client.detect(ai_settings().detectors["pothole"], b"\xff\xd8\xff")
        assert exc.value.code == code and sleeps == [1.0], "one retry, then give up"


def test_non_json_response_is_bad_response():
    client, _ = client_with(lambda r: httpx.Response(200, text="<html>gateway</html>"))
    with pytest.raises(ProviderError, match="bad_response"):
        client.detect(ai_settings().detectors["pavement"], b"\xff\xd8\xff")


def test_missing_key_never_calls_the_provider():
    settings = AiSettings.from_env({"CIVICLENS_AI_PROVIDER": "roboflow"})
    calls = []
    client = RoboflowClient(settings, transport=httpx.MockTransport(lambda r: calls.append(1) or httpx.Response(200, json={})))
    with pytest.raises(ProviderError, match="not_configured"):
        client.detect(settings.detectors["pavement"], b"\xff\xd8\xff")
    assert calls == []


# ---------- Normalisation ----------

def test_model_response_is_normalised_to_top_left_boxes_in_original_pixels():
    raw = {"image": {"width": 500, "height": 375}, "predictions": [
        {"x": 250, "y": 200, "width": 100, "height": 50, "confidence": 0.8, "class": "pothole"},
        {"x": 10, "y": 10, "width": 4, "height": 4, "confidence": 0.3, "class": "car"}]}
    out = normalise(raw, ai_settings(), sent_size=(500, 375), original_size=(1000, 750))
    first = out["detections"][0]
    assert first == {"className": "pothole", "category": "Potholes", "confidence": 0.8,
                     "bbox": {"x": 400.0, "y": 350.0, "width": 200.0, "height": 100.0}}
    assert out["detections"][1]["category"] is None, "unmapped classes are kept but never count as damage"
    assert (out["topClass"], out["topCategory"], out["topConfidence"], out["presentCount"]) == ("pothole", "Potholes", 0.8, 1)
    assert out["confidenceMeta"]["returned"] == 2 and out["confidenceMeta"]["mapped"] == 1


def test_workflow_response_with_segmentation_points_is_found_under_any_output_name():
    raw = {"outputs": [{"sam_output": {"image": {"width": 100, "height": 100}, "predictions": [
        {"x": 50, "y": 50, "width": 20, "height": 10, "confidence": 0.66, "class": "garbage",
         "points": [{"x": 40, "y": 45}, {"x": 60, "y": 45}, {"x": 60, "y": 55}, {"x": 40, "y": 55}]}]},
        "visualization": "base64..."}]}
    out = normalise(raw, ai_settings(), sent_size=(100, 100), original_size=(200, 200))
    det = out["detections"][0]
    assert det["category"] == "Garbage accumulation" and det["bbox"] == {"x": 80.0, "y": 90.0, "width": 40.0, "height": 20.0}
    assert det["polygon"] == [[80.0, 90.0], [120.0, 90.0], [120.0, 110.0], [80.0, 110.0]]


def test_empty_predictions_are_a_valid_result():
    out = normalise({"outputs": [{"x": {"predictions": []}}]}, ai_settings(), (10, 10), (10, 10))
    assert out["detections"] == [] and out["topConfidence"] is None and out["presentCount"] == 0


@pytest.mark.parametrize("raw", [
    {"outputs": [{"count": 3}]},
    {"predictions": [{"x": "a", "y": 1, "width": 1, "height": 1, "confidence": 0.5, "class": "pothole"}]},
    {"predictions": [{"x": 1, "y": 1, "width": 1, "height": 1, "confidence": 7, "class": "pothole"}]},
    {"predictions": [{"x": 1, "y": 1, "width": 1, "height": 1, "confidence": 0.5}]},
    "not an object",
    [],
])
def test_malformed_responses_are_rejected(raw):
    with pytest.raises(ProviderError, match="bad_response"):
        normalise(raw, ai_settings(), (10, 10), (10, 10))


# ---------- Images ----------

class _Storage:
    name = "local"

    def __init__(self, data):
        self.data = data

    def open_read(self, key):
        return io.BytesIO(self.data)


def _evidence(**kw):
    return {"contentType": "image/jpeg", "mediaType": "photo", "byteSize": 10, "storageBackend": "local", "storageKey": "k", **kw}


def test_only_jpeg_photos_are_read_and_videos_are_never_treated_as_images():
    storages = {"local": _Storage(photo())}
    assert read_evidence(storages, _evidence(), 10**8)[:3] == b"\xff\xd8\xff"
    for ev, code in ((_evidence(contentType="video/mp4", mediaType="video"), "unsupported_media"),
                     (_evidence(byteSize=10**9), "image_too_large"),
                     (_evidence(storageBackend="supabase"), "evidence_unavailable")):
        with pytest.raises(ImageProblem) as exc:
            read_evidence(storages, ev, 10**8)
        assert exc.value.code == code
    with pytest.raises(ImageProblem, match="image_too_large"):
        read_evidence({"local": _Storage(b"\xff" * 5000)}, _evidence(), 1000)


@pytest.mark.parametrize("data, code", [
    (b"\x89PNG\r\n\x1a\n" + b"0" * 100, "unsupported_media"),
    (b"\xff\xd8\xff\xe0" + b"\x00" * 50, "invalid_image"),
])
def test_unreadable_images_are_refused(data, code):
    with pytest.raises(ImageProblem) as exc:
        prepare(data, 1280)
    assert exc.value.code == code


def test_provider_copy_is_upright_downscaled_and_has_no_metadata():
    p = prepare(photo(gps=COMPLAINT_GPS), 640)
    assert p.gps == pytest.approx(COMPLAINT_GPS, abs=1e-5)
    assert (p.width, p.height) == (1000, 750) and p.upload_size == (640, 480)
    with Image.open(io.BytesIO(p.upload_jpeg)) as sent:
        assert not sent.getexif() and not sent.getexif().get_ifd(0x8825), "no EXIF (and so no GPS) leaves the server"


def test_quality_flags_dark_and_blurry_photos():
    assert prepare(photo(), 1280).quality["acceptable"]
    q = prepare(photo(dark=True), 1280).quality
    assert not q["acceptable"] and "too_dark" in q["problems"]
    from PIL import ImageFilter
    from app.ai.images import quality_metrics
    from tests.aiutil import scene
    q = quality_metrics(scene(1).filter(ImageFilter.GaussianBlur(10)))
    assert q["problems"] == ["blurry"]
    assert quality_metrics(scene(1).resize((300, 200)))["problems"][:1] == ["low_resolution"]


# ---------- Alignment ----------

def test_alignment_matches_the_same_place_and_rejects_a_different_one():
    same = align(prepare(photo(1), 1280).image, prepare(photo(1, moved=True), 1280).image)
    assert same["available"] and same["inliers"] >= 100 and same["inlierRatio"] > 0.5 and same["homography"]
    box = project_box({"x": 400, "y": 300, "width": 100, "height": 100}, same["homography"])
    # PIL's affine data maps output to input pixels, so (400, 300) moved to about (366, 292).
    assert abs(box["x"] - 366) < 15 and abs(box["y"] - 292) < 15, "the original box lands near the same spot"
    other = align(prepare(photo(1), 1280).image, prepare(photo(2), 1280).image)
    assert other["inliers"] < 12 and other["homography"] is None


# ---------- Decision rules ----------

def _result(detector, conf, state="completed", category="Potholes", x=450):
    dets = [] if conf is None else [{"className": "pothole", "category": category, "confidence": conf,
                                     "bbox": {"x": x, "y": 350, "width": 150, "height": 90}}]
    return {"detector": detector, "state": state, "detections": dets}


STRONG = {"available": True, "inliers": 400, "inlierRatio": 0.9, "homography": [[1, 0, 0], [0, 1, 0], [0, 0, 1]]}
GOOD_Q = {"acceptable": True, "problems": []}


def judge(original_conf=0.9, repair_conf=None, alignment=STRONG, repair_gps=None, quality=GOOD_Q, state="completed", **kw):
    return assess(relevant_categories=("Potholes", "Road cracks and damage"),
                  original=[_result("pothole", original_conf)], repair=[_result("pothole", repair_conf, state=state, **kw)],
                  alignment=alignment, complaint_gps=COMPLAINT_GPS, original_gps=None, repair_gps=repair_gps,
                  quality_original=GOOD_Q, quality_repair=quality, min_confidence=0.5, low_confidence=0.25)


def test_apparent_repair_of_the_same_place():
    r = judge()
    assert r["outcome"] == "likely_repaired" and r["locationCheck"] == "match"
    assert 0 < r["verificationConfidence"] < 0.99
    weaker = judge(alignment={**STRONG, "inliers": 40, "inlierRatio": 0.3})
    assert weaker["outcome"] == "likely_repaired" and weaker["verificationConfidence"] < r["verificationConfidence"], \
        "same model confidences, weaker location evidence: verification confidence is its own measure"
    assert any("reviewer must confirm" in x for x in r["details"]["reasons"])


def test_damage_still_visible():
    r = judge(repair_conf=0.88)
    assert r["outcome"] == "damage_visible" and r["details"]["overlapsOriginalDamage"] is True


def test_damage_elsewhere_in_the_after_photo_lowers_confidence():
    near, far = judge(repair_conf=0.88), judge(repair_conf=0.88, x=20)
    assert far["outcome"] == "damage_visible" and far["details"]["overlapsOriginalDamage"] is False
    assert far["verificationConfidence"] < near["verificationConfidence"]


def test_no_detection_is_not_proof_without_a_location_match():
    r = judge(alignment={"available": True, "inliers": 3, "inlierRatio": 0.05, "homography": None})
    assert r["outcome"] == "inconclusive" and r["locationCheck"] == "unverified" and r["verificationConfidence"] is None


def test_gps_alone_never_confirms_the_location():
    r = judge(alignment={"available": False, "reason": "opencv_missing"}, repair_gps=COMPLAINT_GPS)
    assert r["outcome"] == "inconclusive" and r["details"]["gps"]["status"] == "match"
    assert any("GPS alone" in x for x in r["details"]["reasons"])


def test_gps_supports_a_weak_visual_match():
    weak = {"available": True, "inliers": 15, "inlierRatio": 0.2, "homography": STRONG["homography"]}
    assert judge(alignment=weak)["outcome"] == "inconclusive"
    assert judge(alignment=weak, repair_gps=(13.10075, 77.59635))["outcome"] == "likely_repaired"


def test_wrong_location_by_gps_is_inconclusive_even_with_matching_pixels():
    r = judge(repair_gps=(13.12, 77.60))
    assert r["outcome"] == "inconclusive" and r["locationCheck"] == "mismatch" and r["gpsDistanceM"] > 2000


def test_low_quality_after_photo_is_inconclusive():
    assert judge(quality={"acceptable": False, "problems": ["too_dark"]})["outcome"] == "inconclusive"


def test_possible_low_confidence_damage_is_inconclusive():
    assert judge(repair_conf=0.3)["outcome"] == "inconclusive"


def test_original_damage_must_have_been_detected():
    r = judge(original_conf=0.3)
    assert r["outcome"] == "inconclusive" and any("proves nothing" in x for x in r["details"]["reasons"])


def test_failed_inference_is_inconclusive():
    r = judge(state="failed")
    assert r["outcome"] == "inconclusive" and any("not complete" in x for x in r["details"]["reasons"])


def test_irrelevant_detections_do_not_count():
    r = judge(repair_conf=0.95, category="Other civic issues")
    assert r["outcome"] == "likely_repaired", "graffiti in the after photo is not the reported pothole"
