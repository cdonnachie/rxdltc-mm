import json
import logging
import os
from decimal import Decimal

import pytest

from rxdltc_mm.config import LIVE_CONFIRMATION_PHRASE, BotConfig, live_trading_allowed, load_config, load_dotenv, load_secrets
from rxdltc_mm.kdf.models import SwapInfo
from rxdltc_mm.logging_setup import JsonFormatter, RedactingFilter, get_logger, redact, register_secret
from rxdltc_mm.metrics import render_prometheus
from rxdltc_mm.persistence import Store
from rxdltc_mm.prices.base import dig, parse_timestamp

ROOT = os.path.dirname(os.path.dirname(__file__))


def test_example_config_loads_and_is_dry_run_by_default():
    cfg = load_config(os.path.join(ROOT, "config.example.yaml"), env={})
    assert cfg.dry_run is True
    assert cfg.pair.canonical_price_unit == "RXD_PER_LTC"
    assert [p.type for p in cfg.pricing.providers if p.enabled] == ["coingecko", "coinpaprika", "gleec_cex", "generic_http"]
    assert [p.name for p in cfg.pricing.providers if p.enabled][-1] == "nonkyc"
    assert cfg.kdf.coins["RXD"].electrum[0].protocol == "SSL"


def test_live_requires_config_and_env_confirmation():
    cfg = BotConfig.model_validate({"dry_run": False})
    assert not live_trading_allowed(cfg, env={})
    assert not live_trading_allowed(cfg, env={"MM_BOT_CONFIRM_LIVE": "yes"})
    assert live_trading_allowed(cfg, env={"MM_BOT_CONFIRM_LIVE": LIVE_CONFIRMATION_PHRASE})
    assert not live_trading_allowed(BotConfig.model_validate({"dry_run": True}), env={"MM_BOT_CONFIRM_LIVE": LIVE_CONFIRMATION_PHRASE})


def test_config_rejects_unsafe_combinations():
    with pytest.raises(ValueError):
        BotConfig.model_validate({"pricing": {"bid_offset_pct": 5}, "safety": {"max_quote_deviation_pct": 3}})
    with pytest.raises(ValueError):
        BotConfig.model_validate({"inventory": {"min_rxd_value_pct": 60, "target_rxd_value_pct": 50}})
    with pytest.raises(ValueError):
        BotConfig.model_validate({"trading": {"maker_only": False}})
    with pytest.raises(ValueError):
        BotConfig.model_validate({"order_sizing": {"mode": "fixed"}})
    with pytest.raises(ValueError):
        BotConfig.model_validate({"unknown_key": 1})


def test_secrets_from_env_and_dotenv(tmp_path):
    with pytest.raises(ValueError):
        load_secrets(env={})
    s = load_secrets(env={"KDF_RPC_PASSWORD": "hunter22", "COINGECKO_API_KEY": "k"})
    assert s.rpc_password == "hunter22" and s.api_keys == {"coingecko": "k"} and "hunter22" not in repr(s)
    env_file = tmp_path / ".env"
    env_file.write_text('KDF_RPC_PASSWORD="from-file"\n# comment\nexport MM_BOT_LOG_LEVEL=DEBUG\n')
    os.environ.pop("MM_BOT_LOG_LEVEL", None)
    old = os.environ.get("KDF_RPC_PASSWORD")
    try:
        os.environ["KDF_RPC_PASSWORD"] = "already-set"
        load_dotenv(env_file)
        assert os.environ["KDF_RPC_PASSWORD"] == "already-set"  # no override
        assert os.environ["MM_BOT_LOG_LEVEL"] == "DEBUG"
    finally:
        if old is None:
            os.environ.pop("KDF_RPC_PASSWORD", None)
        else:
            os.environ["KDF_RPC_PASSWORD"] = old
        os.environ.pop("MM_BOT_LOG_LEVEL", None)


def test_log_redaction():
    register_secret("s3cr3t-pass")
    assert redact("userpass=s3cr3t-pass") == "userpass=***"
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "pw is s3cr3t-pass", (), None)
    rec.extra = {"body": "s3cr3t-pass"}
    RedactingFilter().filter(rec)
    out = JsonFormatter().format(rec)
    assert "s3cr3t-pass" not in out and "***" in out
    assert json.loads(out)["body"] == "***"
    get_logger("t").info("hello", key="v")  # adapter smoke test


def test_persistence_stats_and_orders(tmp_path):
    store = Store(tmp_path / "s.sqlite3")
    store.add_stats(swaps_total=1, rxd_sold=Decimal("10.5"))
    store.add_stats(rxd_sold=Decimal("1"))
    assert store.get_stats()["rxd_sold"] == Decimal("11.5")
    store.record_order("u1", "bid", Decimal("2828000"), Decimal("0.045"), 1.0, "open")
    assert store.open_order_uuids() == {"u1"}
    store.set_order_status("u1", "cancelled")
    assert store.open_order_uuids() == set()
    store.record_swap("s1", "ask", "RXD", "LTC", Decimal(1), Decimal(2), 0, False, False)
    assert store.swap_known("s1") == (False, False)
    store.record_swap("s1", "ask", "RXD", "LTC", Decimal(1), Decimal(2), 0, True, True)
    assert store.swap_known("s1") == (True, True)
    store.record_event("INFO", "x", "y")
    assert store.recent_events()[0]["message"] == "y"
    store.set("bot_state", {"state": "ACTIVE"})
    assert store.get("bot_state")["state"] == "ACTIVE"
    store.close()


def test_swap_info_parsing_legacy_shape():
    raw = {"uuid": "sw", "type": "Maker", "my_info": {"my_coin": "RXD", "other_coin": "LTC", "my_amount": "1000", "other_amount": "0.00036", "started_at": 5},
           "error_events": ["StartFailed", "MakerPaymentRefunded"], "success_events": ["Started", "Finished"],
           "events": [{"timestamp": 1, "event": {"type": "Started"}}, {"timestamp": 2, "event": {"type": "Finished"}}]}
    s = SwapInfo.from_rpc(raw)
    assert s.finished and s.success and s.side_for("RXD", "LTC").value == "ask"
    raw["events"].insert(1, {"timestamp": 1, "event": {"type": "MakerPaymentRefunded"}})
    s = SwapInfo.from_rpc(raw)
    assert s.finished and not s.success
    assert SwapInfo.from_rpc({"uuid": "x"}) is None


def test_swap_info_parsing_v2_envelopes():
    # mmrpc 2.0 my_recent_swaps: {"swap_type": "MakerV1", "swap_data": MakerSavedSwap}
    v1 = {"swap_type": "TakerV1", "swap_data": {
        "uuid": "t1", "maker_coin": "RXD", "taker_coin": "LTC", "maker_amount": "1000", "taker_amount": "0.00036",
        "events": [{"timestamp": 1789000000000, "event": {"type": "Started"}}, {"timestamp": 1789000100000, "event": {"type": "Finished"}}],
        "success_events": ["Started", "Finished"], "error_events": ["StartFailed"]}}
    s = SwapInfo.from_rpc(v1)
    assert s.swap_type == "Taker" and s.my_coin == "LTC" and s.other_coin == "RXD"
    assert s.my_amount == Decimal("0.00036") and s.other_amount == Decimal("1000")
    assert s.started_at == 1789000000 and s.finished and s.success
    assert s.side_for("RXD", "LTC").value == "bid"
    # MakerV2 (MySwapForRpc)
    v2 = {"swap_type": "MakerV2", "swap_data": {
        "uuid": "m2", "my_coin": "RXD", "other_coin": "LTC", "started_at": 1789000000, "is_finished": True,
        "maker_volume": {"decimal": "5000", "rational": [[1, [5000]], [1, [1]]]}, "taker_volume": {"decimal": "0.0018"},
        "events": [{"event_type": "Initialized", "event_data": {}}, {"event_type": "Completed", "event_data": {}}]}}
    s = SwapInfo.from_rpc(v2)
    assert s.swap_type == "Maker" and s.my_amount == Decimal("5000") and s.other_amount == Decimal("0.0018")
    assert s.finished and s.success and s.last_event == "Completed" and s.side_for("RXD", "LTC").value == "ask"
    v2["swap_data"]["events"].append({"event_type": "Aborted", "event_data": {"reason": "x"}})
    assert not SwapInfo.from_rpc(v2).success
    v2["swap_data"]["is_finished"] = False
    assert not SwapInfo.from_rpc(v2).finished


def test_helpers():
    assert dig({"RXD": {"last_price": "1"}}, "RXD.last_price") == "1"
    assert dig({"a": [{"b": 2}]}, "a.0.b") == 2
    assert parse_timestamp(1789123520, 0) == 1789123520
    assert parse_timestamp(1789123520000, 0) == 1789123520
    assert parse_timestamp("2026-09-11T10:46:47.946Z", 0) > 1.7e9
    assert parse_timestamp(None, 7) == 7


def test_prometheus_rendering():
    snap = {"state": "ACTIVE", "fair_price_rxd_per_ltc": "2800000", "paused": 0, "paused_reason": None,
            "orders": {"bid": {"price_rxd_per_ltc": "2828000", "amount": "0.045"}, "ask": None},
            "providers": {"gecko": {"healthy": True, "price_rxd_per_ltc": "2800000"}}}
    text = render_prometheus(snap)
    assert 'rxdltc_mm_state{state="ACTIVE"} 1' in text
    assert "rxdltc_mm_fair_price_rxd_per_ltc 2800000.0" in text
    assert 'rxdltc_mm_open_order{side="bid"} 1' in text and 'rxdltc_mm_open_order{side="ask"} 0' in text
    assert 'rxdltc_mm_provider_healthy{provider="gecko"} 1' in text


def test_usd_gauges_are_exported():
    snap = {"state": "ACTIVE", "fair_price_rxd_per_ltc": "1624000", "fair_price_usd_per_rxd": "0.000033",
            "target_bid_usd_per_rxd": "0.0000327", "target_ask_usd_per_rxd": "0.0000333",
            "rxd_usd": "0.000033", "ltc_usd": "53.6", "portfolio_usd": "14.55",
            "balance_rxd_usd": "4.08", "balance_ltc_usd": "10.47", "paused": 0, "orders": {"bid": None, "ask": None},
            "providers": {"nonkyc": {"healthy": True, "price_rxd_per_ltc": "1618000", "rxd_usd": "0.0000331", "ltc_usd": "53.5"}}}
    text = render_prometheus(snap)
    assert "rxdltc_mm_fair_price_usd_per_rxd 3.3e-05" in text
    assert "rxdltc_mm_portfolio_usd 14.55" in text
    assert 'rxdltc_mm_provider_rxd_usd{provider="nonkyc"} 3.31e-05' in text
    assert 'rxdltc_mm_provider_ltc_usd{provider="nonkyc"} 53.5' in text
