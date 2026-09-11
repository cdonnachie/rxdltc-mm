from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum

from rxdltc_mm.logging_setup import get_logger

log = get_logger("state")


class BotState(str, Enum):
    STARTING = "STARTING"
    WAITING_FOR_REFERENCE = "WAITING_FOR_REFERENCE"
    ACTIVE = "ACTIVE"
    REPRICING = "REPRICING"
    PAUSED = "PAUSED"
    RPC_ERROR = "RPC_ERROR"
    SHUTTING_DOWN = "SHUTTING_DOWN"


_ALLOWED: dict[BotState, set[BotState]] = {
    BotState.STARTING: {BotState.WAITING_FOR_REFERENCE, BotState.ACTIVE, BotState.RPC_ERROR, BotState.PAUSED, BotState.SHUTTING_DOWN},
    BotState.WAITING_FOR_REFERENCE: {BotState.ACTIVE, BotState.PAUSED, BotState.RPC_ERROR, BotState.SHUTTING_DOWN},
    BotState.ACTIVE: {BotState.REPRICING, BotState.PAUSED, BotState.RPC_ERROR, BotState.WAITING_FOR_REFERENCE, BotState.SHUTTING_DOWN},
    BotState.REPRICING: {BotState.ACTIVE, BotState.PAUSED, BotState.RPC_ERROR, BotState.SHUTTING_DOWN},
    BotState.PAUSED: {BotState.ACTIVE, BotState.RPC_ERROR, BotState.PAUSED, BotState.SHUTTING_DOWN},
    BotState.RPC_ERROR: {BotState.PAUSED, BotState.RPC_ERROR, BotState.SHUTTING_DOWN},
    BotState.SHUTTING_DOWN: set(),
}


@dataclass(frozen=True)
class Transition:
    at: float
    from_state: BotState
    to_state: BotState
    reason: str


class StateMachine:
    def __init__(self, initial: BotState = BotState.STARTING, *, now: float = 0.0):
        self.state = initial
        self.since = now
        self.reason = "startup"
        self.history: deque[Transition] = deque(maxlen=200)

    def can(self, new: BotState) -> bool:
        return new in _ALLOWED[self.state]

    def transition(self, new: BotState, reason: str, *, now: float) -> bool:
        if new == self.state:
            self.reason = reason
            return False
        if not self.can(new):
            log.error("illegal state transition ignored", from_state=self.state.value, to_state=new.value, reason=reason)
            return False
        t = Transition(now, self.state, new, reason)
        self.history.append(t)
        log.info("STATE %s -> %s", self.state.value, new.value, reason=reason)
        self.state, self.since, self.reason = new, now, reason
        return True
