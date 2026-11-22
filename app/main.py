import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import List, Optional
import aiohttp
from fastapi import FastAPI
import discord
from app.bot.client import DiscordAlertBot
from app.config import get_settings
from app.database.db import AlertDatabase
from app.market_data.binance import BinanceMarketFeed
from app.market_data.feed import UnifiedPriceFeed
from app.market_data.yahoo import YahooFinanceFeed
from app.providers.base import BaseNotificationProvider
from app.providers.discord import DiscordBotNotificationProvider, DiscordWebhookProvider
from app.routes.health import router as health_router
from app.routes.webhook import INSECURE_DEFAULT_SECRETS, router as webhook_router
from app.services.alert_engine import AutonomousAlertEngine
from app.services.queue import AlertQueueService
from app.services.rate_limiter import AsyncTokenBucket

settings = get_settings()

logging.basicConfig(
    level=settings.LOG_LEVEL.upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger("discord").setLevel(logging.INFO)
logging.getLogger("discord.gateway").setLevel(logging.INFO)
logging.getLogger("discord.client").setLevel(logging.INFO)
logger = logging.getLogger("tradingview_alerts")


def is_proxy_connection_error(exc: BaseException) -> bool:
    """Return True if the exception represents an outbound proxy/tunnel failure (e.g. WARP HTTP 503)."""
    if isinstance(
        exc,
        (
            aiohttp.ClientHttpProxyError,
            aiohttp.ClientProxyConnectionError,
            aiohttp.ClientConnectorError,
        ),
    ):
        return True
    err_str = str(exc)
    return (
        "ClientHttpProxyError" in err_str
        or "ClientProxyConnectionError" in err_str
        or "503, message='Service Unavailable'" in err_str
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager initializing both webhook queue and autonomous alert bot."""
    settings = get_settings()
    logger.info("Initializing Autonomous Price Alert & Discord Bot Service...")
    app.state.app_start_time = time.time()

    # 1. Initialize Webhook Queue Infrastructure (TradingView Forwarding)
    providers: List[BaseNotificationProvider] = []
    if settings.DISCORD_WEBHOOK_URL:
        discord_provider = DiscordWebhookProvider(
            webhook_url=settings.DISCORD_WEBHOOK_URL,
            bot_username=settings.DISCORD_BOT_USERNAME,
            avatar_url=settings.DISCORD_AVATAR_URL,
            timeout_seconds=settings.HTTP_TIMEOUT_SECONDS,
        )
        providers.append(discord_provider)

    rate_limiter = AsyncTokenBucket(rate=settings.RATE_LIMIT_PER_SECOND)
    queue_service = AlertQueueService(
        providers=providers,
        rate_limiter=rate_limiter,
        max_capacity=settings.MAX_QUEUE_CAPACITY,
    )
    queue_service.start()
    app.state.queue_service = queue_service

    # 2. Initialize Autonomous Market Engine & SQLite Storage
    raw_secret = (settings.WEBHOOK_SECRET or "").strip()
    if not raw_secret or raw_secret.lower() in INSECURE_DEFAULT_SECRETS:
        logger.warning(
            "WEBHOOK_SECRET is unset or uses an insecure placeholder. "
            "TradingView webhook ingestion is locked (fail-closed) until a strong WEBHOOK_SECRET is configured."
        )

    base_ws_url = settings.BINANCE_WS_URL.strip().rstrip("/")
    stream_suffix = "/!miniTicker@arr"
    binance_ws_url = base_ws_url if base_ws_url.endswith(stream_suffix) else f"{base_ws_url}{stream_suffix}"

    yahoo_poll_interval = max(1.0, float(settings.YAHOO_POLL_INTERVAL_SECONDS))
    cache_ttl_seconds = max(0.1, float(settings.CACHE_TTL_SECONDS))
    http_timeout_seconds = max(1.0, float(settings.HTTP_TIMEOUT_SECONDS))

    db = AlertDatabase(db_path=settings.SQLITE_DB_PATH)
    binance_feed = BinanceMarketFeed(
        ws_url=binance_ws_url,
        timeout_seconds=http_timeout_seconds,
    )
    yahoo_feed = YahooFinanceFeed(
        poll_interval_seconds=yahoo_poll_interval,
        timeout_seconds=http_timeout_seconds,
    )
    price_feed = UnifiedPriceFeed(
        binance_feed=binance_feed,
        yahoo_feed=yahoo_feed,
        cache_ttl_seconds=cache_ttl_seconds,
    )
    alert_engine = AutonomousAlertEngine(
        db=db,
        price_feed=price_feed,
        settings=settings,
    )
    await alert_engine.start()
    app.state.alert_engine = alert_engine

    # 3. Initialize Discord Bot with Resilient Supervisor Loop
    bot_task: Optional[asyncio.Task] = None
    shutdown_event = asyncio.Event()

    token = (settings.DISCORD_BOT_TOKEN or "").strip().strip('"').strip("'")
    if settings.is_discord_bot_configured:
        # If no separate DISCORD_WEBHOOK_URL was configured, use the active Discord Bot to dispatch TradingView alerts!
        if not settings.DISCORD_WEBHOOK_URL and settings.DISCORD_ALERT_CHANNEL_ID:
            bot_provider = DiscordBotNotificationProvider(
                bot=None,
                channel_id=settings.DISCORD_ALERT_CHANNEL_ID,
            )
            queue_service.providers.append(bot_provider)
            logger.info(
                "Attached Discord Bot provider to TradingView Webhook Queue (Target Channel: %s)",
                settings.DISCORD_ALERT_CHANNEL_ID,
            )

        async def _run_bot_supervisor():
            backoff_steps = [5.0, 10.0, 30.0, 60.0]
            backoff_idx = 0
            attempt_num = 0
            configured_proxies = settings.discord_proxy_list
            failover_threshold = max(1, int(settings.DISCORD_PROXY_FAILOVER_THRESHOLD))
            total_proxy_threshold = len(configured_proxies) * failover_threshold
            proxy_idx = 0
            consecutive_proxy_errors = 0

            app.state.discord_bot_last_error = None
            app.state.discord_bot_active_proxy = configured_proxies[0] if configured_proxies else None
            app.state.discord_bot_proxy_failures = 0

            while not shutdown_event.is_set():
                current_bot: Optional[DiscordAlertBot] = None
                connect_start = time.time()

                # Select outbound route: primary/backup proxy, or direct probe after repeated proxy failures
                if not configured_proxies:
                    selected_proxy: Optional[str] = None
                    using_direct_fallback = False
                elif settings.DISCORD_PROXY_FALLBACK_DIRECT and consecutive_proxy_errors >= total_proxy_threshold:
                    selected_proxy = None
                    using_direct_fallback = True
                    # Reset counter so if direct connection is blocked (e.g. Turkey DPI), next cycle returns to proxy
                    consecutive_proxy_errors = 0
                    logger.warning(
                        "Supervisor: All configured proxies (%s) failed %d consecutive times. "
                        "Attempting temporary direct connection fallback...",
                        ", ".join(configured_proxies),
                        total_proxy_threshold,
                    )
                else:
                    selected_proxy = configured_proxies[proxy_idx % len(configured_proxies)]
                    using_direct_fallback = False

                app.state.discord_bot_active_proxy = selected_proxy
                fast_failover = False

                try:
                    logger.info("Supervisor: Instantiating fresh DiscordAlertBot client...")
                    current_bot = DiscordAlertBot(
                        engine=alert_engine,
                        settings=settings,
                        proxy_override=selected_proxy,
                    )
                    app.state.discord_bot = current_bot

                    # Atomically update bot instance in queue providers
                    for provider in queue_service.providers:
                        if isinstance(provider, DiscordBotNotificationProvider):
                            provider.bot = current_bot

                    logger.info("Supervisor: Connecting Discord Bot to Discord Gateway...")
                    await current_bot.start(token)
                    logger.warning("Supervisor: Discord Bot gateway session ended normally.")
                    app.state.discord_bot_last_error = None
                except asyncio.CancelledError:
                    logger.info("Supervisor: Discord Bot supervisor task cancelled.")
                    break
                except (discord.errors.LoginFailure, discord.errors.PrivilegedIntentsRequired) as fatal_err:
                    app.state.discord_bot_last_error = f"{type(fatal_err).__name__}: {fatal_err}"
                    logger.critical(
                        "Supervisor: FATAL Discord authentication/permission error: %s. Aborting supervisor to prevent API bans.",
                        fatal_err,
                    )
                    break
                except Exception as bot_err:
                    app.state.discord_bot_last_error = f"{type(bot_err).__name__}: {bot_err}"
                    if selected_proxy is not None and is_proxy_connection_error(bot_err):
                        consecutive_proxy_errors += 1
                        app.state.discord_bot_proxy_failures = consecutive_proxy_errors
                        proxy_idx += 1
                        logger.error(
                            "Supervisor: Outbound proxy '%s' failed (%s) [consecutive proxy errors: %d]. "
                            "Verify WARP tunnel health (`docker exec warp warp-cli status` / `docker restart warp` "
                            "and ensure `device_cgroup_rules: ['c 10:200 rwm']` is set).",
                            selected_proxy,
                            bot_err,
                            consecutive_proxy_errors,
                        )
                        # Speed up failover when switching to a backup proxy or entering direct fallback
                        if len(configured_proxies) > 1 or (
                            settings.DISCORD_PROXY_FALLBACK_DIRECT
                            and consecutive_proxy_errors >= total_proxy_threshold
                        ):
                            fast_failover = True
                    elif using_direct_fallback:
                        logger.warning(
                            "Supervisor: Direct fallback connection also failed (%s). "
                            "Returning to configured proxy '%s' on next cycle.",
                            bot_err,
                            configured_proxies[0],
                        )
                    else:
                        logger.error("Supervisor: Discord Bot gateway session terminated with error: %s", bot_err, exc_info=True)
                finally:
                    if current_bot and not current_bot.is_closed():
                        try:
                            await current_bot.close()
                        except Exception as close_err:
                            logger.debug("Supervisor: Error closing bot client: %s", close_err)

                if shutdown_event.is_set():
                    break

                # If connection lasted > 60s, reset backoff and proxy error counters
                if time.time() - connect_start > 60.0:
                    backoff_idx = 0
                    consecutive_proxy_errors = 0
                    app.state.discord_bot_proxy_failures = 0

                step_num = min(backoff_idx + 1, len(backoff_steps))
                delay = backoff_steps[step_num - 1]
                if fast_failover:
                    delay = min(delay, 5.0)
                backoff_idx = min(backoff_idx + 1, len(backoff_steps) - 1)
                attempt_num += 1

                logger.warning(
                    "Supervisor: Discord Bot disconnected. Attempting reconnection in %.0fs (step %d/%d, attempt #%d)...",
                    delay,
                    step_num,
                    len(backoff_steps),
                    attempt_num,
                )
                try:
                    await asyncio.wait_for(shutdown_event.wait(), timeout=delay)
                    break
                except asyncio.TimeoutError:
                    pass

        bot_task = asyncio.create_task(_run_bot_supervisor(), name="discord_bot_supervisor")
        app.state.discord_bot_task = bot_task
    else:
        app.state.discord_bot = None
        app.state.discord_bot_task = None
        logger.warning(
            "DISCORD_BOT_TOKEN is empty or placeholder ('%s'). Bot cannot connect to Discord! "
            "Please configure DISCORD_BOT_TOKEN in Coolify Environment Variables and Restart.",
            token[:10] if token else "EMPTY"
        )

    yield

    logger.info("Shutting down background services...")
    shutdown_event.set()

    current_bot = getattr(app.state, "discord_bot", None)
    if current_bot and not current_bot.is_closed():
        try:
            await current_bot.close()
        except Exception as e:
            logger.warning("Error closing Discord bot: %s", e)

    if bot_task and not bot_task.done():
        try:
            await asyncio.wait_for(bot_task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            bot_task.cancel()

    await alert_engine.stop()
    await queue_service.stop()
    logger.info("All background engines safely terminated.")


def create_app() -> FastAPI:
    """FastAPI application factory."""
    application = FastAPI(
        title="Autonomous Price Alert & Discord Bot API",
        description="Unified market price monitor, TradingView webhook ingestion, and interactive Discord bot.",
        version="2.0.0",
        lifespan=lifespan,
    )

    application.include_router(health_router)
    application.include_router(webhook_router)

    return application


app = create_app()
