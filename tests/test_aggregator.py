"""Stale price detection, provider disagreement, outlier rejection, synthetic quotes."""

from decimal import Decimal

from rxdltc_mm.config import PricingConfig
from rxdltc_mm.prices.aggregator import aggregate
from rxdltc_mm.prices.base import LegQuote, ProviderResult
from rxdltc_mm.prices.fake import StaticPriceProvider

NOW = 1_800_000_000.0
CFG = PricingConfig(min_valid_providers=2, reference_stale_seconds=90, max_source_age_seconds=600,
                    max_provider_disagreement_pct=Decimal(5), outlier_rejection_pct=Decimal(5))


def _res(name: str, rxd_usd: str | None, ltc_usd: str | None, ts: float = NOW, ok: bool = True,
         fetched_at: float = NOW) -> ProviderResult:
    legs = {}
    if rxd_usd is not None:
        legs["RXD"] = LegQuote("RXD", Decimal(rxd_usd), "USD", ts)
    if ltc_usd is not None:
        legs["LTC"] = LegQuote("LTC", Decimal(ltc_usd), "USD", ts)
    return ProviderResult(name, ok, legs, None if ok else "boom", fetched_at)


def test_median_of_valid_sources():
    out = aggregate([_res("a", "0.00002", "56"), _res("b", "0.00002", "56.56"), _res("c", "0.00002", "55.44")], CFG, NOW)
    assert out.ok
    assert out.reference.fair_rxd_per_ltc == Decimal("2800000")
    assert out.reference.number_of_sources == 3
    assert out.reference.disagreement_pct == Decimal(2)


def test_usd_legs_are_medians_of_used_sources():
    out = aggregate([_res("a", "0.00002", "56"), _res("b", "0.0000202", "56.56"), _res("c", "0.0000198", "55.44")], CFG, NOW)
    assert out.ok
    assert out.reference.rxd_usd == Decimal("0.00002")
    assert out.reference.ltc_usd == Decimal("56")
    # a single-leg (synthetic) provider contributes only the leg it has
    out = aggregate([_res("a", "0.00002", "56"), _res("b", "0.00002", "56"), _res("g", None, "60")], CFG, NOW)
    assert out.ok and out.reference.ltc_usd == Decimal("56") and out.reference.rxd_usd == Decimal("0.00002")


def test_stale_source_leg_is_rejected_and_min_providers_enforced():
    out = aggregate([_res("a", "0.00002", "56"), _res("b", "0.00002", "56", ts=NOW - 700)], CFG, NOW)
    assert not out.ok
    assert "b" in out.rejected and "stale at source" in out.rejected["b"]
    assert "need 2" in out.reason


def test_source_age_within_limit_is_accepted():
    # free APIs typically report updates 1-4 minutes old; that must not count as stale
    out = aggregate([_res("a", "0.00002", "56", ts=NOW - 240), _res("b", "0.00002", "56", ts=NOW - 120)], CFG, NOW)
    assert out.ok and out.reference.oldest_leg_age_seconds == 240


def test_stale_fetch_is_rejected():
    out = aggregate([_res("a", "0.00002", "56"), _res("b", "0.00002", "56", fetched_at=NOW - 120)], CFG, NOW)
    assert not out.ok
    assert "fetched 120s ago" in out.rejected["b"]


def test_failed_provider_counts_as_rejected():
    out = aggregate([_res("a", "0.00002", "56"), _res("b", None, None, ok=False)], CFG, NOW)
    assert not out.ok
    assert out.rejected["b"].startswith("provider error")


def test_disagreement_beyond_tolerance_blocks_reference():
    out = aggregate([_res("a", "0.00002", "56"), _res("b", "0.00002", "60")], CFG, NOW)  # 7.1% apart
    assert not out.ok
    assert "disagree" in out.reason


def test_outlier_rejected_with_three_sources():
    out = aggregate([_res("a", "0.00002", "56"), _res("b", "0.00002", "56.28"), _res("c", "0.00002", "70")], CFG, NOW)
    assert out.ok
    assert "c" in out.reference.rejected and "outlier" in out.reference.rejected["c"]
    assert out.reference.number_of_sources == 2
    assert out.reference.fair_rxd_per_ltc == Decimal("2807000")


def test_synthetic_quote_from_single_leg_provider():
    out = aggregate([_res("gecko", "0.00002", "56"), _res("paprika", "0.00002", "56.56"), _res("gleec", None, "56.28")], CFG, NOW)
    assert out.ok
    assert out.reference.synthetic_sources == ("gleec",)
    assert out.reference.source_prices["gleec"] == Decimal("2814000")  # 56.28 / median(rxd_usd)=0.00002


def test_single_leg_ignored_when_synthetic_disabled():
    cfg = PricingConfig(min_valid_providers=1, allow_synthetic_quotes=False)
    out = aggregate([_res("gecko", "0.00002", "56"), _res("gleec", None, "56.28")], cfg, NOW)
    assert out.ok and out.reference.number_of_sources == 1
    assert out.rejected["gleec"] == "only one leg available"


def test_single_leg_alone_cannot_form_a_price():
    out = aggregate([_res("gleec", None, "56.28")], PricingConfig(min_valid_providers=1), NOW)
    assert not out.ok


def test_non_positive_price_rejected_by_provider_layer():
    p = StaticPriceProvider("zero", "0", "56")
    r = p.fetch()
    assert not r.ok and p.health.consecutive_failures == 1
    p2 = StaticPriceProvider("ok", "0.00002", "56")
    assert p2.fetch().ok and p2.health.healthy
