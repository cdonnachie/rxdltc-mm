from decimal import Decimal

import pytest

from rxdltc_mm.prices.providers import GenericHttpPriceProvider


class Stub(GenericHttpPriceProvider):
    responses: dict[str, object] = {}

    def _get_json(self, url, params=None, headers=None):
        self.calls = getattr(self, "calls", []) + [url]
        return self.responses[url]


def test_single_url_cipig_format():
    p = Stub("cipig", url="http://p/tickers")
    p.responses = {"http://p/tickers": {"RXD": {"last_price": "0.00002", "last_updated_timestamp": 1789000000},
                                        "LTC": {"last_price": "56", "last_updated_timestamp": 1789000000}}}
    legs = p._fetch()
    assert legs["RXD"].price == Decimal("0.00002") and legs["LTC"].price == Decimal("56")
    assert legs["RXD"].timestamp == 1789000000
    assert p.calls == ["http://p/tickers"]  # one request for both legs


def test_per_leg_urls_and_mid_price():
    p = Stub("nonkyc", rxd_url="http://x/RXD_USDT", ltc_url="http://x/LTC_USDT",
             rxd_price_path=["bid", "ask"], ltc_price_path="last_price", rxd_timestamp_path=None, ltc_timestamp_path=None)
    p.responses = {"http://x/RXD_USDT": {"bid": "0.000017", "ask": "0.000019", "last_price": "0.000018"},
                   "http://x/LTC_USDT": {"last_price": "54"}}
    legs = p._fetch()
    assert legs["RXD"].price == Decimal("0.000018")  # mid of bid/ask
    assert legs["LTC"].price == Decimal("54")
    assert sorted(p.calls) == ["http://x/LTC_USDT", "http://x/RXD_USDT"]


def test_single_leg_provider():
    p = Stub("rxd-only", rxd_url="http://x/RXD_USDT", rxd_price_path="last_price", ltc_price_path=None,
             rxd_timestamp_path=None)
    p.responses = {"http://x/RXD_USDT": {"last_price": "0.000018"}}
    legs = p._fetch()
    assert list(legs) == ["RXD"]


def test_requires_at_least_one_leg():
    with pytest.raises(ValueError):
        GenericHttpPriceProvider("bad", rxd_price_path=None, ltc_price_path=None)
