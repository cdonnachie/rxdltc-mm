from __future__ import annotations

from decimal import Decimal

import pytest

from rxdltc_mm.config import BotConfig
from rxdltc_mm.kdf.fake import FakeKdf
from rxdltc_mm.prices.fake import StaticPriceProvider

FAIR = Decimal("2800000")  # RXD per LTC
# USD legs that produce exactly 2,800,000 RXD/LTC: 56 / 0.00002 = 2,800,000
RXD_USD = Decimal("0.00002")
LTC_USD = Decimal("56")


def make_config(**overrides) -> BotConfig:
    raw = {
        "poll_interval_seconds": 30,
        "pricing": {
            "min_valid_providers": 2,
            "providers": [],
        },
        "order_sizing": {"mode": "balance_percent", "rxd_balance_percent": 50, "ltc_balance_percent": 50},
        "safety": {"cooldown_seconds": 300, "recovery_seconds": 120},
        "persistence": {"sqlite_path": ":memory:"},
        "metrics": {"enabled": False},
        "dry_run": False,
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(raw.get(key), dict):
            raw[key] = {**raw[key], **value}
        else:
            raw[key] = value
    return BotConfig.model_validate(raw)


class FakeClock:
    def __init__(self, start: float = 1_800_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def fake_kdf(clock: FakeClock) -> FakeKdf:
    k = FakeKdf(clock=clock)
    k.enabled = {"RXD", "LTC"}
    return k


def providers_at(fair: Decimal = FAIR, n: int = 2, ts: float | None = None, clock: FakeClock | None = None) -> list[StaticPriceProvider]:
    """Providers quoting exactly ``fair``; quotes are stamped with ``ts`` or the fake ``clock``."""
    rxd_usd = LTC_USD / fair
    stamp = ts if ts is not None else clock
    return [StaticPriceProvider(f"p{i}", rxd_usd, LTC_USD, timestamp=stamp, clock=clock) for i in range(n)]
