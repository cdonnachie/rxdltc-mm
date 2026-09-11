"""Typed views over KDF RPC responses.

KDF's native price convention is always **rel per base** ("how much rel do I
receive for one base"). The models keep KDF-native numbers and expose helpers
that convert to the bot's canonical RXD-per-LTC unit given the pair.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from rxdltc_mm.pricing import ZERO, Side, ltc_per_rxd_to_rxd_per_ltc

# v2 (swap_v2) event types that mean the swap did not complete normally.
V2_FAILURE_EVENTS = frozenset({
    "Aborted", "MakerPaymentRefunded", "MakerPaymentRefundRequired", "TakerPaymentRefunded",
    "TakerFundingRefunded", "TakerPaymentRefundRequired", "TakerFundingRefundRequired",
})


def _dec(value: Any, default: Decimal | None = None) -> Decimal:
    if value is None:
        if default is None:
            raise ValueError("missing numeric value")
        return default
    if isinstance(value, dict):  # MmNumberMultiRepr: {"decimal": "...", "rational": ..., "fraction": ...}
        value = value.get("decimal")
    return Decimal(str(value))


@dataclass(frozen=True)
class Balance:
    coin: str
    spendable: Decimal
    unspendable: Decimal
    address: str

    @classmethod
    def from_rpc(cls, data: dict[str, Any]) -> "Balance":
        return cls(
            coin=str(data["coin"]),
            spendable=_dec(data["balance"]),
            unspendable=_dec(data.get("unspendable_balance", "0")),
            address=str(data.get("address", "")),
        )


@dataclass(frozen=True)
class MakerOrder:
    """One of our own maker orders as returned by ``my_orders`` / ``setprice``."""

    uuid: str
    base: str
    rel: str
    price_rel_per_base: Decimal
    max_base_vol: Decimal
    min_base_vol: Decimal
    available_base_amount: Decimal
    created_at_ms: int
    updated_at_ms: int | None
    cancellable: bool
    matches: int
    started_swaps: tuple[str, ...] = ()

    @classmethod
    def from_rpc(cls, data: dict[str, Any]) -> "MakerOrder":
        return cls(
            uuid=str(data["uuid"]),
            base=str(data["base"]),
            rel=str(data["rel"]),
            price_rel_per_base=_dec(data["price"]),
            max_base_vol=_dec(data["max_base_vol"]),
            min_base_vol=_dec(data.get("min_base_vol", "0")),
            available_base_amount=_dec(data.get("available_amount", data["max_base_vol"])),
            created_at_ms=int(data.get("created_at", 0)),
            updated_at_ms=(int(data["updated_at"]) if data.get("updated_at") is not None else None),
            cancellable=bool(data.get("cancellable", True)),
            matches=len(data.get("matches") or {}),
            started_swaps=tuple(str(u) for u in (data.get("started_swaps") or [])),
        )

    def side_for(self, base: str, quote: str) -> Side | None:
        """Which RXD side this order is on for the configured pair (None = other pair)."""
        if self.base == base and self.rel == quote:
            return Side.ASK  # selling RXD for LTC
        if self.base == quote and self.rel == base:
            return Side.BID  # selling LTC for RXD == buying RXD
        return None

    def price_rxd_per_ltc(self, base: str, quote: str) -> Decimal:
        side = self.side_for(base, quote)
        if side is Side.BID:
            return self.price_rel_per_base  # base=LTC rel=RXD -> already RXD per LTC
        if side is Side.ASK:
            return ltc_per_rxd_to_rxd_per_ltc(self.price_rel_per_base)  # base=RXD rel=LTC -> LTC per RXD
        raise ValueError(f"order {self.uuid} is not on the {base}/{quote} pair")

    @property
    def is_matching(self) -> bool:
        return self.matches > 0 or not self.cancellable

    def created_at_seconds(self) -> float:
        return self.created_at_ms / 1000.0


@dataclass(frozen=True)
class OrderbookEntry:
    price_rel_per_base: Decimal  # for orderbook(base=RXD, rel=LTC): LTC per RXD
    base_max_volume: Decimal
    rel_max_volume: Decimal
    uuid: str
    is_mine: bool
    pubkey: str

    @classmethod
    def from_rpc(cls, data: dict[str, Any]) -> "OrderbookEntry":
        return cls(
            price_rel_per_base=_dec(data["price"]),
            base_max_volume=_dec(data.get("base_max_volume", "0")),
            rel_max_volume=_dec(data.get("rel_max_volume", "0")),
            uuid=str(data.get("uuid", "")),
            is_mine=bool(data.get("is_mine", False)),
            pubkey=str(data.get("pubkey", "")),
        )

    @property
    def price_rxd_per_ltc(self) -> Decimal:
        return ltc_per_rxd_to_rxd_per_ltc(self.price_rel_per_base)


@dataclass(frozen=True)
class Orderbook:
    """``orderbook`` for base=RXD, rel=LTC.

    * ``asks``: others SELLING RXD for LTC (they compete with our ask)
    * ``bids``: others BUYING RXD with LTC (they compete with our bid)
    Prices are KDF-native LTC per RXD; use the helpers for canonical values.
    """

    base: str
    rel: str
    asks: tuple[OrderbookEntry, ...]
    bids: tuple[OrderbookEntry, ...]
    timestamp: int

    @classmethod
    def from_rpc(cls, data: dict[str, Any]) -> "Orderbook":
        return cls(
            base=str(data["base"]),
            rel=str(data["rel"]),
            asks=tuple(OrderbookEntry.from_rpc(e) for e in data.get("asks", [])),
            bids=tuple(OrderbookEntry.from_rpc(e) for e in data.get("bids", [])),
            timestamp=int(data.get("timestamp", 0)),
        )

    def best_foreign_ask_ltc_per_rxd(self) -> Decimal | None:
        """Lowest LTC-per-RXD price at which someone else sells RXD."""
        prices = [e.price_rel_per_base for e in self.asks if not e.is_mine and e.price_rel_per_base > 0]
        return min(prices) if prices else None

    def best_foreign_bid_ltc_per_rxd(self) -> Decimal | None:
        """Highest LTC-per-RXD price at which someone else buys RXD."""
        prices = [e.price_rel_per_base for e in self.bids if not e.is_mine and e.price_rel_per_base > 0]
        return max(prices) if prices else None


@dataclass(frozen=True)
class SwapInfo:
    uuid: str
    my_coin: str
    other_coin: str
    my_amount: Decimal
    other_amount: Decimal
    started_at: int
    finished: bool
    success: bool
    swap_type: str  # "Maker" | "Taker" | ""
    last_event: str = ""

    @classmethod
    def from_rpc(cls, data: dict[str, Any]) -> "SwapInfo | None":
        """Parse a swap from ``my_recent_swaps`` / ``my_swap_status``.

        Handles the three shapes KDF produces (verified in
        ``mm2src/mm2_main/src/lp_swap/swap_v2_rpcs.rs``):

        * mmrpc 2.0 envelope ``{"swap_type": "MakerV1"|"TakerV1"|"MakerV2"|"TakerV2", "swap_data": {...}}``
        * v1 data (``MakerSavedSwap``/``TakerSavedSwap`` or legacy ``MySwapStatusResponse``):
          ``events[].event.type``, ``error_events``, optional ``my_info``
        * v2 data (``MySwapForRpc``): ``my_coin``, ``other_coin``, ``is_finished``,
          ``events[].event_type``, ``maker_volume``, ``taker_volume``

        Returns ``None`` when the swap has no usable info.
        """
        role = ""  # "Maker" | "Taker"
        if isinstance(data.get("swap_data"), dict) and "swap_type" in data:
            tag = str(data["swap_type"])
            role = "Maker" if tag.startswith("Maker") else "Taker" if tag.startswith("Taker") else ""
            data = data["swap_data"]
        if not role:
            t = str(data.get("type", "")).capitalize()
            role = t if t in ("Maker", "Taker") else ""
        uuid = str(data.get("uuid", ""))
        events = data.get("events") or []

        if "is_finished" in data and "my_coin" in data:
            # ---- v2 (MySwapForRpc)
            my_coin, other_coin = str(data["my_coin"]), str(data["other_coin"])
            maker_vol, taker_vol = _dec(data.get("maker_volume"), ZERO), _dec(data.get("taker_volume"), ZERO)
            my_amount, other_amount = (maker_vol, taker_vol) if role == "Maker" else (taker_vol, maker_vol)
            types = [str(ev.get("event_type", "")) for ev in events if isinstance(ev, dict)]
            last_event = types[-1] if types else ""
            finished = bool(data["is_finished"])
            failed = any(t in V2_FAILURE_EVENTS for t in types)
            success = finished and "Completed" in types and not failed
            return cls(uuid, my_coin, other_coin, my_amount, other_amount, int(data.get("started_at", 0)),
                       finished, success, role, last_event)

        # ---- v1
        my_info = data.get("my_info")
        if my_info:
            my_coin, other_coin = str(my_info["my_coin"]), str(my_info["other_coin"])
            my_amount, other_amount = _dec(my_info["my_amount"]), _dec(my_info["other_amount"])
            started_at = int(my_info.get("started_at", 0))
        elif role and data.get("maker_coin") and data.get("taker_coin"):
            maker_amt, taker_amt = _dec(data.get("maker_amount"), ZERO), _dec(data.get("taker_amount"), ZERO)
            if role == "Maker":
                my_coin, other_coin, my_amount, other_amount = str(data["maker_coin"]), str(data["taker_coin"]), maker_amt, taker_amt
            else:
                my_coin, other_coin, my_amount, other_amount = str(data["taker_coin"]), str(data["maker_coin"]), taker_amt, maker_amt
            first_ts = events[0].get("timestamp", 0) if events and isinstance(events[0], dict) else 0
            started_at = int(first_ts) // 1000 if first_ts > 1e12 else int(first_ts)
        else:
            return None
        error_events = set(data.get("error_events") or [])
        last_event, failed = "", False
        for ev in events:
            ev_type = str(ev.get("event", {}).get("type", "")) if isinstance(ev, dict) else ""
            if ev_type:
                last_event = ev_type
                failed = failed or ev_type in error_events
        finished = last_event == "Finished"
        return cls(uuid, my_coin, other_coin, my_amount, other_amount, started_at, finished, finished and not failed,
                   role, last_event)

    def side_for(self, base: str, quote: str) -> Side | None:
        if self.my_coin == base and self.other_coin == quote:
            return Side.ASK  # we gave RXD, received LTC
        if self.my_coin == quote and self.other_coin == base:
            return Side.BID  # we gave LTC, received RXD
        return None


@dataclass
class OpenOrders:
    maker_orders: list[MakerOrder] = field(default_factory=list)
    taker_order_count: int = 0
