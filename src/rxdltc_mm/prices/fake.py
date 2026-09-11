"""Deterministic providers for tests and ``--simulate``."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Callable

from rxdltc_mm.prices.base import LegQuote, PriceProvider


class StaticPriceProvider(PriceProvider):
    def __init__(self, name: str, rxd_usd: Decimal | str | None, ltc_usd: Decimal | str | None,
                 *, timestamp: float | Callable[[], float] | None = None, fail: bool = False,
                 drift_pct_per_call: Decimal = Decimal(0), clock: Callable[[], float] | None = None):
        super().__init__(name, clock=clock or (timestamp if callable(timestamp) else time.time))
        self.rxd_usd = Decimal(str(rxd_usd)) if rxd_usd is not None else None
        self.ltc_usd = Decimal(str(ltc_usd)) if ltc_usd is not None else None
        self.timestamp = timestamp  # fixed value, a clock callable, or None for wall time
        self.fail = fail
        self.drift = drift_pct_per_call
        self.calls = 0

    def _fetch(self) -> dict[str, LegQuote]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("simulated provider outage")
        if callable(self.timestamp):
            ts = float(self.timestamp())
        else:
            ts = self.timestamp if self.timestamp is not None else time.time()
        factor = (Decimal(1) + self.drift / 100) ** (self.calls - 1)
        legs = {}
        if self.rxd_usd is not None:
            legs["RXD"] = LegQuote("RXD", self.rxd_usd * factor, "USD", ts)
        if self.ltc_usd is not None:
            legs["LTC"] = LegQuote("LTC", self.ltc_usd, "USD", ts)
        return legs
