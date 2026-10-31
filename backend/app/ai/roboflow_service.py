"""Roboflow hosted inference over HTTPS, for both of Roboflow's mechanisms behind one call:

- model    (inference_sdk InferenceHTTPClient.infer):        POST {api_url}/{project}/{version}
           body: the base64 JPEG, Content-Type application/x-www-form-urlencoded
- workflow (inference_sdk InferenceHTTPClient.run_workflow): POST {api_url}/{workspace}/workflows/{workflow_id}
           body: {"inputs": {"image": {"type": "base64", "value": ...}, "classes": ...}, "use_cache": true}

These are the same requests inference-sdk 1.8 sends (inference_sdk/http/client.py). The API key goes only
in the `Authorization: Bearer` header, the SDK's api_key_transport="header" mode, never in a URL or
body, so it cannot appear in an exception message or an access log. The SDK itself is not used: its
retries and image handling cannot be bounded the way a request-serving backend needs.

Failures become ProviderError with a short code (stored in ai_inferences.error_code). Neither the key,
nor the image, nor the provider's response body is ever logged or stored in an error.
"""

import base64
import logging
import time

import httpx

from .model_registry import AiSettings, DetectorSpec

logger = logging.getLogger("civiclens.ai")

RETRYABLE = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 2           # one retry at most
MAX_RETRY_WAIT = 5.0       # seconds; a longer Retry-After fails the attempt as rate_limited
MAX_RESPONSE_BYTES = 20 * 1024 * 1024


class ProviderError(Exception):
    """Inference failed. `code` is stored; `retryable` says whether a later retry may succeed."""

    def __init__(self, code: str, retryable: bool = False, status: int | None = None):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.status = status


def _status_error(status: int) -> ProviderError:
    if status in (401, 403):
        return ProviderError("auth_failed", status=status)
    if status == 404:
        return ProviderError("model_not_found", status=status)
    if status == 429:
        return ProviderError("rate_limited", retryable=True, status=status)
    if status in (400, 413, 422):
        return ProviderError("rejected_by_provider", status=status)
    if status >= 500:
        return ProviderError("provider_unavailable", retryable=True, status=status)
    return ProviderError("unexpected_status", status=status)


def _retry_after(response: httpx.Response) -> float:
    raw = response.headers.get("retry-after", "")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 1.0


class RoboflowClient:
    name = "roboflow"

    def __init__(self, settings: AiSettings, transport: httpx.BaseTransport | None = None, sleep=time.sleep):
        self._settings = settings
        self._sleep = sleep
        self._client = httpx.Client(
            base_url=settings.api_url,
            timeout=httpx.Timeout(settings.timeout_seconds, connect=min(10.0, settings.timeout_seconds)),
            headers={"Authorization": f"Bearer {settings.api_key}", "User-Agent": "namma-civiclens-backend"},
            transport=transport,
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def detect(self, spec: DetectorSpec, jpeg: bytes, min_confidence: float = 0.1) -> dict:
        """Run one detector on a JPEG. Returns the provider's parsed JSON (an object or a list)."""
        if not self._settings.api_key:
            raise ProviderError("not_configured")
        encoded = base64.b64encode(jpeg).decode("ascii")
        if spec.mechanism == "model":
            project, version = spec.model_id.split("/", 1)
            request = dict(method="POST", url=f"/{project}/{version}",
                           params={"confidence": min_confidence, "format": "json"},
                           content=encoded, headers={"Content-Type": "application/x-www-form-urlencoded"})
        elif spec.mechanism == "workflow":
            inputs = {"image": {"type": "base64", "value": encoded}}
            if spec.classes:
                inputs["classes"] = spec.classes
            request = dict(method="POST", url=f"/{spec.workspace}/workflows/{spec.workflow_id}",
                           json={"inputs": inputs, "use_cache": True, "enable_profiling": False})
        else:
            raise ProviderError("not_configured")
        return self._send(request, spec)

    def _send(self, request: dict, spec: DetectorSpec):
        last = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self._client.request(**request)
            except httpx.TimeoutException:
                last = ProviderError("timeout", retryable=True)
            except httpx.TransportError:
                last = ProviderError("provider_unreachable", retryable=True)
            else:
                if response.status_code == 200:
                    if len(response.content) > MAX_RESPONSE_BYTES:
                        raise ProviderError("bad_response")
                    try:
                        return response.json()
                    except ValueError:
                        raise ProviderError("bad_response") from None
                last = _status_error(response.status_code)
                if response.status_code == 429:
                    wait = _retry_after(response)
                    if wait > MAX_RETRY_WAIT:
                        break
                    if attempt < MAX_ATTEMPTS:
                        self._sleep(wait)
                        continue
            logger.warning("Roboflow %s %s attempt %d failed: %s", spec.name, spec.mechanism, attempt, last.code)
            if not last.retryable or attempt == MAX_ATTEMPTS:
                break
            self._sleep(1.0)
        raise last
