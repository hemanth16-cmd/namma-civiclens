"""Rate limiting for the anonymous endpoints.

`RateLimiter` is the interface; `InMemoryRateLimiter` is a sliding-window implementation for the
single development process. It is thread-safe, uses an injectable clock (for deterministic tests)
and keeps at most `max_keys` buckets, evicting the least recently used, so arbitrary client keys
cannot grow memory without bound.

Limits reset when the server restarts and are NOT shared between worker processes or machines.
A multi-process or multi-host deployment needs a shared store (for example Redis) behind the same
interface; that is not built.

Client IP: the ASGI peer address is used. Forwarding headers (X-Forwarded-For) are ignored unless
the peer is listed in CIVICLENS_TRUSTED_PROXIES; then the right-most address in X-Forwarded-For
that is not itself a trusted proxy is used.
"""

import ipaddress
import math
import os
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Protocol

from fastapi import Request

from .errors import ApiProblem

RATE_LIMITS_ENV = "CIVICLENS_RATE_LIMITS"          # "off" disables all limits
TRUSTED_PROXIES_ENV = "CIVICLENS_TRUSTED_PROXIES"  # comma-separated IPs or CIDRs; empty by default


@dataclass(frozen=True)
class Rule:
    limit: int
    window_seconds: float

    @classmethod
    def parse(cls, text: str) -> "Rule":
        """"5/600" means 5 requests per 600 seconds."""
        count, _, seconds = text.partition("/")
        rule = cls(int(count), float(seconds))
        if rule.limit < 1 or rule.window_seconds <= 0:
            raise ValueError(f"Invalid rate limit {text!r}")
        return rule


@dataclass(frozen=True)
class RateLimitSettings:
    enabled: bool = True
    create_per_ip: Rule = Rule(5, 600)              # POST /complaints
    track_per_ip: Rule = Rule(60, 60)               # GET /complaints/{id}
    token_per_ip: Rule = Rule(5, 900)               # POST /complaints/{id}/upload-token
    token_per_complaint: Rule = Rule(10, 3600)
    evidence_per_ip: Rule = Rule(30, 3600)          # POST /complaints/{id}/evidence, before authorisation
    evidence_per_complaint: Rule = Rule(10, 3600)   # authorised upload attempts per complaint
    staff_login_per_ip: Rule = Rule(5, 900)         # POST /staff/sessions
    staff_login_per_account: Rule = Rule(10, 3600)
    inbox_per_ip: Rule = Rule(10, 3600)             # POST /notifications/inboxes
    follow_per_ip: Rule = Rule(10, 900)             # POST /notifications/follows (claim-code check)
    follow_per_complaint: Rule = Rule(10, 3600)
    push_register_per_ip: Rule = Rule(30, 3600)     # POST /notifications/subscriptions
    push_test_per_recipient: Rule = Rule(5, 3600)   # POST /notifications/test
    citizen_response_per_ip: Rule = Rule(10, 900)   # POST /complaints/{id}/citizen-responses (claim-code check)
    citizen_response_per_complaint: Rule = Rule(10, 3600)
    resident_register_per_ip: Rule = Rule(5, 3600)  # POST /residents
    resident_login_per_ip: Rule = Rule(10, 900)     # POST /residents/sessions
    resident_login_per_account: Rule = Rule(10, 3600)
    resident_claim_per_ip: Rule = Rule(10, 900)     # POST /residents/me/claims (claim-code check)
    resident_claim_per_complaint: Rule = Rule(10, 3600)
    owner_evidence_per_ip: Rule = Rule(20, 900)     # owner evidence views (claim code or resident account)
    owner_evidence_per_complaint: Rule = Rule(30, 3600)
    trusted_proxies: tuple = field(default_factory=tuple)

    ENV_RULES = {
        "create_per_ip": "CIVICLENS_RATE_CREATE",
        "track_per_ip": "CIVICLENS_RATE_TRACK",
        "token_per_ip": "CIVICLENS_RATE_TOKEN_IP",
        "token_per_complaint": "CIVICLENS_RATE_TOKEN_COMPLAINT",
        "evidence_per_ip": "CIVICLENS_RATE_EVIDENCE_IP",
        "evidence_per_complaint": "CIVICLENS_RATE_EVIDENCE_COMPLAINT",
        "staff_login_per_ip": "CIVICLENS_RATE_STAFF_LOGIN_IP",
        "staff_login_per_account": "CIVICLENS_RATE_STAFF_LOGIN_ACCOUNT",
        "inbox_per_ip": "CIVICLENS_RATE_INBOX_IP",
        "follow_per_ip": "CIVICLENS_RATE_FOLLOW_IP",
        "follow_per_complaint": "CIVICLENS_RATE_FOLLOW_COMPLAINT",
        "push_register_per_ip": "CIVICLENS_RATE_PUSH_REGISTER_IP",
        "push_test_per_recipient": "CIVICLENS_RATE_PUSH_TEST",
        "citizen_response_per_ip": "CIVICLENS_RATE_CITIZEN_RESPONSE_IP",
        "citizen_response_per_complaint": "CIVICLENS_RATE_CITIZEN_RESPONSE_COMPLAINT",
        "resident_register_per_ip": "CIVICLENS_RATE_RESIDENT_REGISTER_IP",
        "resident_login_per_ip": "CIVICLENS_RATE_RESIDENT_LOGIN_IP",
        "resident_login_per_account": "CIVICLENS_RATE_RESIDENT_LOGIN_ACCOUNT",
        "resident_claim_per_ip": "CIVICLENS_RATE_RESIDENT_CLAIM_IP",
        "resident_claim_per_complaint": "CIVICLENS_RATE_RESIDENT_CLAIM_COMPLAINT",
        "owner_evidence_per_ip": "CIVICLENS_RATE_OWNER_EVIDENCE_IP",
        "owner_evidence_per_complaint": "CIVICLENS_RATE_OWNER_EVIDENCE_COMPLAINT",
    }

    @classmethod
    def from_env(cls) -> "RateLimitSettings":
        settings = cls(enabled=os.environ.get(RATE_LIMITS_ENV, "on").strip().lower() not in ("off", "0", "false", "no"))
        overrides = {name: Rule.parse(os.environ[env]) for name, env in cls.ENV_RULES.items() if os.environ.get(env)}
        proxies = tuple(ipaddress.ip_network(p.strip(), strict=False) for p in os.environ.get(TRUSTED_PROXIES_ENV, "").split(",") if p.strip())
        return replace(settings, trusted_proxies=proxies, **overrides)


class RateLimiter(Protocol):
    def hit(self, key: str, rule: Rule) -> float:
        """Record one attempt. Returns 0 if allowed, else the seconds until a retry can succeed."""
        ...


class InMemoryRateLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic, max_keys: int = 10_000):
        self._clock = clock
        self._max_keys = max_keys
        self._buckets: OrderedDict[str, deque] = OrderedDict()
        self._lock = threading.Lock()

    def hit(self, key: str, rule: Rule) -> float:
        now = self._clock()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = deque()
                self._buckets[key] = bucket
                while len(self._buckets) > self._max_keys:
                    self._buckets.popitem(last=False)
            else:
                self._buckets.move_to_end(key)
            cutoff = now - rule.window_seconds
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= rule.limit:
                return max(bucket[0] + rule.window_seconds - now, 0.001)
            bucket.append(now)
            return 0.0

    def __len__(self) -> int:
        return len(self._buckets)


def client_ip(request: Request, trusted_proxies: tuple = ()) -> str:
    peer = request.client.host if request.client else "unknown"
    if not trusted_proxies or not _is_trusted(peer, trusted_proxies):
        return peer
    forwarded = [part.strip() for part in request.headers.get("x-forwarded-for", "").split(",") if part.strip()]
    for candidate in reversed(forwarded):
        if not _is_trusted(candidate, trusted_proxies):
            return candidate if _valid_ip(candidate) else peer
    return peer


def _valid_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _is_trusted(value: str, networks: tuple) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return any(address in net for net in networks)


def enforce(request: Request, rule_name: str, subject: str) -> None:
    """Count one attempt against `rule_name` for `subject` ("ip" for the client address, or a
    complaint ID). Raises 429 rate_limited with Retry-After when the limit is exceeded."""
    settings: RateLimitSettings = request.app.state.rate_limit_settings
    if not settings.enabled:
        return
    rule: Rule = getattr(settings, rule_name)
    who = client_ip(request, settings.trusted_proxies) if subject == "ip" else subject
    wait = request.app.state.rate_limiter.hit(f"{rule_name}:{who}", rule)
    if wait:
        seconds = max(1, math.ceil(wait))
        raise ApiProblem(429, "rate_limited", f"Too many requests. Try again in {seconds} seconds.", headers={"Retry-After": str(seconds)})
