"""Dead-man's-switch pings: sent when healthy, suppressed when throttled, never fatal."""

import httpx
import pytest

from rxdltc_mm.engine.bot import LiquidityBot
from rxdltc_mm.heartbeat import Heartbeat
from rxdltc_mm.persistence import Store
from rxdltc_mm.prices.fake import StaticPriceProvider
from tests.conftest import make_config, providers_at


def _recorder(status: int = 200, boom: bool = False):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if boom:
            raise httpx.ConnectError("no route to host", request=request)
        return httpx.Response(status)

    return seen, httpx.Client(transport=httpx.MockTransport(handler))


def test_disabled_without_a_url():
    seen, client = _recorder()
    hb = Heartbeat("", client=client)
    assert not hb.enabled
    hb.ok(0)
    hb.fail(0, "x")
    assert seen == []


def test_ok_and_fail_hit_the_expected_urls():
    seen, client = _recorder()
    hb = Heartbeat("https://hc.example/abc/", min_interval_seconds=0, client=client)
    hb.ok(0)
    hb.fail(1, "provider outage")
    assert [str(r.url).split("?")[0] for r in seen] == ["https://hc.example/abc", "https://hc.example/abc/fail"]
    assert "provider+outage" in str(seen[1].url) or "provider%20outage" in str(seen[1].url)
    assert hb.total_sent == 2


def test_failure_pings_can_be_switched_off():
    seen, client = _recorder()
    hb = Heartbeat("https://hc.example/abc", min_interval_seconds=0, report_failures=False, client=client)
    hb.fail(0, "paused")
    assert seen == []


def test_minimum_interval_is_respected_per_kind():
    seen, client = _recorder()
    hb = Heartbeat("https://hc.example/abc", min_interval_seconds=60, client=client)
    hb.ok(0)
    hb.ok(30)          # throttled
    hb.fail(31, "x")   # different kind, so allowed
    hb.ok(61)          # interval elapsed
    assert len(seen) == 3


def test_a_network_failure_is_swallowed():
    seen, client = _recorder(boom=True)
    hb = Heartbeat("https://hc.example/abc", min_interval_seconds=0, client=client)
    hb.ok(0)  # must not raise
    assert hb.total_sent == 0 and "ConnectError" in (hb.last_error or "")


def test_bot_pings_on_a_healthy_cycle_and_reports_a_pause(fake_kdf, clock):
    seen, client = _recorder()
    cfg = make_config(monitoring={"heartbeat_url": "https://hc.example/abc", "heartbeat_min_interval_seconds": 0})
    bot = LiquidityBot(cfg, fake_kdf, providers_at(clock=clock), Store(":memory:"), dry_run=False,
                       clock=clock, sleep=lambda s: None)
    bot.heartbeat._client = client
    bot.cycle()
    assert [str(r.url) for r in seen] == ["https://hc.example/abc"]

    bot.providers = [StaticPriceProvider("dead", None, None, fail=True)] * 2
    clock.advance(30)
    bot.cycle()
    assert str(seen[-1].url).startswith("https://hc.example/abc/fail")


def test_heartbeat_url_can_be_changed_by_a_config_reload(tmp_path, fake_kdf, clock):
    import yaml
    path = tmp_path / "config.yaml"
    base = {"pricing": {"providers": []}, "persistence": {"sqlite_path": ":memory:"}, "metrics": {"enabled": False}}
    path.write_text(yaml.safe_dump(base), encoding="utf-8")
    bot = LiquidityBot(make_config(**base), fake_kdf, providers_at(clock=clock), Store(":memory:"),
                       dry_run=False, clock=clock, sleep=lambda s: None, config_path=str(path))
    assert not bot.heartbeat.enabled
    path.write_text(yaml.safe_dump({**base, "monitoring": {"heartbeat_url": "https://hc.example/new"}}), encoding="utf-8")
    out = bot.reload_config()
    assert out["ok"] and "monitoring.heartbeat_url" in out["changed"]
    assert bot.heartbeat.enabled and bot.heartbeat.url == "https://hc.example/new"


@pytest.mark.parametrize("status", [200, 404, 500])
def test_any_http_status_is_tolerated(status):
    seen, client = _recorder(status=status)
    hb = Heartbeat("https://hc.example/abc", min_interval_seconds=0, client=client)
    hb.ok(0)
    assert len(seen) == 1 and hb.total_sent == 1
