"""Configuration loading and validation.

Non-secret settings come from a YAML file; secrets come from the environment
(optionally loaded from a ``.env`` file). Secrets are never part of the YAML
model so they cannot be logged by accident when the config is dumped.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

LIVE_CONFIRMATION_PHRASE = "I_UNDERSTAND_THIS_TRADES_REAL_FUNDS"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PairConfig(_Strict):
    base: str = "RXD"
    quote: str = "LTC"
    canonical_price_unit: Literal["RXD_PER_LTC"] = "RXD_PER_LTC"


class ProviderConfig(BaseModel):
    """Provider entries are open (extra keys allowed) because each provider type
    has its own options; the provider constructor validates what it needs."""

    model_config = ConfigDict(extra="allow")

    name: str
    type: Literal["coingecko", "coinpaprika", "gleec_cex", "generic_http"]
    enabled: bool = True
    timeout_seconds: float = 10.0


class PricingConfig(_Strict):
    bid_offset_pct: Decimal = Decimal("1.0")
    ask_offset_pct: Decimal = Decimal("1.0")
    min_edge_pct: Decimal = Decimal("0.10")
    min_valid_providers: int = 2
    reference_stale_seconds: float = 90.0  # max age of the quote we hold (since we fetched it)
    max_source_age_seconds: float = 600.0  # max age reported by the source itself (last_updated)
    max_provider_disagreement_pct: Decimal = Decimal("5.0")
    outlier_rejection_pct: Decimal = Decimal("5.0")
    allow_synthetic_quotes: bool = True
    providers: list[ProviderConfig] = Field(default_factory=list)

    @field_validator("bid_offset_pct", "ask_offset_pct", "min_edge_pct")
    @classmethod
    def _non_negative(cls, v: Decimal) -> Decimal:
        if v < 0:
            raise ValueError("offsets must be >= 0")
        return v

    @field_validator("min_valid_providers")
    @classmethod
    def _min_providers(cls, v: int) -> int:
        if v < 1:
            raise ValueError("min_valid_providers must be >= 1")
        return v


class InventoryConfig(_Strict):
    target_rxd_value_pct: Decimal = Decimal("50")
    max_rxd_value_pct: Decimal = Decimal("75")
    min_rxd_value_pct: Decimal = Decimal("25")

    @model_validator(mode="after")
    def _ordered(self) -> "InventoryConfig":
        if not (0 <= self.min_rxd_value_pct < self.target_rxd_value_pct < self.max_rxd_value_pct <= 100):
            raise ValueError("inventory: require 0 <= min < target < max <= 100")
        return self


class InventorySkewConfig(_Strict):
    enabled: bool = True
    max_adjustment_pct: Decimal = Decimal("1.0")


class OrderSizingConfig(_Strict):
    mode: Literal["balance_percent", "fixed"] = "balance_percent"
    rxd_balance_percent: Decimal = Decimal("50")
    ltc_balance_percent: Decimal = Decimal("50")
    rxd_order_amount: Decimal = Decimal("0")
    ltc_order_amount: Decimal = Decimal("0")
    min_rxd_order_amount: Decimal = Decimal("0")
    min_ltc_order_amount: Decimal = Decimal("0")
    min_volume_fraction: Decimal = Decimal("0.25")

    @model_validator(mode="after")
    def _ranges(self) -> "OrderSizingConfig":
        for name in ("rxd_balance_percent", "ltc_balance_percent"):
            v = getattr(self, name)
            if not (0 < v <= 100):
                raise ValueError(f"order_sizing.{name} must be in (0, 100]")
        if not (0 < self.min_volume_fraction <= 1):
            raise ValueError("order_sizing.min_volume_fraction must be in (0, 1]")
        if self.mode == "fixed" and (self.rxd_order_amount <= 0 or self.ltc_order_amount <= 0):
            raise ValueError("order_sizing.mode=fixed requires positive rxd_order_amount and ltc_order_amount")
        return self


class ReserveConfig(_Strict):
    rxd_percent: Decimal = Decimal("10")
    ltc_percent: Decimal = Decimal("10")

    @model_validator(mode="after")
    def _ranges(self) -> "ReserveConfig":
        for name in ("rxd_percent", "ltc_percent"):
            v = getattr(self, name)
            if not (0 <= v < 100):
                raise ValueError(f"reserve.{name} must be in [0, 100)")
        return self


class SafetyConfig(_Strict):
    max_price_move_pct: Decimal = Decimal("5.0")
    max_price_move_window_seconds: float = 300.0
    # Slow anchor: the fair price is compared with the median of the last
    # `anchor_window_seconds`, and quoting stops while it sits further away than
    # `max_anchor_deviation_pct`. Protects against a thin market being pushed.
    anchor_enabled: bool = True
    anchor_window_seconds: float = 3600.0
    max_anchor_deviation_pct: Decimal = Decimal("10.0")
    anchor_min_span_seconds: float = 900.0
    max_quote_deviation_pct: Decimal = Decimal("3.0")
    rpc_failure_limit: int = 3
    order_error_limit: int = 3
    cooldown_seconds: float = 300.0
    recovery_seconds: float = 120.0
    max_swap_age_check_seconds: float = 86400.0


class TradingConfig(_Strict):
    maker_only: bool = True
    prevent_crossing_book: bool = True
    on_cross: Literal["skip", "adjust"] = "skip"
    base_confs: int | None = 2
    base_nota: bool | None = False
    rel_confs: int | None = 2
    rel_nota: bool | None = False
    order_timeout_minutes: int | None = None

    @field_validator("maker_only")
    @classmethod
    def _maker_only(cls, v: bool) -> bool:
        if not v:
            raise ValueError(
                "trading.maker_only=false is not supported: this bot only ever places maker orders "
                "(KDF setprice) and never calls buy/sell."
            )
        return v


class ReconcileConfig(_Strict):
    adopt_existing_orders: bool = True
    cancel_unknown_orders: bool = False


class ShutdownConfig(_Strict):
    cancel_orders_on_exit: bool = True


class ElectrumServer(_Strict):
    url: str
    protocol: Literal["TCP", "SSL", "WS", "WSS"] = "TCP"
    disable_cert_verification: bool = False


class CoinActivation(_Strict):
    electrum: list[ElectrumServer] = Field(default_factory=list)
    # KDF UTXO address format. The GLEEC web wallet activates LTC as "segwit" (ltc1... address);
    # a legacy activation derives a different (L...) address from the same seed with its own balance.
    address_format: Literal["standard", "segwit"] | None = None


class KdfConfig(_Strict):
    request_timeout_seconds: float = 20.0
    activate_coins_on_start: bool = True
    coins: dict[str, CoinActivation] = Field(default_factory=dict)


class MonitoringConfig(_Strict):
    """Dead-man's-switch ping. Empty URL disables it."""

    heartbeat_url: str = ""
    heartbeat_min_interval_seconds: float = 60.0
    heartbeat_report_failures: bool = True   # also ping <url>/fail while paused
    heartbeat_timeout_seconds: float = 10.0


class PersistenceConfig(_Strict):
    sqlite_path: str = "data/mm-bot.sqlite3"


class MetricsConfig(_Strict):
    enabled: bool = True
    bind: str = "127.0.0.1"
    port: int = 9109


class LoggingConfig(_Strict):
    level: str = "INFO"
    format: Literal["text", "json"] = "text"
    status_line_every_cycles: int = 1


class BotConfig(_Strict):
    pair: PairConfig = Field(default_factory=PairConfig)
    poll_interval_seconds: float = 30.0
    reprice_threshold_pct: Decimal = Decimal("0.5")
    resize_threshold_pct: Decimal = Decimal("25.0")
    minimum_order_lifetime_seconds: float = 60.0
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    inventory: InventoryConfig = Field(default_factory=InventoryConfig)
    inventory_skew: InventorySkewConfig = Field(default_factory=InventorySkewConfig)
    order_sizing: OrderSizingConfig = Field(default_factory=OrderSizingConfig)
    reserve: ReserveConfig = Field(default_factory=ReserveConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    trading: TradingConfig = Field(default_factory=TradingConfig)
    reconcile: ReconcileConfig = Field(default_factory=ReconcileConfig)
    shutdown: ShutdownConfig = Field(default_factory=ShutdownConfig)
    kdf: KdfConfig = Field(default_factory=KdfConfig)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    persistence: PersistenceConfig = Field(default_factory=PersistenceConfig)
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    dry_run: bool = True

    @model_validator(mode="after")
    def _cross_checks(self) -> "BotConfig":
        p, s = self.pricing, self.safety
        if p.bid_offset_pct > s.max_quote_deviation_pct or p.ask_offset_pct > s.max_quote_deviation_pct:
            raise ValueError("pricing offsets must not exceed safety.max_quote_deviation_pct")
        if p.min_edge_pct > min(p.bid_offset_pct, p.ask_offset_pct):
            raise ValueError("pricing.min_edge_pct must not exceed the bid/ask offsets")
        if self.poll_interval_seconds < 5:
            raise ValueError("poll_interval_seconds must be >= 5")
        return self


class Secrets:
    """Secrets live in a plain object with a redacting ``repr`` so they never
    leak through logging of config objects."""

    def __init__(self, rpc_url: str, rpc_password: str, api_keys: dict[str, str]):
        self.rpc_url = rpc_url
        self.rpc_password = rpc_password
        self.api_keys = api_keys

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"Secrets(rpc_url={self.rpc_url!r}, rpc_password='***', api_keys={sorted(self.api_keys)})"


def load_dotenv(path: str | os.PathLike[str] = ".env", *, override: bool = False) -> None:
    """Minimal .env loader (KEY=VALUE per line, '#' comments, optional quotes)."""
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if override or key not in os.environ:
            os.environ[key] = value


def load_secrets(env: dict[str, str] | None = None) -> Secrets:
    env = os.environ if env is None else env
    password = env.get("KDF_RPC_PASSWORD", "")
    if not password:
        raise ValueError("KDF_RPC_PASSWORD is not set (see .env.example)")
    api_keys = {
        k: v
        for k, v in (
            ("coingecko", env.get("COINGECKO_API_KEY", "")),
            ("coinpaprika", env.get("COINPAPRIKA_API_KEY", "")),
        )
        if v
    }
    return Secrets(env.get("KDF_RPC_URL", "http://127.0.0.1:7783"), password, api_keys)


def load_config(path: str | os.PathLike[str], env: dict[str, str] | None = None) -> BotConfig:
    env = os.environ if env is None else env
    with open(path, "r", encoding="utf-8") as fh:
        raw: dict[str, Any] = yaml.safe_load(fh) or {}
    if env.get("MM_BOT_LOG_LEVEL"):
        raw.setdefault("logging", {})["level"] = env["MM_BOT_LOG_LEVEL"]
    if env.get("MM_BOT_DRY_RUN", "").lower() in ("1", "true", "yes"):
        raw["dry_run"] = True
    return BotConfig.model_validate(raw)


def live_trading_allowed(cfg: BotConfig, env: dict[str, str] | None = None) -> bool:
    """Live trading needs dry_run=false in YAML *and* the confirmation phrase in the env."""
    env = os.environ if env is None else env
    return (not cfg.dry_run) and env.get("MM_BOT_CONFIRM_LIVE", "") == LIVE_CONFIRMATION_PHRASE
