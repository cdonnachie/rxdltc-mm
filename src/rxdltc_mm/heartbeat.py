"""Dead-man's-switch pings.

The bot calls a URL after each healthy cycle. If the pings stop, whatever is
watching that URL alerts you. One check covers the failure modes that matter
most for an unattended process: the host dying, the process crashing, the
network dropping and the loop hanging. Unlike metrics scraped on the same box,
it keeps working when the box does not.

Works with any service that exposes a ping URL (healthchecks.io, Better Stack,
Cronitor, Uptime Kuma). Services that support a ``/fail`` suffix also get told
when the bot pauses, so a safety pause raises an alert rather than looking
healthy.

Nothing here can affect trading: every failure is swallowed and logged.
"""

from __future__ import annotations

import httpx

from rxdltc_mm.logging_setup import get_logger

log = get_logger("heartbeat")


class Heartbeat:
    def __init__(self, url: str = "", *, min_interval_seconds: float = 60.0, report_failures: bool = True,
                 timeout_seconds: float = 10.0, client: httpx.Client | None = None):
        self.url = (url or "").rstrip("/")
        self.min_interval_seconds = min_interval_seconds
        self.report_failures = report_failures
        self.timeout_seconds = timeout_seconds
        self._client = client
        self._last_sent: dict[str, float] = {}
        self.total_sent = 0
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout_seconds, headers={"User-Agent": "rxdltc-mm/heartbeat"})
        return self._client

    def ok(self, now: float) -> None:
        """The bot completed a cycle in good health."""
        self._ping("ok", self.url, now)

    def fail(self, now: float, reason: str = "") -> None:
        """The bot is paused or cannot run. Sent only when the service supports it."""
        if self.report_failures:
            self._ping("fail", f"{self.url}/fail", now, reason)

    def _ping(self, kind: str, url: str, now: float, reason: str = "") -> None:
        if not self.enabled:
            return
        last = self._last_sent.get(kind)
        if last is not None and now - last < self.min_interval_seconds:
            return
        self._last_sent[kind] = now
        try:
            self.client.get(url, params={"reason": reason[:200]} if reason else None)
            self.total_sent += 1
            self.last_error = None
            log.debug("heartbeat sent", kind=kind)
        except Exception as exc:  # noqa: BLE001 - monitoring must never disturb trading
            self.last_error = f"{exc.__class__.__name__}: {exc}"
            log.warning("heartbeat failed", kind=kind, error=self.last_error)

    def reconfigure(self, url: str, min_interval_seconds: float, report_failures: bool, timeout_seconds: float) -> None:
        self.url = (url or "").rstrip("/")
        self.min_interval_seconds = min_interval_seconds
        self.report_failures = report_failures
        self.timeout_seconds = timeout_seconds
