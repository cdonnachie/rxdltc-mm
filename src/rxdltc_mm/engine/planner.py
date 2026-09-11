"""Pure planning: given what we want and what exists, decide what to do.

No I/O happens here, which is what makes repricing thresholds, minimum order
lifetime, duplicate prevention, crossing checks and restart reconciliation
unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from rxdltc_mm.config import BotConfig
from rxdltc_mm.kdf.models import MakerOrder, Orderbook
from rxdltc_mm.pricing import HUNDRED, ONE, Side, deviation_pct, ltc_per_rxd_to_rxd_per_ltc, pct, quote_is_safe

ActionKind = Literal["place", "cancel", "keep", "skip"]


@dataclass(frozen=True)
class OrderIntent:
    side: Side
    price_rxd_per_ltc: Decimal
    amount: Decimal  # LTC for bids, RXD for asks


@dataclass(frozen=True)
class Action:
    kind: ActionKind
    side: Side
    reason: str
    uuid: str | None = None
    price_rxd_per_ltc: Decimal | None = None
    amount: Decimal | None = None


@dataclass
class Plan:
    actions: list[Action] = field(default_factory=list)
    ambiguous: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def mutations(self) -> list[Action]:
        return [a for a in self.actions if a.kind in ("place", "cancel")]


def group_by_side(orders: list[MakerOrder], base: str, quote: str) -> dict[Side, list[MakerOrder]]:
    grouped: dict[Side, list[MakerOrder]] = {Side.BID: [], Side.ASK: []}
    for o in orders:
        side = o.side_for(base, quote)
        if side is not None:
            grouped[side].append(o)
    return grouped


def check_crossing(
    intent: OrderIntent, orderbook: Orderbook | None, cfg: BotConfig, fair_rxd_per_ltc: Decimal
) -> tuple[OrderIntent | None, str | None]:
    """Return ``(intent, note)``. ``intent`` is None when the order must be skipped.

    In LTC-per-RXD terms a bid crosses when ``bid >= best foreign ask`` and an
    ask crosses when ``ask <= best foreign bid``. Converted to the canonical
    RXD-per-LTC unit the comparisons flip: a bid crosses when
    ``bid_c <= best_ask_c`` and an ask crosses when ``ask_c >= best_bid_c``.
    """
    if orderbook is None or not cfg.trading.prevent_crossing_book:
        return intent, None
    tick = pct(cfg.pricing.min_edge_pct)
    if intent.side is Side.BID:
        best = orderbook.best_foreign_ask_ltc_per_rxd()
        if best is None:
            return intent, None
        best_c = ltc_per_rxd_to_rxd_per_ltc(best)
        if intent.price_rxd_per_ltc > best_c:
            return intent, None
        if cfg.trading.on_cross == "skip":
            return None, f"bid {intent.price_rxd_per_ltc:.0f} would cross best foreign ask {best_c:.0f} RXD/LTC; skipped"
        adjusted = best_c * (ONE + tick)
    else:
        best = orderbook.best_foreign_bid_ltc_per_rxd()
        if best is None:
            return intent, None
        best_c = ltc_per_rxd_to_rxd_per_ltc(best)
        if intent.price_rxd_per_ltc < best_c:
            return intent, None
        if cfg.trading.on_cross == "skip":
            return None, f"ask {intent.price_rxd_per_ltc:.0f} would cross best foreign bid {best_c:.0f} RXD/LTC; skipped"
        adjusted = best_c * (ONE - tick)
    if not quote_is_safe(adjusted, intent.side, fair_rxd_per_ltc, cfg.safety.max_quote_deviation_pct):
        return None, f"{intent.side.value} would cross the book and the adjusted price {adjusted:.0f} is outside the safety envelope; skipped"
    return (OrderIntent(intent.side, adjusted, intent.amount),
            f"{intent.side.value} adjusted from {intent.price_rxd_per_ltc:.0f} to {adjusted:.0f} RXD/LTC to avoid crossing")


def plan_side(
    side: Side,
    desired: OrderIntent | None,
    existing: list[MakerOrder],
    *,
    cfg: BotConfig,
    fair_rxd_per_ltc: Decimal,
    now: float,
) -> tuple[list[Action], str | None]:
    """Decide for one side. Returns ``(actions, ambiguity_reason)``."""
    base, quote = cfg.pair.base, cfg.pair.quote
    actions: list[Action] = []

    if len(existing) > 1:
        if cfg.reconcile.cancel_unknown_orders:
            for o in existing:
                actions.append(Action("cancel", side, "multiple orders on one side; cleaning up", uuid=o.uuid))
            return actions, None
        return actions, f"{len(existing)} {side.value} orders exist on {base}/{quote}; expected at most one"

    current = existing[0] if existing else None

    if current is not None:
        cur_price = current.price_rxd_per_ltc(base, quote)
        age = now - current.created_at_seconds()
        unsafe = not quote_is_safe(cur_price, side, fair_rxd_per_ltc, cfg.safety.max_quote_deviation_pct)

        if current.is_matching:
            actions.append(Action("keep", side, "order is matching / has active swaps; cannot cancel", uuid=current.uuid))
            return actions, None

        if desired is None:
            actions.append(Action("cancel", side, "side no longer wanted", uuid=current.uuid))
            return actions, None

        price_dev = deviation_pct(cur_price, desired.price_rxd_per_ltc)
        size_dev = (
            abs(current.available_base_amount - desired.amount) / desired.amount * HUNDRED if desired.amount > 0 else Decimal(0)
        )
        needs_price = price_dev > cfg.reprice_threshold_pct
        needs_size = size_dev > cfg.resize_threshold_pct

        if unsafe:
            actions.append(Action("cancel", side, f"existing quote {cur_price:.0f} is outside the safety envelope", uuid=current.uuid))
        elif not (needs_price or needs_size):
            actions.append(Action("keep", side, f"within thresholds (price dev {price_dev:.3f}%, size dev {size_dev:.1f}%)", uuid=current.uuid))
            return actions, None
        elif age < cfg.minimum_order_lifetime_seconds:
            actions.append(Action("keep", side, f"reprice deferred: order age {age:.0f}s < minimum lifetime "
                                                f"{cfg.minimum_order_lifetime_seconds:.0f}s (price dev {price_dev:.3f}%)", uuid=current.uuid))
            return actions, None
        else:
            why = f"price dev {price_dev:.3f}% > {cfg.reprice_threshold_pct}%" if needs_price else f"size dev {size_dev:.1f}% > {cfg.resize_threshold_pct}%"
            actions.append(Action("cancel", side, f"replace: {why}", uuid=current.uuid))

    if desired is not None:
        actions.append(Action("place", side, "new quote" if current is None else "replacement quote",
                              price_rxd_per_ltc=desired.price_rxd_per_ltc, amount=desired.amount))
    return actions, None


def build_plan(
    *,
    cfg: BotConfig,
    fair_rxd_per_ltc: Decimal,
    bid: OrderIntent | None,
    ask: OrderIntent | None,
    existing: list[MakerOrder],
    orderbook: Orderbook | None,
    now: float,
) -> Plan:
    plan = Plan()
    grouped = group_by_side(existing, cfg.pair.base, cfg.pair.quote)
    for side, desired in ((Side.BID, bid), (Side.ASK, ask)):
        if desired is not None:
            desired, note = check_crossing(desired, orderbook, cfg, fair_rxd_per_ltc)
            if note:
                plan.notes.append(note)
        actions, ambiguity = plan_side(side, desired, grouped[side], cfg=cfg, fair_rxd_per_ltc=fair_rxd_per_ltc, now=now)
        plan.actions.extend(actions)
        if ambiguity:
            plan.ambiguous = ambiguity
    return plan
