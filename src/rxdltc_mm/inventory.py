"""Inventory valuation and skew.

All inventory shares are expressed as the percentage of total portfolio value
(valued in LTC at the fair price) that is held as RXD.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from rxdltc_mm.config import InventoryConfig, InventorySkewConfig
from rxdltc_mm.pricing import HUNDRED, ONE, ZERO


@dataclass(frozen=True)
class Inventory:
    rxd_balance: Decimal
    ltc_balance: Decimal
    fair_rxd_per_ltc: Decimal

    @property
    def rxd_value_ltc(self) -> Decimal:
        return self.rxd_balance / self.fair_rxd_per_ltc

    @property
    def total_value_ltc(self) -> Decimal:
        return self.rxd_value_ltc + self.ltc_balance

    @property
    def rxd_value_pct(self) -> Decimal:
        total = self.total_value_ltc
        if total <= 0:
            return Decimal(50)
        return self.rxd_value_ltc / total * HUNDRED


def compute_skew_pct(rxd_value_pct: Decimal, inv: InventoryConfig, skew: InventorySkewConfig) -> Decimal:
    """Signed skew in canonical (RXD per LTC) percentage terms.

    * RXD share above target -> positive skew (quote more RXD per LTC: sells
      become more attractive to takers, buys less attractive).
    * RXD share below target -> negative skew.

    The skew grows linearly from 0 at the target to ``max_adjustment_pct`` at
    the min/max inventory bounds and is clamped there.
    """
    if not skew.enabled or skew.max_adjustment_pct <= 0:
        return ZERO
    if rxd_value_pct >= inv.target_rxd_value_pct:
        span = inv.max_rxd_value_pct - inv.target_rxd_value_pct
        frac = (rxd_value_pct - inv.target_rxd_value_pct) / span if span > 0 else ONE
        return min(frac, ONE) * skew.max_adjustment_pct
    span = inv.target_rxd_value_pct - inv.min_rxd_value_pct
    frac = (inv.target_rxd_value_pct - rxd_value_pct) / span if span > 0 else ONE
    return -min(frac, ONE) * skew.max_adjustment_pct


def side_permissions(rxd_value_pct: Decimal, inv: InventoryConfig) -> tuple[bool, bool]:
    """``(quote_bid, quote_ask)``: stop buying RXD above ``max``, stop selling
    RXD below ``min``."""
    quote_bid = rxd_value_pct < inv.max_rxd_value_pct
    quote_ask = rxd_value_pct > inv.min_rxd_value_pct
    return quote_bid, quote_ask
