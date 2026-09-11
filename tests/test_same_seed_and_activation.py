"""Orders from another KDF instance on the same seed, and segwit activation / repair."""

from decimal import Decimal

from rxdltc_mm.config import CoinActivation, ElectrumServer, TradingConfig
from rxdltc_mm.engine.bot import LiquidityBot
from rxdltc_mm.engine.state import BotState
from rxdltc_mm.kdf.client import KdfClient
from rxdltc_mm.kdf.dry_run import DryRunKdfClient
from rxdltc_mm.kdf.fake import FakeKdf
from rxdltc_mm.persistence import Store
from rxdltc_mm.pricing import Side
from tests.conftest import make_config, providers_at

SEGWIT_LTC = {"kdf": {"coins": {
    "RXD": {"electrum": [{"url": "rxd.example:50012", "protocol": "SSL"}]},
    "LTC": {"address_format": "segwit", "electrum": [{"url": "ltc.example:20063", "protocol": "SSL"}]},
}}}


def test_orders_from_another_instance_with_same_seed_pause(fake_kdf, clock):
    # KDF marks them is_mine (same pubkey) but my_orders does not list them: e.g. the web wallet is running
    fake_kdf.phantom_mine = [(Decimal(1) / Decimal("2857142"), Decimal("289040"), Side.ASK)]
    bot = LiquidityBot(make_config(), fake_kdf, providers_at(clock=clock), Store(":memory:"), dry_run=False, clock=clock)
    bot.cycle()
    assert bot.sm.state is BotState.PAUSED
    assert "same seed" in bot.pause.reason
    assert "setprice" not in fake_kdf.calls


def test_fresh_activation_uses_configured_format(clock):
    kdf = FakeKdf(clock=clock)  # nothing enabled yet
    bot = LiquidityBot(make_config(**SEGWIT_LTC), kdf, providers_at(clock=clock), Store(":memory:"), dry_run=False, clock=clock)
    bot.cycle()
    assert kdf.address_formats == {"RXD": "standard", "LTC": "segwit"}
    assert kdf.address_of("LTC").startswith("ltc1")
    assert bot.sm.state is BotState.ACTIVE


def test_wrong_address_format_is_repaired_when_no_orders(clock):
    kdf = FakeKdf(clock=clock)
    kdf.activate_coin("RXD", [], None)
    kdf.activate_coin("LTC", [], None)  # legacy, like a previous run without address_format
    kdf.calls.clear()
    bot = LiquidityBot(make_config(**SEGWIT_LTC), DryRunKdfClient(kdf), providers_at(clock=clock), Store(":memory:"), dry_run=True, clock=clock)
    bot.cycle()
    assert kdf.calls.count("disable_coin") == 1 and kdf.calls.count("electrum") == 1
    assert kdf.address_formats["LTC"] == "segwit" and kdf.address_formats["RXD"] == "standard"
    assert bot.sm.state is BotState.ACTIVE
    clock.advance(30)
    bot.cycle()
    assert kdf.calls.count("disable_coin") == 1  # startup-only


def test_wrong_address_format_not_repaired_while_orders_open(clock):
    kdf = FakeKdf(clock=clock)
    kdf.activate_coin("RXD", [], None)
    kdf.activate_coin("LTC", [], None)
    kdf.add_order(Side.ASK, Decimal("2772000"), Decimal("1000"), created_at=clock.now - 600)
    kdf.calls.clear()
    bot = LiquidityBot(make_config(**SEGWIT_LTC), kdf, providers_at(clock=clock), Store(":memory:"), dry_run=False, clock=clock)
    bot.cycle()
    assert "disable_coin" not in kdf.calls
    assert kdf.address_formats["LTC"] == "standard"


class _Rpc:
    def __init__(self):
        self.calls = []

    def legacy(self, method, **params):
        self.calls.append((method, params))
        if method == "disable_coin":
            return {"result": {"coin": params["coin"], "cancelled_orders": ["u-1"], "passivized": False}}
        return {"result": "success", "address": "ltc1qxyz", "balance": "0.1", "unspendable_balance": "0", "coin": "LTC"}


def test_activation_and_disable_requests():
    rpc = _Rpc()
    client = KdfClient(rpc, base="RXD", quote="LTC", trading=TradingConfig(), min_volume_fraction=Decimal("0.25"))
    act = CoinActivation(electrum=[ElectrumServer(url="ltc.example:20063", protocol="SSL")], address_format="segwit")
    bal = client.activate_coin("LTC", act.electrum, act.address_format)
    method, params = rpc.calls[0]
    assert method == "electrum" and params["coin"] == "LTC"
    assert params["address_format"] == {"format": "segwit"}
    assert params["servers"][0]["url"] == "ltc.example:20063"
    assert bal.address == "ltc1qxyz" and bal.spendable == Decimal("0.1")
    client.activate_coin("RXD", act.electrum, None)
    assert "address_format" not in rpc.calls[1][1]
    assert client.disable_coin("LTC") == ["u-1"]
    assert rpc.calls[2] == ("disable_coin", {"coin": "LTC"})
