"""Command line entry point.

    rxdltc-mm --config config.yaml            # dry-run unless config + env say otherwise
    rxdltc-mm --config config.yaml --once     # a single cycle, then exit
    rxdltc-mm --simulate --cycles 6           # offline: fake KDF + fake prices
    rxdltc-mm --check-config
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
from decimal import Decimal

from rxdltc_mm import __version__
from rxdltc_mm.config import LIVE_CONFIRMATION_PHRASE, BotConfig, load_config, load_dotenv, load_secrets, live_trading_allowed
from rxdltc_mm.engine.bot import LiquidityBot
from rxdltc_mm.kdf.client import KdfClient, KdfClientProtocol
from rxdltc_mm.kdf.dry_run import DryRunKdfClient
from rxdltc_mm.kdf.rpc import KdfRpc
from rxdltc_mm.logging_setup import get_logger, register_secret, setup_logging
from rxdltc_mm.metrics import MetricsServer
from rxdltc_mm.persistence import Store
from rxdltc_mm.prices.base import PriceProvider
from rxdltc_mm.prices.providers import build_providers
from rxdltc_mm.pricing import Side

log = get_logger("cli")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="rxdltc-mm", description="RXD/LTC liquidity bot for GLEEC DEX (KDF)")
    p.add_argument("--config", default=os.environ.get("MM_BOT_CONFIG", "config.yaml"), help="YAML config path")
    p.add_argument("--env-file", default=".env", help=".env file to load (default: .env)")
    p.add_argument("--dry-run", action="store_true", help="force dry-run regardless of config")
    p.add_argument("--once", action="store_true", help="run one cycle then exit")
    p.add_argument("--cycles", type=int, default=None, help="run N cycles then exit")
    p.add_argument("--simulate", action="store_true", help="offline simulation with a fake KDF and fake price feeds")
    p.add_argument("--check-config", action="store_true", help="validate config and exit")
    p.add_argument("--version", action="version", version=f"rxdltc-mm {__version__}")
    return p.parse_args(argv)


class _SimClock:
    """Simulated clock: each 'sleep' advances by the poll interval, no real waiting."""

    def __init__(self, start: float, step: float):
        self.now, self.step = start, step

    def __call__(self) -> float:
        return self.now

    def sleep(self, _: float) -> None:
        self.now += self.step


def _build_simulation(cfg: BotConfig, clock: _SimClock) -> tuple[KdfClientProtocol, list[PriceProvider]]:
    from rxdltc_mm.kdf.fake import FakeKdf
    from rxdltc_mm.prices.fake import StaticPriceProvider

    from rxdltc_mm.pricing import Side

    fake = FakeKdf(base=cfg.pair.base, quote=cfg.pair.quote, clock=clock)
    fake.enabled = {cfg.pair.base, cfg.pair.quote}
    # a thin foreign book around 2.8M RXD/LTC: someone sells RXD at 2.70M, someone buys at 2.90M
    fake.foreign_asks_ltc_per_rxd = [(Decimal(1) / Decimal(2_700_000), Decimal(50_000))]
    fake.foreign_bids_ltc_per_rxd = [(Decimal(1) / Decimal(2_900_000), Decimal(80_000))]
    # leftovers "from a previous run": a stale bid that needs repricing and an ask that is still fine
    fake.add_order(Side.BID, Decimal(2_740_000), Decimal("0.045"), created_at=clock() - 600)
    fake.add_order(Side.ASK, Decimal(2_752_000), Decimal("135000"), created_at=clock() - 600)
    # three feeds that disagree by ~1.5%, and a Gleec-CEX-like LTC-only feed
    providers: list[PriceProvider] = [
        StaticPriceProvider("sim_gecko", "0.00001866", "52.24", timestamp=clock),
        StaticPriceProvider("sim_paprika", "0.00001894", "52.30", timestamp=clock),
        StaticPriceProvider("sim_gleec_cex", None, "52.25", timestamp=clock),
    ]
    return fake, providers


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_dotenv(args.env_file)
    try:
        cfg = load_config(args.config)
    except FileNotFoundError:
        print(f"config file not found: {args.config} (copy config.example.yaml)", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"invalid config: {exc}", file=sys.stderr)
        return 2
    setup_logging(cfg.logging.level, cfg.logging.format)

    if args.check_config:
        print("config OK")
        print(f"dry_run={cfg.dry_run} providers={[p.name for p in cfg.pricing.providers if p.enabled]}")
        return 0

    dry_run = True
    sim_clock: _SimClock | None = None
    if args.simulate:
        sim_clock = _SimClock(1_800_000_000.0, cfg.poll_interval_seconds)
        kdf, providers = _build_simulation(cfg, sim_clock)
        dry_run = cfg.dry_run or args.dry_run
        if dry_run:
            kdf = DryRunKdfClient(kdf)
        log.warning("SIMULATION MODE: fake KDF and fake price providers; nothing touches a real node")
    else:
        try:
            secrets = load_secrets()
        except ValueError as exc:
            log.error(str(exc))
            return 2
        register_secret(secrets.rpc_password)
        for key in secrets.api_keys.values():
            register_secret(key)
        rpc = KdfRpc(secrets.rpc_url, secrets.rpc_password, cfg.kdf.request_timeout_seconds)
        real = KdfClient(rpc, base=cfg.pair.base, quote=cfg.pair.quote, trading=cfg.trading,
                         min_volume_fraction=cfg.order_sizing.min_volume_fraction)
        if args.dry_run or cfg.dry_run:
            dry_run = True
        elif live_trading_allowed(cfg):
            dry_run = False
        else:
            log.error("config has dry_run=false but MM_BOT_CONFIRM_LIVE is not set to %s; staying in DRY RUN", LIVE_CONFIRMATION_PHRASE)
            dry_run = True
        kdf = DryRunKdfClient(real) if dry_run else real
        providers = build_providers(cfg.pricing.providers, secrets.api_keys)
        if not providers:
            log.error("no price providers enabled")
            return 2

    store_path = os.environ.get("MM_BOT_DATA_DIR")
    sqlite_path = os.path.join(store_path, os.path.basename(cfg.persistence.sqlite_path)) if store_path else cfg.persistence.sqlite_path
    store = Store(sqlite_path)

    if sim_clock is not None:
        inner = getattr(kdf, "_inner", kdf)

        def sim_sleep(seconds: float) -> None:
            sim_clock.sleep(seconds)
            # In a live simulation a taker fills our ask on the third cycle so swap tracking can be observed.
            if not dry_run and bot.cycles == 3:
                for o in list(inner.orders.values()):
                    if o.side_for(cfg.pair.base, cfg.pair.quote) is Side.ASK:
                        inner.fill_order(o.uuid, Decimal("0.5"))
                        break

        bot = LiquidityBot(cfg, kdf, providers, store, dry_run=dry_run, clock=sim_clock, sleep=sim_sleep)
    else:
        bot = LiquidityBot(cfg, kdf, providers, store, dry_run=dry_run)

    metrics = None
    if cfg.metrics.enabled and not args.once:
        control_token = os.environ.get("MM_BOT_CONTROL_TOKEN") or None
        if control_token:
            register_secret(control_token)
        try:
            metrics = MetricsServer(cfg.metrics.bind, cfg.metrics.port, bot.status_snapshot,
                                    control=bot, control_token=control_token)
            metrics.start()
        except OSError as exc:
            log.error("metrics server failed to start", error=str(exc))

    def _signal(signum: int, _frame: object) -> None:
        log.info("signal received; shutting down", signal=signum)
        bot.stop()

    signal.signal(signal.SIGTERM, _signal)
    signal.signal(signal.SIGINT, _signal)

    max_cycles = 1 if args.once else args.cycles
    try:
        bot.run(max_cycles=max_cycles)
    finally:
        if metrics:
            metrics.stop()
        store.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
