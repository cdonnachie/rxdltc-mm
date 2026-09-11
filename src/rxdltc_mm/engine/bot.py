"""The main loop.

One :meth:`LiquidityBot.cycle` does, in order:

1. fetch reference prices, aggregate, run the price-move breaker
2. read KDF state (balances, own orders, orderbook, recent swaps)
3. account for completed swaps
4. sanity-check balances and order state (ambiguity)
5. if PAUSED: keep orders cancelled, track recovery, maybe resume
6. compute inventory skew, sizes and target quotes; verify the safety envelope
7. plan and execute cancels/places (never place if a cancel failed)
8. persist, update metrics snapshot, log the status line

Any failure in 1-6 calls :meth:`_enter_paused`, which cancels all pair orders
(or logs that it would, in dry-run) and starts the cooldown/recovery timer.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable

from rxdltc_mm.config import BotConfig, CoinActivation
from rxdltc_mm.engine.planner import Action, OrderIntent, Plan, build_plan, group_by_side
from rxdltc_mm.engine.safety import FailureCounter, PauseController, PriceMoveMonitor
from rxdltc_mm.engine.state import BotState, StateMachine
from rxdltc_mm.inventory import Inventory, compute_skew_pct, side_permissions
from rxdltc_mm.kdf.client import KdfClientProtocol
from rxdltc_mm.kdf.models import Balance, MakerOrder, Orderbook, SwapInfo
from rxdltc_mm.kdf.rpc import KdfError
from rxdltc_mm.logging_setup import get_logger
from rxdltc_mm.persistence import Store
from rxdltc_mm.prices.aggregator import AggregationResult, ReferencePrice, aggregate
from rxdltc_mm.prices.base import PriceProvider
from rxdltc_mm.pricing import (
    Side,
    TargetQuotes,
    compute_target_quotes,
    decimal_to_str,
    format_rxd_per_ltc,
    quote_is_safe,
    rxd_per_ltc_to_ltc_per_rxd,
)
from rxdltc_mm.sizing import compute_order_sizes

log = get_logger("bot")


class UnsafeCondition(Exception):
    """Raised inside a cycle when a circuit breaker trips."""


@dataclass
class MarketSnapshot:
    rxd: Balance
    ltc: Balance
    orders: list[MakerOrder]
    taker_orders: int
    orderbook: Orderbook | None
    swaps: list[SwapInfo] = field(default_factory=list)
    rxd_max_maker_vol: Decimal | None = None
    ltc_max_maker_vol: Decimal | None = None


class LiquidityBot:
    def __init__(
        self,
        cfg: BotConfig,
        kdf: KdfClientProtocol,
        providers: list[PriceProvider],
        store: Store,
        *,
        dry_run: bool,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] | None = None,
    ):
        self.cfg = cfg
        self.kdf = kdf
        self.providers = providers
        self.store = store
        self.dry_run = dry_run
        self.clock = clock
        self._stop = threading.Event()
        self._wake = threading.Event()  # set by control requests to run the next cycle immediately
        self._sleep = sleep or self._default_sleep
        # operator control (see request_* methods; consumed at the start of each cycle)
        self._control_lock = threading.Lock()
        self._pending_pause: str | None = None
        self._pending_resume = False
        self.operator_paused = False
        now = clock()
        self.started_at = now
        self.sm = StateMachine(now=now)
        self.pause = PauseController(cfg.safety.cooldown_seconds, cfg.safety.recovery_seconds)
        self.price_monitor = PriceMoveMonitor(cfg.safety.max_price_move_pct, cfg.safety.max_price_move_window_seconds)
        self.rpc_failures = FailureCounter(cfg.safety.rpc_failure_limit)
        self.order_errors = FailureCounter(cfg.safety.order_error_limit)
        self.cycles = 0
        self.reference: ReferencePrice | None = None
        self.last_aggregation: AggregationResult | None = None
        self.targets: TargetQuotes | None = None
        self.snapshot: MarketSnapshot | None = None
        self.last_plan: Plan | None = None
        self.known_order_uuids: set[str] = set(store.open_order_uuids())
        self.stats = store.get_stats()
        self._lock = threading.Lock()
        self._status: dict[str, Any] = {}
        self._reconciled = False

    # ------------------------------------------------------------------ run
    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _default_sleep(self, seconds: float) -> None:
        self._wake.wait(seconds)
        self._wake.clear()

    # ------------------------------------------------------------- control
    # Called from the HTTP control API thread; only flags are set here, the loop acts on them.
    def request_pause(self, reason: str) -> str:
        with self._control_lock:
            self._pending_pause = reason
            self._pending_resume = False
        self._wake.set()
        return f"pause requested: {reason}"

    def request_resume(self) -> str:
        with self._control_lock:
            self._pending_resume = True
            self._pending_pause = None
        self._wake.set()
        return "resume requested; the bot resumes on the next healthy cycle"

    def request_stop(self) -> str:
        self.stop()
        return "stop requested; cancelling orders per shutdown config and exiting"

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.store.recent_events(limit)

    def clear_events(self) -> str:
        n = self.store.clear_events()
        self._publish_status()
        return f"cleared {n} events"

    def _apply_control_requests(self, now: float) -> None:
        with self._control_lock:
            pause, resume = self._pending_pause, self._pending_resume
            self._pending_pause, self._pending_resume = None, False
        if pause is not None:
            self.operator_paused = True
            log.warning("operator pause", reason=pause)
            self.store.record_event("WARNING", "operator", f"pause: {pause}")
            self._enter_paused(f"operator: {pause}", now, cancel=True)
        if resume:
            self.operator_paused = False
            self._resume_asap = True
            log.info("operator resume requested")
            self.store.record_event("INFO", "operator", "resume requested")

    def run(self, max_cycles: int | None = None) -> None:
        log.info("bot starting", dry_run=self.dry_run, pair=f"{self.cfg.pair.base}/{self.cfg.pair.quote}",
                 poll=self.cfg.poll_interval_seconds)
        if self.dry_run:
            log.warning("DRY RUN MODE: no orders will be placed or cancelled")
        try:
            while not self._stop.is_set():
                started = self.clock()
                try:
                    self.cycle()
                except Exception:  # noqa: BLE001 - the loop must survive anything
                    log.exception("unhandled error in cycle")
                    self.store.record_event("ERROR", "cycle", "unhandled exception (see logs)")
                if max_cycles is not None and self.cycles >= max_cycles:
                    break
                elapsed = self.clock() - started
                self._sleep(max(0.0, self.cfg.poll_interval_seconds - elapsed))
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        if self.sm.state is BotState.SHUTTING_DOWN:
            return
        now = self.clock()
        self.sm.transition(BotState.SHUTTING_DOWN, "stop requested", now=now)
        if self.cfg.shutdown.cancel_orders_on_exit:
            try:
                cancelled = self.kdf.cancel_all()
                log.info("DRY RUN: would cancel orders on exit" if self.dry_run else "cancelled orders on exit", uuids=cancelled)
                for u in cancelled:
                    self.store.set_order_status(u, "cancelled")
            except Exception as exc:  # noqa: BLE001
                log.error("failed to cancel orders on exit", error=str(exc))
                self.store.record_event("ERROR", "shutdown", f"cancel on exit failed: {exc}")
        self.store.set("bot_state", {"state": self.sm.state.value, "at": now, "reason": self.sm.reason})
        self._publish_status()
        log.info("bot stopped")

    # ---------------------------------------------------------------- cycle
    def cycle(self) -> None:
        now = self.clock()
        self.cycles += 1
        try:
            self._apply_control_requests(now)
            ref = self._update_reference(now)
            snap = self._read_market(now)
            self._track_swaps(snap, now)
            self._check_balances(snap)
            self._check_order_state(snap)
            if self.sm.state is BotState.RPC_ERROR:
                self._enter_paused("KDF RPC recovered; cooling down before quoting", now, cancel=True)
            if self.pause.paused and not self._while_paused(snap, now):
                return
            if self.sm.state in (BotState.STARTING, BotState.WAITING_FOR_REFERENCE):
                self.sm.transition(BotState.ACTIVE, "reference price and KDF state available", now=now)
            self._quote(ref, snap, now)
        except _Wait as exc:
            log.info("waiting for a valid reference price", reason=str(exc))
        except UnsafeCondition as exc:
            self._enter_paused(str(exc), now, cancel=True)
        except KdfError as exc:
            self._on_rpc_failure(exc, now)
        finally:
            self.store.set("bot_state", {"state": self.sm.state.value, "at": now, "reason": self.sm.reason})
            self._publish_status()
            if self.cfg.logging.status_line_every_cycles and self.cycles % self.cfg.logging.status_line_every_cycles == 0:
                log.info(self.status_line())

    # ------------------------------------------------------------ reference
    def _update_reference(self, now: float) -> ReferencePrice | None:
        results = [p.fetch() for p in self.providers]
        agg = aggregate(results, self.cfg.pricing, now)
        self.last_aggregation = agg
        detail = {"candidates": {k: str(v) for k, v in agg.candidate_prices.items()}, "rejected": agg.rejected, "reason": agg.reason}
        ref = agg.reference
        self.store.record_reference(now, ref.fair_rxd_per_ltc if ref else None, ref.number_of_sources if ref else 0,
                                    ref.disagreement_pct if ref else None, detail)
        if ref is None:
            self.reference = None
            self.targets = None
            for src, why in agg.rejected.items():
                log.warning("reference source rejected", source=src, reason=why)
            if self.sm.state in (BotState.STARTING, BotState.WAITING_FOR_REFERENCE):
                # Not quoting yet: waiting is enough, no pause/cooldown needed. Still talk to KDF so
                # connectivity problems surface, and make sure nothing is left open from a previous run.
                self.sm.transition(BotState.WAITING_FOR_REFERENCE, agg.reason, now=now)
                snap = self._read_market(now)
                if snap.orders:
                    self._cancel_everything("no reference price while starting")
                raise _Wait(agg.reason)
            raise UnsafeCondition(f"no valid reference price: {agg.reason}")
        self.reference = ref
        log.info("reference price", fair_rxd_per_ltc=decimal_to_str(ref.fair_rxd_per_ltc, 2), sources=ref.number_of_sources,
                 disagreement_pct=f"{ref.disagreement_pct:.3f}",
                 prices={k: decimal_to_str(v, 0) for k, v in ref.source_prices.items()},
                 synthetic=list(ref.synthetic_sources), rejected=ref.rejected)
        move = self.price_monitor.observe(now, ref.fair_rxd_per_ltc)
        if move is not None:
            raise UnsafeCondition(f"fair price moved {move:.2f}% within {self.cfg.safety.max_price_move_window_seconds:.0f}s "
                                  f"(limit {self.cfg.safety.max_price_move_pct}%)")
        return ref

    # --------------------------------------------------------------- market
    def _read_market(self, now: float) -> MarketSnapshot:
        base, quote = self.cfg.pair.base, self.cfg.pair.quote
        try:
            if not self._reconciled:
                self._startup_checks()
            rxd = self.kdf.get_balance(base)
            ltc = self.kdf.get_balance(quote)
            open_orders = self.kdf.get_open_orders()
            orderbook = self.kdf.get_orderbook()
            swaps = self.kdf.get_recent_swaps(limit=30)
            rxd_mmv = self.kdf.get_max_maker_vol(base)
            ltc_mmv = self.kdf.get_max_maker_vol(quote)
        except KdfError:
            raise
        self.rpc_failures.success()
        snap = MarketSnapshot(rxd, ltc, open_orders.maker_orders, open_orders.taker_order_count, orderbook, swaps, rxd_mmv, ltc_mmv)
        self.snapshot = snap
        self.store.record_balance(now, base, rxd.spendable, rxd.unspendable)
        self.store.record_balance(now, quote, ltc.spendable, ltc.unspendable)
        self._log_balance_changes(snap)
        if not self._reconciled:
            self._reconcile_orders(snap)
            self._reconciled = True
        return snap

    def _startup_checks(self) -> None:
        version = self.kdf.ping()
        log.info("connected to KDF", version=version)
        enabled = set(self.kdf.get_enabled_coins())
        pair_orders = None  # fetched lazily, only needed for the address-format repair
        for ticker in (self.cfg.pair.base, self.cfg.pair.quote):
            if ticker in enabled:
                wanted = (self.cfg.kdf.coins.get(ticker) or CoinActivation()).address_format
                if wanted and not self._address_format_matches(self.kdf.get_balance(ticker), wanted):
                    if pair_orders is None:
                        pair_orders = self.kdf.get_open_orders().maker_orders
                    self._repair_address_format(ticker, wanted, pair_orders)
                continue
            if not self.cfg.kdf.activate_coins_on_start:
                raise UnsafeCondition(f"coin {ticker} is not enabled in KDF and activate_coins_on_start is false")
            servers = self.cfg.kdf.coins.get(ticker)
            if servers is None or not servers.electrum:
                raise UnsafeCondition(f"coin {ticker} is not enabled and no electrum servers are configured")
            log.info("activating coin", coin=ticker, servers=[s.url for s in servers.electrum], address_format=servers.address_format)
            bal = self.kdf.activate_coin(ticker, servers.electrum, servers.address_format)
            log.info("coin activated", coin=ticker, address=bal.address, balance=str(bal.spendable))

    @staticmethod
    def _address_format_matches(bal: Balance, wanted: str) -> bool:
        if not bal.address:
            return True
        looks_segwit = bal.address == bal.address.lower() and "1" in bal.address  # bech32 is all lower case
        return (wanted == "segwit") == looks_segwit

    def _repair_address_format(self, ticker: str, wanted: str, pair_orders: list[MakerOrder]) -> None:
        """The coin was enabled earlier (e.g. a previous run) with a different address format,
        so KDF is looking at a different address of the same seed. Re-activate it with the
        configured format, but only when nothing is open on the pair: ``disable_coin`` cancels
        the coin's open orders and refuses while swaps are in progress."""
        servers = self.cfg.kdf.coins.get(ticker)
        if servers is None or not servers.electrum:
            raise UnsafeCondition(f"coin {ticker} is enabled with a different address format than configured and no "
                                  f"electrum servers are configured to re-activate it")
        if pair_orders:
            log.warning("coin is enabled with a different address format than configured but orders are open; "
                        "not re-activating. Cancel them or restart KDF.", coin=ticker, configured=wanted)
            return
        log.warning("coin is enabled with a different address format than configured; re-activating",
                    coin=ticker, configured=wanted)
        cancelled = self.kdf.disable_coin(ticker)
        if cancelled:
            log.warning("disable_coin cancelled orders", coin=ticker, uuids=cancelled)
        bal = self.kdf.activate_coin(ticker, servers.electrum, wanted)
        log.info("coin re-activated", coin=ticker, address=bal.address, balance=str(bal.spendable), address_format=wanted)

    def _reconcile_orders(self, snap: MarketSnapshot) -> None:
        """Startup reconciliation: adopt existing orders instead of duplicating them."""
        base, quote = self.cfg.pair.base, self.cfg.pair.quote
        grouped = group_by_side(snap.orders, base, quote)
        for side, orders in grouped.items():
            for o in orders:
                known = o.uuid in self.known_order_uuids
                price = o.price_rxd_per_ltc(base, quote)
                if known or self.cfg.reconcile.adopt_existing_orders:
                    log.info("reconcile: adopting existing order", side=side.value, uuid=o.uuid, known=known,
                             price_rxd_per_ltc=decimal_to_str(price, 0), remaining=str(o.available_base_amount))
                    self.known_order_uuids.add(o.uuid)
                    self.store.record_order(o.uuid, side.value, price, o.max_base_vol, o.created_at_seconds(), "open")
                else:
                    log.warning("reconcile: unknown order present", side=side.value, uuid=o.uuid)
        # Orders we thought were open but KDF no longer has: mark closed.
        live = {o.uuid for o in snap.orders}
        for uuid in list(self.known_order_uuids):
            if uuid not in live:
                self.store.set_order_status(uuid, "closed")
                self.known_order_uuids.discard(uuid)

    def _log_balance_changes(self, snap: MarketSnapshot) -> None:
        prev = getattr(self, "_prev_balances", None)
        cur = (snap.rxd.spendable, snap.ltc.spendable)
        if prev is not None and prev != cur:
            log.info("balance change", rxd_before=decimal_to_str(prev[0], 8), rxd_after=decimal_to_str(cur[0], 8),
                     ltc_before=decimal_to_str(prev[1], 8), ltc_after=decimal_to_str(cur[1], 8))
        self._prev_balances = cur

    def _track_swaps(self, snap: MarketSnapshot, now: float) -> None:
        base, quote = self.cfg.pair.base, self.cfg.pair.quote
        cutoff = now - self.cfg.safety.max_swap_age_check_seconds
        for s in snap.swaps:
            if s.started_at and s.started_at < cutoff:
                continue
            known = self.store.swap_known(s.uuid)
            if known is not None and known[0] == s.finished:
                continue
            side = s.side_for(base, quote)
            self.store.record_swap(s.uuid, side.value if side else None, s.my_coin, s.other_coin, s.my_amount, s.other_amount,
                                   s.started_at, s.finished, s.success)
            if not s.finished:
                log.info("swap in progress", uuid=s.uuid, side=side.value if side else None, last_event=s.last_event)
                continue
            if not s.success:
                self.stats = self.store.add_stats(swaps_failed=1)
                log.warning("swap failed", uuid=s.uuid, side=side.value if side else None, last_event=s.last_event)
                self.store.record_event("WARNING", "swap", f"swap {s.uuid} failed at {s.last_event}")
                continue
            if side is Side.ASK:
                self.stats = self.store.add_stats(swaps_total=1, rxd_sold=s.my_amount, ltc_received=s.other_amount)
            elif side is Side.BID:
                self.stats = self.store.add_stats(swaps_total=1, rxd_bought=s.other_amount, ltc_spent=s.my_amount)
            gave, received = f"{decimal_to_str(s.my_amount, 8)} {s.my_coin}", f"{decimal_to_str(s.other_amount, 8)} {s.other_coin}"
            log.info("swap completed", uuid=s.uuid, side=side.value if side else None, gave=gave, received=received)
            self.store.record_event("INFO", "swap", f"{side.value if side else '?'} swap {s.uuid}: gave {gave}, received {received}")

    # --------------------------------------------------------------- checks
    def _check_balances(self, snap: MarketSnapshot) -> None:
        for b in (snap.rxd, snap.ltc):
            if b.spendable < 0 or b.unspendable < 0 or not b.spendable.is_finite():
                raise UnsafeCondition(f"balance data for {b.coin} cannot be trusted: {b.spendable}/{b.unspendable}")

    def _check_order_state(self, snap: MarketSnapshot) -> None:
        if snap.taker_orders:
            log.warning("taker orders exist on this KDF node; they are not managed by the bot", count=snap.taker_orders)
        grouped = group_by_side(snap.orders, self.cfg.pair.base, self.cfg.pair.quote)
        for side, orders in grouped.items():
            if len(orders) > 1 and not self.cfg.reconcile.cancel_unknown_orders:
                raise UnsafeCondition(f"ambiguous order state: {len(orders)} {side.value} orders on the pair")
        if snap.orderbook is not None:
            # KDF flags orderbook entries `is_mine` by pubkey. An is_mine entry that this instance does not
            # list in my_orders belongs to ANOTHER KDF instance running the same seed (e.g. the web wallet).
            # The bot cannot manage or cancel those, and two instances sharing UTXOs can break swaps.
            own = {o.uuid for o in snap.orders}
            phantom = [e.uuid for e in (*snap.orderbook.asks, *snap.orderbook.bids) if e.is_mine and e.uuid not in own]
            if phantom:
                raise UnsafeCondition(
                    f"{len(phantom)} order(s) with this wallet's pubkey are on the network but not managed by this KDF "
                    f"instance ({', '.join(phantom[:3])}); another wallet with the same seed is running"
                )

    # ---------------------------------------------------------------- pause
    def _enter_paused(self, reason: str, now: float, *, cancel: bool) -> None:
        first = not self.pause.paused
        self.pause.pause(reason, now)
        self.targets = None
        self.sm.transition(BotState.PAUSED, reason, now=now)
        if first:
            log.warning("SAFETY TRIGGER: pausing", reason=reason)
            self.store.record_event("WARNING", "pause", reason)
        else:
            log.warning("still unsafe; recovery timer reset", reason=reason,
                        paused_for=f"{now - (self.pause.paused_at or now):.0f}s")
        if cancel:
            self._cancel_everything("paused: " + reason)

    def _cancel_everything(self, why: str) -> bool:
        try:
            cancelled = self.kdf.cancel_all()
        except KdfError as exc:
            log.error("cancel_all failed", error=str(exc), reason=why)
            self.store.record_event("ERROR", "cancel", f"cancel_all failed: {exc}")
            return False
        for u in cancelled:
            self.store.set_order_status(u, "cancelled")
            self.known_order_uuids.discard(u)
        if cancelled:
            log.info("DRY RUN: would cancel all pair orders" if self.dry_run else "cancelled all pair orders", uuids=cancelled, reason=why)
        return True

    def _while_paused(self, snap: MarketSnapshot, now: float) -> bool:
        """Returns True when the bot resumed this cycle (quoting may continue)."""
        # Reaching here means reference, RPC, balances and order state are all healthy this cycle.
        if snap.orders and not self.dry_run:
            self._cancel_everything("still paused")
        self.pause.mark(True, now)
        if self.operator_paused:
            log.info("paused by operator; conditions healthy", pause_reason=self.pause.reason,
                     paused_for=f"{now - (self.pause.paused_at or now):.0f}s")
            return False
        resume_asap = getattr(self, "_resume_asap", False)
        if self.pause.can_resume(now) or resume_asap:
            self._resume_asap = False
            log.info("conditions healthy; resuming", paused_for=f"{now - (self.pause.paused_at or now):.0f}s",
                     operator=resume_asap)
            self.store.record_event("INFO", "resume", f"resumed after: {self.pause.reason}")
            self.pause.resume()
            self.price_monitor.reset()
            self.order_errors.success()
            self.sm.transition(BotState.ACTIVE, "recovered", now=now)
            return True
        left = self.pause.seconds_until_resume(now)
        healthy_for = now - self.pause.healthy_since if self.pause.healthy_since is not None else 0.0
        log.info("paused; conditions healthy this cycle", resume_in=f"{left:.0f}s" if left is not None else "n/a",
                 healthy_for=f"{healthy_for:.0f}s", paused_for=f"{now - (self.pause.paused_at or now):.0f}s",
                 pause_reason=self.pause.reason)
        return False

    def _on_rpc_failure(self, exc: KdfError, now: float) -> None:
        tripped = self.rpc_failures.failure()
        log.error("KDF RPC failure", error=str(exc), consecutive=self.rpc_failures.count, limit=self.rpc_failures.limit)
        self.store.record_event("ERROR", "rpc", str(exc))
        self.pause.mark(False, now)
        if tripped:
            self.targets = None
            if self.sm.state is not BotState.RPC_ERROR:
                self.sm.transition(BotState.RPC_ERROR, f"{self.rpc_failures.count} consecutive RPC failures", now=now)
                self.pause.pause("KDF RPC unavailable", now)

    # ---------------------------------------------------------------- quote
    def _quote(self, ref: ReferencePrice | None, snap: MarketSnapshot, now: float) -> None:
        assert ref is not None
        cfg = self.cfg
        fair = ref.fair_rxd_per_ltc
        inv = Inventory(snap.rxd.spendable, snap.ltc.spendable, fair)
        rxd_pct = inv.rxd_value_pct
        skew = compute_skew_pct(rxd_pct, cfg.inventory, cfg.inventory_skew)
        quote_bid, quote_ask = side_permissions(rxd_pct, cfg.inventory)
        sizes = compute_order_sizes(cfg.order_sizing, cfg.reserve, rxd_spendable=snap.rxd.spendable, ltc_spendable=snap.ltc.spendable,
                                    rxd_max_maker_vol=snap.rxd_max_maker_vol, ltc_max_maker_vol=snap.ltc_max_maker_vol)
        targets = compute_target_quotes(
            fair, bid_offset_pct=cfg.pricing.bid_offset_pct, ask_offset_pct=cfg.pricing.ask_offset_pct, skew_pct=skew,
            min_edge_pct=cfg.pricing.min_edge_pct, max_deviation_pct=cfg.safety.max_quote_deviation_pct,
            quote_bid=quote_bid and sizes.bid_ltc_amount > 0, quote_ask=quote_ask and sizes.ask_rxd_amount > 0,
        )
        self.targets = targets
        log.info("targets", fair=decimal_to_str(fair, 0),
                 bid_rxd_per_ltc=decimal_to_str(targets.bid_rxd_per_ltc, 0) if targets.bid_rxd_per_ltc else None,
                 ask_rxd_per_ltc=decimal_to_str(targets.ask_rxd_per_ltc, 0) if targets.ask_rxd_per_ltc else None,
                 bid_ltc_per_rxd=decimal_to_str(targets.bid_ltc_per_rxd, 12) if targets.bid_ltc_per_rxd else None,
                 ask_ltc_per_rxd=decimal_to_str(targets.ask_ltc_per_rxd, 12) if targets.ask_ltc_per_rxd else None,
                 inventory_rxd_pct=f"{rxd_pct:.1f}", skew_pct=f"{skew:.3f}", clamped=targets.clamped,
                 bid_size_ltc=decimal_to_str(sizes.bid_ltc_amount), ask_size_rxd=decimal_to_str(sizes.ask_rxd_amount),
                 quote_bid=quote_bid, quote_ask=quote_ask, notes=sizes.notes)
        if not quote_bid:
            log.info("inventory bound: RXD share above max, not bidding", rxd_pct=f"{rxd_pct:.1f}")
        if not quote_ask:
            log.info("inventory bound: RXD share below min, not asking", rxd_pct=f"{rxd_pct:.1f}")

        # Safety control #8: never send a quote outside the envelope, no matter how it was produced.
        for side, price in ((Side.BID, targets.bid_rxd_per_ltc), (Side.ASK, targets.ask_rxd_per_ltc)):
            if price is not None and not quote_is_safe(price, side, fair, cfg.safety.max_quote_deviation_pct):
                raise UnsafeCondition(f"{side.value} target {price:.0f} exceeds max deviation {cfg.safety.max_quote_deviation_pct}% from fair {fair:.0f}")

        bid = OrderIntent(Side.BID, targets.bid_rxd_per_ltc, sizes.bid_ltc_amount) if targets.bid_rxd_per_ltc else None
        ask = OrderIntent(Side.ASK, targets.ask_rxd_per_ltc, sizes.ask_rxd_amount) if targets.ask_rxd_per_ltc else None
        plan = build_plan(cfg=cfg, fair_rxd_per_ltc=fair, bid=bid, ask=ask, existing=snap.orders, orderbook=snap.orderbook, now=now)
        self.last_plan = plan
        for note in plan.notes:
            log.info("planner", note=note)
        if plan.ambiguous:
            raise UnsafeCondition(plan.ambiguous)
        self._log_existing(snap)
        self._execute(plan, snap, now)

    def _log_existing(self, snap: MarketSnapshot) -> None:
        base, quote = self.cfg.pair.base, self.cfg.pair.quote
        for o in snap.orders:
            side = o.side_for(base, quote)
            log.info("existing order", side=side.value if side else None, uuid=o.uuid,
                     price_rxd_per_ltc=decimal_to_str(o.price_rxd_per_ltc(base, quote), 0),
                     remaining=decimal_to_str(o.available_base_amount), of=decimal_to_str(o.max_base_vol),
                     unit=quote if side is Side.BID else base, age_s=int(self.clock() - o.created_at_seconds()), matches=o.matches)
        if snap.orderbook is not None:
            ba, bb = snap.orderbook.best_foreign_ask_ltc_per_rxd(), snap.orderbook.best_foreign_bid_ltc_per_rxd()
            log.info("orderbook", foreign_asks=len([e for e in snap.orderbook.asks if not e.is_mine]),
                     foreign_bids=len([e for e in snap.orderbook.bids if not e.is_mine]),
                     best_foreign_ask_rxd_per_ltc=decimal_to_str(rxd_per_ltc_to_ltc_per_rxd(ba), 0) if ba else None,
                     best_foreign_bid_rxd_per_ltc=decimal_to_str(rxd_per_ltc_to_ltc_per_rxd(bb), 0) if bb else None)

    # -------------------------------------------------------------- execute
    def _execute(self, plan: Plan, snap: MarketSnapshot, now: float) -> None:
        mutations = plan.mutations
        for a in plan.actions:
            if a.kind == "keep":
                log.info("ACTION: keep %s", a.side.value, uuid=a.uuid, reason=a.reason)
        if not mutations:
            return
        if self.sm.state is BotState.ACTIVE:
            self.sm.transition(BotState.REPRICING, "orders need changes", now=now)
        prefix = "DRY RUN: would" if self.dry_run else "will"
        failed_sides: set[Side] = set()
        # cancels first, then places; a failed cancel blocks the place on that side (no duplicates).
        for a in [x for x in mutations if x.kind == "cancel"]:
            log.info("ACTION: %s cancel %s", prefix, a.side.value, uuid=a.uuid, reason=a.reason)
            try:
                self.kdf.cancel_order(a.uuid or "")
                self.store.set_order_status(a.uuid or "", "cancelled")
                self.known_order_uuids.discard(a.uuid or "")
                if not self.dry_run:
                    snap.orders = [o for o in snap.orders if o.uuid != a.uuid]  # keep the status snapshot current
                self.order_errors.success()
            except KdfError as exc:
                failed_sides.add(a.side)
                self._order_error("cancel", a, exc, now)
        for a in [x for x in mutations if x.kind == "place"]:
            if a.side in failed_sides:
                log.warning("skipping placement because the cancel on this side failed", side=a.side.value)
                continue
            assert a.price_rxd_per_ltc is not None and a.amount is not None
            unit = "LTC" if a.side is Side.BID else "RXD"
            log.info("ACTION: %s place %s", prefix, a.side.value, price_rxd_per_ltc=decimal_to_str(a.price_rxd_per_ltc, 0),
                     amount=f"{decimal_to_str(a.amount)} {unit}", reason=a.reason)
            try:
                if a.side is Side.BID:
                    order = self.kdf.place_bid(a.price_rxd_per_ltc, a.amount)
                else:
                    order = self.kdf.place_ask(a.price_rxd_per_ltc, a.amount)
                self.order_errors.success()
                if not self.dry_run:
                    self.known_order_uuids.add(order.uuid)
                    self.store.record_order(order.uuid, a.side.value, a.price_rxd_per_ltc, a.amount, now, "open")
                    snap.orders.append(order)  # so /status shows the new order without waiting a cycle
                    log.info("order placed", side=a.side.value, uuid=order.uuid)
            except KdfError as exc:
                self._order_error("place", a, exc, now)
        if self.sm.state is BotState.REPRICING:
            self.sm.transition(BotState.ACTIVE, "orders updated", now=now)

    def _order_error(self, what: str, a: Action, exc: KdfError, now: float) -> None:
        tripped = self.order_errors.failure()
        log.error("order %s failed", what, side=a.side.value, uuid=a.uuid, error=str(exc), consecutive=self.order_errors.count)
        self.store.record_event("ERROR", "order", f"{what} {a.side.value} failed: {exc}")
        if tripped:
            raise UnsafeCondition(f"{self.order_errors.count} consecutive order {what}/placement errors")

    # --------------------------------------------------------------- status
    def status_line(self) -> str:
        snap, t = self.snapshot, self.targets
        rxd = f"{snap.rxd.spendable:.0f}" if snap else "n/a"
        ltc = f"{snap.ltc.spendable:.4f}" if snap else "n/a"
        fair = format_rxd_per_ltc(self.reference.fair_rxd_per_ltc) if self.reference else "n/a"
        bid = format_rxd_per_ltc(t.bid_rxd_per_ltc) if t and t.bid_rxd_per_ltc else "-"
        ask = format_rxd_per_ltc(t.ask_rxd_per_ltc) if t and t.ask_rxd_per_ltc else "-"
        live = ""
        if snap:
            g = group_by_side(snap.orders, self.cfg.pair.base, self.cfg.pair.quote)
            live = f" orders={len(g[Side.BID])}b/{len(g[Side.ASK])}a"
        mode = " DRY-RUN" if self.dry_run else ""
        return (f"{self.cfg.pair.base}/{self.cfg.pair.quote} fair={fair} bid={bid} ask={ask} "
                f"{self.cfg.pair.base}={rxd} {self.cfg.pair.quote}={ltc}{live} state={self.sm.state.value}{mode}")

    def _publish_status(self) -> None:
        now = self.clock()
        snap, t, ref = self.snapshot, self.targets, self.reference
        orders: dict[str, Any] = {"bid": None, "ask": None}
        if snap:
            g = group_by_side(snap.orders, self.cfg.pair.base, self.cfg.pair.quote)
            for side, lst in g.items():
                if lst:
                    o = lst[0]
                    orders[side.value] = {"uuid": o.uuid, "price_rxd_per_ltc": str(o.price_rxd_per_ltc(self.cfg.pair.base, self.cfg.pair.quote)),
                                          "amount": str(o.available_base_amount), "created_at": o.created_at_seconds()}
            if orders["bid"] and orders["ask"] and self.sm.state in (BotState.ACTIVE, BotState.REPRICING):
                self.stats = self.store.add_stats(two_sided_seconds=self.cfg.poll_interval_seconds)
        providers: dict[str, Any] = {}
        for p in self.providers:
            providers[p.name] = {"healthy": p.health.healthy, "consecutive_failures": p.health.consecutive_failures,
                                 "last_error": p.health.last_error,
                                 "price_rxd_per_ltc": str(ref.source_prices[p.name]) if ref and p.name in ref.source_prices else None}
        status = {
            "state": self.sm.state.value, "state_reason": self.sm.reason, "dry_run": int(self.dry_run),
            "pair": f"{self.cfg.pair.base}/{self.cfg.pair.quote}", "base": self.cfg.pair.base, "quote": self.cfg.pair.quote,
            "poll_interval_seconds": self.cfg.poll_interval_seconds, "started_at": self.started_at, "now": now,
            "paused": int(self.pause.paused), "paused_reason": self.pause.reason, "operator_paused": int(self.operator_paused),
            "targets_detail": ({
                "skew_pct": str(t.skew_pct), "mid_rxd_per_ltc": str(t.mid_rxd_per_ltc), "clamped": t.clamped,
                "bid_ltc_per_rxd": str(t.bid_ltc_per_rxd) if t.bid_ltc_per_rxd else None,
                "ask_ltc_per_rxd": str(t.ask_ltc_per_rxd) if t.ask_ltc_per_rxd else None,
            } if t else None),
            "orderbook": ({
                "foreign_asks": len([e for e in snap.orderbook.asks if not e.is_mine]),
                "foreign_bids": len([e for e in snap.orderbook.bids if not e.is_mine]),
                "best_foreign_ask_rxd_per_ltc": str(rxd_per_ltc_to_ltc_per_rxd(snap.orderbook.best_foreign_ask_ltc_per_rxd()))
                if snap.orderbook.best_foreign_ask_ltc_per_rxd() else None,
                "best_foreign_bid_rxd_per_ltc": str(rxd_per_ltc_to_ltc_per_rxd(snap.orderbook.best_foreign_bid_ltc_per_rxd()))
                if snap.orderbook.best_foreign_bid_ltc_per_rxd() else None,
                "asks": [{"price_rxd_per_ltc": str(e.price_rxd_per_ltc), "rxd": str(e.base_max_volume), "mine": e.is_mine}
                         for e in snap.orderbook.asks[:15]],
                "bids": [{"price_rxd_per_ltc": str(e.price_rxd_per_ltc), "rxd": str(e.base_max_volume), "mine": e.is_mine}
                         for e in snap.orderbook.bids[:15]],
            } if snap and snap.orderbook else None),
            "last_plan": ([{"kind": a.kind, "side": a.side.value, "reason": a.reason, "uuid": a.uuid,
                            "price_rxd_per_ltc": str(a.price_rxd_per_ltc) if a.price_rxd_per_ltc else None,
                            "amount": str(a.amount) if a.amount else None} for a in self.last_plan.actions]
                          if self.last_plan else []),
            "recent_events": self.store.recent_events(15),
            "resume_in_seconds": self.pause.seconds_until_resume(now),
            "fair_price_rxd_per_ltc": str(ref.fair_rxd_per_ltc) if ref else None,
            "target_bid_rxd_per_ltc": str(t.bid_rxd_per_ltc) if t and t.bid_rxd_per_ltc else None,
            "target_ask_rxd_per_ltc": str(t.ask_rxd_per_ltc) if t and t.ask_rxd_per_ltc else None,
            "reference_sources": ref.number_of_sources if ref else 0,
            "reference_disagreement_pct": str(ref.disagreement_pct) if ref else None,
            "reference_reason": self.last_aggregation.reason if self.last_aggregation else None,
            "balance_rxd": str(snap.rxd.spendable) if snap else None,
            "balance_ltc": str(snap.ltc.spendable) if snap else None,
            "address_rxd": snap.rxd.address if snap else None,
            "address_ltc": snap.ltc.address if snap else None,
            "inventory_target_pct": str(self.cfg.inventory.target_rxd_value_pct),
            "inventory_min_pct": str(self.cfg.inventory.min_rxd_value_pct),
            "inventory_max_pct": str(self.cfg.inventory.max_rxd_value_pct),
            "inventory_rxd_value_pct": str(Inventory(snap.rxd.spendable, snap.ltc.spendable, ref.fair_rxd_per_ltc).rxd_value_pct) if snap and ref else None,
            "orders": orders, "uptime_seconds": now - self.started_at, "cycles_total": self.cycles,
            "swaps_total": str(self.stats.get("swaps_total", 0)), "swaps_failed_total": str(self.stats.get("swaps_failed", 0)),
            "rxd_bought_total": str(self.stats.get("rxd_bought", 0)), "rxd_sold_total": str(self.stats.get("rxd_sold", 0)),
            "ltc_spent_total": str(self.stats.get("ltc_spent", 0)), "ltc_received_total": str(self.stats.get("ltc_received", 0)),
            "two_sided_seconds_total": str(self.stats.get("two_sided_seconds", 0)),
            "rpc_failures_total": self.rpc_failures.total, "order_errors_total": self.order_errors.total,
            "providers": providers, "price_unit": "RXD_PER_LTC",
        }
        with self._lock:
            self._status = status

    def status_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._status)


class _Wait(UnsafeCondition):
    """Startup-only: no reference yet, but nothing to cancel and no pause needed."""
