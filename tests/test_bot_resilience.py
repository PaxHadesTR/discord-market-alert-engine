import asyncio
from pathlib import Path
import ssl
import time
from unittest.mock import AsyncMock, MagicMock, patch
import aiohttp
import discord
import pytest
from fastapi.testclient import TestClient
from yarl import URL
from app.config import Settings, get_settings
from app.database.db import AlertRecord
from app.main import create_app, is_proxy_connection_error
from app.providers.discord import DiscordBotNotificationProvider
from app.routes.health import check_bot_health
from tests.conftest import TEST_SECRET


# --- 1. Settings & Token Configuration Tests ---

def test_settings_is_discord_bot_configured():
    # Empty / None
    s1 = Settings(DISCORD_BOT_TOKEN=None)
    assert s1.is_discord_bot_configured is False

    s2 = Settings(DISCORD_BOT_TOKEN="")
    assert s2.is_discord_bot_configured is False

    s3 = Settings(DISCORD_BOT_TOKEN="   ")
    assert s3.is_discord_bot_configured is False

    # Placeholders
    s4 = Settings(DISCORD_BOT_TOKEN="your_discord_bot_token_here")
    assert s4.is_discord_bot_configured is False

    s5 = Settings(DISCORD_BOT_TOKEN="buraya_bot_tokeniniz")
    assert s5.is_discord_bot_configured is False

    s6 = Settings(DISCORD_BOT_TOKEN='"buraya_bot_tokeniniz"')
    assert s6.is_discord_bot_configured is False

    s7 = Settings(DISCORD_BOT_TOKEN="'your_discord_bot_token_here'")
    assert s7.is_discord_bot_configured is False

    # Valid tokens
    s8 = Settings(DISCORD_BOT_TOKEN="MTEzNDU2Nzg5MDEyMzQ1Njc4OQ.ABCDEF.GHIJKLMNOPQRST")
    assert s8.is_discord_bot_configured is True

    s9 = Settings(DISCORD_BOT_TOKEN="  valid_token_xyz  ")
    assert s9.is_discord_bot_configured is True


# --- 2. DiscordAlertBot Lifecycle Events & Guards ---

@pytest.mark.asyncio
async def test_bot_lifecycle_events():
    from app.bot.client import DiscordAlertBot

    mock_engine = MagicMock()
    mock_settings = Settings(DISCORD_BOT_TOKEN="mock_token")

    bot = DiscordAlertBot(engine=mock_engine, settings=mock_settings)

    # Test on_connect doesn't crash
    with patch("app.bot.client.logger.info") as mock_info:
        await bot.on_connect()
        mock_info.assert_called()

    # Test on_resumed
    with patch("app.bot.client.logger.info") as mock_info:
        await bot.on_resumed()
        mock_info.assert_called()

    # Test on_disconnect
    with patch("app.bot.client.logger.warning") as mock_warn:
        await bot.on_disconnect()
        mock_warn.assert_called()

    # Test on_error
    with patch("app.bot.client.logger.error") as mock_err:
        await bot.on_error("on_message")
        mock_err.assert_called()

    await bot.close()


@pytest.mark.asyncio
async def test_dispatch_alert_notification_skips_when_closed():
    from app.bot.client import DiscordAlertBot

    mock_engine = MagicMock()
    mock_settings = Settings(DISCORD_BOT_TOKEN="mock_token", DISCORD_ALERT_CHANNEL_ID=12345)
    bot = DiscordAlertBot(engine=mock_engine, settings=mock_settings)

    alert = AlertRecord(
        id=1,
        user_id="user_123",
        user_name="Trader",
        channel_id="12345",
        guild_id=None,
        symbol="BTCUSDT",
        asset_type="CRYPTO",
        target_price=70000.0,
        initial_price=65000.0,
        direction="ABOVE",
        status="ACTIVE",
        created_at="2026-10-29T20:00:00Z",
    )

    # Force closed state
    with patch.object(bot, "is_closed", return_value=True):
        with patch.object(bot, "get_channel") as mock_get_channel:
            await bot.dispatch_alert_notification(alert, 70500.0)
            mock_get_channel.assert_not_called()

    await bot.close()


# --- 3. Bot-Aware Healthcheck Tests ---

def test_healthcheck_when_bot_disabled(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "")
    get_settings.cache_clear()

    app = create_app()
    client = TestClient(app)

    # /live should return 200 ok
    resp_live = client.get("/live")
    assert resp_live.status_code == 200
    assert resp_live.json() == {"status": "ok"}

    # /healthz should return 200 healthy with bot: disabled
    resp_health = client.get("/healthz")
    assert resp_health.status_code == 200
    data = resp_health.json()
    assert data["status"] == "healthy"
    assert data["discord_bot"]["configured"] is False
    assert data["discord_bot"]["status"] == "disabled"


@pytest.mark.asyncio
async def test_healthcheck_when_supervisor_dead(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "real_token_12345")
    get_settings.cache_clear()

    app = create_app()

    # Create a task that terminates immediately
    async def _dummy():
        pass
    dead_task = asyncio.create_task(_dummy())
    await dead_task

    app.state.discord_bot_task = dead_task
    app.state.discord_bot = MagicMock()

    client = TestClient(app)

    # /live should return 503 degraded
    resp_live = client.get("/live")
    assert resp_live.status_code == 503
    live_data = resp_live.json()
    assert live_data["status"] == "degraded"
    assert live_data["bot"] == "supervisor_dead"

    # /healthz should return 503 unhealthy
    resp_health = client.get("/healthz")
    assert resp_health.status_code == 503
    health_data = resp_health.json()
    assert health_data["status"] == "unhealthy"
    assert health_data["discord_bot"]["supervisor_alive"] is False


@pytest.mark.asyncio
async def test_healthcheck_startup_grace_period(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "real_token_12345")
    get_settings.cache_clear()

    app = create_app()

    # Living task
    stop_event = asyncio.Event()
    async def _keepalive():
        await stop_event.wait()
    running_task = asyncio.create_task(_keepalive())

    mock_bot = MagicMock()
    mock_bot.is_ready.return_value = False
    mock_bot.is_closed.return_value = False

    app.state.discord_bot_task = running_task
    app.state.discord_bot = mock_bot
    # Startup timestamp is recent (within 20s grace period)
    app.state.app_start_time = time.time()

    client = TestClient(app)

    resp_live = client.get("/live")
    assert resp_live.status_code == 200
    assert resp_live.json() == {"status": "ok"}

    resp_health = client.get("/healthz")
    assert resp_health.status_code == 200
    assert resp_health.json()["status"] == "healthy"
    assert resp_health.json()["discord_bot"]["status"] == "connecting"

    # Now simulate grace period elapsed (> 20s) while still not ready
    app.state.app_start_time = time.time() - 25.0
    resp_live_expired = client.get("/live")
    assert resp_live_expired.status_code == 503
    assert resp_live_expired.json()["status"] == "degraded"
    assert resp_live_expired.json()["bot"] == "offline"

    resp_health_expired = client.get("/healthz")
    assert resp_health_expired.status_code == 503
    assert resp_health_expired.json()["status"] == "unhealthy"

    stop_event.set()
    await running_task


@pytest.mark.asyncio
async def test_healthcheck_bot_ready(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "real_token_12345")
    get_settings.cache_clear()

    app = create_app()

    stop_event = asyncio.Event()
    async def _keepalive():
        await stop_event.wait()
    running_task = asyncio.create_task(_keepalive())

    mock_bot = MagicMock()
    mock_bot.is_ready.return_value = True
    mock_bot.is_closed.return_value = False
    mock_bot.latency = 0.0425
    mock_bot.user = "TestBot#0001"

    app.state.discord_bot_task = running_task
    app.state.discord_bot = mock_bot
    app.state.app_start_time = time.time() - 100.0  # long past grace period

    client = TestClient(app)

    resp_live = client.get("/live")
    assert resp_live.status_code == 200
    assert resp_live.json() == {"status": "ok"}

    resp_health = client.get("/healthz")
    assert resp_health.status_code == 200
    data = resp_health.json()
    assert data["status"] == "healthy"
    assert data["discord_bot"]["ready"] is True
    assert data["discord_bot"]["latency_ms"] == 42.5
    assert data["discord_bot"]["user"] == "TestBot#0001"

    stop_event.set()
    await running_task


# --- 4. Supervisor Reconnection & Provider Update Tests ---

@pytest.mark.asyncio
async def test_supervisor_reconnect_and_provider_update(monkeypatch, tmp_path):
    """TEST-SUPERVISOR-002: Real app.main.lifespan supervisor creates a fresh bot on crash and updates provider.bot."""
    from app.main import lifespan

    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "sup_reconnect.db"))
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "valid_token_abc_123")
    monkeypatch.setenv("DISCORD_ALERT_CHANNEL_ID", "999")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "")
    get_settings.cache_clear()

    app = create_app()
    created_bots = []
    attempt_count = 0
    second_attempt_event = asyncio.Event()
    release_second_bot = asyncio.Event()

    async def mock_start(token):
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count == 1:
            raise ConnectionResetError("WARP proxy 503 dropped connection")
        second_attempt_event.set()
        await release_second_bot.wait()

    orig_wait_for = asyncio.wait_for

    async def fast_wait_for(coro, timeout=None):
        effective = min(timeout, 0.02) if timeout is not None and timeout >= 5.0 else timeout
        return await orig_wait_for(coro, timeout=effective)

    def make_bot(*args, **kwargs):
        b = MagicMock()
        b.is_closed.return_value = False

        async def _close():
            b.is_closed.return_value = True
            release_second_bot.set()

        b.close = AsyncMock(side_effect=_close)
        b.start = AsyncMock(side_effect=mock_start)
        created_bots.append(b)
        return b

    with patch("app.main.DiscordAlertBot", side_effect=make_bot), \
         patch("app.main.asyncio.wait_for", side_effect=fast_wait_for), \
         patch("app.market_data.binance.BinanceMarketFeed.start_stream", MagicMock()), \
         patch("app.market_data.yahoo.YahooFinanceFeed.start_polling", MagicMock()), \
         patch("app.market_data.binance.BinanceMarketFeed.stop", AsyncMock()), \
         patch("app.market_data.yahoo.YahooFinanceFeed.stop", AsyncMock()):
        async with lifespan(app):
            await orig_wait_for(second_attempt_event.wait(), timeout=2.0)

            assert len(created_bots) >= 2
            assert created_bots[0] is not created_bots[1]
            created_bots[0].close.assert_awaited()
            assert app.state.discord_bot is created_bots[1]

            bot_providers = [
                p for p in app.state.queue_service.providers
                if isinstance(p, DiscordBotNotificationProvider)
            ]
            assert len(bot_providers) == 1
            assert bot_providers[0].bot is created_bots[1]


@pytest.mark.asyncio
async def test_full_lifespan_startup_and_shutdown(monkeypatch, tmp_path):
    db_file = str(tmp_path / "test_alerts.db")
    monkeypatch.setenv("SQLITE_DB_PATH", db_file)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "valid_secret_bot_token_123")
    monkeypatch.setenv("DISCORD_ALERT_CHANNEL_ID", "123456")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "")
    get_settings.cache_clear()

    from app.main import lifespan

    app = create_app()

    started_event = asyncio.Event()
    closed_event = asyncio.Event()

    async def mock_start(token):
        started_event.set()
        try:
            while True:
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            pass

    async def mock_close():
        closed_event.set()

    with patch("app.main.DiscordAlertBot") as MockBotCls, \
         patch("app.main.AutonomousAlertEngine.start", new_callable=AsyncMock), \
         patch("app.main.AutonomousAlertEngine.stop", new_callable=AsyncMock):

        mock_bot = MagicMock()
        mock_bot.is_closed.return_value = False
        mock_bot.start = AsyncMock(side_effect=mock_start)
        mock_bot.close = AsyncMock(side_effect=mock_close)
        MockBotCls.return_value = mock_bot

        async with lifespan(app):
            # Verify bot was started
            await asyncio.wait_for(started_event.wait(), timeout=2.0)
            assert app.state.discord_bot is not None
            assert app.state.discord_bot_task is not None
            assert not app.state.discord_bot_task.done()

            # Verify DiscordBotNotificationProvider was attached
            providers = app.state.queue_service.providers
            bot_providers = [p for p in providers if isinstance(p, DiscordBotNotificationProvider)]
            assert len(bot_providers) == 1
            assert bot_providers[0].bot == mock_bot

        # After lifespan exits, bot must have been closed
        assert closed_event.is_set()
        assert app.state.discord_bot_task.done()


@pytest.mark.asyncio
async def test_healthcheck_edge_cases_and_missing_attributes(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "real_token_12345")
    get_settings.cache_clear()

    app = create_app()

    stop_event = asyncio.Event()
    async def _keepalive():
        await stop_event.wait()
    running_task = asyncio.create_task(_keepalive())

    # Case A: is_ready is True, BUT is_closed is also True (bot was shut down)
    mock_bot = MagicMock()
    mock_bot.is_ready.return_value = True
    mock_bot.is_closed.return_value = True
    mock_bot.latency = None
    mock_bot.user = None

    app.state.discord_bot_task = running_task
    app.state.discord_bot = mock_bot
    # Deliberately do not set app_start_time or queue_service on app.state
    if hasattr(app.state, "app_start_time"):
        delattr(app.state, "app_start_time")
    if hasattr(app.state, "queue_service"):
        delattr(app.state, "queue_service")

    client = TestClient(app)

    resp_live = client.get("/live")
    assert resp_live.status_code == 503
    assert resp_live.json()["status"] == "degraded"
    assert resp_live.json()["bot"] == "offline"

    resp_health = client.get("/healthz")
    assert resp_health.status_code == 503
    assert resp_health.json()["status"] == "unhealthy"
    assert resp_health.json()["queue"] == {}

    # Case B: is_ready is True, not closed, latency is None, user is None
    mock_bot.is_closed.return_value = False
    resp_live_ok = client.get("/live")
    assert resp_live_ok.status_code == 200
    assert resp_live_ok.json() == {"status": "ok"}

    resp_health_ok = client.get("/healthz")
    assert resp_health_ok.status_code == 200
    data = resp_health_ok.json()
    assert data["status"] == "healthy"
    assert data["discord_bot"]["latency_ms"] == 0.0
    assert data["discord_bot"]["user"] is None

    stop_event.set()
    await running_task


# --- 5. Additional Adversarial & Deep Recovery Verification Tests ---

def test_settings_is_discord_bot_configured_additional_placeholders():
    assert Settings(DISCORD_BOT_TOKEN="None").is_discord_bot_configured is False
    assert Settings(DISCORD_BOT_TOKEN="null").is_discord_bot_configured is False
    assert Settings(DISCORD_BOT_TOKEN="dummy").is_discord_bot_configured is False
    assert Settings(DISCORD_BOT_TOKEN="undefined").is_discord_bot_configured is False
    assert Settings(DISCORD_BOT_TOKEN="FALSE").is_discord_bot_configured is False


@pytest.mark.asyncio
async def test_healthcheck_startup_grace_period_uninitialized_and_closed(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "real_token_12345")
    get_settings.cache_clear()

    app = create_app()

    stop_event = asyncio.Event()
    async def _keepalive():
        await stop_event.wait()
    running_task = asyncio.create_task(_keepalive())

    app.state.discord_bot_task = running_task
    # Bot is initially None during early boot
    app.state.discord_bot = None
    app.state.app_start_time = time.time()

    client = TestClient(app)

    # When bot is None, but within 20s startup grace period -> HTTP 200
    resp_live = client.get("/live")
    assert resp_live.status_code == 200
    assert resp_live.json() == {"status": "ok"}

    resp_health = client.get("/healthz")
    assert resp_health.status_code == 200
    assert resp_health.json()["status"] == "healthy"
    assert resp_health.json()["discord_bot"]["status"] == "connecting"

    # When bot is closed (first handshake failed and retrying), still within grace period -> HTTP 200
    mock_bot = MagicMock()
    mock_bot.is_ready.return_value = False
    mock_bot.is_closed.return_value = True
    app.state.discord_bot = mock_bot

    resp_live_closed = client.get("/live")
    assert resp_live_closed.status_code == 200
    assert resp_live_closed.json() == {"status": "ok"}

    resp_health_closed = client.get("/healthz")
    assert resp_health_closed.status_code == 200
    assert resp_health_closed.json()["status"] == "healthy"
    assert resp_health_closed.json()["discord_bot"]["status"] == "connecting"

    stop_event.set()
    await running_task


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fatal_exc_factory",
    [
        lambda: discord.errors.LoginFailure("Improper token has been passed"),
        lambda: discord.errors.PrivilegedIntentsRequired(shard_id=None),
    ],
)
async def test_supervisor_aborts_on_fatal_login_failure(monkeypatch, tmp_path, fatal_exc_factory):
    """TEST-SUPERVISOR-002: Real app.main.lifespan supervisor aborts immediately on LoginFailure / PrivilegedIntentsRequired."""
    from app.main import lifespan

    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "sup_fatal.db"))
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "invalid_fatal_token_123")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "")
    get_settings.cache_clear()

    app = create_app()
    created_bots = []

    def make_bot(*args, **kwargs):
        b = MagicMock()
        b.is_closed.return_value = False
        b.close = AsyncMock()
        b.start = AsyncMock(side_effect=fatal_exc_factory())
        created_bots.append(b)
        return b

    with patch("app.main.DiscordAlertBot", side_effect=make_bot), \
         patch("app.market_data.binance.BinanceMarketFeed.start_stream", MagicMock()), \
         patch("app.market_data.yahoo.YahooFinanceFeed.start_polling", MagicMock()), \
         patch("app.market_data.binance.BinanceMarketFeed.stop", AsyncMock()), \
         patch("app.market_data.yahoo.YahooFinanceFeed.stop", AsyncMock()):
        async with lifespan(app):
            await asyncio.wait_for(app.state.discord_bot_task, timeout=2.0)
            assert app.state.discord_bot_task.done() is True
            assert len(created_bots) == 1
            created_bots[0].close.assert_awaited()


@pytest.mark.asyncio
async def test_tree_sync_only_once_across_reconnects():
    import app.bot.client as bot_client
    bot_client._commands_synced_globally = False

    mock_engine = MagicMock()
    mock_settings = Settings(DISCORD_BOT_TOKEN="token_123")

    bot1 = bot_client.DiscordAlertBot(engine=mock_engine, settings=mock_settings)
    bot2 = bot_client.DiscordAlertBot(engine=mock_engine, settings=mock_settings)

    with patch.object(bot1.tree, "sync", new_callable=AsyncMock) as sync1, \
         patch.object(bot2.tree, "sync", new_callable=AsyncMock) as sync2:

        sync1.return_value = ["cmd1", "cmd2"]

        await bot1.setup_hook()
        sync1.assert_awaited_once()
        assert bot_client._commands_synced_globally is True

        await bot2.setup_hook()
        sync2.assert_not_called()

    await bot1.close()
    await bot2.close()


@pytest.mark.asyncio
async def test_database_reset_alert_to_active(tmp_path):
    from app.database.db import AlertDatabase
    db = AlertDatabase(str(tmp_path / "test.db"))
    await db.init_db()

    aid = await db.add_alert(
        user_id="u1",
        user_name="User",
        channel_id="c1",
        guild_id=None,
        symbol="BTCUSDT",
        asset_type="CRYPTO",
        target_price=70000.0,
        initial_price=65000.0,
        direction="ABOVE",
    )
    # Trigger alert
    assert await db.trigger_alert(aid) is True
    record = await db.get_alert_by_id(aid)
    assert record.status == "TRIGGERED"
    assert record.triggered_at is not None

    # Reset back to ACTIVE
    assert await db.reset_alert_to_active(aid) is True
    record_reset = await db.get_alert_by_id(aid)
    assert record_reset.status == "ACTIVE"
    assert record_reset.triggered_at is None


@pytest.mark.asyncio
async def test_alert_engine_reverts_alert_when_callback_fails(tmp_path):
    from app.database.db import AlertDatabase
    from app.services.alert_engine import AutonomousAlertEngine

    db = AlertDatabase(str(tmp_path / "test_engine.db"))
    await db.init_db()

    aid = await db.add_alert(
        user_id="u1",
        user_name="User",
        channel_id="c1",
        guild_id=None,
        symbol="BTCUSDT",
        asset_type="CRYPTO",
        target_price=70000.0,
        initial_price=65000.0,
        direction="ABOVE",
    )

    mock_feed = MagicMock()
    engine = AutonomousAlertEngine(db=db, price_feed=mock_feed, settings=Settings())

    # Case 1: Callback returns False (e.g. Bot is closed/reconnecting)
    async def mock_callback_closed(alert, price):
        return False

    engine.trigger_callback = mock_callback_closed
    await engine._on_price_tick("BTCUSDT", 71000.0)

    # Alert should have been reverted to ACTIVE so user does not lose it!
    rec = await db.get_alert_by_id(aid)
    assert rec.status == "ACTIVE"

    # Case 2: Callback raises exception
    async def mock_callback_error(alert, price):
        raise ConnectionError("Network dropped")

    engine.trigger_callback = mock_callback_error
    await engine._on_price_tick("BTCUSDT", 71000.0)

    rec2 = await db.get_alert_by_id(aid)
    assert rec2.status == "ACTIVE"

    # Case 3: Callback succeeds (returns True)
    async def mock_callback_success(alert, price):
        return True

    engine.trigger_callback = mock_callback_success
    await engine._on_price_tick("BTCUSDT", 71000.0)

    rec3 = await db.get_alert_by_id(aid)
    assert rec3.status == "TRIGGERED"


@pytest.mark.asyncio
async def test_discord_bot_notification_provider_skips_when_closed():
    from app.models.alert import AlertPayload

    mock_bot = MagicMock()
    mock_bot.is_closed.return_value = True

    provider = DiscordBotNotificationProvider(bot=mock_bot, channel_id=123)
    payload = AlertPayload(ticker="BTCUSDT", action="BUY", price=70000.0)

    # Should return False cleanly without raising
    result = await provider.send(payload)
    assert result is False
    mock_bot.get_channel.assert_not_called()


# --- 6. WARP Proxy HTTP 503 Failover, Direct Fallback & Infrastructure Regression Tests ---

def _make_proxy_503_error(url: str = "http://warp:1080") -> aiohttp.ClientHttpProxyError:
    req = aiohttp.RequestInfo(
        url=URL(url),
        method="CONNECT",
        headers={},
        real_url=URL(url),
    )
    return aiohttp.ClientHttpProxyError(
        request_info=req,
        history=(),
        status=503,
        message="Service Unavailable",
    )


def test_settings_discord_proxy_list_parsing():
    assert Settings(DISCORD_PROXY=None).discord_proxy_list == []
    assert Settings(DISCORD_PROXY="   ").discord_proxy_list == []
    assert Settings(DISCORD_PROXY="http://warp:1080").discord_proxy_list == ["http://warp:1080"]
    assert Settings(
        DISCORD_PROXY=" http://warp:1080 , http://warp2:1080, http://warp:1080 , "
    ).discord_proxy_list == ["http://warp:1080", "http://warp2:1080"]


def test_is_proxy_connection_error_classification():
    assert is_proxy_connection_error(_make_proxy_503_error()) is True
    assert is_proxy_connection_error(
        RuntimeError("503, message='Service Unavailable', url='http://warp:1080'")
    ) is True
    # DPI TCP reset, disconnect, timeout, and TLS errors through SpoofDPI must also trigger failover
    assert is_proxy_connection_error(ConnectionResetError("ISP DPI RST")) is True
    assert is_proxy_connection_error(aiohttp.ServerDisconnectedError("proxy closed")) is True
    assert is_proxy_connection_error(asyncio.TimeoutError("handshake timed out")) is True
    assert is_proxy_connection_error(ssl.SSLCertVerificationError("self-signed")) is True
    assert is_proxy_connection_error(ValueError("unrelated error")) is False
    assert is_proxy_connection_error(discord.errors.LoginFailure("bad token")) is False


@pytest.mark.asyncio
async def test_supervisor_proxy_503_failover_to_backup_proxy(monkeypatch, tmp_path):
    """Regression: HTTP 503 from http://warp:1080 rotates to configured backup proxy."""
    from app.main import lifespan

    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "proxy_backup.db"))
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "valid_token_abc_123")
    monkeypatch.setenv("DISCORD_PROXY", "http://warp:1080,http://warp2:1080")
    monkeypatch.setenv("DISCORD_PROXY_FALLBACK_DIRECT", "false")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "")
    get_settings.cache_clear()

    app = create_app()
    seen_proxies = []
    connected_event = asyncio.Event()
    release_bot = asyncio.Event()

    orig_wait_for = asyncio.wait_for

    async def fast_wait_for(coro, timeout=None):
        effective = min(timeout, 0.01) if timeout is not None and timeout >= 1.0 else timeout
        return await orig_wait_for(coro, timeout=effective)

    def make_bot(*args, **kwargs):
        proxy = kwargs.get("proxy_override")
        seen_proxies.append(proxy)
        b = MagicMock()
        b.is_closed.return_value = False

        async def _close():
            b.is_closed.return_value = True
            if proxy != "http://warp:1080":
                release_bot.set()

        async def _start(token):
            if proxy == "http://warp:1080":
                raise _make_proxy_503_error(proxy)
            connected_event.set()
            await release_bot.wait()

        b.close = AsyncMock(side_effect=_close)
        b.start = AsyncMock(side_effect=_start)
        return b

    with patch("app.main.DiscordAlertBot", side_effect=make_bot), \
         patch("app.main.asyncio.wait_for", side_effect=fast_wait_for), \
         patch("app.market_data.binance.BinanceMarketFeed.start_stream", MagicMock()), \
         patch("app.market_data.yahoo.YahooFinanceFeed.start_polling", MagicMock()), \
         patch("app.market_data.binance.BinanceMarketFeed.stop", AsyncMock()), \
         patch("app.market_data.yahoo.YahooFinanceFeed.stop", AsyncMock()):
        async with lifespan(app):
            await orig_wait_for(connected_event.wait(), timeout=2.0)
            assert seen_proxies[:2] == ["http://warp:1080", "http://warp2:1080"]
            assert app.state.discord_bot_active_proxy == "http://warp2:1080"


@pytest.mark.asyncio
async def test_supervisor_proxy_503_direct_fallback_and_dpi_return(monkeypatch, tmp_path):
    """Regression: Repeated WARP 503 probes direct connection, and returns to proxy if direct is DPI-blocked."""
    from app.main import lifespan

    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "proxy_direct_fb.db"))
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "valid_token_abc_123")
    monkeypatch.setenv("DISCORD_PROXY", "http://warp:1080")
    monkeypatch.setenv("DISCORD_PROXY_FALLBACK_DIRECT", "true")
    monkeypatch.setenv("DISCORD_PROXY_FAILOVER_THRESHOLD", "2")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "")
    get_settings.cache_clear()

    app = create_app()
    seen_proxies = []
    recovered_event = asyncio.Event()
    release_bot = asyncio.Event()

    orig_wait_for = asyncio.wait_for

    async def fast_wait_for(coro, timeout=None):
        effective = min(timeout, 0.01) if timeout is not None and timeout >= 1.0 else timeout
        return await orig_wait_for(coro, timeout=effective)

    def make_bot(*args, **kwargs):
        proxy = kwargs.get("proxy_override")
        attempt = len(seen_proxies) + 1
        seen_proxies.append(proxy)
        b = MagicMock()
        b.is_closed.return_value = False

        async def _close():
            b.is_closed.return_value = True
            if attempt >= 4:
                release_bot.set()

        async def _start(token):
            if attempt in (1, 2):
                # Primary WARP proxy returns HTTP 503 twice
                raise _make_proxy_503_error("http://warp:1080")
            if attempt == 3:
                # Direct fallback probe blocked by ISP DPI
                raise ssl.SSLCertVerificationError("ISP DPI block")
            # Attempt 4: returned to http://warp:1080 after WARP came back up
            recovered_event.set()
            await release_bot.wait()

        b.close = AsyncMock(side_effect=_close)
        b.start = AsyncMock(side_effect=_start)
        return b

    with patch("app.main.DiscordAlertBot", side_effect=make_bot), \
         patch("app.main.asyncio.wait_for", side_effect=fast_wait_for), \
         patch("app.market_data.binance.BinanceMarketFeed.start_stream", MagicMock()), \
         patch("app.market_data.yahoo.YahooFinanceFeed.start_polling", MagicMock()), \
         patch("app.market_data.binance.BinanceMarketFeed.stop", AsyncMock()), \
         patch("app.market_data.yahoo.YahooFinanceFeed.stop", AsyncMock()):
        async with lifespan(app):
            await orig_wait_for(recovered_event.wait(), timeout=2.0)
            assert seen_proxies[:4] == [
                "http://warp:1080",
                "http://warp:1080",
                None,
                "http://warp:1080",
            ]


@pytest.mark.asyncio
async def test_supervisor_proxy_503_disabled_direct_fallback_and_clamped_backoff_diagnostics(
    monkeypatch, tmp_path, caplog
):
    """Regression: Ensures backoff never prints 'step 5/4', respects disabled direct fallback, and exposes 503 in /live & /healthz."""
    from app.main import lifespan

    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "proxy_503_diag.db"))
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "valid_token_abc_123")
    monkeypatch.setenv("DISCORD_PROXY", "http://warp:1080")
    monkeypatch.setenv("DISCORD_PROXY_FALLBACK_DIRECT", "false")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "")
    get_settings.cache_clear()

    app = create_app()
    seen_proxies = []
    sixth_attempt = asyncio.Event()
    release_bot = asyncio.Event()

    orig_wait_for = asyncio.wait_for

    async def fast_wait_for(coro, timeout=None):
        effective = min(timeout, 0.01) if timeout is not None and timeout >= 1.0 else timeout
        return await orig_wait_for(coro, timeout=effective)

    def make_bot(*args, **kwargs):
        proxy = kwargs.get("proxy_override")
        attempt = len(seen_proxies) + 1
        seen_proxies.append(proxy)
        b = MagicMock()
        b.is_closed.return_value = False
        b.is_ready.return_value = False

        async def _close():
            b.is_closed.return_value = True
            if attempt > 5:
                release_bot.set()

        async def _start(token):
            if attempt <= 5:
                raise _make_proxy_503_error("http://warp:1080")
            sixth_attempt.set()
            await release_bot.wait()

        b.close = AsyncMock(side_effect=_close)
        b.start = AsyncMock(side_effect=_start)
        return b

    with caplog.at_level("WARNING", logger="tradingview_alerts"), \
         patch("app.main.DiscordAlertBot", side_effect=make_bot), \
         patch("app.main.asyncio.wait_for", side_effect=fast_wait_for), \
         patch("app.market_data.binance.BinanceMarketFeed.start_stream", MagicMock()), \
         patch("app.market_data.yahoo.YahooFinanceFeed.start_polling", MagicMock()), \
         patch("app.market_data.binance.BinanceMarketFeed.stop", AsyncMock()), \
         patch("app.market_data.yahoo.YahooFinanceFeed.stop", AsyncMock()):
        async with lifespan(app):
            await orig_wait_for(sixth_attempt.wait(), timeout=2.0)

            # Direct fallback must NEVER be used when disabled
            assert all(p == "http://warp:1080" for p in seen_proxies)

            # Backoff step must remain clamped (never 'step 5/4')
            assert "step 5/4" not in caplog.text
            assert "step 4/4, attempt #5" in caplog.text

            # Expire startup grace period and check /live + /healthz surface exact proxy 503 root cause
            app.state.app_start_time = time.time() - 30.0
            client = TestClient(app)

            live_resp = client.get("/live")
            assert live_resp.status_code == 503
            live_details = live_resp.json()["details"]
            assert "503" in live_details["last_error"]
            assert live_details["active_proxy"] == "http://warp:1080"
            assert live_details["proxy_failures"] >= 5

            health_resp = client.get("/healthz")
            assert health_resp.status_code == 503
            health_bot = health_resp.json()["discord_bot"]
            assert "ClientHttpProxyError" in health_bot["last_error"]
            assert health_bot["active_proxy"] == "http://warp:1080"
            assert health_bot["proxy_failures"] >= 5


@pytest.mark.asyncio
async def test_supervisor_spoofdpi_dpi_reset_failover_to_warp(monkeypatch, tmp_path):
    """Regression: Strict ISP TCP reset / timeout through SpoofDPI rotates immediately to backup WARP proxy."""
    from app.main import lifespan

    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "spoofdpi_failover.db"))
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "valid_token_abc_123")
    monkeypatch.setenv("DISCORD_PROXY", "http://spoof-dpi:8080,http://warp:1080")
    monkeypatch.setenv("DISCORD_PROXY_FALLBACK_DIRECT", "false")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "")
    get_settings.cache_clear()

    app = create_app()
    seen_proxies = []
    connected_event = asyncio.Event()
    release_bot = asyncio.Event()

    orig_wait_for = asyncio.wait_for

    async def fast_wait_for(coro, timeout=None):
        effective = min(timeout, 0.01) if timeout is not None and timeout >= 1.0 else timeout
        return await orig_wait_for(coro, timeout=effective)

    def make_bot(*args, **kwargs):
        proxy = kwargs.get("proxy_override")
        seen_proxies.append(proxy)
        b = MagicMock()
        b.is_closed.return_value = False

        async def _close():
            b.is_closed.return_value = True
            if proxy == "http://warp:1080":
                release_bot.set()

        async def _start(token):
            if proxy == "http://spoof-dpi:8080":
                # Simulate strict ISP DPI TCP RST after CONNECT through SpoofDPI
                raise ConnectionResetError("Connection reset by peer (ISP DPI)")
            connected_event.set()
            await release_bot.wait()

        b.close = AsyncMock(side_effect=_close)
        b.start = AsyncMock(side_effect=_start)
        return b

    with patch("app.main.DiscordAlertBot", side_effect=make_bot), \
         patch("app.main.asyncio.wait_for", side_effect=fast_wait_for), \
         patch("app.market_data.binance.BinanceMarketFeed.start_stream", MagicMock()), \
         patch("app.market_data.yahoo.YahooFinanceFeed.start_polling", MagicMock()), \
         patch("app.market_data.binance.BinanceMarketFeed.stop", AsyncMock()), \
         patch("app.market_data.yahoo.YahooFinanceFeed.stop", AsyncMock()):
        async with lifespan(app):
            await orig_wait_for(connected_event.wait(), timeout=2.0)
            assert seen_proxies[:2] == ["http://spoof-dpi:8080", "http://warp:1080"]
            assert app.state.discord_bot_active_proxy == "http://warp:1080"


def test_docker_compose_warp_sidecar_prevents_503_regressions():
    """Infrastructure regression guard: ensures SpoofDPI + hardened WARP + autoheal stay configured."""
    compose_path = Path(__file__).resolve().parent.parent / "docker-compose.yml"
    content = compose_path.read_text(encoding="utf-8")

    # Primary zero-tunnel DPI bypass proxy
    assert "spoof-dpi:" in content
    assert "ghcr.io/unmedius/spoof-dpi:latest" in content

    # Mandatory for caomingjun/warp /dev/net/tun access & forwarding on modern Docker/Linux kernels
    assert "device_cgroup_rules:" in content
    assert "c 10:200 rwm" in content
    assert "NET_ADMIN" in content
    assert "net.ipv6.conf.all.disable_ipv6=0" in content
    assert "net.ipv4.conf.all.src_valid_mark=1" in content
    assert "net.ipv4.ip_forward=1" in content
    assert "WARP_ENABLE_NAT=1" in content
    assert "autoheal=true" in content

    # Mandatory healthcheck verifying actual WARP tunnel state (prevents proxy 503 race conditions)
    assert "cloudflare.com/cdn-cgi/trace" in content
    assert "warp=(on|plus)" in content
    assert "condition: service_healthy" in content

    # Autoheal sidecar restarts unhealthy warp containers automatically
    assert "willfarrell/autoheal:latest" in content



