"""Web Push delivery with VAPID (RFC 8030 push, RFC 8291 message encryption, RFC 8292 VAPID).

Configuration (all server-side; the private key never leaves the backend):
    VAPID_PUBLIC_KEY   base64url uncompressed P-256 public key (65 bytes). Sent to browsers.
    VAPID_PRIVATE_KEY  base64url raw P-256 private key (32 bytes). SECRET.
    VAPID_SUBJECT      contact for push services: mailto:... or https://...
Without the two keys, push is disabled: notifications are still stored in history, and their
push_status is "disabled". Generate a key pair with:  python -m app.push generate-vapid-keys

Push endpoints are capability URLs at the browser vendor's push service. The server POSTs to them,
so only hosts in CIVICLENS_PUSH_ENDPOINT_HOSTS (default: the Chrome/Edge, Firefox, Windows and
Apple push services) are accepted; this stops the API being used to send requests to arbitrary
or internal addresses. Payloads are encrypted end to end (aes128gcm) for the subscribing browser.
"""

import base64
import binascii
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import NamedTuple, Protocol

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from py_vapid import Vapid02
from pywebpush import WebPushException, webpush

logger = logging.getLogger("civiclens.push")

PUBLIC_KEY_ENV = "VAPID_PUBLIC_KEY"
PRIVATE_KEY_ENV = "VAPID_PRIVATE_KEY"
SUBJECT_ENV = "VAPID_SUBJECT"
ENDPOINT_HOSTS_ENV = "CIVICLENS_PUSH_ENDPOINT_HOSTS"
TTL_ENV = "CIVICLENS_PUSH_TTL_SECONDS"
# Push services of Chrome/Edge/Android (FCM), Firefox, Edge on Windows (WNS) and Safari.
DEFAULT_ENDPOINT_HOSTS = ("fcm.googleapis.com", "updates.push.services.mozilla.com", "push.services.mozilla.com",
                          "notify.windows.com", "push.apple.com")
DEFAULT_TTL = 24 * 3600
SEND_TIMEOUT = 10


class PushConfigError(ValueError):
    """Invalid push configuration. The message names the variable and never contains a key."""


def b64url_decode(value: str) -> bytes:
    value = value.strip()
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _public_from_private(private_raw: bytes) -> bytes:
    key = ec.derive_private_key(int.from_bytes(private_raw, "big"), ec.SECP256R1())
    return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def generate_vapid_keys() -> tuple[str, str]:
    """(public, private) as base64url strings."""
    key = ec.generate_private_key(ec.SECP256R1())
    private_raw = key.private_numbers().private_value.to_bytes(32, "big")
    return b64url_encode(_public_from_private(private_raw)), b64url_encode(private_raw)


def endpoint_host_allowed(host: str, allowed: tuple[str, ...]) -> bool:
    host = (host or "").lower().rstrip(".")
    return any(host == h or host.endswith("." + h) for h in allowed)


@dataclass(frozen=True)
class PushSettings:
    public_key: str
    private_key: str = field(repr=False)
    subject: str
    endpoint_hosts: tuple[str, ...] = DEFAULT_ENDPOINT_HOSTS
    ttl: int = DEFAULT_TTL

    def __post_init__(self):
        try:
            private_raw = b64url_decode(self.private_key)
            public_raw = b64url_decode(self.public_key)
        except (binascii.Error, ValueError):
            raise PushConfigError(f"{PUBLIC_KEY_ENV} and {PRIVATE_KEY_ENV} must be base64url strings.") from None
        if len(private_raw) != 32:
            raise PushConfigError(f"{PRIVATE_KEY_ENV} must be a raw 32-byte P-256 private key (base64url).")
        if len(public_raw) != 65 or public_raw[0] != 4:
            raise PushConfigError(f"{PUBLIC_KEY_ENV} must be an uncompressed 65-byte P-256 public key (base64url).")
        try:
            matches = _public_from_private(private_raw) == public_raw
        except ValueError:
            raise PushConfigError(f"{PRIVATE_KEY_ENV} is not a valid P-256 private key.") from None
        if not matches:
            raise PushConfigError(f"{PUBLIC_KEY_ENV} does not belong to {PRIVATE_KEY_ENV}; generate them together.")
        if not (self.subject.startswith("mailto:") and "@" in self.subject or self.subject.startswith("https://")):
            raise PushConfigError(f"{SUBJECT_ENV} must be a mailto: address or an https:// URL (push services use it to contact you).")
        if not self.endpoint_hosts:
            raise PushConfigError(f"{ENDPOINT_HOSTS_ENV} must list at least one push service host.")
        if not 0 <= self.ttl <= 28 * 24 * 3600:
            raise PushConfigError(f"{TTL_ENV} must be between 0 and 2419200 seconds.")

    @classmethod
    def from_env(cls) -> "PushSettings | None":
        """None when neither key is set (push disabled)."""
        public, private = os.environ.get(PUBLIC_KEY_ENV, "").strip(), os.environ.get(PRIVATE_KEY_ENV, "").strip()
        hosts_raw = os.environ.get(ENDPOINT_HOSTS_ENV, "")
        hosts = tuple(h.strip().lower() for h in hosts_raw.split(",") if h.strip()) if hosts_raw.strip() else DEFAULT_ENDPOINT_HOSTS
        if not public and not private:
            return None
        if not public or not private:
            raise PushConfigError(f"Set both {PUBLIC_KEY_ENV} and {PRIVATE_KEY_ENV}, or neither (push disabled).")
        ttl_raw = os.environ.get(TTL_ENV, "").strip()
        try:
            ttl = int(ttl_raw) if ttl_raw else DEFAULT_TTL
        except ValueError:
            raise PushConfigError(f"{TTL_ENV} must be a whole number of seconds.") from None
        return cls(public, private, os.environ.get(SUBJECT_ENV, "").strip(), hosts, ttl)


class PushResult(NamedTuple):
    outcome: str        # "sent" | "gone" (expired/unsubscribed: delete the device) | "retry" | "rejected"
    status: int | None  # push service HTTP status, if any


class PushSender(Protocol):
    def send(self, device: dict, payload: str) -> PushResult:
        """device: {"endpoint", "p256dh", "auth"}. Never raises for delivery failures."""
        ...


class WebPushSender:
    """Sends one encrypted message per device with pywebpush. `session` is injectable for tests."""

    def __init__(self, settings: PushSettings, session: requests.Session | None = None):
        self.settings = settings
        self._vapid = Vapid02.from_raw(settings.private_key.encode("ascii"))
        self._session = session or requests.Session()

    def send(self, device: dict, payload: str) -> PushResult:
        info = {"endpoint": device["endpoint"], "keys": {"p256dh": device["p256dh"], "auth": device["auth"]}}
        try:
            response = webpush(info, data=payload, vapid_private_key=self._vapid, vapid_claims={"sub": self.settings.subject},
                               ttl=self.settings.ttl, timeout=SEND_TIMEOUT, headers={"Urgency": "normal"},
                               requests_session=self._session)
            return PushResult("sent", getattr(response, "status_code", None))
        except WebPushException as exc:
            status = exc.response.status_code if exc.response is not None else None
            # The exception text contains the push service's response body: log the status only.
            logger.warning("Push delivery failed (HTTP %s)", status)
            if status in (404, 410):
                return PushResult("gone", status)
            if status is None:  # raised before any request was sent, e.g. undecodable subscription keys
                return PushResult("rejected", None)
            if status == 429 or status >= 500:
                return PushResult("retry", status)
            return PushResult("rejected", status)
        except requests.RequestException as exc:
            logger.warning("Push service unreachable (%s)", type(exc).__name__)
            return PushResult("retry", None)
        except (ValueError, TypeError) as exc:  # malformed subscription keys
            logger.warning("Push subscription keys are invalid (%s)", type(exc).__name__)
            return PushResult("rejected", None)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args != ["generate-vapid-keys"]:
        print("Usage: python -m app.push generate-vapid-keys", file=sys.stderr)
        return 2
    public, private = generate_vapid_keys()
    print("# Web Push VAPID keys. Put them in the server's environment (never in Git or frontend files).")
    print(f"{PUBLIC_KEY_ENV}={public}")
    print(f"{PRIVATE_KEY_ENV}={private}")
    print(f"{SUBJECT_ENV}=mailto:you@example.org")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
