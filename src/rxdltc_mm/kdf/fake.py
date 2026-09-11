"""In-memory KDF simulator used by the unit tests and by ``--simulate``.

It models exactly the parts of KDF the bot touches: balances, own maker
orders (with KDF's rel-per-base price convention), a foreign orderbook and a
list of swaps. Fills can be injected with :meth:`fill_order`.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from decimal import Decimal

from rxdltc_mm.config import ElectrumServer
from rxdltc_mm.kdf.models import Balance, MakerOrder, OpenOrders, Orderbook, OrderbookEntry, SwapInfo
from rxdltc_mm.kdf.rpc import KdfConnectionError, KdfRpcError
from rxdltc_mm.pricing import Side, rxd_per_ltc_to_ltc_per_rxd


@dataclass
class FakeKdf:
    base: str = "RXD"
    quote: str = "LTC"
    balances: dict[str, Decimal] = field(default_factory=lambda: {"RXD": Decimal("300000"), "LTC": Decimal("0.1")})
    enabled: set[str] = field(default_factory=set)
    address_formats: dict[str, str] = field(default_factory=dict)  # coin -> "standard" | "segwit"
    orders: dict[str, MakerOrder] = field(default_factory=dict)
    foreign_asks_ltc_per_rxd: list[tuple[Decimal, Decimal]] = field(default_factory=list)  # (price, rxd volume)
    foreign_bids_ltc_per_rxd: list[tuple[Decimal, Decimal]] = field(default_factory=list)
    # entries flagged is_mine by KDF (same pubkey) but NOT in this instance's my_orders: another
    # wallet instance running the same seed. (price ltc/rxd, rxd volume, side)
    phantom_mine: list[tuple[Decimal, Decimal, Side]] = field(default_factory=list)
    swaps: list[SwapInfo] = field(default_factory=list)
    offline: bool = False
    fail_next_setprice: int = 0
    fail_next_cancel: int = 0
    clock: "callable" = time.time
    calls: list[str] = field(default_factory=list)
    _ids: "itertools.count" = field(default_factory=lambda: itertools.count(1))

    # helpers for tests ---------------------------------------------------
    def _check(self, name: str) -> None:
        self.calls.append(name)
        if self.offline:
            raise KdfConnectionError(f"{name}: connection refused (simulated)")

    def add_order(self, side: Side, price_rxd_per_ltc: Decimal, amount: Decimal, *, created_at: float | None = None,
                  matches: int = 0, cancellable: bool = True) -> MakerOrder:
        uuid = f"fake-{next(self._ids)}"
        if side is Side.BID:
            base, rel, price = self.quote, self.base, price_rxd_per_ltc
        else:
            base, rel, price = self.base, self.quote, rxd_per_ltc_to_ltc_per_rxd(price_rxd_per_ltc)
        order = MakerOrder(
            uuid=uuid, base=base, rel=rel, price_rel_per_base=price, max_base_vol=amount, min_base_vol=amount / 4,
            available_base_amount=amount, created_at_ms=int((created_at if created_at is not None else self.clock()) * 1000),
            updated_at_ms=None, cancellable=cancellable, matches=matches,
        )
        self.orders[uuid] = order
        return order

    def fill_order(self, uuid: str, fraction: Decimal = Decimal(1)) -> SwapInfo:
        """Simulate a taker filling ``fraction`` of an order: balances move and a
        finished swap appears in history."""
        order = self.orders[uuid]
        base_amt = order.available_base_amount * fraction
        rel_amt = base_amt * order.price_rel_per_base
        self.balances[order.base] -= base_amt
        self.balances[order.rel] = self.balances.get(order.rel, Decimal(0)) + rel_amt
        remaining = order.available_base_amount - base_amt
        if remaining <= 0:
            del self.orders[uuid]
        else:
            self.orders[uuid] = MakerOrder(**{**order.__dict__, "available_base_amount": remaining})
        swap = SwapInfo(
            uuid=f"swap-{next(self._ids)}", my_coin=order.base, other_coin=order.rel, my_amount=base_amt,
            other_amount=rel_amt, started_at=int(self.clock()), finished=True, success=True, swap_type="Maker",
            last_event="Finished",
        )
        self.swaps.insert(0, swap)
        return swap

    # protocol ------------------------------------------------------------
    def ping(self) -> str:
        self._check("version")
        return "fake-kdf 0.0"

    def get_enabled_coins(self) -> list[str]:
        self._check("get_enabled_coins")
        return sorted(self.enabled)

    def activate_coin(self, ticker: str, servers: list[ElectrumServer], address_format: str | None = None) -> Balance:
        self._check("electrum")
        if ticker in self.enabled:
            raise KdfRpcError("electrum", f"Coin {ticker} already initialized")
        self.enabled.add(ticker)
        self.address_formats[ticker] = address_format or "standard"
        return self.get_balance(ticker)

    def disable_coin(self, ticker: str) -> list[str]:
        self._check("disable_coin")
        if ticker not in self.enabled:
            raise KdfRpcError("disable_coin", f"No such coin: {ticker}")
        if any(o.is_matching for o in self.orders.values() if ticker in (o.base, o.rel)):
            raise KdfRpcError("disable_coin", "There are currently matching orders, active swaps")
        cancelled = [u for u, o in self.orders.items() if ticker in (o.base, o.rel)]
        for u in cancelled:
            del self.orders[u]
        self.enabled.discard(ticker)
        return cancelled

    def address_of(self, coin: str) -> str:
        # segwit-looking (all lowercase, contains "1") vs legacy-looking (has upper case)
        return f"{coin.lower()}1q{coin.lower()}addr" if self.address_formats.get(coin) == "segwit" else f"L{coin}addr"

    def get_balance(self, coin: str) -> Balance:
        self._check("my_balance")
        if coin not in self.balances:
            raise KdfRpcError("my_balance", f"No such coin: {coin}")
        return Balance(coin=coin, spendable=self.balances[coin], unspendable=Decimal(0), address=self.address_of(coin))

    def get_max_maker_vol(self, coin: str) -> Decimal | None:
        self._check("max_maker_vol")
        return None

    def get_orderbook(self) -> Orderbook:
        self._check("orderbook")
        asks = [OrderbookEntry(p, v, p * v, f"fa-{i}", False, "pk") for i, (p, v) in enumerate(self.foreign_asks_ltc_per_rxd)]
        bids = [OrderbookEntry(p, v, p * v, f"fb-{i}", False, "pk") for i, (p, v) in enumerate(self.foreign_bids_ltc_per_rxd)]
        for o in self.orders.values():
            side = o.side_for(self.base, self.quote)
            if side is Side.ASK:
                asks.append(OrderbookEntry(o.price_rel_per_base, o.available_base_amount,
                                           o.available_base_amount * o.price_rel_per_base, o.uuid, True, "me"))
            elif side is Side.BID:
                p = rxd_per_ltc_to_ltc_per_rxd(o.price_rel_per_base)
                bids.append(OrderbookEntry(p, o.available_base_amount * o.price_rel_per_base, o.available_base_amount, o.uuid, True, "me"))
        for i, (p, v, side) in enumerate(self.phantom_mine):
            entry = OrderbookEntry(p, v, p * v, f"phantom-{i}", True, "me")
            (asks if side is Side.ASK else bids).append(entry)
        asks.sort(key=lambda e: e.price_rel_per_base)
        bids.sort(key=lambda e: -e.price_rel_per_base)
        return Orderbook(self.base, self.quote, tuple(asks), tuple(bids), int(self.clock()))

    def get_open_orders(self) -> OpenOrders:
        self._check("my_orders")
        return OpenOrders(maker_orders=list(self.orders.values()), taker_order_count=0)

    def place_bid(self, price_rxd_per_ltc: Decimal, ltc_amount: Decimal) -> MakerOrder:
        self._check("setprice")
        if self.fail_next_setprice > 0:
            self.fail_next_setprice -= 1
            raise KdfRpcError("setprice", "simulated failure")
        return self.add_order(Side.BID, price_rxd_per_ltc, ltc_amount)

    def place_ask(self, price_rxd_per_ltc: Decimal, rxd_amount: Decimal) -> MakerOrder:
        self._check("setprice")
        if self.fail_next_setprice > 0:
            self.fail_next_setprice -= 1
            raise KdfRpcError("setprice", "simulated failure")
        return self.add_order(Side.ASK, price_rxd_per_ltc, rxd_amount)

    def cancel_order(self, uuid: str) -> bool:
        self._check("cancel_order")
        if self.fail_next_cancel > 0:
            self.fail_next_cancel -= 1
            raise KdfRpcError("cancel_order", "simulated failure")
        order = self.orders.get(uuid)
        if order is None:
            raise KdfRpcError("cancel_order", f"Order with uuid {uuid} is not found")
        if not order.cancellable:
            raise KdfRpcError("cancel_order", "Order is being matched now, can't cancel")
        del self.orders[uuid]
        return True

    def cancel_all(self) -> list[str]:
        self._check("cancel_all_orders")
        cancelled = [u for u, o in self.orders.items() if o.cancellable]
        for u in cancelled:
            del self.orders[u]
        return cancelled

    def get_swap_status(self, uuid: str) -> SwapInfo | None:
        self._check("my_swap_status")
        return next((s for s in self.swaps if s.uuid == uuid), None)

    def get_recent_swaps(self, limit: int = 50) -> list[SwapInfo]:
        self._check("my_recent_swaps")
        return list(self.swaps[:limit])

    def get_active_swaps(self) -> list[str]:
        self._check("active_swaps")
        return []
