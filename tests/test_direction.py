"""The actual order direction sent to KDF must correspond to buying / selling RXD."""

from decimal import Decimal

from rxdltc_mm.config import TradingConfig
from rxdltc_mm.kdf.client import build_setprice_request
from rxdltc_mm.kdf.fake import FakeKdf
from rxdltc_mm.kdf.models import MakerOrder
from rxdltc_mm.pricing import Side

TRADING = TradingConfig()
FAIR = Decimal("2800000")


def _req(side: Side, price: Decimal, amount: Decimal) -> dict:
    return build_setprice_request(side, price, amount, base="RXD", quote="LTC", trading=TRADING, min_volume_fraction=Decimal("0.25"))


def test_bid_sells_ltc_for_rxd_at_canonical_price():
    """RXD bid == bot buys RXD == KDF order that SELLS LTC (base) for RXD (rel).
    KDF price is rel-per-base = RXD per LTC, i.e. the canonical number unchanged."""
    req = _req(Side.BID, Decimal("2828000"), Decimal("0.05"))
    assert req["base"] == "LTC" and req["rel"] == "RXD"
    assert req["price"] == "2828000"
    assert req["volume"] == "0.05"  # volume is in base == LTC
    assert req["min_volume"] == "0.0125"
    assert req["cancel_previous"] is False


def test_ask_sells_rxd_for_ltc_at_inverted_price():
    """RXD ask == bot sells RXD == KDF order that SELLS RXD (base) for LTC (rel).
    KDF price is LTC per RXD, so the canonical price is inverted exactly once."""
    req = _req(Side.ASK, Decimal("2772000"), Decimal("100000"))
    assert req["base"] == "RXD" and req["rel"] == "LTC"
    assert Decimal(req["price"]) == (Decimal(1) / Decimal("2772000")).quantize(Decimal("1e-18"))
    assert req["volume"] == "100000"  # volume is in base == RXD


def test_kdf_prices_imply_buying_cheaper_and_selling_dearer_than_fair():
    """Cross-check economics through the KDF-native numbers."""
    bid = _req(Side.BID, Decimal("2828000"), Decimal("1"))
    ask = _req(Side.ASK, Decimal("2772000"), Decimal("1"))
    # bid: give 1 LTC, receive 2,828,000 RXD -> cost per RXD in LTC
    cost_per_rxd = Decimal(1) / Decimal(bid["price"])
    # ask: give 1 RXD, receive `price` LTC
    revenue_per_rxd = Decimal(ask["price"])
    fair_ltc_per_rxd = Decimal(1) / FAIR
    assert cost_per_rxd < fair_ltc_per_rxd < revenue_per_rxd


def test_orders_never_reversed_by_reciprocal_confusion():
    """A reversed order would be an RXD 'bid' with base=RXD or an 'ask' priced in RXD per LTC."""
    for price in (Decimal("2500000"), Decimal("2800000"), Decimal("3100000")):
        bid, ask = _req(Side.BID, price, Decimal(1)), _req(Side.ASK, price, Decimal(1))
        assert (bid["base"], bid["rel"]) == ("LTC", "RXD")
        assert (ask["base"], ask["rel"]) == ("RXD", "LTC")
        assert Decimal(bid["price"]) > 1  # RXD per LTC is a big number
        assert Decimal(ask["price"]) < 1  # LTC per RXD is a tiny number


def test_maker_order_side_and_canonical_price_round_trip():
    kdf = FakeKdf()
    bid = kdf.place_bid(Decimal("2828000"), Decimal("0.05"))
    ask = kdf.place_ask(Decimal("2772000"), Decimal("100000"))
    assert bid.side_for("RXD", "LTC") is Side.BID
    assert ask.side_for("RXD", "LTC") is Side.ASK
    assert bid.price_rxd_per_ltc("RXD", "LTC") == Decimal("2828000")
    assert ask.price_rxd_per_ltc("RXD", "LTC").quantize(Decimal("0.01")) == Decimal("2772000")


def test_my_orders_parsing_uses_kdf_native_price():
    raw = {"uuid": "u1", "base": "RXD", "rel": "LTC", "price": "0.000000360750360750", "max_base_vol": "100000",
           "min_base_vol": "25000", "available_amount": "60000", "created_at": 1789000000000, "cancellable": True,
           "matches": {}, "started_swaps": []}
    o = MakerOrder.from_rpc(raw)
    assert o.side_for("RXD", "LTC") is Side.ASK
    assert o.price_rxd_per_ltc("RXD", "LTC").quantize(Decimal(1)) == Decimal("2772000")
    assert o.available_base_amount == Decimal("60000")
    assert o.created_at_seconds() == 1789000000.0
