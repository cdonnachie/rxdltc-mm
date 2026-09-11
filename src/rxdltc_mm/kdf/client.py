"""``KdfClient`` -- the trading abstraction used by the bot.

Real RPC methods used (verified against GLEECBTC/komodo-defi-framework,
``mm2src/mm2_main/src/rpc/dispatcher``):

=====================  ==========  ==============================================
bot method             RPC style   KDF method
=====================  ==========  ==============================================
ping / version         legacy      ``version``
get_enabled_coins      v2          ``get_enabled_coins``
activate_coin          legacy      ``electrum`` (UTXO coins via Electrum servers)
get_balance            legacy      ``my_balance``
get_max_maker_vol      v2          ``max_maker_vol``
get_min_trading_vol    legacy      ``min_trading_vol``
get_orderbook          v2          ``orderbook``
get_open_orders        legacy      ``my_orders``
get_order_status       legacy      ``order_status``
place_bid / place_ask  legacy      ``setprice``  (maker order; post-only by construction)
cancel_order           legacy      ``cancel_order``
cancel_all             legacy      ``cancel_all_orders`` (``cancel_by: Pair``)
get_swap_status        v2          ``my_swap_status``
get_recent_swaps       v2          ``my_recent_swaps``
get_active_swaps       legacy      ``active_swaps``
trade_preimage         v2          ``trade_preimage`` (fee/volume check, read-only)
=====================  ==========  ==============================================

Direction mapping (see :mod:`rxdltc_mm.pricing` for the full explanation):

* ``place_bid(price_rxd_per_ltc, ltc_amount)`` -> ``setprice base=LTC rel=RXD
  price=price_rxd_per_ltc volume=ltc_amount``. KDF price is "rel per base" =
  RXD per LTC, which IS the canonical unit, so no conversion is needed.
* ``place_ask(price_rxd_per_ltc, rxd_amount)`` -> ``setprice base=RXD rel=LTC
  price=1/price_rxd_per_ltc volume=rxd_amount``. KDF price is LTC per RXD, so
  the canonical price is inverted exactly once, here.

``buy``/``sell`` (taker methods) are deliberately not wrapped: the bot must
never take liquidity.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Protocol

from rxdltc_mm.config import ElectrumServer, TradingConfig
from rxdltc_mm.kdf.models import Balance, MakerOrder, OpenOrders, Orderbook, SwapInfo
from rxdltc_mm.kdf.rpc import KdfRpc, KdfRpcError
from rxdltc_mm.logging_setup import get_logger
from rxdltc_mm.pricing import Side, decimal_to_str, quantize_amount, rxd_per_ltc_to_ltc_per_rxd

log = get_logger("kdf.client")


class KdfClientProtocol(Protocol):
    """What the engine needs from a KDF client (real, dry-run wrapper or fake)."""

    base: str
    quote: str

    def ping(self) -> str: ...
    def get_enabled_coins(self) -> list[str]: ...
    def activate_coin(self, ticker: str, servers: list[ElectrumServer], address_format: str | None = None) -> Balance: ...
    def disable_coin(self, ticker: str) -> list[str]: ...
    def get_balance(self, coin: str) -> Balance: ...
    def get_max_maker_vol(self, coin: str) -> Decimal | None: ...
    def get_orderbook(self) -> Orderbook: ...
    def get_open_orders(self) -> OpenOrders: ...
    def place_bid(self, price_rxd_per_ltc: Decimal, ltc_amount: Decimal) -> MakerOrder: ...
    def place_ask(self, price_rxd_per_ltc: Decimal, rxd_amount: Decimal) -> MakerOrder: ...
    def cancel_order(self, uuid: str) -> bool: ...
    def cancel_all(self) -> list[str]: ...
    def get_swap_status(self, uuid: str) -> SwapInfo | None: ...
    def get_recent_swaps(self, limit: int = 50) -> list[SwapInfo]: ...
    def get_active_swaps(self) -> list[str]: ...


def build_setprice_request(
    side: Side,
    price_rxd_per_ltc: Decimal,
    amount: Decimal,
    *,
    base: str,
    quote: str,
    trading: TradingConfig,
    min_volume_fraction: Decimal,
) -> dict[str, Any]:
    """Pure function producing the ``setprice`` parameters for a bot order.

    Exposed separately so the direction mapping can be unit-tested without a
    running KDF.
    """
    if price_rxd_per_ltc <= 0 or amount <= 0:
        raise ValueError("price and amount must be positive")
    volume = quantize_amount(amount)
    min_volume = quantize_amount(volume * min_volume_fraction)
    if side is Side.BID:
        # Bot buys RXD == sells LTC for RXD. Price = RXD received per 1 LTC given.
        req: dict[str, Any] = {
            "base": quote,
            "rel": base,
            "price": decimal_to_str(price_rxd_per_ltc),
            "volume": decimal_to_str(volume),
        }
    else:
        # Bot sells RXD for LTC. Price = LTC received per 1 RXD given.
        req = {
            "base": base,
            "rel": quote,
            "price": decimal_to_str(rxd_per_ltc_to_ltc_per_rxd(price_rxd_per_ltc)),
            "volume": decimal_to_str(volume),
        }
    if min_volume > 0:
        req["min_volume"] = decimal_to_str(min_volume)
    # Never let KDF silently cancel other orders on the pair: we manage that ourselves.
    req["cancel_previous"] = False
    req["save_in_history"] = True
    if trading.base_confs is not None:
        req["base_confs"] = trading.base_confs
    if trading.base_nota is not None:
        req["base_nota"] = trading.base_nota
    if trading.rel_confs is not None:
        req["rel_confs"] = trading.rel_confs
    if trading.rel_nota is not None:
        req["rel_nota"] = trading.rel_nota
    if trading.order_timeout_minutes:
        req["timeout_in_minutes"] = trading.order_timeout_minutes
    return req


class KdfClient:
    def __init__(self, rpc: KdfRpc, *, base: str, quote: str, trading: TradingConfig, min_volume_fraction: Decimal):
        self._rpc = rpc
        self.base = base
        self.quote = quote
        self._trading = trading
        self._min_volume_fraction = min_volume_fraction

    # ---------------------------------------------------------------- node
    def ping(self) -> str:
        data = self._rpc.legacy("version")
        return str(data.get("result", "unknown"))

    def get_enabled_coins(self) -> list[str]:
        data = self._rpc.v2("get_enabled_coins")
        coins = data.get("coins", []) if isinstance(data, dict) else []
        return [str(c["ticker"]) for c in coins]

    def activate_coin(self, ticker: str, servers: list[ElectrumServer], address_format: str | None = None) -> Balance:
        if not servers:
            raise ValueError(f"no electrum servers configured for {ticker}")
        params: dict[str, Any] = {"coin": ticker, "servers": [s.model_dump() for s in servers], "tx_history": False}
        if address_format:
            params["address_format"] = {"format": address_format}  # UtxoAddressFormat: {"format": "segwit"|"standard"}
        data = self._rpc.legacy("electrum", **params)
        return Balance(coin=ticker, spendable=Decimal(str(data["balance"])),
                       unspendable=Decimal(str(data.get("unspendable_balance", "0"))), address=str(data.get("address", "")))

    def disable_coin(self, ticker: str) -> list[str]:
        """Legacy ``disable_coin``. KDF refuses while the coin has matching orders or active
        swaps; otherwise it cancels the coin's open orders and returns their uuids."""
        data = self._rpc.legacy("disable_coin", coin=ticker)
        result = data.get("result", {}) if isinstance(data, dict) else {}
        return [str(u) for u in result.get("cancelled_orders", [])]

    # ------------------------------------------------------------- balances
    def get_balance(self, coin: str) -> Balance:
        return Balance.from_rpc(self._rpc.legacy("my_balance", coin=coin))

    def get_max_maker_vol(self, coin: str) -> Decimal | None:
        try:
            data = self._rpc.v2("max_maker_vol", {"coin": coin})
        except KdfRpcError as exc:
            log.debug("max_maker_vol unavailable", coin=coin, error=str(exc))
            return None
        vol = data.get("volume") if isinstance(data, dict) else None
        if isinstance(vol, dict):
            vol = vol.get("decimal")
        return Decimal(str(vol)) if vol is not None else None

    def get_min_trading_vol(self, coin: str) -> Decimal | None:
        try:
            data = self._rpc.legacy("min_trading_vol", coin=coin)
        except KdfRpcError:
            return None
        vol = data.get("result", {}).get("volume") if isinstance(data, dict) else None
        if isinstance(vol, dict):
            vol = vol.get("decimal")
        return Decimal(str(vol)) if vol is not None else None

    # --------------------------------------------------------------- market
    def get_orderbook(self) -> Orderbook:
        data = self._rpc.v2("orderbook", {"base": self.base, "rel": self.quote})
        return Orderbook.from_rpc(data)

    def get_open_orders(self) -> OpenOrders:
        data = self._rpc.legacy("my_orders")
        result = data.get("result", {})
        makers = result.get("maker_orders", {}) or {}
        takers = result.get("taker_orders", {}) or {}
        orders = [MakerOrder.from_rpc(o) for o in makers.values()]
        pair_orders = [o for o in orders if o.side_for(self.base, self.quote) is not None]
        return OpenOrders(maker_orders=pair_orders, taker_order_count=len(takers))

    def get_order_status(self, uuid: str) -> dict[str, Any]:
        return self._rpc.legacy("order_status", uuid=uuid)

    # --------------------------------------------------------------- orders
    def _setprice(self, side: Side, price_rxd_per_ltc: Decimal, amount: Decimal) -> MakerOrder:
        req = build_setprice_request(side, price_rxd_per_ltc, amount, base=self.base, quote=self.quote,
                                     trading=self._trading, min_volume_fraction=self._min_volume_fraction)
        log.info("setprice", side=side.value, **{k: v for k, v in req.items() if k in ("base", "rel", "price", "volume", "min_volume")})
        data = self._rpc.legacy("setprice", **req)
        return MakerOrder.from_rpc(data["result"])

    def place_bid(self, price_rxd_per_ltc: Decimal, ltc_amount: Decimal) -> MakerOrder:
        """Buy RXD with ``ltc_amount`` LTC at ``price_rxd_per_ltc`` RXD per LTC."""
        return self._setprice(Side.BID, price_rxd_per_ltc, ltc_amount)

    def place_ask(self, price_rxd_per_ltc: Decimal, rxd_amount: Decimal) -> MakerOrder:
        """Sell ``rxd_amount`` RXD for LTC at ``price_rxd_per_ltc`` RXD per LTC."""
        return self._setprice(Side.ASK, price_rxd_per_ltc, rxd_amount)

    def cancel_order(self, uuid: str) -> bool:
        data = self._rpc.legacy("cancel_order", uuid=uuid)
        return str(data.get("result", "")).lower() == "success"

    def cancel_all(self) -> list[str]:
        cancelled: list[str] = []
        for base, rel in ((self.base, self.quote), (self.quote, self.base)):
            data = self._rpc.legacy("cancel_all_orders", cancel_by={"type": "Pair", "data": {"base": base, "rel": rel}})
            result = data.get("result", {}) if isinstance(data, dict) else {}
            cancelled.extend(str(u) for u in result.get("cancelled", []))
            if result.get("currently_matching"):
                log.warning("orders currently matching could not be cancelled", uuids=result["currently_matching"])
        return cancelled

    # ---------------------------------------------------------------- swaps
    def get_swap_status(self, uuid: str) -> SwapInfo | None:
        data = self._rpc.v2("my_swap_status", {"uuid": uuid})
        return SwapInfo.from_rpc(data) if isinstance(data, dict) else None

    def get_recent_swaps(self, limit: int = 50) -> list[SwapInfo]:
        data = self._rpc.v2("my_recent_swaps", {"limit": limit, "page_number": 1})
        swaps = data.get("swaps", []) if isinstance(data, dict) else []
        out: list[SwapInfo] = []
        for raw in swaps:
            parsed = SwapInfo.from_rpc(raw)
            if parsed is not None and parsed.side_for(self.base, self.quote) is not None:
                out.append(parsed)
        return out

    def get_active_swaps(self) -> list[str]:
        data = self._rpc.legacy("active_swaps", include_status=False)
        uuids = data.get("uuids", []) if isinstance(data, dict) else []
        return [str(u) for u in uuids]

    def trade_preimage(self, side: Side, price_rxd_per_ltc: Decimal, amount: Decimal) -> dict[str, Any]:
        """Ask KDF what fees/volume a maker order would have. Read-only."""
        req = build_setprice_request(side, price_rxd_per_ltc, amount, base=self.base, quote=self.quote,
                                     trading=self._trading, min_volume_fraction=self._min_volume_fraction)
        params = {"base": req["base"], "rel": req["rel"], "swap_method": "setprice",
                  "price": req["price"], "volume": req["volume"]}
        return self._rpc.v2("trade_preimage", params)
