"""External reference price providers and the aggregator.

Every provider returns *legs*: the price of RXD and/or LTC in a common quote
currency (USD). The aggregator turns legs into canonical RXD-per-LTC quotes
(``ltc_usd / rxd_usd``), rejects stale and outlying quotes and takes the median.
"""

from rxdltc_mm.prices.base import LegQuote, PriceProvider, ProviderResult
from rxdltc_mm.prices.aggregator import AggregationResult, ReferencePrice, aggregate

__all__ = ["LegQuote", "PriceProvider", "ProviderResult", "AggregationResult", "ReferencePrice", "aggregate"]
