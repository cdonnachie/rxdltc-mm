"""Repricing thresholds, minimum lifetime, duplicate prevention, crossing checks."""

from decimal import Decimal

from rxdltc_mm.engine.planner import OrderIntent, build_plan, check_crossing
from rxdltc_mm.kdf.fake import FakeKdf
from rxdltc_mm.kdf.models import Orderbook, OrderbookEntry
from rxdltc_mm.pricing import Side
from tests.conftest import FakeClock, make_config

FAIR = Decimal("2800000")
BID = OrderIntent(Side.BID, Decimal("2828000"), Decimal("0.045"))
ASK = OrderIntent(Side.ASK, Decimal("2772000"), Decimal("135000"))


def _kinds(plan, side):
    return [(a.kind, a.side) for a in plan.actions if a.side is side]


def test_places_both_sides_when_nothing_exists():
    cfg = make_config()
    plan = build_plan(cfg=cfg, fair_rxd_per_ltc=FAIR, bid=BID, ask=ASK, existing=[], orderbook=None, now=1000)
    assert [(a.kind, a.side) for a in plan.actions] == [("place", Side.BID), ("place", Side.ASK)]


def test_keeps_orders_within_threshold_no_duplicates():
    cfg = make_config()
    kdf = FakeKdf(clock=FakeClock(0))
    kdf.add_order(Side.BID, Decimal("2830000"), Decimal("0.045"), created_at=0)  # 0.07% off
    kdf.add_order(Side.ASK, Decimal("2775000"), Decimal("135000"), created_at=0)
    plan = build_plan(cfg=cfg, fair_rxd_per_ltc=FAIR, bid=BID, ask=ASK, existing=list(kdf.orders.values()), orderbook=None, now=1000)
    assert all(a.kind == "keep" for a in plan.actions)
    assert plan.mutations == []


def test_reprices_when_price_drifts_beyond_threshold():
    cfg = make_config()
    kdf = FakeKdf(clock=FakeClock(0))
    kdf.add_order(Side.BID, Decimal("2800000"), Decimal("0.045"), created_at=0)  # 0.99% off the 2,828,000 target
    plan = build_plan(cfg=cfg, fair_rxd_per_ltc=FAIR, bid=BID, ask=None, existing=list(kdf.orders.values()), orderbook=None, now=1000)
    assert _kinds(plan, Side.BID) == [("cancel", Side.BID), ("place", Side.BID)]


def test_reprice_deferred_while_order_is_young():
    cfg = make_config(minimum_order_lifetime_seconds=60)
    kdf = FakeKdf(clock=FakeClock(0))
    kdf.add_order(Side.BID, Decimal("2800000"), Decimal("0.045"), created_at=1000)
    plan = build_plan(cfg=cfg, fair_rxd_per_ltc=FAIR, bid=BID, ask=None, existing=list(kdf.orders.values()), orderbook=None, now=1030)
    assert _kinds(plan, Side.BID) == [("keep", Side.BID)]
    assert "deferred" in plan.actions[0].reason
    plan = build_plan(cfg=cfg, fair_rxd_per_ltc=FAIR, bid=BID, ask=None, existing=list(kdf.orders.values()), orderbook=None, now=1061)
    assert _kinds(plan, Side.BID) == [("cancel", Side.BID), ("place", Side.BID)]


def test_unsafe_existing_order_is_cancelled_regardless_of_age():
    cfg = make_config()
    kdf = FakeKdf(clock=FakeClock(0))
    kdf.add_order(Side.BID, Decimal("2700000"), Decimal("0.045"), created_at=1000)  # bid below fair = overpaying
    plan = build_plan(cfg=cfg, fair_rxd_per_ltc=FAIR, bid=BID, ask=None, existing=list(kdf.orders.values()), orderbook=None, now=1001)
    assert _kinds(plan, Side.BID) == [("cancel", Side.BID), ("place", Side.BID)]


def test_resize_after_partial_fill():
    cfg = make_config(resize_threshold_pct=25)
    kdf = FakeKdf(clock=FakeClock(0))
    o = kdf.add_order(Side.ASK, Decimal("2772000"), Decimal("135000"), created_at=0)
    kdf.fill_order(o.uuid, Decimal("0.5"))  # 67,500 RXD remain
    plan = build_plan(cfg=cfg, fair_rxd_per_ltc=FAIR, bid=None, ask=ASK, existing=list(kdf.orders.values()), orderbook=None, now=1000)
    assert _kinds(plan, Side.ASK) == [("cancel", Side.ASK), ("place", Side.ASK)]
    assert "size dev" in plan.actions[0].reason


def test_matching_order_is_left_alone():
    cfg = make_config()
    kdf = FakeKdf(clock=FakeClock(0))
    kdf.add_order(Side.ASK, Decimal("2000000"), Decimal("135000"), created_at=0, matches=1, cancellable=False)
    plan = build_plan(cfg=cfg, fair_rxd_per_ltc=FAIR, bid=None, ask=ASK, existing=list(kdf.orders.values()), orderbook=None, now=1000)
    assert _kinds(plan, Side.ASK) == [("keep", Side.ASK)]


def test_cancels_when_side_no_longer_wanted():
    cfg = make_config()
    kdf = FakeKdf(clock=FakeClock(0))
    kdf.add_order(Side.BID, Decimal("2828000"), Decimal("0.045"), created_at=0)
    plan = build_plan(cfg=cfg, fair_rxd_per_ltc=FAIR, bid=None, ask=None, existing=list(kdf.orders.values()), orderbook=None, now=1000)
    assert _kinds(plan, Side.BID) == [("cancel", Side.BID)]


def test_two_orders_on_one_side_is_ambiguous():
    cfg = make_config()
    kdf = FakeKdf(clock=FakeClock(0))
    kdf.add_order(Side.BID, Decimal("2828000"), Decimal("0.045"), created_at=0)
    kdf.add_order(Side.BID, Decimal("2820000"), Decimal("0.045"), created_at=0)
    plan = build_plan(cfg=cfg, fair_rxd_per_ltc=FAIR, bid=BID, ask=None, existing=list(kdf.orders.values()), orderbook=None, now=1000)
    assert plan.ambiguous and "2 bid orders" in plan.ambiguous
    assert not any(a.kind == "place" for a in plan.actions)


def test_two_orders_on_one_side_cleaned_up_when_configured():
    cfg = make_config(reconcile={"cancel_unknown_orders": True})
    kdf = FakeKdf(clock=FakeClock(0))
    kdf.add_order(Side.BID, Decimal("2828000"), Decimal("0.045"), created_at=0)
    kdf.add_order(Side.BID, Decimal("2820000"), Decimal("0.045"), created_at=0)
    plan = build_plan(cfg=cfg, fair_rxd_per_ltc=FAIR, bid=BID, ask=None, existing=list(kdf.orders.values()), orderbook=None, now=1000)
    assert plan.ambiguous is None
    assert [a.kind for a in plan.actions] == ["cancel", "cancel"]


def _book(foreign_ask_rxd_per_ltc=None, foreign_bid_rxd_per_ltc=None) -> Orderbook:
    asks = [OrderbookEntry(Decimal(1) / Decimal(foreign_ask_rxd_per_ltc), Decimal(1000), Decimal(1), "a", False, "pk")] if foreign_ask_rxd_per_ltc else []
    bids = [OrderbookEntry(Decimal(1) / Decimal(foreign_bid_rxd_per_ltc), Decimal(1000), Decimal(1), "b", False, "pk")] if foreign_bid_rxd_per_ltc else []
    return Orderbook("RXD", "LTC", tuple(asks), tuple(bids), 0)


def test_bid_that_would_cross_foreign_ask_is_skipped():
    # someone sells RXD at 2,850,000 RXD/LTC (cheaper than our bid wants); our bid at 2,828,000 would be a taker-able cross
    cfg = make_config()
    intent, note = check_crossing(BID, _book(foreign_ask_rxd_per_ltc=2_850_000), cfg, FAIR)
    assert intent is None and "cross" in note


def test_bid_not_crossing_is_untouched():
    cfg = make_config()
    intent, note = check_crossing(BID, _book(foreign_ask_rxd_per_ltc=2_700_000), cfg, FAIR)
    assert intent == BID and note is None


def test_ask_crossing_adjusted_when_configured():
    cfg = make_config(trading={"on_cross": "adjust"})
    # foreign bid buys RXD at 2,760,000 RXD/LTC (they pay more LTC per RXD than our ask asks) -> our ask 2,772,000 crosses
    intent, note = check_crossing(ASK, _book(foreign_bid_rxd_per_ltc=2_760_000), cfg, FAIR)
    assert intent is not None and intent.price_rxd_per_ltc < Decimal(2_760_000)
    assert "adjusted" in note


def test_adjustment_outside_envelope_is_skipped():
    cfg = make_config(trading={"on_cross": "adjust"})
    intent, note = check_crossing(BID, _book(foreign_ask_rxd_per_ltc=3_000_000), cfg, FAIR)  # would need > 3% deviation
    assert intent is None and "safety envelope" in note


def test_own_orders_in_book_are_ignored_for_crossing():
    cfg = make_config()
    mine = OrderbookEntry(Decimal(1) / Decimal(2_850_000), Decimal(1000), Decimal(1), "m", True, "me")
    book = Orderbook("RXD", "LTC", (mine,), (), 0)
    intent, _ = check_crossing(BID, book, cfg, FAIR)
    assert intent == BID
