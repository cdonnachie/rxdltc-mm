"""Live config reload: what can change under a running bot and what cannot."""

from decimal import Decimal

import pytest
import yaml

from rxdltc_mm.engine.bot import LiquidityBot
from rxdltc_mm.engine.state import BotState
from rxdltc_mm.persistence import Store
from tests.conftest import make_config, providers_at

BASE_YAML = {
    "pair": {"base": "RXD", "quote": "LTC"},
    "poll_interval_seconds": 30,
    "reprice_threshold_pct": 0.5,
    "pricing": {"bid_offset_pct": 1.0, "ask_offset_pct": 1.0, "providers": []},
    "safety": {"max_price_move_pct": 5.0, "cooldown_seconds": 300, "recovery_seconds": 120,
               "max_anchor_deviation_pct": 10, "rpc_failure_limit": 3},
    "persistence": {"sqlite_path": ":memory:"},
    "metrics": {"enabled": False},
    "dry_run": False,
}


def _write(path, **overrides):
    data = {**BASE_YAML}
    for k, v in overrides.items():
        data[k] = {**data[k], **v} if isinstance(v, dict) and isinstance(data.get(k), dict) else v
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _bot(tmp_path, fake_kdf, clock):
    cfg_path = _write(tmp_path / "config.yaml")
    return LiquidityBot(make_config(**BASE_YAML), fake_kdf, providers_at(clock=clock), Store(":memory:"),
                        dry_run=False, clock=clock, sleep=lambda s: None, config_path=str(cfg_path)), cfg_path


def test_reload_applies_values_and_retunes_the_breakers(tmp_path, fake_kdf, clock):
    bot, path = _bot(tmp_path, fake_kdf, clock)
    _write(path, pricing={"bid_offset_pct": 2.5, "ask_offset_pct": 2.5, "providers": []},
           safety={"max_price_move_pct": 12, "cooldown_seconds": 60, "recovery_seconds": 30,
                   "max_anchor_deviation_pct": 20, "rpc_failure_limit": 5},
           reprice_threshold_pct=0.9)
    out = bot.reload_config()
    assert out["ok"] and not out["needs_restart"]
    assert bot.cfg.pricing.bid_offset_pct == Decimal("2.5")
    assert bot.cfg.reprice_threshold_pct == Decimal("0.9")
    # objects built at startup must pick the new values up, not just the config object
    assert bot.price_monitor.max_move_pct == Decimal(12)
    assert bot.anchor.max_deviation_pct == Decimal(20)
    assert bot.pause.cooldown_seconds == 60 and bot.pause.recovery_seconds == 30
    assert bot.rpc_failures.limit == 5
    assert sorted(out["changed"]) == sorted([
        "reprice_threshold_pct", "pricing.bid_offset_pct", "pricing.ask_offset_pct",
        "safety.max_price_move_pct", "safety.max_anchor_deviation_pct",
        "safety.rpc_failure_limit", "safety.cooldown_seconds", "safety.recovery_seconds",
    ])


def test_new_offsets_take_effect_on_the_next_cycle(tmp_path, fake_kdf, clock):
    bot, path = _bot(tmp_path, fake_kdf, clock)
    bot.cycle()
    first = {o.side_for("RXD", "LTC"): o.price_rxd_per_ltc("RXD", "LTC") for o in fake_kdf.orders.values()}
    _write(path, pricing={"bid_offset_pct": 3.0, "ask_offset_pct": 3.0, "providers": []},
           safety={**BASE_YAML["safety"], "max_quote_deviation_pct": 5}, minimum_order_lifetime_seconds=0)
    bot.reload_config()
    clock.advance(30)
    bot.cycle()
    second = {o.side_for("RXD", "LTC"): o.price_rxd_per_ltc("RXD", "LTC") for o in fake_kdf.orders.values()}
    from rxdltc_mm.pricing import Side
    assert second[Side.BID] > first[Side.BID]  # wider bid: more RXD per LTC demanded
    assert second[Side.ASK] < first[Side.ASK]


def test_restart_only_settings_are_reported_and_not_applied(tmp_path, fake_kdf, clock):
    bot, path = _bot(tmp_path, fake_kdf, clock)
    _write(path, pair={"base": "RXD", "quote": "LTC-segwit"}, dry_run=True,
           metrics={"enabled": True, "port": 9999}, pricing={"bid_offset_pct": 2.0, "ask_offset_pct": 1.0, "providers": []})
    out = bot.reload_config()
    assert out["ok"]
    assert sorted(out["needs_restart"]) == ["dry_run", "metrics", "pair"]
    assert bot.cfg.pair.quote == "LTC"        # the running pair is untouched
    assert bot.cfg.dry_run is False           # live/dry stays with the launch decision
    assert bot.cfg.metrics.enabled is False
    assert bot.cfg.pricing.bid_offset_pct == Decimal("2.0")  # the safe part still applied


def test_provider_list_changes_need_a_restart(tmp_path, fake_kdf, clock):
    bot, path = _bot(tmp_path, fake_kdf, clock)
    _write(path, pricing={"bid_offset_pct": 1.0, "ask_offset_pct": 1.0,
                          "providers": [{"name": "coingecko", "type": "coingecko"}]})
    out = bot.reload_config()
    assert "pricing.providers" in out["needs_restart"]
    assert bot.cfg.pricing.providers == []  # still the providers the bot was built with


def test_an_invalid_file_changes_nothing(tmp_path, fake_kdf, clock):
    bot, path = _bot(tmp_path, fake_kdf, clock)
    before = bot.cfg.pricing.bid_offset_pct
    path.write_text("pricing:\n  bid_offset_pct: -5\n", encoding="utf-8")
    out = bot.reload_config()
    assert not out["ok"] and "rejected" in out["error"]
    assert bot.cfg.pricing.bid_offset_pct == before
    bot.cycle()  # and the bot keeps running
    assert bot.sm.state is BotState.ACTIVE


def test_reload_without_a_config_path_is_refused(fake_kdf, clock):
    bot = LiquidityBot(make_config(), fake_kdf, providers_at(clock=clock), Store(":memory:"),
                       dry_run=False, clock=clock, sleep=lambda s: None)
    assert not bot.reload_config()["ok"]


def test_reload_request_is_applied_by_the_loop(tmp_path, fake_kdf, clock):
    bot, path = _bot(tmp_path, fake_kdf, clock)
    _write(path, pricing={"bid_offset_pct": 2.0, "ask_offset_pct": 2.0, "providers": []},
           safety={**BASE_YAML["safety"], "max_quote_deviation_pct": 4})
    assert bot.request_reload().startswith("reload requested")
    assert bot.cfg.pricing.bid_offset_pct == Decimal("1.0")  # not applied from the control thread
    bot.cycle()
    assert bot.cfg.pricing.bid_offset_pct == Decimal("2.0")  # applied at the top of the cycle
    assert bot.status_snapshot()["last_reload"]["ok"] is True


@pytest.mark.parametrize("field", ["kdf", "persistence"])
def test_other_restart_only_sections(tmp_path, fake_kdf, clock, field):
    bot, path = _bot(tmp_path, fake_kdf, clock)
    override = {"kdf": {"request_timeout_seconds": 99}, "persistence": {"sqlite_path": "other.sqlite3"}}[field]
    _write(path, **{field: override})
    out = bot.reload_config()
    assert field in out["needs_restart"]
