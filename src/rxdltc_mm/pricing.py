"""Canonical price representation and quote mathematics.

CANONICAL UNIT: **RXD per LTC** (``Decimal``). Example: ``Decimal("2800000")``
means 1 LTC = 2,800,000 RXD.

Consequences of that unit (this is the single most important thing to keep
straight in this code base):

* A LARGER canonical number means RXD is CHEAPER (one LTC buys more RXD).
* The RXD **bid** is the bot BUYING RXD and paying LTC. To buy RXD below fair
  value the bot must receive MORE RXD per LTC than fair, so the canonical bid
  is ABOVE fair:  ``bid = fair * (1 + bid_offset)``.
* The RXD **ask** is the bot SELLING RXD and receiving LTC. To sell RXD above
  fair value the bot must give FEWER RXD per LTC than fair, so the canonical
  ask is BELOW fair: ``ask = fair * (1 - ask_offset)``.
* Expressed in LTC per RXD (the price of one RXD, which is what KDF uses for a
  ``setprice`` order with ``base=RXD, rel=LTC``) the picture is the familiar
  one: ``bid_ltc_per_rxd < fair_ltc_per_rxd < ask_ltc_per_rxd``.

Example with fair = 2,800,000 and 1% offsets:

===========  ==================  =====================
side         RXD per LTC         LTC per RXD
===========  ==================  =====================
RXD bid      2,828,000           3.5361e-7 (below fair)
fair         2,800,000           3.5714e-7
RXD ask      2,772,000           3.6075e-7 (above fair)
===========  ==================  =====================

The KDF mapping lives in :mod:`rxdltc_mm.kdf.client`:

* RXD bid  -> ``setprice base=LTC rel=RXD price=<RXD per LTC>`` (sell LTC for RXD)
* RXD ask  -> ``setprice base=RXD rel=LTC price=<LTC per RXD>`` (sell RXD for LTC)
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal, getcontext
from enum import Enum

getcontext().prec = 40

ZERO = Decimal(0)
ONE = Decimal(1)
HUNDRED = Decimal(100)


class Side(str, Enum):
    """Order side from the bot's point of view, always relative to RXD."""

    BID = "bid"  # bot BUYS RXD, pays LTC   (KDF: base=LTC, rel=RXD)
    ASK = "ask"  # bot SELLS RXD, receives LTC (KDF: base=RXD, rel=LTC)


def pct(value: Decimal | float | int | str) -> Decimal:
    """Convert a percentage (``1.0`` == 1%) into a fraction (``0.01``)."""
    return Decimal(str(value)) / HUNDRED


def rxd_per_ltc_to_ltc_per_rxd(price_rxd_per_ltc: Decimal) -> Decimal:
    if price_rxd_per_ltc <= 0:
        raise ValueError("price must be positive")
    return ONE / price_rxd_per_ltc


def ltc_per_rxd_to_rxd_per_ltc(price_ltc_per_rxd: Decimal) -> Decimal:
    if price_ltc_per_rxd <= 0:
        raise ValueError("price must be positive")
    return ONE / price_ltc_per_rxd


def deviation_pct(price: Decimal, reference: Decimal) -> Decimal:
    """Unsigned percentage distance of ``price`` from ``reference``."""
    if reference <= 0:
        raise ValueError("reference must be positive")
    return abs(price - reference) / reference * HUNDRED


def signed_deviation_pct(price: Decimal, reference: Decimal) -> Decimal:
    if reference <= 0:
        raise ValueError("reference must be positive")
    return (price - reference) / reference * HUNDRED


def decimal_to_str(value: Decimal, max_decimals: int = 18) -> str:
    """Plain (non-scientific) decimal string suitable for KDF ``MmNumber`` fields."""
    q = value.quantize(Decimal(1).scaleb(-max_decimals), rounding=ROUND_HALF_EVEN)
    s = format(q, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def quantize_amount(value: Decimal, decimals: int = 8) -> Decimal:
    """Round an on-chain amount DOWN to the coin's precision (8 decimals for UTXO coins)."""
    return value.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_DOWN)


def format_rxd_per_ltc(value: Decimal | None) -> str:
    """Compact human display, e.g. ``2.80M``."""
    if value is None:
        return "n/a"
    v = float(value)
    if v >= 1_000_000:
        return f"{v / 1_000_000:.3f}M"
    if v >= 1_000:
        return f"{v / 1_000:.1f}K"
    return f"{v:.4g}"


@dataclass(frozen=True)
class TargetQuotes:
    """Desired quotes in the canonical unit (RXD per LTC)."""

    fair_rxd_per_ltc: Decimal
    mid_rxd_per_ltc: Decimal  # fair after inventory skew
    bid_rxd_per_ltc: Decimal | None  # None -> do not quote this side
    ask_rxd_per_ltc: Decimal | None
    skew_pct: Decimal  # signed, canonical space (+ means RXD-heavy -> quote more RXD per LTC)
    clamped: bool  # True if min_edge / max_deviation limits had to be applied

    @property
    def bid_ltc_per_rxd(self) -> Decimal | None:
        return None if self.bid_rxd_per_ltc is None else rxd_per_ltc_to_ltc_per_rxd(self.bid_rxd_per_ltc)

    @property
    def ask_ltc_per_rxd(self) -> Decimal | None:
        return None if self.ask_rxd_per_ltc is None else rxd_per_ltc_to_ltc_per_rxd(self.ask_rxd_per_ltc)


def compute_target_quotes(
    fair_rxd_per_ltc: Decimal,
    *,
    bid_offset_pct: Decimal,
    ask_offset_pct: Decimal,
    skew_pct: Decimal = ZERO,
    min_edge_pct: Decimal = ZERO,
    max_deviation_pct: Decimal = Decimal("3.0"),
    quote_bid: bool = True,
    quote_ask: bool = True,
) -> TargetQuotes:
    """Compute target bid/ask in RXD per LTC.

    ``skew_pct`` shifts the midpoint in canonical space: positive when the bot
    holds too much RXD (it then quotes MORE RXD per LTC on both sides, i.e.
    sells RXD cheaper and buys RXD only if it gets more of it), negative when
    it holds too much LTC.

    Safety envelope (applied after skew, in this order):

    1. ``min_edge_pct``: the bid never goes below ``fair*(1+min_edge)`` and the
       ask never above ``fair*(1-min_edge)`` -- quotes never cross fair value.
    2. ``max_deviation_pct``: the bid never goes above ``fair*(1+max_dev)`` and
       the ask never below ``fair*(1-max_dev)``.
    """
    if fair_rxd_per_ltc <= 0:
        raise ValueError("fair price must be positive")
    mid = fair_rxd_per_ltc * (ONE + pct(skew_pct))
    clamped = False

    bid: Decimal | None = None
    ask: Decimal | None = None
    if quote_bid:
        bid = mid * (ONE + pct(bid_offset_pct))
        lo = fair_rxd_per_ltc * (ONE + pct(min_edge_pct))
        hi = fair_rxd_per_ltc * (ONE + pct(max_deviation_pct))
        if bid < lo:
            bid, clamped = lo, True
        if bid > hi:
            bid, clamped = hi, True
    if quote_ask:
        ask = mid * (ONE - pct(ask_offset_pct))
        hi = fair_rxd_per_ltc * (ONE - pct(min_edge_pct))
        lo = fair_rxd_per_ltc * (ONE - pct(max_deviation_pct))
        if ask > hi:
            ask, clamped = hi, True
        if ask < lo:
            ask, clamped = lo, True
    return TargetQuotes(
        fair_rxd_per_ltc=fair_rxd_per_ltc,
        mid_rxd_per_ltc=mid,
        bid_rxd_per_ltc=bid,
        ask_rxd_per_ltc=ask,
        skew_pct=Decimal(str(skew_pct)),
        clamped=clamped,
    )


def quote_is_safe(price_rxd_per_ltc: Decimal, side: Side, fair_rxd_per_ltc: Decimal, max_deviation_pct: Decimal) -> bool:
    """A quote is safe when it is on the correct side of fair value and within
    ``max_deviation_pct`` of it. Used as the final guard before any order is
    sent (safety control #8)."""
    if deviation_pct(price_rxd_per_ltc, fair_rxd_per_ltc) > max_deviation_pct:
        return False
    if side is Side.BID:
        return price_rxd_per_ltc >= fair_rxd_per_ltc  # buying RXD: must get at least fair amount of RXD per LTC
    return price_rxd_per_ltc <= fair_rxd_per_ltc  # selling RXD: must give at most fair amount of RXD per LTC
