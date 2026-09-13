"""Tiny HTTP server exposing ``/status`` (JSON), ``/metrics`` (Prometheus text),
``/healthz``, ``/events`` and an optional authenticated control API.

Bound to localhost by default. The control endpoints are only enabled when a
token is configured (``MM_BOT_CONTROL_TOKEN``); every control request must carry
``Authorization: Bearer <token>``. They are what the desktop UI uses.

    POST /control/pause       {"reason": "..."}  -> cancel all orders, hold until /control/resume
    POST /control/resume                          -> resume as soon as conditions are healthy
    POST /control/cancel_all                      -> same as pause (orders are cancelled and stay cancelled)
    POST /control/stop                            -> graceful shutdown (cancel on exit per config)
    POST /control/clear_events                    -> delete the persisted event log
    POST /control/reload                          -> re-read config.yaml and apply what can change live
    GET  /events?limit=50                         -> recent persisted events
"""

from __future__ import annotations

import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Protocol
from urllib.parse import parse_qs, urlparse

from rxdltc_mm.logging_setup import get_logger

log = get_logger("metrics")

SnapshotFn = Callable[[], dict[str, Any]]


class ControlTarget(Protocol):
    def request_pause(self, reason: str) -> str: ...
    def request_resume(self) -> str: ...
    def request_stop(self) -> str: ...
    def recent_events(self, limit: int) -> list[dict[str, Any]]: ...
    def clear_events(self) -> str: ...
    def request_reload(self) -> str: ...


_GAUGES = (
    ("fair_price_rxd_per_ltc", "Fair reference price, RXD per LTC"),
    ("target_bid_rxd_per_ltc", "Target RXD bid (bot buys RXD), RXD per LTC"),
    ("target_ask_rxd_per_ltc", "Target RXD ask (bot sells RXD), RXD per LTC"),
    ("balance_rxd", "Spendable RXD balance"),
    ("balance_ltc", "Spendable LTC balance"),
    ("inventory_rxd_value_pct", "Share of portfolio value held as RXD"),
    ("reference_sources", "Number of valid reference price sources"),
    ("reference_disagreement_pct", "Spread between reference sources"),
    ("uptime_seconds", "Bot uptime"),
    ("swaps_total", "Completed swaps"),
    ("swaps_failed_total", "Failed swaps"),
    ("rxd_bought_total", "Total RXD bought"),
    ("rxd_sold_total", "Total RXD sold"),
    ("ltc_spent_total", "Total LTC spent"),
    ("ltc_received_total", "Total LTC received"),
    ("two_sided_seconds_total", "Seconds with live quotes on both sides"),
    ("paused", "1 when paused"),
    ("dry_run", "1 in dry-run mode"),
    ("rpc_failures_total", "Total KDF RPC failures"),
    ("order_errors_total", "Total order placement/cancel errors"),
    ("cycles_total", "Completed loop iterations"),
)


def render_prometheus(snap: dict[str, Any]) -> str:
    lines: list[str] = []
    p = "rxdltc_mm_"
    for key, help_text in _GAUGES:
        value = snap.get(key)
        if value is None:
            continue
        lines.append(f"# HELP {p}{key} {help_text}")
        lines.append(f"# TYPE {p}{key} gauge")
        lines.append(f"{p}{key} {float(value)}")
    lines.append(f"# HELP {p}state Current bot state (1 for the active label)")
    lines.append(f"# TYPE {p}state gauge")
    for st in ("STARTING", "WAITING_FOR_REFERENCE", "ACTIVE", "REPRICING", "PAUSED", "RPC_ERROR", "SHUTTING_DOWN"):
        lines.append(f'{p}state{{state="{st}"}} {1 if snap.get("state") == st else 0}')
    for side in ("bid", "ask"):
        o = (snap.get("orders") or {}).get(side)
        lines.append(f'{p}open_order{{side="{side}"}} {1 if o else 0}')
        if o:
            lines.append(f'{p}open_order_price_rxd_per_ltc{{side="{side}"}} {float(o["price_rxd_per_ltc"])}')
            lines.append(f'{p}open_order_amount{{side="{side}"}} {float(o["amount"])}')
    reason = str(snap.get("paused_reason") or "").replace('"', "'").replace("\n", " ")
    lines.append(f'{p}paused_info{{reason="{reason}"}} {1 if snap.get("paused") else 0}')
    for name, info in (snap.get("providers") or {}).items():
        lines.append(f'{p}provider_healthy{{provider="{name}"}} {1 if info.get("healthy") else 0}')
        if info.get("price_rxd_per_ltc") is not None:
            lines.append(f'{p}provider_price_rxd_per_ltc{{provider="{name}"}} {float(info["price_rxd_per_ltc"])}')
    return "\n".join(lines) + "\n"


class MetricsServer:
    def __init__(self, bind: str, port: int, snapshot: SnapshotFn, *, control: ControlTarget | None = None,
                 control_token: str | None = None):
        self._snapshot = snapshot
        self._control = control
        self._token = control_token
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
                log.debug("http %s", format % args)

            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, obj: Any) -> None:
                self._send(code, json.dumps(obj, default=str, indent=2).encode(), "application/json")

            def do_GET(self) -> None:  # noqa: N802 - stdlib naming
                url = urlparse(self.path)
                try:
                    if url.path == "/metrics":
                        self._send(200, render_prometheus(server._snapshot()).encode(), "text/plain; version=0.0.4")
                    elif url.path == "/status":
                        self._json(200, server._snapshot())
                    elif url.path == "/healthz":
                        self._send(200, b"ok\n", "text/plain")
                    elif url.path == "/events":
                        if server._control is None:
                            self._json(404, {"ok": False, "error": "events not available"})
                            return
                        limit = int(parse_qs(url.query).get("limit", ["50"])[0])
                        self._json(200, {"ok": True, "events": server._control.recent_events(max(1, min(limit, 500)))})
                    else:
                        self._json(404, {"ok": False, "error": "not found"})
                except Exception as exc:  # noqa: BLE001
                    self._json(500, {"ok": False, "error": str(exc)})

            def _authorized(self) -> bool:
                if not server._token or server._control is None:
                    return False
                header = self.headers.get("Authorization", "")
                if not header.startswith("Bearer "):
                    return False
                return hmac.compare_digest(header[len("Bearer "):].strip(), server._token)

            def do_POST(self) -> None:  # noqa: N802 - stdlib naming
                url = urlparse(self.path)
                if not url.path.startswith("/control/"):
                    self._json(404, {"ok": False, "error": "not found"})
                    return
                if not server._token or server._control is None:
                    self._json(403, {"ok": False, "error": "control API disabled (set MM_BOT_CONTROL_TOKEN)"})
                    return
                if not self._authorized():
                    self._json(401, {"ok": False, "error": "unauthorized"})
                    return
                length = int(self.headers.get("Content-Length") or 0)
                payload: dict[str, Any] = {}
                if length:
                    try:
                        payload = json.loads(self.rfile.read(length) or b"{}")
                    except ValueError:
                        self._json(400, {"ok": False, "error": "invalid JSON body"})
                        return
                action = url.path[len("/control/"):]
                try:
                    if action == "pause":
                        msg = server._control.request_pause(str(payload.get("reason") or "operator pause"))
                    elif action == "cancel_all":
                        msg = server._control.request_pause("operator cancel_all")
                    elif action == "resume":
                        msg = server._control.request_resume()
                    elif action == "stop":
                        msg = server._control.request_stop()
                    elif action == "clear_events":
                        msg = server._control.clear_events()
                    elif action == "reload":
                        msg = server._control.request_reload()
                    else:
                        self._json(404, {"ok": False, "error": f"unknown action {action}"})
                        return
                except Exception as exc:  # noqa: BLE001
                    self._json(500, {"ok": False, "error": str(exc)})
                    return
                log.info("control request", action=action)
                self._json(200, {"ok": True, "message": msg})

        self._httpd = ThreadingHTTPServer((bind, port), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="metrics", daemon=True)

    @property
    def address(self) -> tuple[str, int]:
        return self._httpd.server_address[:2]  # type: ignore[return-value]

    def start(self) -> None:
        self._thread.start()
        log.info("metrics server listening", bind=self.address[0], port=self.address[1],
                 control_api=bool(self._token and self._control))

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
