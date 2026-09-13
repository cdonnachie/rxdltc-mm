"""Profit and loss of the bot's trading, measured against simply holding.

The question worth answering is not "is the wallet up", because that is mostly the
market. It is "did trading help or hurt compared with never trading at all". Every
completed swap changed the holdings by a known amount of each coin; reversing those
changes gives the hold-only wallet. So the effect of trading, valued at today's
prices, is just the net change in each coin times its current price:

    vs_hold = net_rxd * rxd_usd_now + net_ltc * ltc_usd_now

Deposits and withdrawals never enter it, because only swaps are counted.

That figure then splits into two parts that mean different things:

* **Execution edge** compares each fill with the fair price when the swap started.
  A sell is worth ``ltc_received - rxd_sold / fair``, a buy ``rxd_bought / fair -
  ltc_spent``, both in LTC. Positive means the bot sold above or bought below fair,
  which is the spread doing its job.
* **Inventory effect** is the rest: what the RXD/LTC ratio did to the traded coins
  after the trade. Selling RXD before a rally shows up here as a loss. It is the
  adverse selection a market maker carries, and on a trending coin it usually
  dominates the edge.

Both are valued at today's LTC price, so an LTC/USD move cancels out of the split and
the inventory effect isolates the RXD/LTC move. Miner fees are excluded; on RXD and
LTC they are fractions of a cent per swap.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Iterable

from rxdltc_mm.pricing import ZERO, Side


@dataclass(frozen=True)
class SwapFill:
    """One completed swap on the pair, from the bot's side."""

    side: Side               # ASK = sold base, BID = bought base
    base_amount: Decimal     # RXD sold (ask) or bought (bid)
    quote_amount: Decimal    # LTC received (ask) or spent (bid)
    started_at: float


@dataclass(frozen=True)
class PnL:
    swaps: int
    net_base: Decimal                 # + RXD bought, - RXD sold
    net_quote: Decimal                # + LTC received, - LTC spent
    vs_hold_usd: Decimal | None       # effect of trading at today's prices
    edge_quote: Decimal | None        # execution edge in LTC, over swaps with a known fair price
    edge_usd: Decimal | None
    inventory_usd: Decimal | None     # vs_hold - edge; None unless every swap had a fair price
    volume_usd: Decimal | None        # LTC leg of every swap, at today's LTC price
    edge_coverage: int                # swaps for which a fair price was found


def fill_from_swap(side: Side, my_amount: Decimal, other_amount: Decimal, started_at: float) -> SwapFill:
    """Map a stored swap to base/quote amounts. On an ask the bot gave base and got quote;
    on a bid it gave quote and got base."""
    if side is Side.ASK:
        return SwapFill(side, base_amount=my_amount, quote_amount=other_amount, started_at=started_at)
    return SwapFill(side, base_amount=other_amount, quote_amount=my_amount, started_at=started_at)


def compute_pnl(
    fills: Iterable[SwapFill],
    fair_at: Callable[[float], Decimal | None],
    base_usd: Decimal | None,
    quote_usd: Decimal | None,
) -> PnL:
    """``fair_at(ts)`` returns the fair price (base per quote) near ``ts`` or None."""
    fills = list(fills)
    net_base = net_quote = edge = volume_quote = ZERO
    covered = 0
    for f in fills:
        if f.side is Side.ASK:
            net_base -= f.base_amount
            net_quote += f.quote_amount
        else:
            net_base += f.base_amount
            net_quote -= f.quote_amount
        volume_quote += f.quote_amount
        fair = fair_at(f.started_at)
        if fair is not None and fair > 0:
            fair_value_quote = f.base_amount / fair  # what the traded base was worth in quote
            edge += (f.quote_amount - fair_value_quote) if f.side is Side.ASK else (fair_value_quote - f.quote_amount)
            covered += 1

    have_prices = base_usd is not None and quote_usd is not None and base_usd > 0 and quote_usd > 0
    vs_hold = (net_base * base_usd + net_quote * quote_usd) if have_prices else None
    edge_quote = edge if covered else None
    edge_usd = (edge * quote_usd) if covered and quote_usd else None
    complete = covered == len(fills) and fills
    inventory = (vs_hold - edge_usd) if complete and vs_hold is not None and edge_usd is not None else None
    if not fills:
        inventory = ZERO if have_prices else None
    return PnL(
        swaps=len(fills),
        net_base=net_base,
        net_quote=net_quote,
        vs_hold_usd=vs_hold,
        edge_quote=edge_quote,
        edge_usd=edge_usd,
        inventory_usd=inventory,
        volume_usd=(volume_quote * quote_usd) if quote_usd else None,
        edge_coverage=covered,
    )
