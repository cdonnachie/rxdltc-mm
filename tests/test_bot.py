"""End-to-end cycles against the in-memory KDF: restart reconciliation, dry-run
guarantees, circuit breakers, resume, swaps and shutdown."""

from decimal import Decimal

from rxdltc_mm.engine.bot import LiquidityBot
from rxdltc_mm.engine.state import BotState
from rxdltc_mm.kdf.dry_run import DryRunKdfClient
from rxdltc_mm.persistence import Store
from rxdltc_mm.prices.fake import StaticPriceProvider
from rxdltc_mm.pricing import Side
from tests.conftest import FAIR, LTC_USD, make_config, providers_at


def _bot(cfg, kdf, providers, clock, store=None, dry_run=False):
    store = store or Store(":memory:")
    return LiquidityBot(cfg, kdf, providers, store, dry_run=dry_run, clock=clock, sleep=lambda s: None)


def test_first_cycle_places_two_sided_quotes(fake_kdf, clock):
    cfg = make_config()
    fake_kdf.balances = {"RXD": Decimal("280000"), "LTC": Decimal("0.1")}  # exactly 50/50 at 2.8M -> zero skew
    bot = _bot(cfg, fake_kdf, providers_at(clock=clock), clock)
    bot.cycle()
    assert bot.sm.state is BotState.ACTIVE
    orders = list(fake_kdf.orders.values())
    sides = sorted(o.side_for("RXD", "LTC").value for o in orders)
    assert sides == ["ask", "bid"]
    bid = next(o for o in orders if o.side_for("RXD", "LTC") is Side.BID)
    ask = next(o for o in orders if o.side_for("RXD", "LTC") is Side.ASK)
    assert bid.price_rxd_per_ltc("RXD", "LTC") == Decimal("2828000")
    assert ask.price_rxd_per_ltc("RXD", "LTC").quantize(Decimal("0.01")) == Decimal("2772000")
    assert bid.max_base_vol == Decimal("0.045")  # 0.1 LTC * 0.9 * 0.5
    assert ask.max_base_vol == Decimal("126000")  # 280000 * 0.9 * 0.5
    assert "setprice" in fake_kdf.calls
    # status line mentions the canonical unit numbers
    assert "fair=2.800M" in bot.status_line()


def test_second_cycle_is_idempotent(fake_kdf, clock):
    cfg = make_config()
    bot = _bot(cfg, fake_kdf, providers_at(clock=clock), clock)
    bot.cycle()
    n = fake_kdf.calls.count("setprice")
    clock.advance(30)
    bot.cycle()
    assert fake_kdf.calls.count("setprice") == n
    assert len(fake_kdf.orders) == 2


def test_dry_run_never_mutates(fake_kdf, clock):
    cfg = make_config()
    dry = DryRunKdfClient(fake_kdf)
    bot = _bot(cfg, dry, providers_at(clock=clock), clock, dry_run=True)
    bot.cycle()
    clock.advance(30)
    bot.cycle()
    assert "setprice" not in fake_kdf.calls
    assert "cancel_order" not in fake_kdf.calls
    assert "cancel_all_orders" not in fake_kdf.calls
    assert fake_kdf.orders == {}
    assert len(dry.would_place) == 4  # same two intents logged on both cycles
    # even a pause + shutdown must not cancel through the real client
    bot.providers = [StaticPriceProvider("dead", None, None, fail=True)] * 2
    clock.advance(30)
    bot.cycle()
    assert bot.sm.state is BotState.PAUSED
    bot.shutdown()
    assert "cancel_all_orders" not in fake_kdf.calls


def test_restart_adopts_existing_orders_without_duplicating(fake_kdf, clock):
    cfg = make_config()
    fake_kdf.add_order(Side.BID, Decimal("2828000"), Decimal("0.045"), created_at=clock.now - 600)
    fake_kdf.add_order(Side.ASK, Decimal("2772000"), Decimal("135000"), created_at=clock.now - 600)
    bot = _bot(cfg, fake_kdf, providers_at(clock=clock), clock)
    bot.cycle()
    assert "setprice" not in fake_kdf.calls
    assert len(fake_kdf.orders) == 2
    assert len(bot.known_order_uuids) == 2


def test_restart_with_persisted_uuid_marks_missing_order_closed(fake_kdf, clock):
    store = Store(":memory:")
    store.record_order("gone-1", "bid", Decimal("2828000"), Decimal("0.045"), clock.now - 600, "open")
    live = fake_kdf.add_order(Side.ASK, Decimal("2772000"), Decimal("135000"), created_at=clock.now - 600)
    store.record_order(live.uuid, "ask", Decimal("2772000"), Decimal("135000"), clock.now - 600, "open")
    bot = _bot(make_config(), fake_kdf, providers_at(clock=clock), clock, store=store)
    bot.cycle()
    assert store.open_order_uuids() >= {live.uuid}
    assert "gone-1" not in store.open_order_uuids()
    assert bot.known_order_uuids >= {live.uuid}


def test_restart_with_duplicates_pauses_and_cancels(fake_kdf, clock):
    cfg = make_config()
    fake_kdf.add_order(Side.BID, Decimal("2828000"), Decimal("0.045"), created_at=clock.now - 600)
    fake_kdf.add_order(Side.BID, Decimal("2820000"), Decimal("0.045"), created_at=clock.now - 600)
    bot = _bot(cfg, fake_kdf, providers_at(clock=clock), clock)
    bot.cycle()
    assert bot.sm.state is BotState.PAUSED
    assert "ambiguous" in bot.pause.reason
    assert fake_kdf.orders == {}  # cancel_all ran


def test_price_move_breaker_pauses_and_cancels_then_resumes(fake_kdf, clock):
    cfg = make_config(safety={"cooldown_seconds": 60, "recovery_seconds": 30, "max_price_move_window_seconds": 60})
    p = providers_at(clock=clock)
    bot = _bot(cfg, fake_kdf, p, clock)
    bot.cycle()
    assert len(fake_kdf.orders) == 2
    # jump the reference 6% within the window
    bot.providers = providers_at(FAIR * Decimal("1.06"), clock=clock)
    clock.advance(30)
    bot.cycle()
    assert bot.sm.state is BotState.PAUSED
    assert "moved" in bot.pause.reason
    assert fake_kdf.orders == {}
    # stays paused during cooldown even though the price is stable now (the 60s window still
    # contains the pre-jump observation at t=60, then it ages out)
    for _ in range(2):
        clock.advance(30)
        bot.cycle()
    assert bot.sm.state is BotState.PAUSED
    assert fake_kdf.orders == {}
    # cooldown (60s) elapsed and healthy for >= 30s -> resume and re-quote
    clock.advance(30)
    bot.cycle()
    assert bot.sm.state is BotState.ACTIVE
    assert len(fake_kdf.orders) == 2


def test_stale_reference_pauses(fake_kdf, clock):
    cfg = make_config(pricing={"reference_stale_seconds": 90, "max_source_age_seconds": 600})
    bot = _bot(cfg, fake_kdf, providers_at(clock=clock), clock)
    bot.cycle()
    bot.providers = providers_at(ts=clock.now - 700, clock=clock)  # source says its price is 700s old
    clock.advance(30)
    bot.cycle()
    assert bot.sm.state is BotState.PAUSED and "stale at source" in bot.pause.reason
    assert fake_kdf.orders == {}


def test_provider_disagreement_pauses(fake_kdf, clock):
    cfg = make_config()
    bot = _bot(cfg, fake_kdf, providers_at(clock=clock), clock)
    bot.cycle()
    rxd = LTC_USD / FAIR
    bot.providers = [StaticPriceProvider("a", rxd, LTC_USD, timestamp=clock), StaticPriceProvider("b", rxd, LTC_USD * Decimal("1.08"), timestamp=clock)]
    clock.advance(30)
    bot.cycle()
    assert bot.sm.state is BotState.PAUSED and "disagree" in bot.pause.reason


def test_startup_without_reference_waits_instead_of_pausing(fake_kdf, clock):
    cfg = make_config()
    bot = _bot(cfg, fake_kdf, [StaticPriceProvider("dead", None, None, fail=True)] * 2, clock)
    bot.cycle()
    assert bot.sm.state is BotState.WAITING_FOR_REFERENCE
    assert not bot.pause.paused
    bot.providers = providers_at(clock=clock)
    clock.advance(30)
    bot.cycle()
    assert bot.sm.state is BotState.ACTIVE and len(fake_kdf.orders) == 2


def test_rpc_outage_enters_rpc_error_then_cools_down(fake_kdf, clock):
    cfg = make_config(safety={"rpc_failure_limit": 2, "cooldown_seconds": 60, "recovery_seconds": 0})
    bot = _bot(cfg, fake_kdf, providers_at(clock=clock), clock)
    bot.cycle()
    fake_kdf.offline = True
    for _ in range(2):
        clock.advance(30)
        bot.cycle()
    assert bot.sm.state is BotState.RPC_ERROR
    fake_kdf.offline = False
    clock.advance(30)
    bot.cycle()
    assert bot.sm.state is BotState.PAUSED  # recovered -> cooldown with orders cancelled
    assert fake_kdf.orders == {}
    clock.advance(61)
    bot.cycle()
    assert bot.sm.state is BotState.ACTIVE and len(fake_kdf.orders) == 2


def test_repeated_order_errors_pause(fake_kdf, clock):
    cfg = make_config(safety={"order_error_limit": 2})
    fake_kdf.fail_next_setprice = 5
    bot = _bot(cfg, fake_kdf, providers_at(clock=clock), clock)
    bot.cycle()
    assert bot.sm.state is BotState.PAUSED and "order" in bot.pause.reason


def test_failed_cancel_blocks_replacement_on_that_side(fake_kdf, clock):
    cfg = make_config(minimum_order_lifetime_seconds=0)
    stale = fake_kdf.add_order(Side.BID, Decimal("2800000"), Decimal("0.045"), created_at=clock.now - 600)
    fake_kdf.fail_next_cancel = 1
    bot = _bot(cfg, fake_kdf, providers_at(clock=clock), clock)
    bot.cycle()
    bids = [o for o in fake_kdf.orders.values() if o.side_for("RXD", "LTC") is Side.BID]
    assert [o.uuid for o in bids] == [stale.uuid]  # no duplicate bid was placed
    assert any(o.side_for("RXD", "LTC") is Side.ASK for o in fake_kdf.orders.values())


def test_swap_fill_updates_stats_and_resizes(fake_kdf, clock):
    cfg = make_config(minimum_order_lifetime_seconds=0)
    bot = _bot(cfg, fake_kdf, providers_at(clock=clock), clock)
    bot.cycle()
    ask = next(o for o in fake_kdf.orders.values() if o.side_for("RXD", "LTC") is Side.ASK)
    fake_kdf.fill_order(ask.uuid, Decimal(1))  # fully filled: 135000 RXD sold
    clock.advance(30)
    bot.cycle()
    stats = bot.store.get_stats()
    assert stats["swaps_total"] == 1
    assert stats["rxd_sold"] == Decimal("135000")
    assert stats["ltc_received"] > 0
    # the ask was recreated from the new (smaller) RXD balance and the bid resized to the larger LTC balance
    new_ask = next(o for o in fake_kdf.orders.values() if o.side_for("RXD", "LTC") is Side.ASK)
    assert new_ask.max_base_vol < Decimal("135000")
    assert bot.status_snapshot()["swaps_total"] == "1"


def test_inventory_bound_disables_bid_when_rxd_heavy(fake_kdf, clock):
    fake_kdf.balances = {"RXD": Decimal("1000000"), "LTC": Decimal("0.05")}  # RXD worth 0.357 LTC vs 0.05 LTC -> 88%
    bot = _bot(make_config(), fake_kdf, providers_at(clock=clock), clock)
    bot.cycle()
    sides = [o.side_for("RXD", "LTC") for o in fake_kdf.orders.values()]
    assert sides == [Side.ASK]


def test_shutdown_cancels_orders_when_configured(fake_kdf, clock):
    bot = _bot(make_config(), fake_kdf, providers_at(clock=clock), clock)
    bot.cycle()
    assert len(fake_kdf.orders) == 2
    bot.shutdown()
    assert fake_kdf.orders == {} and bot.sm.state is BotState.SHUTTING_DOWN


def test_shutdown_keeps_orders_when_configured(fake_kdf, clock):
    bot = _bot(make_config(shutdown={"cancel_orders_on_exit": False}), fake_kdf, providers_at(clock=clock), clock)
    bot.cycle()
    bot.shutdown()
    assert len(fake_kdf.orders) == 2


def test_run_loop_stops_after_max_cycles(fake_kdf, clock):
    bot = _bot(make_config(), fake_kdf, providers_at(clock=clock), clock)
    bot.run(max_cycles=2)
    assert bot.cycles == 2 and bot.sm.state is BotState.SHUTTING_DOWN
