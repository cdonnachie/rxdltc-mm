"""Circuit-breaker building blocks. All are pure bookkeeping; the bot decides
what to do with their verdicts."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal

from rxdltc_mm.pricing import HUNDRED


class PriceMoveMonitor:
    """Trips when the fair price differs by more than ``max_move_pct`` from ANY
    observation inside the trailing ``window_seconds``."""

    def __init__(self, max_move_pct: Decimal, window_seconds: float):
        self.max_move_pct = max_move_pct
        self.window_seconds = window_seconds
        self._history: deque[tuple[float, Decimal]] = deque()

    def observe(self, now: float, price: Decimal) -> Decimal | None:
        """Record ``price`` and return the offending move (in %) if it exceeds the limit."""
        while self._history and now - self._history[0][0] > self.window_seconds:
            self._history.popleft()
        worst: Decimal | None = None
        for _, past in self._history:
            move = abs(price - past) / past * HUNDRED
            if move > self.max_move_pct and (worst is None or move > worst):
                worst = move
        self._history.append((now, price))
        return worst

    def reset(self) -> None:
        self._history.clear()


class FailureCounter:
    def __init__(self, limit: int):
        self.limit = max(1, limit)
        self.count = 0
        self.total = 0

    def failure(self) -> bool:
        """Record a failure; True when the limit is reached."""
        self.count += 1
        self.total += 1
        return self.count >= self.limit

    def success(self) -> None:
        self.count = 0

    @property
    def tripped(self) -> bool:
        return self.count >= self.limit


@dataclass
class PauseController:
    """Tracks why we paused and whether we may resume.

    Resumption requires both: at least ``cooldown_seconds`` since the pause,
    and ``recovery_seconds`` of consecutive healthy checks.
    """

    cooldown_seconds: float
    recovery_seconds: float
    paused_at: float | None = None
    reason: str | None = None
    healthy_since: float | None = None

    @property
    def paused(self) -> bool:
        return self.paused_at is not None

    def pause(self, reason: str, now: float) -> None:
        if self.paused_at is None:
            self.paused_at = now
        self.reason = reason
        self.healthy_since = None

    def mark(self, healthy: bool, now: float) -> None:
        if not healthy:
            self.healthy_since = None
        elif self.healthy_since is None:
            self.healthy_since = now

    def can_resume(self, now: float) -> bool:
        if self.paused_at is None:
            return True
        if now - self.paused_at < self.cooldown_seconds:
            return False
        return self.healthy_since is not None and now - self.healthy_since >= self.recovery_seconds

    def resume(self) -> None:
        self.paused_at = None
        self.reason = None
        self.healthy_since = None

    def seconds_until_resume(self, now: float) -> float | None:
        if self.paused_at is None:
            return None
        cooldown_left = max(0.0, self.cooldown_seconds - (now - self.paused_at))
        if self.healthy_since is None:
            return max(cooldown_left, self.recovery_seconds)
        return max(cooldown_left, self.recovery_seconds - (now - self.healthy_since))
