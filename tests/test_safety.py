from decimal import Decimal

from rxdltc_mm.engine.safety import FailureCounter, PauseController, PriceMoveMonitor


def test_price_move_breaker_trips_on_large_move_within_window():
    m = PriceMoveMonitor(Decimal(5), 300)
    assert m.observe(0, Decimal("2800000")) is None
    assert m.observe(60, Decimal("2850000")) is None  # 1.8%
    move = m.observe(120, Decimal("2960000"))  # 5.7% vs first observation
    assert move is not None and move > 5


def test_price_move_breaker_forgets_old_observations():
    m = PriceMoveMonitor(Decimal(5), 300)
    m.observe(0, Decimal("2800000"))
    assert m.observe(301, Decimal("3000000")) is None  # first observation fell out of the window


def test_price_move_breaker_downwards():
    m = PriceMoveMonitor(Decimal(5), 300)
    m.observe(0, Decimal("2800000"))
    assert m.observe(10, Decimal("2600000")) is not None


def test_failure_counter():
    c = FailureCounter(3)
    assert not c.failure() and not c.failure()
    assert c.failure() and c.tripped
    c.success()
    assert not c.tripped and c.total == 3


def test_pause_controller_requires_cooldown_and_recovery():
    p = PauseController(cooldown_seconds=300, recovery_seconds=120)
    p.pause("test", 0)
    p.mark(True, 0)
    assert not p.can_resume(100)
    assert not p.can_resume(299)
    assert p.can_resume(300)  # cooldown over and healthy for 300s
    p.mark(False, 310)  # something went wrong again
    assert not p.can_resume(320)
    p.mark(True, 320)
    assert not p.can_resume(400)
    assert p.can_resume(440)


def test_pause_controller_keeps_first_pause_time():
    p = PauseController(300, 120)
    p.pause("a", 0)
    p.pause("b", 100)
    assert p.paused_at == 0 and p.reason == "b"
    p.resume()
    assert not p.paused
