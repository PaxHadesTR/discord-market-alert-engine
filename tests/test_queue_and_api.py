import asyncio
import pytest
from fastapi.testclient import TestClient
from tests.conftest import TEST_SECRET, MockNotificationProvider
from app.models.alert import AlertPayload
from app.services.queue import AlertQueueService
from app.services.rate_limiter import AsyncTokenBucket


@pytest.mark.asyncio
async def test_full_pipeline_ingest_and_dispatch(
    mock_provider: MockNotificationProvider,
):
    rate_limiter = AsyncTokenBucket(rate=1000.0)
    queue = AlertQueueService(
        providers=[mock_provider],
        rate_limiter=rate_limiter,
        max_capacity=50,
    )
    queue.start()
    alert = AlertPayload(
        ticker="SOLUSDT",
        action="BUY",
        price=195.50,
        strategy="MACD Golden Cross",
    )
    enqueued = queue.enqueue(alert)
    assert enqueued is True

    # Give async queue worker time to process item
    await asyncio.sleep(0.05)

    assert len(mock_provider.dispatched_alerts) == 1
    dispatched = mock_provider.dispatched_alerts[0]
    assert dispatched.ticker == "SOLUSDT"
    assert dispatched.action == "BUY"
    assert dispatched.price == 195.50

    await queue.stop()


def test_queue_overflow_rejection():
    # Capacity of 2 items
    rate_limiter = AsyncTokenBucket(rate=0.01)  # slow so items stay in queue
    mock = MockNotificationProvider()
    queue = AlertQueueService(
        providers=[mock],
        rate_limiter=rate_limiter,
        max_capacity=2,
    )
    alert = AlertPayload(ticker="BTCUSDT", action="BUY")
    assert queue.enqueue(alert) is True
    assert queue.enqueue(alert) is True
    # 3rd item should be rejected (buffer full)
    assert queue.enqueue(alert) is False
    assert queue.stats["total_failed"] == 1


def test_healthz_endpoint(client: TestClient):
    response = client.get("/healthz")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert "queue" in data


def test_live_endpoint(client: TestClient):
    response = client.get("/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_queue_stop_cancelling_inflight_dispatch_does_not_double_task_done():
    """LOGIC-QUEUE-001: Cancelling _worker_loop mid-dispatch does not raise ValueError and closes providers."""
    started = asyncio.Event()
    never_finish = asyncio.Event()
    closed = False

    class _BlockingProvider:
        async def send(self, alert: AlertPayload) -> bool:
            started.set()
            await never_finish.wait()
            return True

        async def close(self) -> None:
            nonlocal closed
            closed = True

    provider = _BlockingProvider()
    queue = AlertQueueService(
        providers=[provider],
        rate_limiter=AsyncTokenBucket(rate=1000.0),
        max_capacity=10,
    )
    queue.start()
    assert queue.enqueue(AlertPayload(ticker="BTCUSDT", action="BUY", price=70000.0)) is True
    await asyncio.wait_for(started.wait(), timeout=2.0)

    # Stop while worker is suspended inside provider.send() -> must NOT raise ValueError
    await queue.stop()
    assert queue.queue._unfinished_tasks == 0
    assert closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "configured_ws_url",
    [
        "wss://custom.binance.example/ws",
        "wss://custom.binance.example/ws/!miniTicker@arr",
    ],
)
async def test_lifespan_wires_market_feed_settings(monkeypatch, tmp_path, configured_ws_url: str):
    """F-eb4d08f930e2: lifespan passes YAHOO_POLL_INTERVAL_SECONDS, CACHE_TTL_SECONDS, HTTP_TIMEOUT_SECONDS, and BINANCE_WS_URL."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from fastapi import FastAPI
    from app.config import get_settings
    from app.main import lifespan

    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "lifespan.db"))
    monkeypatch.setenv("YAHOO_POLL_INTERVAL_SECONDS", "45.0")
    monkeypatch.setenv("CACHE_TTL_SECONDS", "12.5")
    monkeypatch.setenv("HTTP_TIMEOUT_SECONDS", "15.0")
    monkeypatch.setenv("BINANCE_WS_URL", configured_ws_url)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "")
    get_settings.cache_clear()

    test_app = FastAPI()
    with patch("app.market_data.binance.BinanceMarketFeed.start_stream", MagicMock()), \
         patch("app.market_data.yahoo.YahooFinanceFeed.start_polling", MagicMock()), \
         patch("app.market_data.binance.BinanceMarketFeed.stop", AsyncMock()), \
         patch("app.market_data.yahoo.YahooFinanceFeed.stop", AsyncMock()):
        async with lifespan(test_app):
            feed = test_app.state.alert_engine.feed
            assert feed.cache_ttl == 12.5
            assert feed.yahoo.poll_interval == 45.0
            assert feed.yahoo.timeout == 15.0
            assert feed.binance.timeout == 15.0
            assert feed.binance.ws_url == "wss://custom.binance.example/ws/!miniTicker@arr"
            assert feed.binance.ws_url.count("/!miniTicker@arr") == 1


def test_cors_does_not_reflect_untrusted_origin_with_credentials(client: TestClient):
    """F-7a56bb60bb7d: Untrusted Origin does not receive wildcard/reflected CORS with credentials."""
    headers = {"Origin": "https://evil.example"}
    r_get = client.get("/live", headers=headers)
    assert r_get.status_code == 200
    assert r_get.headers.get("access-control-allow-credentials") != "true"
    assert r_get.headers.get("access-control-allow-origin") not in ("*", "https://evil.example")

    r_opt = client.options("/webhook", headers={**headers, "Access-Control-Request-Method": "POST"})
    assert r_opt.headers.get("access-control-allow-credentials") != "true"
    assert r_opt.headers.get("access-control-allow-origin") not in ("*", "https://evil.example")


@pytest.mark.asyncio
async def test_webhook_placeholder_validation_bot_fallback_and_missing_provider_warning(
    monkeypatch, tmp_path, caplog
):
    """V7-CONTRACT-001: Placeholder DISCORD_WEBHOOK_URL allows bot fallback and missing providers log WARNING."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from fastapi import FastAPI
    from app.config import get_settings
    from app.main import is_valid_discord_webhook_url, lifespan
    from app.providers.discord import DiscordBotNotificationProvider

    # 1. Placeholder vs valid URL classification
    for bad_url in (
        None,
        "",
        "   ",
        "https://discord.com/api/webhooks/your/webhook/url/here",
        "https://discord.com/api/webhooks/id/token",
        "not-a-url",
    ):
        assert is_valid_discord_webhook_url(bad_url) is False

    for good_url in (
        "https://discord.com/api/webhooks/mock/test",
        "https://discord.com/api/webhooks/123456/abcdef",
    ):
        assert is_valid_discord_webhook_url(good_url) is True

    # 2. Placeholder DISCORD_WEBHOOK_URL + DISCORD_ALERT_CHANNEL_ID falls back to DiscordBotNotificationProvider
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "fallback.db"))
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/your/webhook/url/here")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "valid_token_abc_123")
    monkeypatch.setenv("DISCORD_ALERT_CHANNEL_ID", "987654321")
    get_settings.cache_clear()

    release_bot = asyncio.Event()

    def make_bot(*args, **kwargs):
        b = MagicMock()
        b.is_closed.return_value = False

        async def _start(token):
            await release_bot.wait()

        async def _close():
            b.is_closed.return_value = True
            release_bot.set()

        b.start = AsyncMock(side_effect=_start)
        b.close = AsyncMock(side_effect=_close)
        return b

    app_fb = FastAPI()
    with patch("app.main.DiscordAlertBot", side_effect=make_bot), \
         patch("app.market_data.binance.BinanceMarketFeed.start_stream", MagicMock()), \
         patch("app.market_data.yahoo.YahooFinanceFeed.start_polling", MagicMock()), \
         patch("app.market_data.binance.BinanceMarketFeed.stop", AsyncMock()), \
         patch("app.market_data.yahoo.YahooFinanceFeed.stop", AsyncMock()):
        async with lifespan(app_fb):
            providers = app_fb.state.queue_service.providers
            assert len(providers) == 1
            assert isinstance(providers[0], DiscordBotNotificationProvider)
            assert providers[0].channel_id == 987654321

    # 3. Neither webhook nor channel ID configured logs explicit startup WARNING
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "no_provider.db"))
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "")
    monkeypatch.delenv("DISCORD_ALERT_CHANNEL_ID", raising=False)
    get_settings.cache_clear()

    app_none = FastAPI()
    with caplog.at_level("WARNING", logger="tradingview_alerts"), \
         patch("app.market_data.binance.BinanceMarketFeed.start_stream", MagicMock()), \
         patch("app.market_data.yahoo.YahooFinanceFeed.start_polling", MagicMock()), \
         patch("app.market_data.binance.BinanceMarketFeed.stop", AsyncMock()), \
         patch("app.market_data.yahoo.YahooFinanceFeed.stop", AsyncMock()):
        async with lifespan(app_none):
            assert app_none.state.queue_service.providers == []
            assert "No active TradingView webhook notification provider" in caplog.text


@pytest.mark.asyncio
async def test_queue_stop_before_start_closes_providers_and_rejects_enqueue_after_stop():
    """RES-QUEUE-001: stop() closes providers even if never started; enqueue() rejects items after stop()."""
    # 1. Stop-before-start closes provider resources cleanly and is idempotent
    close_calls = 0

    class _TrackedProvider(MockNotificationProvider):
        async def close(self) -> None:
            nonlocal close_calls
            close_calls += 1
            await super().close()

    provider_unstarted = _TrackedProvider()
    queue_unstarted = AlertQueueService(
        providers=[provider_unstarted],
        rate_limiter=AsyncTokenBucket(rate=100.0),
        max_capacity=10,
    )
    await queue_unstarted.stop()
    assert provider_unstarted.is_closed is True
    assert close_calls == 1
    await queue_unstarted.stop()
    assert close_calls == 1

    # 2. Enqueue-after-stop returns False and does not revive worker
    provider_stopped = _TrackedProvider()
    queue_stopped = AlertQueueService(
        providers=[provider_stopped],
        rate_limiter=AsyncTokenBucket(rate=100.0),
        max_capacity=10,
    )
    queue_stopped.start()
    await queue_stopped.stop()
    assert queue_stopped.enqueue(AlertPayload(ticker="BTCUSDT", action="BUY")) is False
    assert queue_stopped._worker_task is None
    assert queue_stopped._running is False

    # 3. Happy path: lazy start via enqueue() before stop() still dispatches normally
    provider_lazy = MockNotificationProvider()
    queue_lazy = AlertQueueService(
        providers=[provider_lazy],
        rate_limiter=AsyncTokenBucket(rate=1000.0),
        max_capacity=10,
    )
    assert queue_lazy.enqueue(AlertPayload(ticker="ETHUSDT", action="BUY", price=3500.0)) is True
    await asyncio.sleep(0.05)
    assert len(provider_lazy.dispatched_alerts) == 1
    await queue_lazy.stop()
    assert provider_lazy.is_closed is True


def test_readme_max_active_alerts_default_matches_settings():
    """V7-DOCS-002: README Coolify configuration and table match Settings.MAX_ACTIVE_ALERTS (100)."""
    from pathlib import Path
    from app.config import Settings

    default_cap = Settings().MAX_ACTIVE_ALERTS
    assert default_cap == 100
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
    assert f"MAX_ACTIVE_ALERTS={default_cap}" in readme
    assert "MAX_ACTIVE_ALERTS=250" not in readme
    assert "`250` / `25`" not in readme
    assert f"`{default_cap}` / `25`" in readme


def test_settings_accepts_blank_discord_alert_channel_id(monkeypatch):
    """CONTRACT-CONFIG-CHANNEL-001: Blank DISCORD_ALERT_CHANNEL_ID coerces to None while valid ints and invalid text behave properly."""
    from pydantic import ValidationError
    from app.config import Settings

    # Adversarial: blank and whitespace-only env values (as in .env.example) must not crash Settings
    for blank in ("", "   "):
        monkeypatch.setenv("DISCORD_ALERT_CHANNEL_ID", blank)
        s = Settings(_env_file=None)
        assert s.DISCORD_ALERT_CHANNEL_ID is None

    # Happy path: numeric channel ID parses as int
    monkeypatch.setenv("DISCORD_ALERT_CHANNEL_ID", "123456789012345678")
    assert Settings(_env_file=None).DISCORD_ALERT_CHANNEL_ID == 123456789012345678

    # Adversarial: non-numeric text still raises ValidationError
    monkeypatch.setenv("DISCORD_ALERT_CHANNEL_ID", "not-a-channel-id")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
