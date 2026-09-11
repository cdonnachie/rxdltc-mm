"""Bid/ask calculation, unit conversion and price-direction correctness."""

from decimal import Decimal

import pytest

from rxdltc_mm.pricing import (
    Side,
    compute_target_quotes,
    decimal_to_str,
    deviation_pct,
    ltc_per_rxd_to_rxd_per_ltc,
    quote_is_safe,
    rxd_per_ltc_to_ltc_per_rxd,
)

FAIR = Decimal("2800000")


def test_reciprocal_conversions_round_trip():
    ltc_per_rxd = rxd_per_ltc_to_ltc_per_rxd(FAIR)
    assert ltc_per_rxd == Decimal(1) / FAIR
    assert ltc_per_rxd_to_rxd_per_ltc(ltc_per_rxd).quantize(Decimal("0.0001")) == FAIR
    assert rxd_per_ltc_to_ltc_per_rxd(Decimal("2000000")) == Decimal("0.0000005")


def test_spec_example_numbers_land_on_the_correct_sides():
    """Fair 2,800,000 RXD/LTC with 1% offsets produces the numbers from the spec
    (2,772,000 and 2,828,000), assigned to the economically correct sides:
    the bot buys RXD at 2,828,000 RXD/LTC (it receives MORE RXD per LTC than
    fair) and sells RXD at 2,772,000 RXD/LTC (it gives FEWER RXD per LTC)."""
    t = compute_target_quotes(FAIR, bid_offset_pct=Decimal("1.0"), ask_offset_pct=Decimal("1.0"),
                              min_edge_pct=Decimal(0), max_deviation_pct=Decimal(3))
    assert t.bid_rxd_per_ltc == Decimal("2828000")
    assert t.ask_rxd_per_ltc == Decimal("2772000")
    # In LTC-per-RXD (the price of one RXD) the familiar ordering holds:
    assert t.bid_ltc_per_rxd < rxd_per_ltc_to_ltc_per_rxd(FAIR) < t.ask_ltc_per_rxd


def test_bid_buys_below_fair_and_ask_sells_above_fair_in_ltc_terms():
    t = compute_target_quotes(FAIR, bid_offset_pct=Decimal("1.5"), ask_offset_pct=Decimal("0.7"),
                              min_edge_pct=Decimal("0.1"), max_deviation_pct=Decimal(3))
    fair_ltc = rxd_per_ltc_to_ltc_per_rxd(FAIR)
    # buying 1 RXD costs less LTC than fair; selling 1 RXD yields more LTC than fair
    assert t.bid_ltc_per_rxd < fair_ltc
    assert t.ask_ltc_per_rxd > fair_ltc
    # total spread in LTC/RXD terms is roughly 2.2%
    spread = (t.ask_ltc_per_rxd - t.bid_ltc_per_rxd) / fair_ltc * 100
    assert Decimal("2.1") < spread < Decimal("2.3")


def test_zero_offsets_are_pushed_out_to_min_edge():
    t = compute_target_quotes(FAIR, bid_offset_pct=Decimal(0), ask_offset_pct=Decimal(0),
                              min_edge_pct=Decimal("0.1"), max_deviation_pct=Decimal(3))
    assert t.clamped
    assert t.bid_rxd_per_ltc == FAIR * Decimal("1.001")
    assert t.ask_rxd_per_ltc == FAIR * Decimal("0.999")


def test_quotes_never_cross_fair_even_with_large_skew():
    # RXD-heavy skew pushes the midpoint up; the ask (below fair) must never go above fair - min_edge
    t = compute_target_quotes(FAIR, bid_offset_pct=Decimal(1), ask_offset_pct=Decimal(1), skew_pct=Decimal("2.5"),
                              min_edge_pct=Decimal("0.1"), max_deviation_pct=Decimal(3))
    assert t.ask_rxd_per_ltc <= FAIR * Decimal("0.999")
    assert t.bid_rxd_per_ltc <= FAIR * Decimal("1.03")
    assert t.clamped
    assert quote_is_safe(t.bid_rxd_per_ltc, Side.BID, FAIR, Decimal(3))
    assert quote_is_safe(t.ask_rxd_per_ltc, Side.ASK, FAIR, Decimal(3))


def test_skew_direction():
    base = compute_target_quotes(FAIR, bid_offset_pct=Decimal(1), ask_offset_pct=Decimal(1), min_edge_pct=Decimal(0))
    heavy = compute_target_quotes(FAIR, bid_offset_pct=Decimal(1), ask_offset_pct=Decimal(1), skew_pct=Decimal("0.5"), min_edge_pct=Decimal(0))
    light = compute_target_quotes(FAIR, bid_offset_pct=Decimal(1), ask_offset_pct=Decimal(1), skew_pct=Decimal("-0.5"), min_edge_pct=Decimal(0))
    # too much RXD: sell cheaper (ask gives more RXD per LTC) and buy pickier (bid wants more RXD per LTC)
    assert heavy.ask_rxd_per_ltc > base.ask_rxd_per_ltc
    assert heavy.bid_rxd_per_ltc > base.bid_rxd_per_ltc
    # too much LTC: the opposite
    assert light.ask_rxd_per_ltc < base.ask_rxd_per_ltc
    assert light.bid_rxd_per_ltc < base.bid_rxd_per_ltc


def test_side_toggles():
    t = compute_target_quotes(FAIR, bid_offset_pct=Decimal(1), ask_offset_pct=Decimal(1), quote_bid=False)
    assert t.bid_rxd_per_ltc is None and t.ask_rxd_per_ltc is not None
    t = compute_target_quotes(FAIR, bid_offset_pct=Decimal(1), ask_offset_pct=Decimal(1), quote_ask=False)
    assert t.ask_rxd_per_ltc is None and t.bid_rxd_per_ltc is not None


def test_quote_is_safe_rejects_wrong_side_and_far_quotes():
    assert quote_is_safe(Decimal("2828000"), Side.BID, FAIR, Decimal(3))
    assert not quote_is_safe(Decimal("2772000"), Side.BID, FAIR, Decimal(3))  # a bid below fair would overpay
    assert quote_is_safe(Decimal("2772000"), Side.ASK, FAIR, Decimal(3))
    assert not quote_is_safe(Decimal("2828000"), Side.ASK, FAIR, Decimal(3))  # an ask above fair would undersell
    assert not quote_is_safe(Decimal("2900000"), Side.BID, FAIR, Decimal(3))  # 3.57% away


def test_deviation_and_string_formatting():
    assert deviation_pct(Decimal("2814000"), FAIR) == Decimal("0.5")
    assert decimal_to_str(Decimal(1) / FAIR) == "0.000000357142857143"
    assert decimal_to_str(Decimal("2828000.000")) == "2828000"
    assert "E" not in decimal_to_str(Decimal("3.5E-7"))


def test_negative_prices_rejected():
    with pytest.raises(ValueError):
        rxd_per_ltc_to_ltc_per_rxd(Decimal(0))
    with pytest.raises(ValueError):
        compute_target_quotes(Decimal(-1), bid_offset_pct=Decimal(1), ask_offset_pct=Decimal(1))
