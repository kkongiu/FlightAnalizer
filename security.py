"""Security helpers: rate limiting, CSRF comparison, password policy.

No third-party dependencies: the rate limiter is an in-process fixed-window
counter (appropriate for the recommended single-worker uvicorn deployment).
"""
import hmac
import os
import re
import threading
import time


class RateLimiter:
    """Thread-safe fixed-window rate limiter keyed by arbitrary strings."""

    def __init__(self):
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window: float) -> bool:
        now = time.monotonic()
        with self._lock:
            bucket = self._hits.setdefault(key, [])
            cutoff = now - window
            bucket[:] = [t for t in bucket if t > cutoff]
            if len(bucket) >= limit:
                return False
            bucket.append(now)
            return True

    def retry_after(self, key: str, window: float) -> int:
        """Seconds until the oldest recorded attempt leaves the window."""
        now = time.monotonic()
        with self._lock:
            bucket = self._hits.get(key, [])
            if not bucket:
                return 0
            oldest = min(bucket)
            return max(1, int(window - (now - oldest)) + 1)

    def clear(self):
        with self._lock:
            self._hits.clear()


def client_ip(request) -> str:
    """Best-effort client IP.

    X-Forwarded-For is only trusted when POCKET_TRUSTED_PROXY is enabled, to
    avoid letting remote clients spoof the rate-limit key on a direct setup."""
    if os.environ.get("POCKET_TRUSTED_PROXY", "").lower() in ("1", "true", "yes"):
        forwarded = request.headers.get("x-forwarded-for", "")
        first = forwarded.split(",")[0].strip() if forwarded else ""
        if first:
            return first
    if request.client:
        return request.client.host
    return "unknown"


def tokens_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(str(a or ""), str(b or ""))


_LOWER = re.compile(r"[a-z]")
_UPPER = re.compile(r"[A-Z]")
_DIGIT = re.compile(r"[0-9]")
_SPECIAL = re.compile(r"[^A-Za-z0-9]")


def validate_password(password: str) -> str | None:
    """Return an error message when the password fails the policy, else None.

    Policy: at least 10 characters, matching at least 3 of 4 character
    classes (lowercase, uppercase, digits, symbols)."""
    if len(password) < 10:
        return "Password must be at least 10 characters"
    classes = sum(1 for pat in (_LOWER, _UPPER, _DIGIT, _SPECIAL) if pat.search(password))
    if classes < 3:
        return "Password must contain at least 3 of: lowercase, uppercase, digits, symbols"
    return None
