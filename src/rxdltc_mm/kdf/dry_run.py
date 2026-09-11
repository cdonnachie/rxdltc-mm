"""Dry-run wrapper: read-only pass-through, mutations are logged and faked.

Guarantees in dry-run mode:

* ``setprice`` is never called (``place_bid``/``place_ask`` return a synthetic
  order with a ``dry-run-`` uuid that is NOT reported by ``get_open_orders``,
  so the next cycle sees the same real state again and logs the same intent).
* ``cancel_order`` / ``cancel_all_orders`` are never called.
* Taker methods do not exist anywhere in the client.
"""

from __future__ import annotations

import itertools
import time
from decimal import Decimal

from rxdltc_mm.config import ElectrumServer
from rxdltc_mm.kdf.client import KdfClientProtocol
from rxdltc_mm.kdf.models import Balance, MakerOrder, OpenOrders, Orderbook, SwapInfo
from rxdltc_mm.logging_setup import get_logger
from rxdltc_mm.pricing import Side, decimal_to_str, rxd_per_ltc_to_ltc_per_rxd

log = get_logger("kdf.dry_run")


class DryRunKdfClient:
    def __init__(self, inner: KdfClientProtocol):
        self._inner = inner
        self.base = inner.base
        self.quote = inner.quote
        self._ids = itertools.count(1)
        self.would_place: list[dict[str, str]] = []
        self.would_cancel: list[str] = []

    # read-only pass-through --------------------------------------------
    def ping(self) -> str:
        return self._inner.ping()

    def get_enabled_coins(self) -> list[str]:
        return self._inner.get_enabled_coins()

    def activate_coin(self, ticker: str, servers: list[ElectrumServer], address_format: str | None = None) -> Balance:
        # Activation is not a trade; it is allowed in dry-run so balances can be read.
        return self._inner.activate_coin(ticker, servers, address_format)

    def disable_coin(self, ticker: str) -> list[str]:
        # Only ever called by the bot when the pair has no open orders, so nothing gets cancelled.
        return self._inner.disable_coin(ticker)

    def get_balance(self, coin: str) -> Balance:
        return self._inner.get_balance(coin)

    def get_max_maker_vol(self, coin: str) -> Decimal | None:
        return self._inner.get_max_maker_vol(coin)

    def get_orderbook(self) -> Orderbook:
        return self._inner.get_orderbook()

    def get_open_orders(self) -> OpenOrders:
        return self._inner.get_open_orders()

    def get_swap_status(self, uuid: str) -> SwapInfo | None:
        return self._inner.get_swap_status(uuid)

    def get_recent_swaps(self, limit: int = 50) -> list[SwapInfo]:
        return self._inner.get_recent_swaps(limit)

    def get_active_swaps(self) -> list[str]:
        return self._inner.get_active_swaps()

    # mutations: log only -------------------------------------------------
    def _fake_order(self, side: Side, price_rxd_per_ltc: Decimal, amount: Decimal) -> MakerOrder:
        n = next(self._ids)
        if side is Side.BID:
            base, rel, price = self.quote, self.base, price_rxd_per_ltc
        else:
            base, rel, price = self.base, self.quote, rxd_per_ltc_to_ltc_per_rxd(price_rxd_per_ltc)
        record = {"side": side.value, "base": base, "rel": rel, "price": decimal_to_str(price), "volume": decimal_to_str(amount)}
        self.would_place.append(record)
        log.info("DRY RUN: would place maker order", **record, price_rxd_per_ltc=decimal_to_str(price_rxd_per_ltc, 2))
        return MakerOrder(
            uuid=f"dry-run-{n}", base=base, rel=rel, price_rel_per_base=price, max_base_vol=amount,
            min_base_vol=Decimal(0), available_base_amount=amount, created_at_ms=int(time.time() * 1000),
            updated_at_ms=None, cancellable=True, matches=0,
        )

    def place_bid(self, price_rxd_per_ltc: Decimal, ltc_amount: Decimal) -> MakerOrder:
        return self._fake_order(Side.BID, price_rxd_per_ltc, ltc_amount)

    def place_ask(self, price_rxd_per_ltc: Decimal, rxd_amount: Decimal) -> MakerOrder:
        return self._fake_order(Side.ASK, price_rxd_per_ltc, rxd_amount)

    def cancel_order(self, uuid: str) -> bool:
        self.would_cancel.append(uuid)
        log.info("DRY RUN: would cancel order", uuid=uuid)
        return True

    def cancel_all(self) -> list[str]:
        try:
            uuids = [o.uuid for o in self._inner.get_open_orders().maker_orders]
        except Exception:  # noqa: BLE001 - dry run must never raise here
            uuids = []
        self.would_cancel.extend(uuids)
        log.info("DRY RUN: would cancel all pair orders", uuids=uuids)
        return uuids
