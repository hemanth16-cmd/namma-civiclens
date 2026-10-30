"""AI configuration: which detectors exist, how complaint categories are routed to them, and how a
provider's class names map to CivicLens categories. Everything is read from environment variables
so model IDs, workflow IDs and class prompts can change without a code change.

Off by default. Set CIVICLENS_AI_PROVIDER=roboflow and ROBOFLOW_API_KEY to enable it.

The four detectors and their defaults (each overridable, see backend/.env.example):

  pothole         Roboflow workflow  <workspace>/general-segmentation-api-18, classes "potholes"
  pavement        Roboflow model     pavement-gvib5/1  (Universe project spl-project/pavement-gvib5)
  garbage         Roboflow workflow  <workspace>/general-segmentation-api-20, classes "box, garbage, z"
  infrastructure  Roboflow workflow  <workspace>/general-segmentation-api-19,
                  classes "pothole, graffiti, damaged-sign, electricity-pole-damage, fallen-trees"

The three workflows are prompt-based segmentation workflows in the project's Roboflow workspace (the
class list is a text prompt), not the trained models of the Universe projects cloudcam/garbage-dumping-6avsy,
rizz-tiu7z/potholes-pxnfg and infrastructure-aqfrw/infrastructure-dtiwq. To use one of those trained
models instead, set ROBOFLOW_<NAME>_MODE=model and ROBOFLOW_<NAME>_MODEL_ID=<project>/<version> once the
version number has been confirmed on Roboflow. No version is assumed here.
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field

logger = logging.getLogger("civiclens.ai")

PROVIDER_ENV = "CIVICLENS_AI_PROVIDER"
API_KEY_ENV = "ROBOFLOW_API_KEY"
DEFAULT_API_URL = "https://serverless.roboflow.com"
DEFAULT_WORKSPACE = "mohammed-arshad-nb1p3"
MB = 1024 * 1024

# Same list as models.CATEGORIES (kept here so this module has no app imports).
CATEGORIES = (
    "Potholes", "Road cracks and damage", "Garbage accumulation", "Illegal dumping",
    "Drainage or waterlogging", "Broken streetlights", "Damaged footpaths", "Water leakage", "Other civic issues",
)
MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}/[0-9]{1,6}$")
SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")


class AiConfigError(ValueError):
    """The AI configuration is invalid. The message names the variable and never contains a secret."""


@dataclass(frozen=True)
class DetectorSpec:
    name: str                 # "pothole", "pavement", "garbage", "infrastructure"
    mechanism: str            # "model" (Roboflow infer) or "workflow" (Roboflow run_workflow)
    model_id: str = ""        # mechanism "model": "<project>/<version>"
    workspace: str = ""       # mechanism "workflow"
    workflow_id: str = ""     # mechanism "workflow"
    classes: str = ""         # workflow class prompt, sent as the "classes" parameter
    source: str = ""          # documentation only: where this detector comes from

    @property
    def identifier(self) -> str:
        """What is stored in ai_inferences.model_id: the model ID or <workspace>/<workflow_id>."""
        return self.model_id if self.mechanism == "model" else f"{self.workspace}/{self.workflow_id}"

    @property
    def version(self) -> str | None:
        return self.model_id.split("/", 1)[1] if self.mechanism == "model" else None


@dataclass(frozen=True)
class Route:
    detectors: tuple[str, ...]
    relevant: tuple[str, ...]  # CivicLens categories that count as "the reported damage" for this complaint


DEFAULT_DETECTORS = {
    "pothole": {"mode": "workflow", "workflow_id": "general-segmentation-api-18", "classes": "potholes",
                "source": "Prompt-based segmentation workflow (tested by the team) for Universe project rizz-tiu7z/potholes-pxnfg's use case"},
    "pavement": {"mode": "model", "model_id": "pavement-gvib5/1",
                 "source": "Universe project spl-project/pavement-gvib5, hosted model version 1"},
    "garbage": {"mode": "workflow", "workflow_id": "general-segmentation-api-20", "classes": "box, garbage, z",
                "source": "Prompt-based segmentation workflow (tested by the team) for Universe project cloudcam/garbage-dumping-6avsy's use case"},
    "infrastructure": {"mode": "workflow", "workflow_id": "general-segmentation-api-19",
                       "classes": "pothole, graffiti, damaged-sign, electricity-pole-damage, fallen-trees",
                       "source": "Prompt-based segmentation workflow (tested by the team) for Universe project infrastructure-aqfrw/infrastructure-dtiwq's use case"},
}

_ROAD = ("Potholes", "Road cracks and damage")
_WASTE = ("Garbage accumulation", "Illegal dumping")
DEFAULT_ROUTES: dict[str, Route] = {
    "Potholes": Route(("pothole", "pavement"), _ROAD),
    "Road cracks and damage": Route(("pavement", "pothole"), _ROAD),
    "Damaged footpaths": Route(("pavement", "infrastructure"), (*_ROAD, "Damaged footpaths")),
    "Garbage accumulation": Route(("garbage",), _WASTE),
    "Illegal dumping": Route(("garbage",), _WASTE),
    "Broken streetlights": Route(("infrastructure",), ("Broken streetlights", "Other civic issues")),
    "Other civic issues": Route(("infrastructure",), CATEGORIES),
    # No configured detector can see water; these are left to people.
    "Drainage or waterlogging": Route((), ()),
    "Water leakage": Route((), ()),
}

# Class name -> category, by keyword, applied when CIVICLENS_AI_CLASS_MAP has no exact entry. Class names
# are compared lower-case with spaces and underscores turned into dashes. A class that matches nothing is
# kept in the results with category null and never counts as damage.
KEYWORD_CATEGORIES = (
    ("pothole", "Potholes"),
    ("crack", "Road cracks and damage"),
    ("garbage", "Garbage accumulation"),
    ("trash", "Garbage accumulation"),
    ("litter", "Garbage accumulation"),
    ("waste", "Garbage accumulation"),
    ("dump", "Illegal dumping"),
    ("graffiti", "Other civic issues"),
    ("sign", "Other civic issues"),
    ("pole", "Other civic issues"),
    ("tree", "Other civic issues"),
)


def normalise_class(name: str) -> str:
    return re.sub(r"[\s_]+", "-", str(name).strip().lower())


@dataclass(frozen=True)
class AiSettings:
    provider: str = "off"                       # "off" or "roboflow"
    api_key: str = field(default="", repr=False)
    api_url: str = DEFAULT_API_URL
    detectors: dict = field(default_factory=dict)  # name -> DetectorSpec (enabled ones only)
    routes: dict = field(default_factory=dict)     # category -> Route
    class_map: dict = field(default_factory=dict)  # normalised class -> category | None
    min_confidence: float = 0.5                 # a detection at or above this counts as "present"
    low_confidence: float = 0.25                # between this and min_confidence: "possible", makes repair checks inconclusive
    timeout_seconds: float = 30.0
    max_image_bytes: int = 25 * MB
    max_side_px: int = 1280                     # images are downscaled to this before upload to the provider
    workers: int = 2
    max_queue: int = 100
    disabled_reason: str | None = None          # why AI is off, for GET /ai and logs

    @property
    def enabled(self) -> bool:
        return self.provider != "off" and self.disabled_reason is None

    def route(self, category: str) -> Route:
        return self.routes.get(category) or self.routes.get("Other civic issues") or Route((), ())

    def detectors_for(self, category: str) -> list[DetectorSpec]:
        return [self.detectors[n] for n in self.route(category).detectors if n in self.detectors]

    def category_for(self, class_name: str) -> str | None:
        key = normalise_class(class_name)
        if key in self.class_map:
            return self.class_map[key]
        for word, category in KEYWORD_CATEGORIES:
            if word in key:
                return category
        return None

    @classmethod
    def from_env(cls, env: dict | None = None) -> "AiSettings":
        env = os.environ if env is None else env
        provider = (env.get(PROVIDER_ENV) or "off").strip().lower()
        if provider not in ("off", "roboflow"):
            raise AiConfigError(f'{PROVIDER_ENV} must be "off" or "roboflow", not {provider!r}.')

        def num(name, default, cast, lo, hi):
            raw = (env.get(name) or "").strip()
            if not raw:
                return default
            try:
                value = cast(raw)
            except ValueError:
                raise AiConfigError(f"{name} must be a number.") from None
            if not lo <= value <= hi:
                raise AiConfigError(f"{name} must be between {lo} and {hi}.")
            return value

        api_url = (env.get("ROBOFLOW_API_URL") or DEFAULT_API_URL).strip().rstrip("/")
        if not api_url.startswith("https://"):
            raise AiConfigError("ROBOFLOW_API_URL must be an https:// URL.")
        workspace = (env.get("ROBOFLOW_WORKSPACE") or DEFAULT_WORKSPACE).strip()
        if not SLUG.fullmatch(workspace):
            raise AiConfigError("ROBOFLOW_WORKSPACE must be a Roboflow workspace slug.")

        enabled_names = [n.strip() for n in (env.get("CIVICLENS_AI_DETECTORS") or ",".join(DEFAULT_DETECTORS)).split(",") if n.strip()]
        detectors = {}
        for name in enabled_names:
            if name not in DEFAULT_DETECTORS:
                raise AiConfigError(f"CIVICLENS_AI_DETECTORS: unknown detector {name!r} (use {', '.join(DEFAULT_DETECTORS)}).")
            detectors[name] = _detector(env, name, workspace)

        routes = dict(DEFAULT_ROUTES)
        raw_routes = (env.get("CIVICLENS_AI_CATEGORY_ROUTES") or "").strip()
        if raw_routes:
            routes.update(_parse_routes(raw_routes))

        class_map = {}
        raw_map = (env.get("CIVICLENS_AI_CLASS_MAP") or "").strip()
        if raw_map:
            try:
                parsed = json.loads(raw_map)
            except json.JSONDecodeError:
                raise AiConfigError("CIVICLENS_AI_CLASS_MAP must be a JSON object of class name -> category (or null).") from None
            if not isinstance(parsed, dict):
                raise AiConfigError("CIVICLENS_AI_CLASS_MAP must be a JSON object of class name -> category (or null).")
            for key, category in parsed.items():
                if category is not None and category not in CATEGORIES:
                    raise AiConfigError(f"CIVICLENS_AI_CLASS_MAP: {category!r} is not a CivicLens category.")
                class_map[normalise_class(key)] = category

        min_conf = num("CIVICLENS_AI_MIN_CONFIDENCE", 0.5, float, 0.05, 0.99)
        low_conf = num("CIVICLENS_AI_LOW_CONFIDENCE", min(0.25, min_conf), float, 0.01, min_conf)
        api_key = (env.get(API_KEY_ENV) or "").strip()
        reason = None
        if provider == "off":
            reason = "disabled"
        elif not api_key:
            reason = "not_configured"
            logger.warning("%s=roboflow but %s is not set: AI analysis is off.", PROVIDER_ENV, API_KEY_ENV)
        return cls(
            provider=provider, api_key=api_key, api_url=api_url, detectors=detectors, routes=routes, class_map=class_map,
            min_confidence=min_conf, low_confidence=low_conf,
            timeout_seconds=num("CIVICLENS_AI_TIMEOUT_SECONDS", 30.0, float, 1, 120),
            max_image_bytes=num("CIVICLENS_AI_MAX_IMAGE_BYTES", 25 * MB, int, 1024, 50 * MB),
            max_side_px=num("CIVICLENS_AI_MAX_SIDE_PX", 1280, int, 320, 4096),
            workers=num("CIVICLENS_AI_WORKERS", 2, int, 1, 8),
            max_queue=num("CIVICLENS_AI_MAX_QUEUE", 100, int, 1, 10000),
            disabled_reason=reason,
        )


def _detector(env, name: str, workspace: str) -> DetectorSpec:
    defaults = DEFAULT_DETECTORS[name]
    prefix = f"ROBOFLOW_{name.upper()}_"
    mode = (env.get(prefix + "MODE") or defaults["mode"]).strip().lower()
    if mode == "model":
        model_id = (env.get(prefix + "MODEL_ID") or defaults.get("model_id", "")).strip()
        if not MODEL_ID.fullmatch(model_id):
            raise AiConfigError(f"{prefix}MODEL_ID must be '<project>/<version>' (for example pavement-gvib5/1); "
                                "check the project's Deploy tab on Roboflow for the exact value.")
        return DetectorSpec(name, "model", model_id=model_id, source=defaults["source"] if model_id == defaults.get("model_id") else "configured")
    if mode == "workflow":
        workflow_id = (env.get(prefix + "WORKFLOW_ID") or defaults.get("workflow_id", "")).strip()
        if not SLUG.fullmatch(workflow_id):
            raise AiConfigError(f"{prefix}WORKFLOW_ID must be a Roboflow workflow ID.")
        classes = env.get(prefix + "CLASSES")
        classes = (defaults.get("classes", "") if classes is None else classes).strip()
        if len(classes) > 500:
            raise AiConfigError(f"{prefix}CLASSES is too long (500 characters at most).")
        return DetectorSpec(name, "workflow", workspace=(env.get(prefix + "WORKSPACE") or workspace).strip(),
                            workflow_id=workflow_id, classes=classes,
                            source=defaults["source"] if workflow_id == defaults.get("workflow_id") else "configured")
    raise AiConfigError(f'{prefix}MODE must be "model" or "workflow".')


def _parse_routes(raw: str) -> dict:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    if not isinstance(parsed, dict):
        raise AiConfigError('CIVICLENS_AI_CATEGORY_ROUTES must be a JSON object: {"<category>": {"detectors": [...], "relevant": [...]}}.')
    routes = {}
    for category, spec in parsed.items():
        if category not in CATEGORIES:
            raise AiConfigError(f"CIVICLENS_AI_CATEGORY_ROUTES: {category!r} is not a CivicLens category.")
        if not isinstance(spec, dict) or not isinstance(spec.get("detectors", []), list):
            raise AiConfigError(f"CIVICLENS_AI_CATEGORY_ROUTES[{category!r}] must have a 'detectors' list.")
        names = tuple(str(n) for n in spec.get("detectors", []))
        unknown = [n for n in names if n not in DEFAULT_DETECTORS]
        if unknown:
            raise AiConfigError(f"CIVICLENS_AI_CATEGORY_ROUTES[{category!r}]: unknown detector(s) {', '.join(unknown)}.")
        relevant = tuple(str(c) for c in spec.get("relevant", [category]))
        bad = [c for c in relevant if c not in CATEGORIES]
        if bad:
            raise AiConfigError(f"CIVICLENS_AI_CATEGORY_ROUTES[{category!r}]: unknown relevant categor(ies) {', '.join(bad)}.")
        routes[category] = Route(names, relevant)
    return routes
