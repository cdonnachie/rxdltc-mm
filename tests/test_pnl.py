"""P&L against holding: the effect of trading, split into execution edge and inventory."""

from decimal import Decimal

from rxdltc_mm.pnl import SwapFill, compute_pnl, fill_from_swap
from rxdltc_mm.pricing import Side

D = Decimal
FAIR = D("2000000")          # RXD per LTC at the time of every trade below
LTC_USD = D("50")
RXD_USD = LTC_USD / FAIR     # 0.000025, i.e. prices unchanged since the trade


def _fair(price=FAIR):
    return lambda ts: price


def _ask(rxd, ltc, ts=0.0):
    return SwapFill(Side.ASK, base_amount=D(rxd), quote_amount=D(ltc), started_at=ts)


def _bid(rxd, ltc, ts=0.0):
    return SwapFill(Side.BID, base_amount=D(rxd), quote_amount=D(ltc), started_at=ts)


def test_a_sell_above_fair_with_prices_unchanged_is_all_edge():
    # 100k RXD is worth 0.05 LTC at fair; selling it for 0.0505 earns 1%
    p = compute_pnl([_ask("100000", "0.0505")], _fair(), RXD_USD, LTC_USD)
    assert p.net_base == D(-100000) and p.net_quote == D("0.0505")
    assert p.edge_quote == D("0.0005")
    assert p.edge_usd == D("0.025")
    assert p.vs_hold_usd == D("0.025")
    assert p.inventory_usd == 0


def test_selling_before_a_rally_shows_as_inventory_loss():
    # same fill, but RXD has since doubled against LTC
    rxd_now = LTC_USD / D("1000000")
    p = compute_pnl([_ask("100000", "0.0505")], _fair(), rxd_now, LTC_USD)
    assert p.edge_usd == D("0.025")          # the fill itself was still good
    assert p.vs_hold_usd == D("-2.475")      # but holding would have been better
    assert p.inventory_usd == D("-2.5")      # and the market move is why


def test_a_round_trip_earns_the_spread_whatever_the_price_does():
    fills = [_ask("100000", "0.0505"), _bid("100000", "0.0495")]
    for rxd_now in (RXD_USD, RXD_USD * 2, RXD_USD / 2):
        p = compute_pnl(fills, _fair(), rxd_now, LTC_USD)
        assert p.net_base == 0                    # back to the same RXD
        assert p.net_quote == D("0.001")          # with more LTC
        assert p.vs_hold_usd == D("0.05")
        assert p.edge_usd == D("0.05")
        assert p.inventory_usd == 0               # nothing left exposed to the move


def test_a_bad_fill_is_negative_edge():
    # bought 100k RXD for 0.0510 LTC when it was worth 0.05
    p = compute_pnl([_bid("100000", "0.0510")], _fair(), RXD_USD, LTC_USD)
    assert p.edge_quote == D("-0.001")


def test_missing_fair_prices_leave_the_split_incomplete():
    lookup = lambda ts: FAIR if ts < 100 else None  # noqa: E731
    p = compute_pnl([_ask("100000", "0.0505", ts=1), _ask("100000", "0.0505", ts=500)], lookup, RXD_USD, LTC_USD)
    assert p.edge_coverage == 1 and p.swaps == 2
    assert p.vs_hold_usd is not None          # still exact: it needs only today's prices
    assert p.inventory_usd is None            # but it cannot be split honestly


def test_without_usd_prices_nothing_is_invented():
    p = compute_pnl([_ask("100000", "0.0505")], _fair(), None, None)
    assert p.vs_hold_usd is None and p.edge_usd is None and p.volume_usd is None
    assert p.edge_quote == D("0.0005")        # the LTC-denominated edge is still known


def test_no_swaps():
    p = compute_pnl([], _fair(), RXD_USD, LTC_USD)
    assert p.swaps == 0 and p.vs_hold_usd == 0 and p.inventory_usd == 0 and p.edge_usd is None


def test_fill_from_swap_maps_each_side():
    ask = fill_from_swap(Side.ASK, my_amount=D("100000"), other_amount=D("0.05"), started_at=1)
    assert (ask.base_amount, ask.quote_amount) == (D("100000"), D("0.05"))
    bid = fill_from_swap(Side.BID, my_amount=D("0.05"), other_amount=D("100000"), started_at=1)
    assert (bid.base_amount, bid.quote_amount) == (D("100000"), D("0.05"))


def test_the_real_wallet_matches_the_hand_calculation():
    """The two sells this wallet actually made, valued at the prices discussed at the time."""
    fills = [
        _ask("146792.09345614", "0.05330646568627450980"),
        _ask("89572.52992815", "0.03273584828171690245"),
    ]
    p = compute_pnl(fills, _fair(D("2750000")), D("0.0000373"), D("53.5"))
    assert round(p.vs_hold_usd, 2) == D("-4.21")
    assert p.edge_usd is not None and p.edge_usd > 0      # the fills themselves were fine
    assert p.inventory_usd < p.vs_hold_usd                # the rally is what cost money


def test_bot_status_reports_pnl_from_recorded_swaps(fake_kdf, clock):
    from rxdltc_mm.engine.bot import LiquidityBot
    from rxdltc_mm.persistence import Store
    from tests.conftest import FAIR as CONF_FAIR, make_config, providers_at

    cfg = make_config(minimum_order_lifetime_seconds=0)
    bot = LiquidityBot(cfg, fake_kdf, providers_at(clock=clock), Store(":memory:"), dry_run=False,
                       clock=clock, sleep=lambda s: None)
    bot.cycle()
    ask = next(o for o in fake_kdf.orders.values() if o.side_for("RXD", "LTC") is Side.ASK)
    fake_kdf.fill_order(ask.uuid, Decimal(1))
    clock.advance(30)
    bot.cycle()
    pnl = bot.status_snapshot()["pnl"]
    assert pnl["swaps"] == 1 and pnl["edge_coverage"] == 1
    assert Decimal(pnl["net_base"]) < 0 and Decimal(pnl["net_quote"]) > 0     # sold RXD for LTC
    assert Decimal(pnl["edge_usd"]) > 0                                        # the ask sits above fair
    # prices have not moved in the simulation, so trading effect is the edge alone
    assert abs(Decimal(pnl["vs_hold_usd"]) - Decimal(pnl["edge_usd"])) < Decimal("0.001")
    assert bot.status_snapshot()["pnl_vs_hold_usd"] == pnl["vs_hold_usd"]      # flattened for /metrics
