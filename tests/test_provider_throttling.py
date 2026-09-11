"""Rate limiting: minimum interval, 429 back-off and last-good reuse."""

from decimal import Decimal

import httpx

from rxdltc_mm.prices.base import LegQuote, PriceProvider


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


class Flaky(PriceProvider):
    def __init__(self, clock, **kw):
        super().__init__("flaky", clock=clock, **kw)
        self.calls = 0
        self.mode = "ok"
        self.retry_after: str | None = None

    def _fetch(self):
        self.calls += 1
        if self.mode == "429":
            req = httpx.Request("GET", "https://api.example/price")
            resp = httpx.Response(429, request=req, headers={"Retry-After": self.retry_after} if self.retry_after else {})
            raise httpx.HTTPStatusError("429", request=req, response=resp)
        if self.mode == "boom":
            raise RuntimeError("boom")
        return {"RXD": LegQuote("RXD", Decimal("0.00002"), "USD", self.clock()), "LTC": LegQuote("LTC", Decimal("56"), "USD", self.clock())}


def test_min_interval_reuses_last_quote():
    clock = Clock()
    p = Flaky(clock, min_interval_seconds=60)
    r1 = p.fetch()
    clock.now += 30
    r2 = p.fetch()
    assert p.calls == 1 and r2.cached and r2.fetched_at == r1.fetched_at and r2.ok
    clock.now += 31
    r3 = p.fetch()
    assert p.calls == 2 and not r3.cached


def test_429_backs_off_and_reuses_cache_without_health_penalty():
    clock = Clock()
    p = Flaky(clock)
    p.fetch()
    p.mode = "429"
    clock.now += 30
    r = p.fetch()
    assert r.ok and r.cached and p.health.consecutive_failures == 0
    assert "rate limited" in (p.health.last_error or "")
    # inside the back-off window the source is not called at all
    clock.now += 10
    p.fetch()
    assert p.calls == 2
    # after the back-off (30s initial) it retries; the cached quote is now 70s old but still returned
    clock.now += 25
    r = p.fetch()
    assert p.calls == 3 and r.cached
    # second 429 doubles the back-off to 60s
    clock.now += 45
    p.fetch()
    assert p.calls == 3
    p.mode = "ok"
    clock.now += 20
    r = p.fetch()
    assert p.calls == 4 and not r.cached and r.ok


def test_429_without_cache_is_a_failure():
    clock = Clock()
    p = Flaky(clock)
    p.mode = "429"
    r = p.fetch()
    assert not r.ok and p.health.consecutive_failures == 1


def test_retry_after_header_is_honoured():
    clock = Clock()
    p = Flaky(clock)
    p.fetch()
    p.mode = "429"
    p.retry_after = "120"
    clock.now += 1
    p.fetch()
    clock.now += 100
    p.fetch()
    assert p.calls == 2  # still backing off
    clock.now += 25
    p.fetch()
    assert p.calls == 3


def test_other_errors_do_not_return_cache():
    clock = Clock()
    p = Flaky(clock)
    p.fetch()
    p.mode = "boom"
    clock.now += 5
    r = p.fetch()
    assert not r.ok and p.health.consecutive_failures == 1
