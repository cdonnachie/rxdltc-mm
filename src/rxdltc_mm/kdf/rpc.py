"""Low-level HTTP transport for the KDF / mm2 JSON RPC.

Two request styles exist in KDF and both are used by the bot:

* **legacy**: ``{"userpass": ..., "method": "my_balance", "coin": "RXD"}`` --
  parameters live at the top level; errors come back as HTTP 4xx/5xx with a
  JSON ``{"error": "..."}`` body.
* **mmrpc 2.0**: ``{"userpass": ..., "mmrpc": "2.0", "method": "orderbook",
  "params": {...}, "id": n}`` -- response ``{"result": ...}`` or
  ``{"error", "error_type", "error_data", ...}``.

The ``userpass`` field is injected here and is never included in log output
or exception messages.
"""

from __future__ import annotations

import itertools
import json
import time
from typing import Any

import httpx

from rxdltc_mm.logging_setup import get_logger

log = get_logger("kdf.rpc")


class KdfError(Exception):
    """Base class for KDF failures."""


class KdfConnectionError(KdfError):
    """KDF unreachable, timed out, or returned garbage (transport-level)."""


class KdfRpcError(KdfError):
    """KDF answered with an application-level error."""

    def __init__(self, method: str, message: str, *, error_type: str | None = None,
                 error_data: Any = None, http_status: int | None = None):
        super().__init__(f"{method}: {message}")
        self.method = method
        self.message = message
        self.error_type = error_type
        self.error_data = error_data
        self.http_status = http_status


class KdfRpc:
    def __init__(self, url: str, userpass: str, timeout_seconds: float = 20.0, client: httpx.Client | None = None):
        self._url = url
        self._userpass = userpass
        self._client = client or httpx.Client(timeout=timeout_seconds)
        self._ids = itertools.count(1)

    @property
    def url(self) -> str:
        return self._url

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ core
    def _post(self, method: str, body: dict[str, Any]) -> tuple[int, Any]:
        payload = dict(body)
        payload["userpass"] = self._userpass
        started = time.monotonic()
        try:
            resp = self._client.post(self._url, content=json.dumps(payload), headers={"Content-Type": "application/json"})
        except httpx.HTTPError as exc:
            raise KdfConnectionError(f"{method}: {exc.__class__.__name__}: {_safe(str(exc))}") from exc
        elapsed_ms = (time.monotonic() - started) * 1000
        try:
            data = resp.json()
        except ValueError as exc:
            raise KdfConnectionError(f"{method}: non-JSON response (HTTP {resp.status_code})") from exc
        log.debug("rpc", method=method, status=resp.status_code, ms=round(elapsed_ms, 1))
        return resp.status_code, data

    def legacy(self, method: str, **params: Any) -> Any:
        body: dict[str, Any] = {"method": method}
        body.update(params)
        status, data = self._post(method, body)
        if isinstance(data, dict) and "error" in data:
            raise KdfRpcError(method, _safe(str(data["error"])), http_status=status)
        if status >= 400:
            raise KdfRpcError(method, f"HTTP {status}: {_safe(json.dumps(data)[:300])}", http_status=status)
        return data

    def v2(self, method: str, params: dict[str, Any] | None = None) -> Any:
        body = {"mmrpc": "2.0", "method": method, "params": params or {}, "id": next(self._ids)}
        status, data = self._post(method, body)
        if isinstance(data, dict) and data.get("error") is not None:
            raise KdfRpcError(
                method,
                _safe(str(data.get("error"))),
                error_type=data.get("error_type"),
                error_data=data.get("error_data"),
                http_status=status,
            )
        if status >= 400:
            raise KdfRpcError(method, f"HTTP {status}: {_safe(json.dumps(data)[:300])}", http_status=status)
        if isinstance(data, dict) and "result" in data:
            return data["result"]
        return data


def _safe(text: str) -> str:
    """Belt and braces: strip anything that looks like a userpass from error text."""
    if "userpass" in text:
        return "<redacted: message contained userpass>"
    return text
