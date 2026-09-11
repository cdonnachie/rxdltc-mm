"""Concrete price providers.

All of them quote in USD so that legs from different providers are comparable
and can be combined into synthetic RXD/LTC quotes.
"""

from __future__ import annotations

import time
from typing import Any

from rxdltc_mm.config import ProviderConfig
from rxdltc_mm.prices.base import LegQuote, PriceProvider, dig, parse_timestamp, to_decimal


class CoinGeckoPriceProvider(PriceProvider):
    """``/api/v3/simple/price`` -- free endpoint, optional demo API key."""

    URL = "https://api.coingecko.com/api/v3/simple/price"

    def __init__(self, name: str = "coingecko", *, rxd_id: str = "radiant", ltc_id: str = "litecoin",
                 api_key: str | None = None, **kw: Any):
        super().__init__(name, **kw)
        self.rxd_id, self.ltc_id, self.api_key = rxd_id, ltc_id, api_key

    def _fetch(self) -> dict[str, LegQuote]:
        headers = {"x-cg-demo-api-key": self.api_key} if self.api_key else None
        data = self._get_json(self.URL, {"ids": f"{self.rxd_id},{self.ltc_id}", "vs_currencies": "usd",
                                         "include_last_updated_at": "true"}, headers)
        now = time.time()
        legs = {}
        for coin, cid in (("RXD", self.rxd_id), ("LTC", self.ltc_id)):
            entry = data.get(cid)
            if not entry:
                raise ValueError(f"coingecko: no data for {cid}")
            legs[coin] = LegQuote(coin, to_decimal(entry["usd"]), "USD", parse_timestamp(entry.get("last_updated_at"), now))
        return legs


class CoinPaprikaPriceProvider(PriceProvider):
    """``/v1/tickers/{id}`` -- free endpoint, optional API key (pro host)."""

    URL = "https://api.coinpaprika.com/v1/tickers/{id}"
    PRO_URL = "https://api-pro.coinpaprika.com/v1/tickers/{id}"

    def __init__(self, name: str = "coinpaprika", *, rxd_id: str = "rxd-radiant", ltc_id: str = "ltc-litecoin",
                 api_key: str | None = None, **kw: Any):
        super().__init__(name, **kw)
        self.rxd_id, self.ltc_id, self.api_key = rxd_id, ltc_id, api_key

    def _fetch(self) -> dict[str, LegQuote]:
        headers = {"Authorization": self.api_key} if self.api_key else None
        url = self.PRO_URL if self.api_key else self.URL
        now = time.time()
        legs = {}
        for coin, cid in (("RXD", self.rxd_id), ("LTC", self.ltc_id)):
            data = self._get_json(url.format(id=cid), {"quotes": "USD"}, headers)
            legs[coin] = LegQuote(coin, to_decimal(dig(data, "quotes.USD.price")), "USD",
                                  parse_timestamp(data.get("last_updated"), now))
        return legs


class GleecCexPriceProvider(PriceProvider):
    """Gleec centralized exchange public API (HitBTC v3 compatible), the same
    endpoint mm2-client's ``gleeccex_service.go`` uses.

    As of 2026-09 Gleec CEX lists LTC (LTCUSDT, LTCBTC, LTCUSDC) but not RXD, so
    by default only the LTC leg is produced; ``rxd_symbol`` can be set if RXD
    gets listed. USDT is treated as USD.
    """

    URL = "https://api.exchange.gleec.com/api/3/public/ticker"

    def __init__(self, name: str = "gleec_cex", *, ltc_symbol: str | None = "LTCUSDT", rxd_symbol: str | None = None,
                 **kw: Any):
        super().__init__(name, **kw)
        self.ltc_symbol, self.rxd_symbol = ltc_symbol, rxd_symbol

    def _fetch(self) -> dict[str, LegQuote]:
        wanted = {c: s for c, s in (("LTC", self.ltc_symbol), ("RXD", self.rxd_symbol)) if s}
        if not wanted:
            raise ValueError("gleec_cex: no symbols configured")
        data = self._get_json(self.URL, {"symbols": ",".join(wanted.values())})
        now = time.time()
        legs = {}
        for coin, symbol in wanted.items():
            t = data.get(symbol)
            if not t:
                raise ValueError(f"gleec_cex: symbol {symbol} not returned")
            legs[coin] = LegQuote(coin, to_decimal(t["last"]), "USD", parse_timestamp(t.get("timestamp"), now))
        return legs


PathSpec = str | list[str] | None


class GenericHttpPriceProvider(PriceProvider):
    """Any JSON endpoint(s).

    * ``url`` serves both legs, or ``rxd_url`` / ``ltc_url`` give one endpoint per
      leg (exchange tickers). A leg without a URL or price path is skipped, so a
      single-leg provider (RXD only) is possible and becomes a synthetic quote.
    * A price path is a dotted key path. A **list of paths is averaged**, which
      turns ``["bid", "ask"]`` into the mid price -- more robust than ``last``
      on thin markets.
    * Defaults match the mm2-client / cipig price service (``/api/v2/tickers``):
      ``{"RXD": {"last_price": "...", "last_updated_timestamp": 123}}``.
    """

    def __init__(self, name: str = "generic_http", *, url: str | None = None, rxd_url: str | None = None,
                 ltc_url: str | None = None, rxd_price_path: PathSpec = "RXD.last_price",
                 ltc_price_path: PathSpec = "LTC.last_price", rxd_timestamp_path: str | None = "RXD.last_updated_timestamp",
                 ltc_timestamp_path: str | None = "LTC.last_updated_timestamp", quote_currency: str = "USD",
                 headers: dict[str, str] | None = None, **kw: Any):
        super().__init__(name, **kw)
        self.urls = {"RXD": rxd_url or url, "LTC": ltc_url or url}
        self.paths: dict[str, tuple[PathSpec, str | None]] = {
            "RXD": (rxd_price_path, rxd_timestamp_path), "LTC": (ltc_price_path, ltc_timestamp_path)}
        if not any(u and p for u, (p, _) in zip(self.urls.values(), self.paths.values())):
            raise ValueError(f"{name}: needs a url and a price path for at least one leg")
        self.quote_currency = quote_currency
        self.headers = headers

    def _fetch(self) -> dict[str, LegQuote]:
        now = time.time()
        legs = {}
        fetched: dict[str, Any] = {}
        for coin, (price_path, ts_path) in self.paths.items():
            url = self.urls[coin]
            if not url or not price_path:
                continue
            if url not in fetched:
                fetched[url] = self._get_json(url, headers=self.headers)
            data = fetched[url]
            paths = [price_path] if isinstance(price_path, str) else list(price_path)
            values = [to_decimal(dig(data, p)) for p in paths]
            price = sum(values) / len(values)
            ts = parse_timestamp(dig(data, ts_path), now) if ts_path else now
            legs[coin] = LegQuote(coin, price, self.quote_currency, ts)
        return legs


def build_providers(configs: list[ProviderConfig], api_keys: dict[str, str]) -> list[PriceProvider]:
    providers: list[PriceProvider] = []
    for cfg in configs:
        if not cfg.enabled:
            continue
        extra = dict(cfg.model_extra or {})
        kw: dict[str, Any] = {"timeout_seconds": cfg.timeout_seconds,
                              "min_interval_seconds": float(extra.pop("min_interval_seconds", 0) or 0)}
        if cfg.type == "coingecko":
            providers.append(CoinGeckoPriceProvider(cfg.name, api_key=api_keys.get("coingecko"), **extra, **kw))
        elif cfg.type == "coinpaprika":
            providers.append(CoinPaprikaPriceProvider(cfg.name, api_key=api_keys.get("coinpaprika"), **extra, **kw))
        elif cfg.type == "gleec_cex":
            providers.append(GleecCexPriceProvider(cfg.name, **extra, **kw))
        elif cfg.type == "generic_http":
            providers.append(GenericHttpPriceProvider(cfg.name, **extra, **kw))
    return providers
