import httpx
import pytest

from steam_intel.config import Settings
from steam_intel.http import CircuitOpenError, HttpClient, RateLimiter


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def test_rate_limiter_spaces_calls():
    clk = FakeClock()
    rl = RateLimiter(rate=2.0, capacity=1, clock=clk, sleep=clk.sleep)
    for _ in range(5):
        rl.acquire()
    # first call is free, the next four each wait 0.5s
    assert clk.t == pytest.approx(2.0)


def test_rate_limiter_penalize_creates_debt():
    clk = FakeClock()
    rl = RateLimiter(rate=1.0, capacity=1, clock=clk, sleep=clk.sleep)
    rl.acquire()
    rl.penalize(10)
    rl.acquire()
    assert clk.t >= 10


def _client(handler, sleeps):
    return HttpClient(Settings(store_rps=1000, webapi_rps=1000, steamspy_rps=1000),
                      transport=httpx.MockTransport(handler), sleep=sleeps.append,
                      forbidden_pause_s=1, max_forbidden=1)


def test_retries_429_then_succeeds():
    calls, sleeps = [], []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json={"ok": True})

    res = _client(handler, sleeps).get("webapi", "https://api.steampowered.com/x")
    assert res.ok and res.payload == {"ok": True}
    assert len(calls) == 3 and sleeps[:2] == [7.0, 7.0]


def test_non_retryable_status_returned_immediately():
    calls, sleeps = [], []

    def handler(request):
        calls.append(1)
        return httpx.Response(400, text="bad")

    res = _client(handler, sleeps).get("store", "https://store.steampowered.com/x")
    assert res.status == 400 and not res.ok and len(calls) == 1


def test_repeated_403_opens_circuit():
    sleeps = []
    client = _client(lambda r: httpx.Response(403), sleeps)
    with pytest.raises(CircuitOpenError):
        client.get("store", "https://store.steampowered.com/x")
    assert sleeps == [1]          # paused once, then gave up


def test_5xx_exhausts_retries():
    sleeps = []
    client = _client(lambda r: httpx.Response(503), sleeps)
    client.max_attempts = 3
    res = client.get("webapi", "https://api.steampowered.com/x")
    assert res.status == 503 and not res.ok and len(sleeps) == 3
