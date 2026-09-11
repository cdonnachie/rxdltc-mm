"""List every GLEEC DEX pair that currently has orders for a coin (default RXD).

Uses KDF's ``best_orders`` (mmrpc 2.0), which scans all pairs involving the coin,
then fetches the ``orderbook`` for each pair found to count both sides.

    python scripts/rxd_markets.py            # reads KDF_RPC_URL / KDF_RPC_PASSWORD from .env
    python scripts/rxd_markets.py --coin LTC --top 20

Prices are printed as <coin> per 1 <other> (e.g. RXD per LTC), the bot's convention.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from rxdltc_mm.config import load_dotenv, load_secrets  # noqa: E402
from rxdltc_mm.kdf.rpc import KdfError, KdfRpc  # noqa: E402


def dec(v) -> Decimal:
    if isinstance(v, dict):
        v = v.get("decimal")
    return Decimal(str(v))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--coin", default="RXD")
    ap.add_argument("--top", type=int, default=50, help="max orders per side to request")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--json", action="store_true", help="print raw JSON instead of a table")
    args = ap.parse_args()
    load_dotenv(args.env_file)
    try:
        secrets = load_secrets()
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    rpc = KdfRpc(secrets.rpc_url, secrets.rpc_password, 30)
    try:
        rpc.legacy("version")
    except KdfError as exc:
        print(f"KDF is not reachable at {secrets.rpc_url}: {exc}\nStart kdf.exe (or the KDF node tab in the app) and retry.", file=sys.stderr)
        return 1

    # best_orders "buy" = others SELLING <coin> (their asks); "sell" = others BUYING <coin> (their bids)
    found: dict[str, dict[str, int]] = {}
    for action in ("buy", "sell"):
        res = rpc.v2("best_orders", {"coin": args.coin, "action": action, "request_by": {"type": "number", "value": args.top}})
        for other, entries in (res.get("orders") or {}).items():
            found.setdefault(other, {"asks": 0, "bids": 0})
            found[other]["asks" if action == "buy" else "bids"] += len(entries)

    if not found:
        print(f"no orders involving {args.coin} were found on this node's view of the network")
        return 0

    rows = []
    for other in sorted(found):
        try:
            ob = rpc.v2("orderbook", {"base": args.coin, "rel": other})
        except Exception as exc:  # noqa: BLE001
            rows.append({"pair": f"{args.coin}/{other}", "error": str(exc)})
            continue
        asks = ob.get("asks", [])
        bids = ob.get("bids", [])
        # KDF prices are <other> per 1 <coin>; convert to <coin> per 1 <other>
        best_ask = min((dec(e["price"]) for e in asks), default=None)   # cheapest <coin> for sale
        best_bid = max((dec(e["price"]) for e in bids), default=None)   # highest paid for <coin>
        rows.append({
            "pair": f"{args.coin}/{other}",
            "asks": len(asks), "bids": len(bids),
            "ask_vol_coin": str(dec(ob.get("total_asks_base_vol", "0"))),
            "bid_vol_coin": str(dec(ob.get("total_bids_base_vol", "0"))),
            "best_ask_coin_per_other": str((1 / best_ask).quantize(Decimal(1))) if best_ask else None,
            "best_bid_coin_per_other": str((1 / best_bid).quantize(Decimal(1))) if best_bid else None,
            "mine": sum(1 for e in asks + bids if e.get("is_mine")),
        })

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    c = args.coin
    print(f"{'pair':<14} {'asks':>5} {'bids':>5} {c + ' for sale':>16} {c + ' wanted':>16} {'best ask':>14} {'best bid':>14} mine")
    print(f"{'':<14} {'':>5} {'':>5} {'':>16} {'':>16} {'(' + c + '/other)':>14} {'(' + c + '/other)':>14}")
    for r in rows:
        if "error" in r:
            print(f"{r['pair']:<14} error: {r['error']}")
            continue
        print(f"{r['pair']:<14} {r['asks']:>5} {r['bids']:>5} {float(r['ask_vol_coin']):>16,.0f} {float(r['bid_vol_coin']):>16,.0f} "
              f"{r['best_ask_coin_per_other'] or '-':>14} {r['best_bid_coin_per_other'] or '-':>14} {r['mine']}")
    print("\nbest ask = cheapest price someone SELLS the coin at; best bid = most someone PAYS for it (both as coin per 1 other).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
