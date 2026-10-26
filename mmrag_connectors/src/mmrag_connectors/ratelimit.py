"""HTTP client that respects server rate limits (Confluence / Jira Data Center).

Data Center rate limiting is a per-user token bucket. When it is exhausted the server answers 429 with
`Retry-After` (seconds) and `X-RateLimit-*` headers. Retrying immediately just burns the next token, so this client:
  * paces requests to at most `max_rps` per second,
  * slows down before the bucket is empty (X-RateLimit-Remaining <= 1 -> wait for one refill),
  * on 429 / 503 waits for Retry-After (or exponential backoff with jitter) and retries up to `max_retries` times.
"""
from __future__ import annotations

import random
import threading
import time
from email.utils import parsedate_to_datetime
from typing import Callable

import httpx

RETRY_STATUS = {429, 503}


def _retry_after(r: httpx.Response) -> float | None:
    v = r.headers.get("Retry-After")
    if not v:
        return None
    try:
        return max(0.0, float(v))
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(v).timestamp() - time.time())
        except (TypeError, ValueError):
            return None


class RateLimitedClient:
    def __init__(self, *, headers: dict, verify, max_rps: float = 2.0, max_retries: int = 8,
                 max_wait: float = 300.0, log: Callable[[str], None] | None = None, timeout: float = 60):
        self.timeout = timeout
        self.http = httpx.Client(headers=headers, timeout=httpx.Timeout(timeout, connect=min(timeout, 15)),
                                 verify=verify, follow_redirects=True)
        self.min_interval = 1.0 / max_rps if max_rps and max_rps > 0 else 0.0
        self.max_retries, self.max_wait = max_retries, max_wait
        self.log = log or (lambda m: None)
        self._next = 0.0
        self._lock = threading.Lock()
        self.requests = 0          # sent (incl. retries) — handy for tests / logs
        self.throttled = 0         # 429/503 answers received

    def _pace(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._next - now
            self._next = max(now, self._next) + self.min_interval
        if wait > 0:
            time.sleep(wait)

    def _respect_headers(self, r: httpx.Response) -> None:
        """Pre-emptive slow-down when the bucket is (nearly) empty."""
        rem = r.headers.get("X-RateLimit-Remaining")
        if rem is None:
            return
        try:
            if float(rem) > 1:
                return
            fill = float(r.headers.get("X-RateLimit-FillRate", "1") or 1)
            interval = float(r.headers.get("X-RateLimit-Interval-Seconds", "1") or 1)
            pause = min(interval / max(fill, 1e-6), self.max_wait)
        except ValueError:
            return
        with self._lock:
            self._next = max(self._next, time.monotonic() + pause)

    def get(self, url: str, **kw) -> httpx.Response:
        attempt = 0
        while True:
            self._pace()
            self.requests += 1
            r = self.http.get(url, **kw)
            self._respect_headers(r)
            if r.status_code not in RETRY_STATUS or attempt >= self.max_retries:
                return r
            self.throttled += 1
            attempt += 1
            wait = _retry_after(r)
            if wait is None:
                wait = min(2 ** attempt, 120)
            wait = min(wait + random.uniform(0, 0.5 + 0.1 * wait), self.max_wait)
            self.log(f"server rate limit ({r.status_code}); waiting {wait:.1f}s before retry {attempt}/{self.max_retries}")
            with self._lock:  # everyone waits, not just this request
                self._next = max(self._next, time.monotonic() + wait)

    def close(self) -> None:
        self.http.close()
