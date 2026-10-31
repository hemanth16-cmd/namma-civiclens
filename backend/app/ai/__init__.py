"""AI-assisted evidence analysis (Roboflow object detection and before/after repair checks).

Everything this package produces is a suggestion for staff. It never changes a complaint's status,
its citizen verification or any work order; people make those decisions.

Modules:
- model_registry: configuration from environment variables (detectors, routing, class mapping)
- roboflow_service: the HTTP client for Roboflow's hosted model (infer) and workflow APIs
- result_normalizer: provider responses -> one internal detection format
- images: reading private evidence, validation, resizing, EXIF GPS and quality metrics
- image_alignment: OpenCV feature matching between an original and a repair photo
- repair_verifier: the before/after decision rules (pure functions)
- ai_store: SQL for ai_inferences and repair_assessments
- service: the bounded background worker that ties these together
"""
