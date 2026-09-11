from decimal import Decimal

from rxdltc_mm.config import InventoryConfig, InventorySkewConfig, OrderSizingConfig, ReserveConfig
from rxdltc_mm.inventory import Inventory, compute_skew_pct, side_permissions
from rxdltc_mm.sizing import compute_order_sizes

FAIR = Decimal("2800000")
INV = InventoryConfig()
SKEW = InventorySkewConfig(enabled=True, max_adjustment_pct=Decimal("1.0"))


def test_inventory_valuation():
    inv = Inventory(rxd_balance=Decimal("280000"), ltc_balance=Decimal("0.1"), fair_rxd_per_ltc=FAIR)
    assert inv.rxd_value_ltc == Decimal("0.1")
    assert inv.rxd_value_pct == Decimal(50)
    heavy = Inventory(Decimal("840000"), Decimal("0.1"), FAIR)  # 0.3 LTC of RXD vs 0.1 LTC
    assert heavy.rxd_value_pct == Decimal(75)


def test_skew_is_zero_at_target_and_saturates_at_bounds():
    assert compute_skew_pct(Decimal(50), INV, SKEW) == 0
    assert compute_skew_pct(Decimal("62.5"), INV, SKEW) == Decimal("0.5")
    assert compute_skew_pct(Decimal(75), INV, SKEW) == Decimal("1.0")
    assert compute_skew_pct(Decimal(90), INV, SKEW) == Decimal("1.0")
    assert compute_skew_pct(Decimal("37.5"), INV, SKEW) == Decimal("-0.5")
    assert compute_skew_pct(Decimal(10), INV, SKEW) == Decimal("-1.0")


def test_skew_disabled():
    assert compute_skew_pct(Decimal(90), INV, InventorySkewConfig(enabled=False)) == 0


def test_side_permissions_at_bounds():
    assert side_permissions(Decimal(50), INV) == (True, True)
    assert side_permissions(Decimal(75), INV) == (False, True)  # too much RXD: stop buying
    assert side_permissions(Decimal(25), INV) == (True, False)  # too little RXD: stop selling
    assert side_permissions(Decimal(80), INV) == (False, True)


def test_balance_percent_sizing_respects_reserve():
    sizes = compute_order_sizes(OrderSizingConfig(), ReserveConfig(), rxd_spendable=Decimal("300000"), ltc_spendable=Decimal("0.1"))
    # 300000 * 0.9 * 0.5 = 135000 ; 0.1 * 0.9 * 0.5 = 0.045
    assert sizes.ask_rxd_amount == Decimal("135000")
    assert sizes.bid_ltc_amount == Decimal("0.045")


def test_never_uses_full_balance_even_at_100_percent():
    sizing = OrderSizingConfig(rxd_balance_percent=Decimal(100), ltc_balance_percent=Decimal(100))
    reserve = ReserveConfig(rxd_percent=Decimal(10), ltc_percent=Decimal(5))
    sizes = compute_order_sizes(sizing, reserve, rxd_spendable=Decimal("1000"), ltc_spendable=Decimal("1"))
    assert sizes.ask_rxd_amount == Decimal("900")
    assert sizes.bid_ltc_amount == Decimal("0.95")


def test_fixed_sizing_capped_by_available():
    sizing = OrderSizingConfig(mode="fixed", rxd_order_amount=Decimal("500000"), ltc_order_amount=Decimal("0.02"))
    sizes = compute_order_sizes(sizing, ReserveConfig(), rxd_spendable=Decimal("300000"), ltc_spendable=Decimal("1"))
    assert sizes.ask_rxd_amount == Decimal("270000")  # 300000 * 0.9
    assert sizes.bid_ltc_amount == Decimal("0.02")


def test_minimums_and_max_maker_vol():
    sizing = OrderSizingConfig(min_rxd_order_amount=Decimal("1000"), min_ltc_order_amount=Decimal("0.01"))
    sizes = compute_order_sizes(sizing, ReserveConfig(), rxd_spendable=Decimal("1000"), ltc_spendable=Decimal("0.01"),
                                rxd_max_maker_vol=Decimal("300"))
    assert sizes.ask_rxd_amount == 0  # capped to 300 by max_maker_vol, then below the 1000 minimum
    assert sizes.bid_ltc_amount == 0  # 0.0045 < 0.01
    assert any("max_maker_vol" in n for n in sizes.notes)


def test_zero_balances_give_zero_sizes():
    sizes = compute_order_sizes(OrderSizingConfig(), ReserveConfig(), rxd_spendable=Decimal(0), ltc_spendable=Decimal(0))
    assert sizes.ask_rxd_amount == 0 and sizes.bid_ltc_amount == 0
