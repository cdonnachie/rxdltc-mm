"""Turn provider legs into one fair RXD-per-LTC reference price.

Pipeline (all thresholds configurable):

1. Drop failed providers, non-positive prices, results fetched more than
   ``reference_stale_seconds`` ago, and legs whose *source-reported* update
   time is older than ``max_source_age_seconds`` (free aggregator APIs update
   every few minutes, so this is looser than the fetch-age limit).
2. Providers with both legs (same quote currency) yield a *complete* quote:
   ``rxd_per_ltc = ltc_price / rxd_price``.
3. Optionally, providers with exactly one leg yield a *synthetic* quote by
   borrowing the median of the missing leg from the complete quotes.
4. With three or more quotes, reject outliers further than
   ``outlier_rejection_pct`` from the median.
5. Require at least ``min_valid_providers`` quotes.
6. Require the remaining quotes to agree within ``max_provider_disagreement_pct``.
7. ``fair = median(remaining quotes)``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from statistics import median

from rxdltc_mm.config import PricingConfig
from rxdltc_mm.prices.base import LegQuote, ProviderResult
from rxdltc_mm.pricing import HUNDRED


@dataclass(frozen=True)
class ReferencePrice:
    fair_rxd_per_ltc: Decimal
    number_of_sources: int
    source_prices: dict[str, Decimal]  # after outlier rejection, canonical unit
    disagreement_pct: Decimal  # (max - min) / median * 100 over the used sources
    timestamp: float  # aggregation time
    oldest_leg_age_seconds: float
    synthetic_sources: tuple[str, ...] = ()
    rejected: dict[str, str] = field(default_factory=dict)  # source -> reason
    rxd_usd: Decimal | None = None  # median USD price of one RXD over the used sources (informational)
    ltc_usd: Decimal | None = None  # median USD price of one LTC over the used sources (informational)


@dataclass(frozen=True)
class AggregationResult:
    reference: ReferencePrice | None
    reason: str  # "ok" or why no reference could be produced
    rejected: dict[str, str] = field(default_factory=dict)
    candidate_prices: dict[str, Decimal] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.reference is not None


def _median(values: list[Decimal]) -> Decimal:
    return Decimal(median(values))


def aggregate(results: list[ProviderResult], cfg: PricingConfig, now: float) -> AggregationResult:
    rejected: dict[str, str] = {}
    valid_legs: dict[str, dict[str, LegQuote]] = {}
    ages: dict[str, float] = {}

    for r in results:
        if not r.ok:
            rejected[r.source] = f"provider error: {r.error}"
            continue
        fetch_age = now - r.fetched_at
        if fetch_age > cfg.reference_stale_seconds:
            rejected[r.source] = f"quote stale (fetched {fetch_age:.0f}s ago > {cfg.reference_stale_seconds:.0f}s)"
            continue
        legs: dict[str, LegQuote] = {}
        worst_age = fetch_age
        for coin, leg in r.legs.items():
            age = now - leg.timestamp
            if age > cfg.max_source_age_seconds:
                rejected[r.source] = f"{coin} leg stale at source ({age:.0f}s > {cfg.max_source_age_seconds:.0f}s)"
                legs = {}
                break
            if leg.price <= 0:
                rejected[r.source] = f"{coin} leg non-positive"
                legs = {}
                break
            legs[coin] = leg
            worst_age = max(worst_age, age)
        if legs:
            valid_legs[r.source] = legs
            ages[r.source] = worst_age

    complete: dict[str, Decimal] = {}
    partial: dict[str, dict[str, LegQuote]] = {}
    for source, legs in valid_legs.items():
        if "RXD" in legs and "LTC" in legs:
            if legs["RXD"].quote_currency != legs["LTC"].quote_currency:
                rejected[source] = "legs quoted in different currencies"
                continue
            complete[source] = legs["LTC"].price / legs["RXD"].price
        else:
            partial[source] = legs

    synthetic: list[str] = []
    candidates = dict(complete)
    if cfg.allow_synthetic_quotes and partial and complete:
        rxd_usd = [valid_legs[s]["RXD"].price for s in complete]
        ltc_usd = [valid_legs[s]["LTC"].price for s in complete]
        med_rxd, med_ltc = _median(rxd_usd), _median(ltc_usd)
        for source, legs in partial.items():
            currency = next(iter(legs.values())).quote_currency
            if any(valid_legs[s]["RXD"].quote_currency != currency for s in complete):
                rejected[source] = "single leg in a different currency than the complete quotes"
                continue
            if "LTC" in legs:
                candidates[source] = legs["LTC"].price / med_rxd
            else:
                candidates[source] = med_ltc / legs["RXD"].price
            synthetic.append(source)
    else:
        for source in partial:
            rejected[source] = "only one leg available"

    if not candidates:
        detail = "; ".join(f"{s}: {why}" for s, why in rejected.items()) or "no providers"
        return AggregationResult(None, f"no valid reference prices ({detail})", rejected, {})

    used = dict(candidates)
    if len(used) >= 3:
        med = _median(list(used.values()))
        for source, price in list(used.items()):
            dev = abs(price - med) / med * HUNDRED
            if dev > cfg.outlier_rejection_pct:
                rejected[source] = f"outlier: {dev:.2f}% from median"
                del used[source]

    if len(used) < cfg.min_valid_providers:
        return AggregationResult(None, f"only {len(used)} valid provider(s), need {cfg.min_valid_providers}", rejected, candidates)

    prices = list(used.values())
    fair = _median(prices)
    disagreement = (max(prices) - min(prices)) / fair * HUNDRED if len(prices) > 1 else Decimal(0)
    if disagreement > cfg.max_provider_disagreement_pct:
        return AggregationResult(
            None, f"providers disagree by {disagreement:.2f}% > {cfg.max_provider_disagreement_pct}%", rejected, candidates
        )

    def usd_leg(coin: str) -> Decimal | None:
        vals = [valid_legs[s][coin].price for s in used if coin in valid_legs.get(s, {})
                and valid_legs[s][coin].quote_currency.upper() in ("USD", "USDT", "USDC")]
        return _median(vals) if vals else None

    ref = ReferencePrice(
        fair_rxd_per_ltc=fair,
        number_of_sources=len(used),
        source_prices=used,
        disagreement_pct=disagreement,
        timestamp=now,
        oldest_leg_age_seconds=max((ages[s] for s in used), default=0.0),
        synthetic_sources=tuple(s for s in synthetic if s in used),
        rejected=rejected,
        rxd_usd=usd_leg("RXD"),
        ltc_usd=usd_leg("LTC"),
    )
    return AggregationResult(ref, "ok", rejected, candidates)
