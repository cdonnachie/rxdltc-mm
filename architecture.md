# Architecture

`rxdltc-mm` is a single-process Python 3.12 service that keeps one maker bid
and one maker ask on the RXD/LTC market of GLEEC DEX through the Komodo DeFi
Framework (KDF, formerly mm2) JSON RPC. Its goal is *useful, safe two-sided
liquidity*, not profit maximisation.

```
                     +-----------------------------------------------------------+
  CoinGecko  ------> |  prices/                                                  |
  CoinPaprika -----> |   providers.py  -> LegQuote(RXD usd, LTC usd, timestamp)  |
  Gleec CEX  ------> |   aggregator.py -> ReferencePrice (median RXD per LTC)     |
  generic HTTP ----> |                                                           |
                     +-----------------------------+-----------------------------+
                                                   |
                                                   v
+---------------------------+      +-----------------------------------------+
| engine/                   |      | engine/bot.py  LiquidityBot.cycle()      |
|  state.py    StateMachine |<---->|  1 reference + price-move breaker        |
|  safety.py   breakers     |      |  2 read KDF (balances/orders/book/swaps) |
|  planner.py  pure plan    |<-----|  3 swaps -> stats                        |
+---------------------------+      |  4 sanity + ambiguity checks             |
                                   |  5 paused? cancel/track recovery         |
+---------------------------+      |  6 inventory skew, sizes, target quotes  |
| inventory.py  sizing.py   |<-----|  7 plan + execute (cancel then place)    |
| pricing.py (canonical)    |      |  8 persist, metrics, status line         |
+---------------------------+      +-------------------+---------------------+
                                                       |
                        +------------------------------+-----------------------------+
                        v                              v                             v
             +--------------------+        +----------------------+       +--------------------+
             | kdf/               |        | persistence.py       |       | metrics.py         |
             |  rpc.py  transport |        |  SQLite (WAL)        |       |  /status /metrics  |
             |  client.py KdfClient        |  swaps, orders,      |       |  /healthz          |
             |  dry_run.py wrapper|        |  balances, refs,     |       |  localhost:9109    |
             |  fake.py  simulator|        |  events, stats, state|       +--------------------+
             +---------+----------+        +----------------------+
                       |
                       v
             KDF / mm2 (127.0.0.1:7783)  --->  GLEEC DEX P2P orderbook, atomic swaps
```

## Canonical price unit

Every price inside the bot, in config, logs, persistence and metrics is
**RXD per LTC** (`Decimal`). See the module docstring of
[`src/rxdltc_mm/pricing.py`](src/rxdltc_mm/pricing.py) for the full
reasoning. Two facts follow from that unit and drive the whole design:

1. A larger number means RXD is cheaper.
2. The RXD **bid** (bot buys RXD) is therefore quoted *above* fair
   (`fair * (1 + bid_offset)`) and the RXD **ask** (bot sells RXD) *below* fair
   (`fair * (1 - ask_offset)`). In LTC-per-RXD terms this is the usual
   `bid < fair < ask`.

KDF's native price is always "rel per base". The mapping is confined to one
function, `build_setprice_request` in
[`src/rxdltc_mm/kdf/client.py`](src/rxdltc_mm/kdf/client.py):

| bot order | KDF `setprice`            | KDF price          | conversion |
|-----------|---------------------------|--------------------|------------|
| RXD bid   | `base=LTC rel=RXD`        | RXD per LTC        | none       |
| RXD ask   | `base=RXD rel=LTC`        | LTC per RXD        | `1 / p`    |

`tests/test_direction.py` locks this down.

## Modules

| module | responsibility | pure? |
|--------|----------------|-------|
| `config.py` | pydantic models for the YAML, `.env` loader, live-trading double opt-in | yes |
| `logging_setup.py` | text/JSON formatter, secret redaction filter, `log.info("msg", key=value)` adapter | yes |
| `pricing.py` | canonical unit, conversions, `compute_target_quotes`, `quote_is_safe` | yes |
| `inventory.py` | RXD value share, skew, side permissions (min/max bounds) | yes |
| `sizing.py` | reserve-aware fixed / percentage sizing, `max_maker_vol` cap, minimums | yes |
| `prices/base.py` | `PriceProvider` ABC with health bookkeeping, `LegQuote`, `ProviderResult` | I/O in subclasses |
| `prices/providers.py` | CoinGecko, CoinPaprika, Gleec CEX, generic HTTP (JSON path) | I/O |
| `prices/aggregator.py` | staleness, synthetic quotes, outlier rejection, min sources, disagreement, median | yes |
| `kdf/rpc.py` | HTTP transport for legacy and mmrpc 2.0 calls; `userpass` injected here, never logged | I/O |
| `kdf/models.py` | `Balance`, `MakerOrder`, `Orderbook`, `SwapInfo` parsers incl. v1/v2 swap envelopes | yes |
| `kdf/client.py` | `KdfClient`: the trading abstraction; the only place that knows RPC method names | I/O |
| `kdf/dry_run.py` | read-through wrapper that logs and fakes every mutation | no I/O for mutations |
| `kdf/fake.py` | in-memory KDF for tests and `--simulate` | yes |
| `engine/state.py` | `BotState` enum, transition table, logged transitions | yes |
| `engine/safety.py` | `PriceMoveMonitor`, `FailureCounter`, `PauseController` (cooldown + recovery) | yes |
| `engine/planner.py` | `build_plan`: keep / cancel / place / skip decisions, crossing check, ambiguity | yes |
| `engine/bot.py` | `LiquidityBot`: the loop, reconciliation, swap tracking, execution, status | orchestrates |
| `persistence.py` | SQLite store (kv, reference_prices, balances, orders, swaps, events) | I/O |
| `metrics.py` | `/status` JSON, `/metrics` Prometheus, `/healthz` on a daemon thread | I/O |
| `cli.py` | argument parsing, wiring, signals, `--simulate`, `--once`, `--check-config` | glue |

Pure modules contain all the decision logic and are what the unit tests
exercise. I/O modules are thin.

## State machine

```
STARTING --(reference + KDF ok)--> ACTIVE <--> REPRICING
   |                                  ^  |
   +--(no reference yet)--> WAITING_FOR_REFERENCE
                                      |  |
        any breaker -----------------+  +--> PAUSED --(cooldown && healthy for recovery_seconds)--> ACTIVE
        RPC failures >= limit ----------> RPC_ERROR --(RPC back)--> PAUSED
        SIGTERM / --once done ----------> SHUTTING_DOWN
```

* `WAITING_FOR_REFERENCE` is a startup-only state: no orders are wanted yet,
  so no cooldown is needed. Any order left over from a previous run is
  cancelled while waiting.
* `PAUSED` is entered by every circuit breaker. On entry the bot calls
  `cancel_all_orders` for both directions of the pair (in dry-run it logs the
  intent). While paused it keeps polling everything, re-cancels anything that
  reappears, and resumes only after `cooldown_seconds` **and**
  `recovery_seconds` of consecutive healthy cycles.
* `RPC_ERROR` cannot cancel anything (KDF is unreachable). When KDF comes back
  the bot goes to `PAUSED` first so that stale orders are cancelled and a
  cooldown applies before quoting again.
* `REPRICING` is transient within a cycle (visible in metrics and logs).

## One cycle in detail

1. **Reference.** Every provider is polled. Legs older than
   `reference_stale_seconds` or non-positive are rejected. Providers with both
   legs form `ltc_usd / rxd_usd`. If `allow_synthetic_quotes`, single-leg
   providers (Gleec CEX only lists LTC) borrow the median of the missing leg.
   With three or more quotes, outliers beyond `outlier_rejection_pct` are
   dropped. `min_valid_providers` and `max_provider_disagreement_pct` are then
   enforced and the median becomes `fair`. The `PriceMoveMonitor` compares
   `fair` with every observation inside `max_price_move_window_seconds`.
2. **KDF read.** `my_balance` x2, `my_orders`, `orderbook`, `my_recent_swaps`,
   `max_maker_vol` x2. On the first cycle also `version`, `get_enabled_coins`,
   optional `electrum` activation and order reconciliation. Any transport or
   RPC error increments the RPC failure counter.
3. **Swaps.** New or newly finished swaps on the pair update statistics
   (RXD bought/sold, LTC spent/received) and the SQLite `swaps` table.
4. **Checks.** Negative or non-finite balances and more than one order on a
   side are unsafe conditions.
5. **Paused handling** as above.
6. **Quotes.** `Inventory` values RXD at fair; `compute_skew_pct` produces a
   signed skew that shifts the midpoint (RXD-heavy: quote more RXD per LTC on
   both sides). `side_permissions` drops the bid above `max_rxd_value_pct` and
   the ask below `min_rxd_value_pct`. `compute_order_sizes` applies reserves
   and minimums. `compute_target_quotes` applies offsets, then clamps to the
   `min_edge_pct` / `max_quote_deviation_pct` envelope. `quote_is_safe` is a
   final independent check (breaker #8).
7. **Plan and execute.** `build_plan` compares each side's desired order with
   the existing one: keep when within `reprice_threshold_pct` and
   `resize_threshold_pct`; defer when younger than
   `minimum_order_lifetime_seconds`; cancel+place otherwise; cancel
   unconditionally when the existing quote is outside the envelope; never
   touch an order that is matching. Crossing is checked against the best
   *foreign* quote and either skipped or adjusted. Execution runs cancels
   before places and never places on a side whose cancel failed, so there can
   be no duplicate order.
8. **Persist and report.** Bot state, balances and the reference go to SQLite;
   the status snapshot feeds `/status` and `/metrics`; a one-line summary is
   logged.

## Why maker-only is inherent

KDF has no post-only flag because it does not need one: `setprice` creates a
maker order that is never matched against other maker orders. Only the taker
methods `buy` and `sell` can cross the book. The bot does not wrap those
methods at all, so it cannot become a taker. The crossing check exists because
a maker order priced through the best foreign quote is a free option for
arbitrage takers, not because KDF would execute it.

## Persistence

SQLite in WAL mode (`persistence.sqlite_path`, or `$MM_BOT_DATA_DIR`).
Tables: `kv` (bot_state, stats), `reference_prices`, `balances`, `orders`
(uuid, side, price, amount, status), `swaps`, `events`. Nothing secret is
stored. On restart `orders` with status `open` are compared with `my_orders`:
still-open ones are adopted, missing ones are marked closed.

## Reuse from the GLEEC repositories

| item | reused how |
|------|-----------|
| KDF RPC method names, request/response shapes | read from `GLEECBTC/komodo-defi-framework` (`rpc/dispatcher/*.rs`, `lp_ordermatch.rs`, `lp_swap/*.rs`, `coins/utxo.rs`); no names invented |
| RXD and LTC coin parameters, electrum servers | `GLEECBTC/coins` (`coins`, `electrums/RXD`, `electrums/LTC`) copied into `config.example.yaml` |
| Gleec CEX API endpoint and response format | from `mm2-client/external_services/gleeccex_service.go` (HitBTC v3 compatible, `/api/3/public/ticker`) |
| mm2-client price service JSON format | supported by `GenericHttpPriceProvider` defaults (`RXD.last_price`, `RXD.last_updated_timestamp`) |
| KDF built-in `start_simple_market_maker_bot` and mm2-client's Go bot | **not** wrapped, see below |

The Go `mm2-client` cannot be imported from Python and its bot (like KDF's
built-in `simple_market_maker.rs`, which is the same design ported to Rust)
places one-sided sell orders at `price * spread` from a single price URL, with
no inventory management, no cross-provider validation, no circuit breakers
and no dry-run. Wrapping it would have meant two independent one-sided bots
(RXD/LTC and LTC/RXD) with none of the required safety behaviour, so the loop
was written fresh and kept small.
