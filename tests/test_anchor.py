"""The slow price anchor: a thin market being pushed must not become a quote."""

from decimal import Decimal

from rxdltc_mm.engine.bot import LiquidityBot
from rxdltc_mm.engine.safety import PriceAnchor
from rxdltc_mm.engine.state import BotState
from rxdltc_mm.persistence import Store
from tests.conftest import FAIR, make_config, providers_at

WINDOW = 3600.0
LIMIT = Decimal(10)


def _anchor() -> PriceAnchor:
    return PriceAnchor(WINDOW, LIMIT, min_span_seconds=900)


def test_no_check_until_the_history_is_long_enough():
    a = _anchor()
    now = 1000.0
    for i in range(10):
        a.observe(now + i * 30, Decimal("2800000"))
    assert a.value(now + 300) is None  # only 300s of history
    assert a.check(Decimal("9999999"), now + 300) is None  # so nothing is refused yet


def test_median_over_the_window_and_deviation():
    a = _anchor()
    start = 1000.0
    for i in range(40):  # 40 samples over 1200s
        a.observe(start + i * 30, Decimal("2800000"))
    now = start + 1200
    assert a.value(now) == Decimal("2800000")
    assert a.deviation_pct(Decimal("2940000"), now) == Decimal(5)
    assert a.check(Decimal("2940000"), now) is None  # 5% is inside the limit
    assert a.check(Decimal("3220000"), now) == Decimal(15)  # 15% is not
    assert a.check(Decimal("2380000"), now) == Decimal(-15)  # and it is symmetric


def test_old_samples_leave_the_window():
    a = _anchor()
    a.observe(0, Decimal("1000000"))
    for i in range(40):
        a.observe(4000 + i * 30, Decimal("2800000"))
    assert a.value(5200) == Decimal("2800000")  # the ancient sample is gone
    assert a.samples == 40


def test_a_step_change_trips_then_the_anchor_follows():
    a = _anchor()
    t = 1000.0
    for _ in range(60):  # an hour at the old level
        a.observe(t, Decimal("2800000"))
        t += 30
    doubled = Decimal("5600000")
    assert a.check(doubled, t) is not None  # refused immediately
    for _ in range(29):  # less than half the window at the new level
        a.observe(t, doubled)
        t += 30
    assert a.check(doubled, t) is not None  # still refused
    for _ in range(40):  # now the new level is the majority of the window
        a.observe(t, doubled)
        t += 30
    assert a.check(doubled, t) is None  # a sustained move is accepted


def _trend(rate_per_sample: str, samples: int = 240) -> bool:
    """Run a steady trend through the anchor; True if it ever trips."""
    a = _anchor()
    t, price, tripped = 1000.0, Decimal("2800000"), False
    for _ in range(samples):
        a.observe(t, price)
        tripped = tripped or a.check(price, t) is not None
        price *= Decimal(rate_per_sample)
        t += 30
    return tripped


def test_a_gradual_move_never_trips():
    # The median sits half a window back, so the anchor tolerates roughly its limit per
    # half-window: 10% per 30 minutes here. +0.05% per 30s is +3% per half-window, well inside,
    # and still +12.7% over the two hours simulated.
    assert not _trend("1.0005")


def test_a_fast_trend_does_trip():
    # +0.2% per 30s compounds to +12.7% per half-window, past the 10% limit.
    assert _trend("1.002")


def test_seed_from_history_ignores_stale_and_bad_rows():
    a = _anchor()
    now = 10_000.0
    loaded = a.seed([(now - 300, Decimal("2800000")), (now - 60, Decimal("2810000")),
                     (now - 99_999, Decimal("1")), (now - 30, Decimal(0))], now)
    assert loaded == 2  # outside the window and non-positive rows are dropped


def test_bot_pauses_on_a_pushed_price_and_resumes_when_it_holds(fake_kdf, clock):
    cfg = make_config(safety={"anchor_window_seconds": 3600, "max_anchor_deviation_pct": 10,
                              "anchor_min_span_seconds": 300, "cooldown_seconds": 0, "recovery_seconds": 0,
                              "max_price_move_pct": 1000})  # isolate the anchor from the fast breaker
    bot = LiquidityBot(cfg, fake_kdf, providers_at(clock=clock), Store(":memory:"), dry_run=False,
                       clock=clock, sleep=lambda s: None)
    for _ in range(12):  # six minutes of steady price
        bot.cycle()
        clock.advance(30)
    assert bot.sm.state is BotState.ACTIVE and len(fake_kdf.orders) == 2

    bot.providers = providers_at(FAIR * Decimal("1.5"), clock=clock)  # the only live market gets pushed
    bot.cycle()
    assert bot.sm.state is BotState.PAUSED
    assert "anchor" in bot.pause.reason
    assert fake_kdf.orders == {}

    for _ in range(40):  # the pushed level persists and becomes the median
        clock.advance(30)
        bot.cycle()
    assert bot.sm.state is BotState.ACTIVE and len(fake_kdf.orders) == 2


def test_anchor_can_be_disabled(fake_kdf, clock):
    cfg = make_config(safety={"anchor_enabled": False, "anchor_min_span_seconds": 60,
                              "max_price_move_pct": 1000, "cooldown_seconds": 0, "recovery_seconds": 0})
    bot = LiquidityBot(cfg, fake_kdf, providers_at(clock=clock), Store(":memory:"), dry_run=False,
                       clock=clock, sleep=lambda s: None)
    for _ in range(6):
        bot.cycle()
        clock.advance(30)
    bot.providers = providers_at(FAIR * Decimal("1.5"), clock=clock)
    bot.cycle()
    assert bot.sm.state is BotState.ACTIVE


def test_anchor_survives_a_restart_through_the_database(fake_kdf, clock):
    store = Store(":memory:")
    cfg = make_config(safety={"anchor_min_span_seconds": 300, "max_anchor_deviation_pct": 10,
                              "max_price_move_pct": 1000, "cooldown_seconds": 0, "recovery_seconds": 0})
    bot = LiquidityBot(cfg, fake_kdf, providers_at(clock=clock), store, dry_run=False, clock=clock, sleep=lambda s: None)
    for _ in range(20):
        bot.cycle()
        clock.advance(30)
    # a fresh process with the same database keeps the history, so a pushed price is refused at once
    restarted = LiquidityBot(cfg, fake_kdf, providers_at(FAIR * Decimal("1.5"), clock=clock), store,
                             dry_run=False, clock=clock, sleep=lambda s: None)
    assert restarted.anchor.samples >= 20
    restarted.cycle()
    assert restarted.sm.state is BotState.PAUSED and "anchor" in restarted.pause.reason
