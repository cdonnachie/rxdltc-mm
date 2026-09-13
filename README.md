# rxdltc-mm — RXD/LTC liquidity bot for GLEEC DEX

A small, conservative market-making service that keeps one maker **bid** and
one maker **ask** on the RXD/LTC market of GLEEC DEX through the Komodo DeFi
Framework (KDF, formerly mm2) RPC.

> **This bot is designed primarily to improve RXD/LTC liquidity, not to
> maximise trading profit.** It quotes a configurable spread around an
> externally derived fair price, rebalances gently with inventory skew, and
> pulls all quotes whenever anything looks wrong.

It controls real funds when run live. Read the whole README, run it in
dry-run first, and use a dedicated liquidity wallet.

---

## Contents

1. [What the bot does](#1-what-the-bot-does)
2. [Architecture](#2-architecture)
3. [KDF / mm2 prerequisites](#3-kdf--mm2-prerequisites)
4. [RXD setup](#4-rxd-setup)
5. [LTC setup](#5-ltc-setup)
6. [Configuration](#6-configuration)
7. [Running in dry-run mode](#7-running-in-dry-run-mode)
8. [Running live](#8-running-live)
9. [Risk controls](#9-risk-controls)
10. [Troubleshooting](#10-troubleshooting)
11. [Example logs](#11-example-logs)
12. [systemd deployment](#12-systemd-deployment)
13. [Docker deployment](#13-docker-deployment)
14. [Security recommendations](#14-security-recommendations)
15. [Price convention (read this)](#15-price-convention-read-this)
16. [KDF RPC methods used and code reuse](#16-kdf-rpc-methods-used-and-code-reuse)
17. [Milestones and status](#17-milestones-and-status)
18. [Development](#18-development)
19. [Desktop control panel](#19-desktop-control-panel)

---

## 1. What the bot does

Every `poll_interval_seconds` (default 30 s) it:

1. fetches RXD and LTC prices from several public sources (CoinGecko,
   CoinPaprika, Gleec CEX, any JSON endpoint), validates freshness and
   agreement, and takes the **median** as the fair RXD/LTC price;
2. reads RXD and LTC balances, its own open maker orders, the order book and
   recent swaps from KDF;
3. computes a target bid and ask around fair value, shifted by inventory skew
   and clamped to a safety envelope;
4. sizes both orders from balances minus a reserve;
5. keeps, cancels or replaces orders only when they drift beyond the reprice
   threshold and are older than the minimum lifetime;
6. cancels everything and pauses when a circuit breaker trips, and resumes
   automatically after a cooldown plus a healthy recovery period;
7. accounts for completed swaps, persists state, serves `/status`,
   `/metrics`, `/events` and an optional token-protected control API
   (`/control/pause|resume|cancel_all|stop`, enabled by `MM_BOT_CONTROL_TOKEN`).

In **dry-run** mode (the default) steps 5 and 6 only *log* what would happen.
Nothing is ever placed or cancelled.

## 2. Architecture

See [architecture.md](architecture.md) for the module map, the state machine
and a step-by-step description of one cycle. In short: pure decision modules
(`pricing`, `inventory`, `sizing`, `prices/aggregator`, `engine/planner`,
`engine/safety`) are exercised by unit tests; thin I/O modules (`kdf/rpc`,
`kdf/client`, `prices/providers`, `persistence`, `metrics`) talk to the
outside world; `engine/bot.py` orchestrates.

## 3. KDF / mm2 prerequisites

You need a running Komodo DeFi Framework node connected to the GLEEC DEX
network. Build it from [GLEECBTC/komodo-defi-framework](https://github.com/GLEECBTC/komodo-defi-framework)
(see its `docs/DEV_ENVIRONMENT.md`) or use a release binary that GLEEC
distributes with its wallet.

Minimal `MM2.json` (see [deploy/MM2.json.example](deploy/MM2.json.example)):

```json
{
  "gui": "rxdltc-mm",
  "netid": 6133,
  "seednodes": ["staking1.gleec.com", "staking2.gleec.com", "seed01.kmdefi.net", "seed03.kmdefi.net"],
  "rpcip": "127.0.0.1",
  "rpcport": 7783,
  "rpc_password": "Change-Me-2026!x",
  "passphrase": "dedicated liquidity wallet seed phrase",
  "dbdir": "./DB",
  "i_am_seed": false
}
```

* `netid` and `seednodes` must match the network your GLEEC DEX wallet uses.
  [GLEECBTC/coins `seed-nodes.json`](https://github.com/GLEECBTC/coins/blob/master/seed-nodes.json)
  lists GLEEC's own seeds (`staking1.gleec.com`, `staking2.gleec.com`,
  `kdfseed1.decker.im`) on netid **6133**; netid 8762 is the general Komodo
  network. Orders are only visible to peers on the same netid. Always set
  `seednodes`: without it KDF starts as an isolated bootstrap node.
* `rpc_password` must satisfy KDF's policy: at least 8 characters with an
  upper-case letter, a lower-case letter, a digit and a special character, no
  character repeated three times in a row, and not containing the word
  "password".
* `rpcip` **must** stay `127.0.0.1` on a host install. The bot talks to it
  locally. Never expose the RPC port to the internet.
* The `coins` file next to KDF must contain `RXD` and `LTC` entries. Use the
  [GLEECBTC/coins](https://github.com/GLEECBTC/coins) `coins` file, which has
  both (RXD: `pubtype 0`, `p2shtype 5`, `txfee 10000000`, `fork_id 0x40`,
  `signature_version fork_id_rxd`, 2 confirmations).
* Start KDF, e.g. `./kdf` (or `./mm2`) in the directory with `MM2.json` and
  `coins`, and confirm it answers:

```bash
curl -s http://127.0.0.1:7783 -d '{"userpass":"<rpc_password>","method":"version"}'
```

The bot uses the same `rpc_password` via the `KDF_RPC_PASSWORD` environment
variable.

## 4. RXD setup

RXD (Radiant) is a UTXO coin activated over Electrum. With
`kdf.activate_coins_on_start: true` (default) the bot activates it on its
first cycle using the servers from the config (taken from
`GLEECBTC/coins/electrums/RXD`):

```yaml
kdf:
  coins:
    RXD:
      electrum:
        - { url: "electrumx.radiant4people.com:50012", protocol: SSL }
        - { url: "electrumx2.radiant4people.com:50012", protocol: SSL }
        - { url: "electrumx.rxd-radiant.com:50012", protocol: SSL }
```

To activate manually instead:

```bash
curl -s http://127.0.0.1:7783 -d '{"userpass":"<rpc_password>","method":"electrum","coin":"RXD",
 "servers":[{"url":"electrumx.radiant4people.com:50012","protocol":"SSL"}]}'
```

The response contains the RXD address of the liquidity wallet. Fund it with
the RXD you want to provide as liquidity, plus a margin for transaction fees
(RXD `txfee` is 0.1 RXD per transaction in the coins file).

## 5. LTC setup

Same procedure with the LTC Electrum servers (`GLEECBTC/coins/electrums/LTC`,
the cipig.net servers are pre-filled). The GLEEC coins file defines two LTC
tickers from the same seed: `LTC` (legacy `L...` address) and `LTC-segwit`
(bech32 `ltc1...` address, what the GLEEC web wallet shows as "LTC SEGWIT").
Both trade in the same RXD/LTC order book. Set `pair.quote` to the one whose
address holds your funds (the example uses `LTC-segwit`). The `kdf.coins`
entry may be keyed as either `LTC` or `LTC-segwit`; both tickers use the same
Electrum servers, so the bot uses whichever entry exists. Fund that LTC address. Keep in mind
that every swap costs an LTC transaction fee and that KDF locks amounts during
in-flight swaps; the `reserve` settings keep 10% of each balance untouched by
default.

Check both balances:

```bash
curl -s http://127.0.0.1:7783 -d '{"userpass":"<rpc_password>","method":"my_balance","coin":"RXD"}'
curl -s http://127.0.0.1:7783 -d '{"userpass":"<rpc_password>","method":"my_balance","coin":"LTC"}'
```

## 6. Configuration

Non-secret settings live in a YAML file; secrets only in the environment.

```bash
cp config.example.yaml config.yaml
cp .env.example .env        # edit KDF_RPC_PASSWORD; never commit .env
```

[config.example.yaml](config.example.yaml) documents every key. The important
groups:

| group | keys | notes |
|-------|------|-------|
| `pricing` | `bid_offset_pct`, `ask_offset_pct` (1.0 each), `min_edge_pct`, `min_valid_providers`, `reference_stale_seconds` (fetch age), `max_source_age_seconds` (source-reported age), `max_provider_disagreement_pct`, `outlier_rejection_pct`, `allow_synthetic_quotes`, `providers[]` | offsets are applied in RXD-per-LTC space, see section 15 |
| `inventory` / `inventory_skew` | `target/min/max_rxd_value_pct`, `max_adjustment_pct` | above `max` the bot stops bidding; below `min` it stops asking |
| `order_sizing` / `reserve` | `mode: balance_percent | fixed`, percentages or fixed amounts, minimums, `min_volume_fraction`, reserves | never 100% of a balance |
| `reprice_threshold_pct`, `resize_threshold_pct`, `minimum_order_lifetime_seconds` | | order churn control |
| `safety` | `max_price_move_pct`, `max_price_move_window_seconds`, `max_quote_deviation_pct`, `rpc_failure_limit`, `order_error_limit`, `cooldown_seconds`, `recovery_seconds` | circuit breakers |
| `trading` | `maker_only` (must be true), `prevent_crossing_book`, `on_cross: skip | adjust`, confirmations | |
| `reconcile` | `adopt_existing_orders`, `cancel_unknown_orders` | restart behaviour |
| `shutdown.cancel_orders_on_exit` | | SIGTERM behaviour |
| `kdf` | `request_timeout_seconds`, `activate_coins_on_start`, `coins.*.electrum` | |
| `persistence.sqlite_path`, `metrics.*`, `logging.*` | | |
| `dry_run` | `true` by default | |

Environment variables (`.env` is loaded automatically):

| variable | purpose |
|----------|---------|
| `KDF_RPC_URL` | default `http://127.0.0.1:7783` |
| `KDF_RPC_PASSWORD` | **required**, the `rpc_password` from `MM2.json` |
| `MM_BOT_CONFIG` | config path (default `config.yaml`) |
| `MM_BOT_CONFIRM_LIVE` | must equal `I_UNDERSTAND_THIS_TRADES_REAL_FUNDS` for live trading |
| `MM_BOT_DRY_RUN` | `true` forces dry-run regardless of config |
| `MM_BOT_DATA_DIR` | directory for the SQLite file |
| `MM_BOT_LOG_LEVEL` | overrides `logging.level` |
| `COINGECKO_API_KEY`, `COINPAPRIKA_API_KEY` | optional |
| `MM_BOT_CONTROL_TOKEN` | enables the HTTP control API; requests must send `Authorization: Bearer <token>` (the desktop app sets this per launch) |

Validate: `rxdltc-mm --check-config`.

### Reference price providers

| type | legs | notes |
|------|------|-------|
| `coingecko` | RXD, LTC (USD) | `/api/v3/simple/price?ids=radiant,litecoin` |
| `coinpaprika` | RXD, LTC (USD) | `/v1/tickers/rxd-radiant`, `ltc-litecoin` |
| `gleec_cex` | LTC only (USDT) | Gleec CEX does not list RXD as of 2026-09; used as a synthetic quote by combining its LTC leg with the median RXD leg of the other providers. Set `rxd_symbol` if RXD gets listed. |
| `generic_http` | configurable | dotted JSON paths; one `url` for both legs or `rxd_url`/`ltc_url` per leg; a list of paths (e.g. `["bid","ask"]`) is averaged into a mid price; defaults match the mm2-client / cipig `tickers` format |
| `nonkyc` (a `generic_http` entry in the example) | RXD, LTC (USDT) | the live RXD/USDT market. Checked 2026-09: CoinGecko lists only two RXD markets and flags the larger (MEXC) as stale, so the aggregators can lag the real price by 5-10%; NonKYC's bid/ask mid plus outlier rejection keeps the reference honest |

Fair price = `median` of the RXD-per-LTC quotes that survive staleness,
outlier and disagreement checks. `min_valid_providers: 2` by default.
Synthetic quotes (Gleec CEX) carry no independent RXD information: they use
the median RXD leg of the complete providers, so they always sit in the
middle and count toward the minimum but never break a tie.

## 7. Running in dry-run mode

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install .                 # or: pip install -e .[dev]
rxdltc-mm --config config.yaml
```

With `dry_run: true` the bot connects to KDF, activates coins if needed,
reads balances, order book and orders, fetches reference prices and logs the
targets and every action it *would* take:

```
INFO bot: reference price fair_rxd_per_ltc=2779255.32 sources=3 disagreement_pct=1.375 ...
INFO bot: targets fair=2779255 bid_rxd_per_ltc=2809192 ask_rxd_per_ltc=2753565 ...
INFO bot: existing order side=bid uuid=... price_rxd_per_ltc=2740000 remaining=0.045 ...
INFO bot: ACTION: keep ask uuid=... reason="within thresholds (price dev 0.057%, size dev 0.0%)"
INFO bot: ACTION: DRY RUN: would cancel bid uuid=... reason="existing quote 2740000 is outside the safety envelope"
INFO bot: ACTION: DRY RUN: would place bid price_rxd_per_ltc=2809192 amount="0.045 LTC" reason="replacement quote"
INFO bot: RXD/LTC fair=2.779M bid=2.809M ask=2.754M RXD=300000 LTC=0.1000 orders=1b/1a state=ACTIVE DRY-RUN
```

Useful flags:

* `--once` runs a single cycle and exits (good for a first connectivity test);
* `--simulate --cycles 3` runs entirely offline against an in-memory KDF and
  static price feeds (no node needed) — this is how
  [docs/sample_dry_run.log](docs/sample_dry_run.log) was produced;
* `--dry-run` forces dry-run whatever the config says.

Let it run for a while and confirm: the fair price agrees with what you see
on exchanges, the bid is *above* fair and the ask *below* fair in RXD-per-LTC
terms (section 15), the sizes are what you expect, and the status line shows
`state=ACTIVE`.

## 8. Running live

Live trading is a double opt-in:

1. `dry_run: false` in the YAML, **and**
2. `MM_BOT_CONFIRM_LIVE=I_UNDERSTAND_THIS_TRADES_REAL_FUNDS` in the environment.

Without both the bot stays in dry-run and logs why. Start small: a fixed
sizing mode with tiny amounts, then grow.

```yaml
order_sizing:
  mode: fixed
  rxd_order_amount: 20000
  ltc_order_amount: 0.01
```

On startup the bot reads `my_orders` and **adopts** one existing order per
side instead of placing duplicates (`reconcile.adopt_existing_orders`). Two
orders on a side is ambiguous: the bot cancels them and pauses, unless
`reconcile.cancel_unknown_orders: true`.

On `SIGTERM`/`SIGINT` it stops quoting, cancels open orders if
`shutdown.cancel_orders_on_exit` is true, persists state and exits. Orders
that are currently matching cannot be cancelled by KDF; the swap completes
and the bot picks it up after restart.

## 9. Risk controls

The bot **cancels all pair orders and enters `PAUSED`** when:

| # | trigger | config |
|---|---------|--------|
| 1 | no valid reference price | `pricing.min_valid_providers` |
| 2 | quote fetched too long ago, or source-reported update time too old | `pricing.reference_stale_seconds`, `pricing.max_source_age_seconds` |
| 3 | fewer healthy sources than required | `pricing.min_valid_providers` |
| 4 | fair price moved more than X% inside the window | `safety.max_price_move_pct`, `max_price_move_window_seconds` |
| 4b | fair price sits too far from the slow anchor (median of the last hour) | `safety.anchor_enabled`, `anchor_window_seconds`, `max_anchor_deviation_pct`, `anchor_min_span_seconds` |
| 5 | KDF RPC unavailable (`RPC_ERROR`, then `PAUSED` on recovery) | `safety.rpc_failure_limit` |
| 6 | balance data cannot be trusted (negative / non-finite) | |
| 7 | ambiguous order state (more than one order per side, or a failed cancel followed by uncertainty) | `reconcile.cancel_unknown_orders` |
| 8 | a computed quote exceeds the maximum deviation from fair or lands on the wrong side of fair | `safety.max_quote_deviation_pct`, `pricing.min_edge_pct` |
| 9 | repeated order placement/cancellation errors | `safety.order_error_limit` |
| 10 | providers disagree beyond tolerance (after outlier rejection) | `pricing.max_provider_disagreement_pct`, `outlier_rejection_pct` |

While paused it keeps monitoring, re-cancels anything that reappears, and
resumes only after `safety.cooldown_seconds` **and** `safety.recovery_seconds`
of consecutive healthy cycles.

**The slow anchor** deserves a note of its own. RXD trades a few thousand
dollars a day on a single live market, so a few hundred dollars of buying can
move the reference by tens of percent. Every accepted fair price is recorded,
and quoting stops while the current one sits further than
`max_anchor_deviation_pct` from the median of the last
`anchor_window_seconds`. Because that median is itself rolling, a genuine
move is followed rather than blocked: a step change pauses quoting for about
half the window, and a steady trend is tolerated up to roughly the limit per
half-window, about 10% per 30 minutes with the defaults. History is seeded
from the database on startup, so a restart does not reset the protection.

Additional protections that are always on:

* **Maker-only by construction.** Only `setprice` is used. KDF never matches
  maker orders against each other; only `buy`/`sell` (never called) can take.
* **No crossing.** Before placing, the best *foreign* quote in the book is
  checked; a crossing order is skipped (or moved just inside, with
  `trading.on_cross: adjust`) and never placed outside the envelope.
* **Quotes never cross fair value.** `min_edge_pct` keeps the bid at least
  that far above fair and the ask that far below (RXD-per-LTC terms), whatever
  the inventory skew says.
* **Inventory bounds.** Above `max_rxd_value_pct` the bot only sells RXD; below
  `min_rxd_value_pct` it only buys.
* **Reserves.** `reserve.rxd_percent` / `ltc_percent` of each balance is never
  committed; `max_maker_vol` from KDF caps sizes further.
* **Churn control.** `reprice_threshold_pct`, `resize_threshold_pct` and
  `minimum_order_lifetime_seconds` (except when an order is unsafe).
* **No duplicates.** Cancels run before places; a failed cancel blocks the
  place on that side.
* **Restart safety.** Persisted order UUIDs are reconciled with `my_orders`.

## 10. Troubleshooting

| symptom | cause / fix |
|---------|-------------|
| `KDF_RPC_PASSWORD is not set` | create `.env` or export the variable |
| `KDF RPC failure ... connection refused` then `RPC_ERROR` | KDF not running or wrong `KDF_RPC_URL`; the bot keeps retrying and cools down when KDF is back |
| `coin RXD is not enabled and no electrum servers are configured` | add `kdf.coins.RXD.electrum` or activate manually |
| `electrum: ... Deactivated coin due to error in balance querying` | all Electrum servers unreachable; try other servers from `GLEECBTC/coins/electrums` |
| `waiting for a valid reference price` at startup | providers failing/stale; check the `reference source rejected` lines and network access to the APIs |
| `RXD leg stale at source (240s > ...)` | free CoinGecko/CoinPaprika tiers update every few minutes; keep `max_source_age_seconds` at 600 or above |
| `SAFETY TRIGGER: pausing reason="providers disagree by 6.2%"` | RXD is thinly traded and sources drift apart; raise `max_provider_disagreement_pct` cautiously or add a source |
| `ambiguous order state: 2 bid orders on the pair` | another tool placed orders; cancel them (`cancel_all_orders`) or set `reconcile.cancel_unknown_orders: true` |
| `config has dry_run=false but MM_BOT_CONFIRM_LIVE is not set` | intended: set the confirmation phrase only when you mean it |
| bid number is larger than the ask number in the logs | correct, see section 15 |
| `setprice: ... Not enough balance` | reserve too small for KDF's own fee/locked-amount checks; lower the sizing percentage or raise the reserve |
| metrics port in use | change `metrics.port` or disable |
| LTC balance is 0 although the GLEEC web wallet shows LTC | the web wallet uses the **`LTC-segwit`** ticker (`ltc1...` address); plain `LTC` is the legacy `L...` address of the same seed. Set `pair.quote: LTC-segwit` and configure `kdf.coins.LTC-segwit`. RXD has no segwit variant. |
| `Both conf Standard and request Segwit must be either Segwit or Standard/CashAddress` (KDF log) | `address_format: segwit` was requested for the plain `LTC` ticker. This fork only accepts it for a coins-file entry that is segwit itself; use the `LTC-segwit` ticker instead. |
| `order(s) with this wallet's pubkey are on the network but not managed by this KDF instance` | another KDF with the same seed is running (typically the GLEEC web wallet in your browser). Orders placed there are invisible to `my_orders` here, the bot cannot cancel them, and two instances sharing the same UTXOs can make swaps fail. Cancel them in the UI and close it, or give the bot its own seed. |
| the same message persists after cancelling the UI orders | KDF never expires order-book entries carrying its *own* pubkey (`lp_ordermatch.rs`, stale-pubkey purge). If the cancel broadcast was missed, restart `kdf.exe` with the web wallet closed for at least 90 s; peers will have dropped the entries by then. The `paused; conditions healthy this cycle` line means the check now passes and the bot is counting down to resume. |
| `coin is enabled with a different address format than configured; re-activating` | LTC was activated earlier (for example by a previous run) without `address_format: segwit`. When the pair has no open orders the bot calls `disable_coin` and re-activates the coin with the configured format; otherwise it warns and leaves it, so cancel the orders or restart `kdf.exe`. |

Inspect state: `curl -s localhost:9109/status | jq` (state, paused reason,
fair, targets, balances, orders, statistics, provider health). The SQLite
file (`data/mm-bot.sqlite3`) has `events`, `swaps`, `orders`,
`reference_prices` and `balances` tables.

### Which pairs have orders?

`scripts/rxd_markets.py` asks the node's `best_orders` RPC for every pair
involving a coin and prints one line per pair with counts, volumes and the
best prices in the bot's unit:

```powershell
.\.venv\Scripts\python scripts
xd_markets.py            # RXD against everything
.\.venv\Scripts\python scripts
xd_markets.py --coin LTC
```

It needs a running KDF node (`KDF_RPC_URL` / `KDF_RPC_PASSWORD` from `.env`).
The result is this node's view of the network: pairs only appear once the
node has received their order-book gossip, so let it run a minute first.

## 11. Example logs

* [docs/sample_dry_run.log](docs/sample_dry_run.log) — dry-run against the
  simulator with a leftover stale bid (would be cancelled and replaced) and an
  ask within thresholds (kept).
* [docs/sample_live_simulation.log](docs/sample_live_simulation.log) — the
  same scenario in live mode against the simulator, including a taker filling
  half of the ask: `balance change`, `swap completed`, updated statistics and
  the resize (`replace: size dev 35.5% > 25.0%`).
* [docs/sample_once_real_providers.log](docs/sample_once_real_providers.log) —
  `--once` in dry-run with the **real** CoinGecko / CoinPaprika / Gleec CEX
  APIs and a stand-in HTTP server answering KDF-shaped JSON: three sources
  aggregated (3.7% disagreement), a completed swap accounted, a stale ask
  flagged, and a bid skipped because it would cross a foreign ask.

Status line format:

```
RXD/LTC fair=2.779M bid=2.809M ask=2.754M RXD=300000 LTC=0.1000 orders=1b/1a state=ACTIVE
```

State transitions are logged as `STATE ACTIVE -> PAUSED reason="..."`. Set
`logging.format: json` for one JSON object per line.

## 12. systemd deployment

[deploy/rxdltc-mm.service](deploy/rxdltc-mm.service) contains the unit and
the install steps in its header. Key points: runs as an unprivileged user,
secrets in `/etc/rxdltc-mm/env` (mode 600), state in `/var/lib/rxdltc-mm`,
`Restart=on-failure`, `TimeoutStopSec=60` so SIGTERM has time to cancel
orders, and a hardened sandbox (`ProtectSystem=strict`, `NoNewPrivileges`).

```bash
sudo systemctl enable --now rxdltc-mm
journalctl -u rxdltc-mm -f
```

## 13. Docker deployment

[Dockerfile](Dockerfile) builds a slim non-root image;
[docker-compose.yml](docker-compose.yml) runs KDF and the bot on a private
network where the KDF RPC port is *not* published to the host, and exposes the
bot's `/status` on `127.0.0.1:9109` only.

```bash
mkdir kdf && cp deploy/MM2.json.example kdf/MM2.json   # edit; set rpcip to 0.0.0.0 (container-internal only)
curl -L https://raw.githubusercontent.com/GLEECBTC/coins/master/coins -o kdf/coins
cp config.example.yaml config.yaml && cp .env.example .env   # edit
docker compose up -d --build
docker compose logs -f bot
```

`stop_grace_period: 45s` lets the bot cancel orders on `docker compose stop`.

## 14. Security recommendations

* **Dedicated wallet.** Use a fresh seed for the liquidity wallet with only
  the funds you intend to quote. Never the main wallet. Never run two KDF
  instances (for example `kdf.exe` and the GLEEC web wallet) on the same seed
  at the same time: each has its own order state and both spend the same
  UTXOs. The bot pauses if it sees orders carrying its pubkey that it does not
  manage.
* **KDF RPC on localhost.** `rpcip: 127.0.0.1`. Firewall the host anyway:
  `ufw default deny incoming`, allow SSH only. KDF's P2P port (`netid`
  dependent) is outbound-initiated for a non-seed node.
* **No secrets in files that are committed.** `.env`, `config.yaml`, `data/`
  and `*.sqlite3` are git-ignored. `config.yaml` carries no secrets by
  design; `MM2.json` (seed + rpc_password) must live outside the repo with
  mode 600.
* **Log sanitisation.** The RPC transport injects `userpass` at send time and
  never logs request bodies; a redaction filter additionally replaces the
  password and API keys with `***` in every log record.
* **Least privilege.** Run as an unprivileged user (systemd unit / Docker
  image do), read-only config mount, writable state directory only.
* **Metrics endpoint** is read-only and bound to localhost by default. If you
  scrape it remotely, tunnel or restrict by IP; it reveals balances.
* **Double opt-in for live trading** (`dry_run: false` + confirmation env).
* **Update path.** Pin KDF to a version you have tested with; RPC shapes
  change between releases (this bot targets the GLEEC fork at commit
  `d56a7bc`, April 2026).

## 15. Price convention (read this)

The canonical unit is **RXD per LTC** (`pair.canonical_price_unit:
RXD_PER_LTC`). `2800000` means 1 LTC = 2,800,000 RXD.

Because a larger number means *cheaper RXD*, the sides look "reversed"
compared with a market quoted in LTC per RXD:

| | RXD per LTC (canonical) | LTC per RXD (KDF `base=RXD`) |
|---|---|---|
| RXD **bid** — bot **buys** RXD, pays LTC | `fair × (1 + 1%)` = **2,828,000** | 3.5361e-7 (below fair: buys cheaper) |
| fair | 2,800,000 | 3.5714e-7 |
| RXD **ask** — bot **sells** RXD, gets LTC | `fair × (1 − 1%)` = **2,772,000** | 3.6075e-7 (above fair: sells dearer) |

> Note on the original specification: it asked for the canonical unit RXD per
> LTC *and* for `bid = fair × (1 − 1%) = 2,772,000`, `ask = fair × (1 + 1%) =
> 2,828,000` with "bid = bot buys RXD". Those two requirements contradict
> each other: a bot buying RXD at 2,772,000 RXD/LTC receives *fewer* RXD per
> LTC than fair, i.e. it overpays by 1%, and selling at 2,828,000 undersells
> by 1% — both quotes would sit on the wrong side of fair value. The
> implementation keeps the definitions (bid = buy RXD, ask = sell RXD) and
> the canonical unit, and assigns the numbers to the economically correct
> sides as shown above. `tests/test_pricing.py::test_spec_example_numbers_land_on_the_correct_sides`
> and `tests/test_direction.py` verify this and the resulting KDF order
> direction.

KDF mapping (the only place the reciprocal is taken,
`kdf/client.py::build_setprice_request`):

* RXD bid → `setprice base=LTC rel=RXD price=2828000 volume=<LTC amount>`
* RXD ask → `setprice base=RXD rel=LTC price=1/2772000 volume=<RXD amount>`

Every log line shows canonical prices; the `targets` line also prints the
LTC-per-RXD equivalents for cross-checking.

## 16. KDF RPC methods used and code reuse

All method names were taken from the dispatchers in
`GLEECBTC/komodo-defi-framework` (`mm2src/mm2_main/src/rpc/dispatcher/`),
none were invented:

| purpose | method | style |
|---------|--------|-------|
| connectivity | `version` | legacy |
| enabled coins | `get_enabled_coins` | mmrpc 2.0 |
| activate RXD / LTC | `electrum` (servers list, `tx_history: false`) | legacy |
| balances | `my_balance` | legacy |
| maker capacity | `max_maker_vol`, `min_trading_vol` | 2.0 / legacy |
| order book | `orderbook` (`base=RXD, rel=LTC`) | mmrpc 2.0 |
| own orders | `my_orders`, `order_status` | legacy |
| place maker order | `setprice` (`cancel_previous: false`, `min_volume`, confs) | legacy |
| cancel | `cancel_order`, `cancel_all_orders` (`cancel_by: {type: Pair}` for both directions) | legacy |
| swaps | `my_recent_swaps`, `my_swap_status` (both `swap_type`/`swap_data` envelopes and v1/v2 payloads parsed), `active_swaps` | 2.0 / legacy |
| fee preview | `trade_preimage` (`swap_method: setprice`) | mmrpc 2.0 |

Taker methods (`buy`, `sell`) and `update_maker_order` are intentionally not
used (the latter would reprice in place, but replacing keeps the audit trail
and lifetime accounting simple).

**Reused from GLEEC repositories:** RXD/LTC coin parameters and Electrum
servers (`GLEECBTC/coins`), the Gleec CEX public API endpoint and format and
the mm2-client price-service JSON format (`GLEECBTC/mm2-client`), the RPC
contract (`GLEECBTC/komodo-defi-framework`).

**Not reused:** the Go mm2-client bot and KDF's built-in
`start_simple_market_maker_bot`. Both implement the same one-sided
`price × spread` sell loop from a single price URL without inventory
management, multi-source validation, circuit breakers or dry-run; wrapping
them would have required running two independent one-sided bots and would
still lack every safety requirement. Details in
[architecture.md](architecture.md#reuse-from-the-gleec-repositories).

## 17. Milestones and status

| milestone | scope | status |
|-----------|-------|--------|
| 1 — read-only / dry-run | connect, activate, read balances/book/orders, reference price, targets, replace decisions, log intents, never mutate | implemented; default mode; verified against the simulator and unit tests, **not yet verified against a real KDF node** |
| 2 — live maker order management | place/cancel/replace, reconciliation, crossing check, shutdown cancel | implemented behind the double opt-in |
| 3 — hardening | inventory skew and bounds, SQLite persistence, `/status` + Prometheus metrics, swap accounting, systemd/Docker | implemented |

Before enabling live trading:

1. Run dry-run against your real KDF for at least a day. Check every
   `reference price` line against exchanges; check the `targets` line; check
   `existing order` lines match what `my_orders` shows.
2. Run `--once` live with `order_sizing.mode: fixed` and tiny amounts; verify
   the two orders in the GLEEC DEX order book sit on the correct sides and at
   the expected prices; let one get taken; check `swap completed` accounting.
3. Review `safety.*` thresholds for RXD's real volatility and liquidity
   (RXD/USD sources disagreed by ~4% when this was written — the 5% default
   disagreement limit is tight for RXD).
4. Set up monitoring on `/metrics` (`rxdltc_mm_paused`, `rxdltc_mm_state`,
   `rxdltc_mm_provider_healthy`).

## 18. Development

```bash
pip install -e .[dev]
pytest                      # 83 tests
rxdltc-mm --simulate --cycles 5          # offline end-to-end run
rxdltc-mm --check-config --config config.example.yaml
```

Layout:

```
src/rxdltc_mm/
  cli.py  config.py  logging_setup.py  pricing.py  inventory.py  sizing.py
  persistence.py  metrics.py
  prices/   base.py providers.py aggregator.py fake.py
  kdf/      rpc.py models.py client.py dry_run.py fake.py
  engine/   state.py safety.py planner.py bot.py
tests/      one file per concern (pricing, direction, inventory/sizing, aggregator, safety, planner, bot, config/misc)
deploy/     rxdltc-mm.service, MM2.json.example
docs/       sample logs
```

## 19. Desktop control panel

[ui/](ui/) contains a Tauri desktop app (Rust + React) with a first-run
wizard (workspace, pinned and checksum-verified KDF download, coins file,
`MM2.json` with an encrypted wallet, seed backup, deposit addresses with QR
codes) that then starts and supervises both `kdf.exe` and the bot, shows the
dashboard with a health checklist, edits `config.yaml` and `MM2.json`,
streams both logs, and stores the RPC and wallet passwords in the OS
keychain. The bot ships inside the installer as a single-file sidecar, so end
users do not need Python. It talks to the bot only through the localhost status and
control API, so the Python bot stays the single trading component. See
[ui/README.md](ui/README.md) for the safety model, development run
(`npm run tauri dev`) and installer build.
