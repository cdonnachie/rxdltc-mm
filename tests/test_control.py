"""Operator control: pause / resume / stop through the bot API and the HTTP control endpoints."""

import json
import urllib.error
import urllib.request
from decimal import Decimal

from rxdltc_mm.engine.bot import LiquidityBot
from rxdltc_mm.engine.state import BotState
from rxdltc_mm.metrics import MetricsServer
from rxdltc_mm.persistence import Store
from tests.conftest import make_config, providers_at


def _bot(fake_kdf, clock, **cfg):
    return LiquidityBot(make_config(**cfg), fake_kdf, providers_at(clock=clock), Store(":memory:"), dry_run=False,
                        clock=clock, sleep=lambda s: None)


def test_operator_pause_cancels_and_holds_until_resume(fake_kdf, clock):
    bot = _bot(fake_kdf, clock, safety={"cooldown_seconds": 0, "recovery_seconds": 0})
    bot.cycle()
    assert len(fake_kdf.orders) == 2
    bot.request_pause("maintenance")
    clock.advance(30)
    bot.cycle()
    assert bot.sm.state is BotState.PAUSED and bot.operator_paused
    assert "operator: maintenance" in bot.pause.reason
    assert fake_kdf.orders == {}
    # auto-resume never happens while the operator holds the pause, even with zero cooldown/recovery
    for _ in range(3):
        clock.advance(30)
        bot.cycle()
    assert bot.sm.state is BotState.PAUSED and fake_kdf.orders == {}
    bot.request_resume()
    clock.advance(30)
    bot.cycle()
    assert bot.sm.state is BotState.ACTIVE and len(fake_kdf.orders) == 2
    assert bot.status_snapshot()["operator_paused"] == 0


def test_operator_resume_skips_cooldown_but_not_health(fake_kdf, clock):
    bot = _bot(fake_kdf, clock, safety={"cooldown_seconds": 600, "recovery_seconds": 300})
    bot.cycle()
    bot.request_pause("x")
    clock.advance(30)
    bot.cycle()
    bot.request_resume()
    clock.advance(30)
    bot.cycle()  # healthy cycle -> immediate resume although cooldown is 600s
    assert bot.sm.state is BotState.ACTIVE
    # a resume while conditions are unsafe must not resume
    bot.request_pause("y")
    clock.advance(30)
    bot.cycle()
    from rxdltc_mm.prices.fake import StaticPriceProvider
    bot.providers = [StaticPriceProvider("dead", None, None, fail=True)] * 2
    bot.request_resume()
    clock.advance(30)
    bot.cycle()
    assert bot.sm.state is BotState.PAUSED


def test_clear_events(fake_kdf, clock):
    bot = _bot(fake_kdf, clock)
    bot.cycle()
    bot.request_pause("x")
    clock.advance(30)
    bot.cycle()
    assert bot.status_snapshot()["recent_events"]
    assert bot.clear_events().startswith("cleared ")
    assert bot.status_snapshot()["recent_events"] == []


def test_request_stop_ends_run_loop_and_cancels(fake_kdf, clock):
    bot = _bot(fake_kdf, clock)
    bot.cycle()
    assert len(fake_kdf.orders) == 2
    bot.request_stop()
    bot.run()  # returns immediately because stop is set; shutdown cancels
    assert bot.sm.state is BotState.SHUTTING_DOWN and fake_kdf.orders == {}


class _Target:
    def __init__(self):
        self.calls = []

    def request_pause(self, reason):
        self.calls.append(("pause", reason))
        return "paused"

    def request_resume(self):
        self.calls.append(("resume", None))
        return "resumed"

    def request_stop(self):
        self.calls.append(("stop", None))
        return "stopping"

    def recent_events(self, limit):
        return [{"ts": 1, "level": "INFO", "kind": "x", "message": "hello"}][:limit]

    def clear_events(self):
        self.calls.append(("clear_events", None))
        return "cleared 1 events"


def _post(url, token=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_http_control_api_requires_token():
    target = _Target()
    server = MetricsServer("127.0.0.1", 0, lambda: {"state": "ACTIVE"}, control=target, control_token="s3cret-token")
    server.start()
    try:
        host, port = server.address
        base = f"http://{host}:{port}"
        assert _post(base + "/control/pause")[0] == 401
        assert _post(base + "/control/pause", token="wrong")[0] == 401
        code, body = _post(base + "/control/pause", token="s3cret-token", body={"reason": "ui"})
        assert code == 200 and body["ok"] and target.calls[-1] == ("pause", "ui")
        assert _post(base + "/control/cancel_all", token="s3cret-token")[1]["ok"]
        assert target.calls[-1] == ("pause", "operator cancel_all")
        assert _post(base + "/control/resume", token="s3cret-token")[0] == 200
        assert _post(base + "/control/stop", token="s3cret-token")[0] == 200
        assert _post(base + "/control/clear_events", token="s3cret-token")[1]["message"] == "cleared 1 events"
        assert _post(base + "/control/bogus", token="s3cret-token")[0] == 404
        with urllib.request.urlopen(base + "/events?limit=5", timeout=5) as resp:
            assert json.loads(resp.read())["events"][0]["message"] == "hello"
        with urllib.request.urlopen(base + "/status", timeout=5) as resp:
            assert json.loads(resp.read())["state"] == "ACTIVE"
    finally:
        server.stop()


def test_http_control_api_disabled_without_token():
    server = MetricsServer("127.0.0.1", 0, lambda: {}, control=_Target(), control_token=None)
    server.start()
    try:
        host, port = server.address
        code, body = _post(f"http://{host}:{port}/control/stop", token="anything")
        assert code == 403 and "disabled" in body["error"]
    finally:
        server.stop()
