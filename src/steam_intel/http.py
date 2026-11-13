"""HTTP layer: per-host token buckets, retries with backoff, and a 403 circuit breaker.

Every API call in the project goes through `HttpClient.get`, so rate limits are enforced in one place.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Self

import httpx

from .config import Settings

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class RateLimiter:
    """Token bucket. `rate` tokens/second, bursts up to `capacity`. Thread-safe."""

    def __init__(self, rate: float, capacity: float = 1.0,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        if rate <= 0:
            raise ValueError("rate must be > 0")
        self.rate, self.capacity = rate, capacity
        self._tokens = capacity
        self._clock, self._sleep = clock, sleep
        self._last = clock()
        self._lock = threading.Lock()

    def acquire(self) -> float:
        """Block until a token is available. Returns seconds waited."""
        waited = 0.0
        while True:
            with self._lock:
                now = self._clock()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return waited
                wait = (1 - self._tokens) / self.rate
            self._sleep(wait)
            waited += wait

    def penalize(self, seconds: float) -> None:
        """Push the bucket into debt so the next calls wait (used after a 429)."""
        with self._lock:
            self._tokens = min(self._tokens, 0) - seconds * self.rate


class CircuitOpenError(RuntimeError):
    """Raised when a host keeps returning 403 and we must stop hammering it."""


@dataclass
class ApiResult:
    status: int
    payload: Any                 # parsed JSON, or None if the body wasn't JSON
    url: str
    params: dict | None
    elapsed_s: float

    @property
    def ok(self) -> bool:
        return self.status == 200 and self.payload is not None


class HttpClient:
    """Shared client. `host_key` selects the rate-limit bucket: 'store', 'webapi', 'steamspy'."""

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep, max_attempts: int = 6,
                 forbidden_pause_s: float = 300.0, max_forbidden: int = 2):
        self._client = httpx.Client(
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"User-Agent": settings.user_agent, "Accept": "application/json"},
            transport=transport,
            follow_redirects=False,
        )
        self.limiters = {
            "store": RateLimiter(settings.store_rps),
            "webapi": RateLimiter(settings.webapi_rps),
            "steamspy": RateLimiter(settings.steamspy_rps),
        }
        self._sleep = sleep
        self.max_attempts = max_attempts
        self.forbidden_pause_s = forbidden_pause_s
        self.max_forbidden = max_forbidden
        self._forbidden_streak = {k: 0 for k in self.limiters}

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
        return min(120.0, 2 ** attempt) + random.uniform(0, 1)

    def get(self, host_key: str, url: str, params: dict | None = None) -> ApiResult:
        limiter = self.limiters[host_key]
        last_exc: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            limiter.acquire()
            t0 = time.monotonic()
            try:
                resp = self._client.get(url, params=params)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
                delay = self._backoff(attempt, None)
                log.warning("network error on %s (%s); retry in %.1fs", url, exc, delay)
                self._sleep(delay)
                continue
            elapsed = time.monotonic() - t0

            if resp.status_code == 403:
                self._forbidden_streak[host_key] += 1
                if self._forbidden_streak[host_key] > self.max_forbidden:
                    raise CircuitOpenError(f"{host_key}: repeated 403 Forbidden from {url}")
                log.warning("403 from %s; pausing %.0fs (circuit breaker)", host_key, self.forbidden_pause_s)
                self._sleep(self.forbidden_pause_s)
                continue
            self._forbidden_streak[host_key] = 0

            if resp.status_code in RETRYABLE_STATUS:
                delay = self._backoff(attempt, resp.headers.get("retry-after"))
                if resp.status_code == 429:
                    limiter.penalize(delay)
                log.warning("HTTP %s from %s; retry %d in %.1fs", resp.status_code, url, attempt, delay)
                self._sleep(delay)
                continue

            try:
                payload = resp.json()
            except ValueError:
                payload = None
            return ApiResult(resp.status_code, payload, str(resp.url), params, elapsed)

        if last_exc:
            raise last_exc
        return ApiResult(resp.status_code, None, url, params, 0.0)  # retries exhausted
