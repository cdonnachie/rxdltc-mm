"""Order sizing with reserves.

* The RXD **ask** is sized in RXD (the coin the bot gives away).
* The RXD **bid** is sized in LTC (the coin the bot gives away).

Neither side ever uses more than ``(100 - reserve_pct)%`` of the spendable
balance, and ``max_maker_vol`` (KDF's own view of what can be committed after
fees and amounts locked by in-flight swaps) caps the result when available.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from rxdltc_mm.config import OrderSizingConfig, ReserveConfig
from rxdltc_mm.pricing import HUNDRED, ONE, ZERO, quantize_amount


@dataclass(frozen=True)
class OrderSizes:
    bid_ltc_amount: Decimal  # 0 -> do not place a bid
    ask_rxd_amount: Decimal  # 0 -> do not place an ask
    notes: list[str] = field(default_factory=list)


def available_after_reserve(spendable: Decimal, reserve_pct: Decimal) -> Decimal:
    if spendable <= 0:
        return ZERO
    return spendable * (ONE - reserve_pct / HUNDRED)


def compute_order_sizes(
    sizing: OrderSizingConfig,
    reserve: ReserveConfig,
    *,
    rxd_spendable: Decimal,
    ltc_spendable: Decimal,
    rxd_max_maker_vol: Decimal | None = None,
    ltc_max_maker_vol: Decimal | None = None,
) -> OrderSizes:
    notes: list[str] = []
    rxd_avail = available_after_reserve(rxd_spendable, reserve.rxd_percent)
    ltc_avail = available_after_reserve(ltc_spendable, reserve.ltc_percent)

    if sizing.mode == "fixed":
        ask = min(sizing.rxd_order_amount, rxd_avail)
        bid = min(sizing.ltc_order_amount, ltc_avail)
    else:
        ask = rxd_avail * sizing.rxd_balance_percent / HUNDRED
        bid = ltc_avail * sizing.ltc_balance_percent / HUNDRED

    if rxd_max_maker_vol is not None and ask > rxd_max_maker_vol:
        notes.append(f"ask capped by max_maker_vol {rxd_max_maker_vol}")
        ask = rxd_max_maker_vol
    if ltc_max_maker_vol is not None and bid > ltc_max_maker_vol:
        notes.append(f"bid capped by max_maker_vol {ltc_max_maker_vol}")
        bid = ltc_max_maker_vol

    ask = quantize_amount(max(ask, ZERO))
    bid = quantize_amount(max(bid, ZERO))

    if ask < sizing.min_rxd_order_amount or ask <= 0:
        if ask > 0:
            notes.append(f"ask size {ask} RXD below minimum {sizing.min_rxd_order_amount}")
        ask = ZERO
    if bid < sizing.min_ltc_order_amount or bid <= 0:
        if bid > 0:
            notes.append(f"bid size {bid} LTC below minimum {sizing.min_ltc_order_amount}")
        bid = ZERO
    return OrderSizes(bid_ltc_amount=bid, ask_rxd_amount=ask, notes=notes)
