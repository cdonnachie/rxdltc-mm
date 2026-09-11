from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

import httpx

from rxdltc_mm.logging_setup import get_logger

log = get_logger("prices")


@dataclass(frozen=True)
class LegQuote:
    coin: str  # "RXD" | "LTC"
    price: Decimal  # price of one coin in ``quote_currency``
    quote_currency: str  # "USD" | "BTC" ...
    timestamp: float  # unix seconds, as reported by the source (or fetch time if the source has none)


@dataclass
class ProviderResult:
    source: str
    ok: bool
    legs: dict[str, LegQuote] = field(default_factory=dict)
    error: str | None = None
    fetched_at: float = 0.0  # when the data was actually fetched (cached results keep the original time)
    latency_ms: float = 0.0
    cached: bool = False


@dataclass
class ProviderHealth:
    consecutive_failures: int = 0
    last_ok_at: float | None = None
    last_error: str | None = None
    total_ok: int = 0
    total_failures: int = 0

    @property
    def healthy(self) -> bool:
        return self.consecutive_failures == 0 and self.last_ok_at is not None


class PriceProvider(ABC):
    """Base class. Subclasses implement :meth:`_fetch`; this class adds timing,
    error capture, health bookkeeping, rate limiting and a last-good cache.

    * ``min_interval_seconds``: do not call the source more often than this;
      in between, the last good result is returned (with its original
      ``fetched_at``, so the aggregator's fetch-age check still applies).
    * HTTP 429 triggers an exponential back-off (or ``Retry-After``); while
      backing off the last good result is reused if there is one.
    """

    BACKOFF_INITIAL = 30.0
    BACKOFF_MAX = 600.0

    def __init__(self, name: str, timeout_seconds: float = 10.0, client: httpx.Client | None = None,
                 clock: Callable[[], float] = time.time, min_interval_seconds: float = 0.0):
        self.name = name
        self.timeout_seconds = timeout_seconds
        self.min_interval_seconds = float(min_interval_seconds)
        self.health = ProviderHealth()
        self._client = client
        self.clock = clock  # injectable so simulations/tests can use a fake clock for fetched_at
        self._last_ok: ProviderResult | None = None
        self._backoff_until = 0.0
        self._backoff = self.BACKOFF_INITIAL

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout_seconds, headers={"User-Agent": "rxdltc-mm/0.1"})
        return self._client

    @abstractmethod
    def _fetch(self) -> dict[str, LegQuote]:
        """Return the legs this provider can supply. Raise on any failure."""

    def _cached(self) -> ProviderResult | None:
        if self._last_ok is None:
            return None
        r = self._last_ok
        return ProviderResult(r.source, True, dict(r.legs), None, r.fetched_at, 0.0, cached=True)

    def fetch(self) -> ProviderResult:
        started = time.monotonic()
        now = self.clock()
        cached = self._cached()
        if cached is not None and now < self._backoff_until:
            return cached
        if cached is not None and self.min_interval_seconds > 0 and now - cached.fetched_at < self.min_interval_seconds:
            return cached
        try:
            legs = self._fetch()
            if not legs:
                raise ValueError("provider returned no legs")
            for leg in legs.values():
                if leg.price <= 0:
                    raise ValueError(f"non-positive price for {leg.coin}")
            self.health.consecutive_failures = 0
            self.health.last_ok_at = now
            self.health.total_ok += 1
            self._backoff = self.BACKOFF_INITIAL
            result = ProviderResult(self.name, True, legs, None, now, (time.monotonic() - started) * 1000)
            self._last_ok = result
            return result
        except Exception as exc:  # noqa: BLE001 - any provider failure is a data point, not a crash
            err = f"{exc.__class__.__name__}: {exc}"
            throttled = isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429
            if throttled:
                retry_after = _retry_after(exc.response.headers.get("Retry-After"))
                wait = retry_after if retry_after is not None else self._backoff
                self._backoff = min(self._backoff * 2, self.BACKOFF_MAX)
                self._backoff_until = now + wait
                err = f"rate limited (HTTP 429), backing off {wait:.0f}s"
            self.health.last_error = err
            if throttled and cached is not None:
                # not a health failure while a fresh cached quote covers the gap
                log.warning("price provider throttled; reusing last quote", provider=self.name,
                            quote_age=f"{now - cached.fetched_at:.0f}s", error=err)
                return cached
            self.health.consecutive_failures += 1
            self.health.total_failures += 1
            log.warning("price provider failed", provider=self.name, error=err)
            return ProviderResult(self.name, False, {}, err, now, (time.monotonic() - started) * 1000)

    # helpers for subclasses -------------------------------------------
    def _get_json(self, url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> Any:
        resp = self.client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        return resp.json()


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(1.0, min(float(value), 3600.0))
    except ValueError:
        return None  # HTTP-date form: fall back to exponential back-off


def to_decimal(value: Any) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"not a number: {value!r}") from exc
    if not d.is_finite():
        raise ValueError(f"not a finite number: {value!r}")
    return d


def parse_timestamp(value: Any, default: float) -> float:
    """Accept unix seconds, unix milliseconds or ISO-8601 strings."""
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.replace(".", "", 1).isdigit()):
        ts = float(value)
        return ts / 1000.0 if ts > 1e12 else ts
    if isinstance(value, str):
        from datetime import datetime, timezone

        s = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return default
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return default


def dig(data: Any, path: str) -> Any:
    """Dotted-path lookup into nested dicts/lists: ``"RXD.last_price"``, ``"quotes.USD.price"``, ``"data.0.x"``."""
    cur = data
    for part in path.split("."):
        if isinstance(cur, list):
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            if part not in cur:
                raise KeyError(f"path {path!r}: missing {part!r}")
            cur = cur[part]
        else:
            raise KeyError(f"path {path!r}: cannot descend into {type(cur).__name__}")
    return cur
