import pytest
import httpx
from app.models.alert import AlertPayload
from app.providers.discord import DiscordWebhookProvider


def test_build_discord_payload_bullish():
    provider = DiscordWebhookProvider(
        webhook_url="https://discord.com/api/webhooks/123/abc",
        bot_username="TV Bot",
    )
    alert = AlertPayload(
        ticker="BTCUSDT",
        action="BUY",
        price=68450.50,
        strategy="SuperTrend",
        timeframe="15m",
        message="Breakout detected",
        extra_data={"RSI": "72.4"},
    )
    body = provider.build_discord_payload(alert)

    assert body["username"] == "TV Bot"
    assert len(body["embeds"]) == 1
    embed = body["embeds"][0]
    assert "🟢 BTCUSDT - BUY" in embed["title"]
    assert embed["color"] == 0x00FF88
    assert "> Breakout detected" == embed["description"]

    field_names = [f["name"] for f in embed["fields"]]
    assert "💵 Price" in field_names
    assert "🎯 Strategy" in field_names
    assert "⏱ Timeframe" in field_names
    assert "🔹 Rsi" in field_names


def test_build_discord_payload_bearish():
    provider = DiscordWebhookProvider(
        webhook_url="https://discord.com/api/webhooks/123/abc",
    )
    alert = AlertPayload(
        ticker="ETHUSDT",
        action="SELL",
        price=3420.10,
    )
    body = provider.build_discord_payload(alert)
    embed = body["embeds"][0]

    assert "🔴 ETHUSDT - SELL" in embed["title"]
    assert embed["color"] == 0xFF3366


@pytest.mark.asyncio
async def test_discord_mock_http_dispatch():
    # Mock transport to simulate Discord returning 204 No Content
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=204)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as mock_client:
        provider = DiscordWebhookProvider(
            webhook_url="https://discord.com/api/webhooks/test",
            client=mock_client,
        )
        alert = AlertPayload(ticker="BTCUSDT", action="BUY", price=65000)
        success = await provider.send(alert)
        assert success is True


@pytest.mark.asyncio
async def test_discord_rate_limit_retry():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            # First attempt: 429 Too Many Requests
            return httpx.Response(status_code=429, json={"retry_after": 0.05})
        return httpx.Response(status_code=204)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as mock_client:
        provider = DiscordWebhookProvider(
            webhook_url="https://discord.com/api/webhooks/test",
            client=mock_client,
        )
        alert = AlertPayload(ticker="BTCUSDT", action="BUY", price=65000)
        success = await provider.send(alert)
        assert success is True
        assert attempts == 2


@pytest.mark.asyncio
async def test_discord_bot_notification_provider():
    from app.providers.discord import DiscordBotNotificationProvider

    sent_messages = []

    class DummyChannel:
        async def send(self, embed=None, content=None):
            sent_messages.append(embed)

    class DummyBot:
        def get_channel(self, cid):
            return DummyChannel() if cid == 999 else None
        async def fetch_channel(self, cid):
            return self.get_channel(cid)

    provider = DiscordBotNotificationProvider(bot=DummyBot(), channel_id=999)
    alert = AlertPayload(ticker="BTCUSDT", action="BUY", price=68000, message="Breakout test")
    success = await provider.send(alert)
    assert success is True
    assert len(sent_messages) == 1
    assert "BTCUSDT" in sent_messages[0].title


@pytest.mark.asyncio
async def test_discord_providers_enforce_embed_length_and_total_budget():
    """DATA-VALID-002: Oversized webhook and bot embeds stay strictly within Discord API limits."""
    from app.providers.discord import DiscordBotNotificationProvider

    oversized_alert = AlertPayload(
        ticker="T" * 500,
        action="A" * 500,
        price=68450.50,
        strategy="S" * 2000,
        timeframe="F" * 2000,
        message="M" * 6000,
        extra_data={f"k{i}_" + ("x" * 400): "v" * 2000 for i in range(5)},
    )

    # 1. DiscordWebhookProvider.build_discord_payload
    webhook_provider = DiscordWebhookProvider(webhook_url="https://discord.com/api/webhooks/123/abc")
    body = webhook_provider.build_discord_payload(oversized_alert)
    embed = body["embeds"][0]
    assert len(embed["title"]) <= 256
    assert embed["description"] is None or len(embed["description"]) <= 4096
    assert len(embed["footer"]["text"]) <= 2048
    total_webhook_chars = (
        len(embed["title"])
        + len(embed["description"] or "")
        + len(embed["footer"]["text"])
        + sum(len(f["name"]) + len(f["value"]) for f in embed["fields"])
    )
    assert total_webhook_chars <= 6000
    for f in embed["fields"]:
        assert 1 <= len(f["name"]) <= 256
        assert 1 <= len(f["value"]) <= 1024

    # 2. DiscordBotNotificationProvider.send
    sent_embeds = []

    class DummyChannel:
        async def send(self, embed=None, content=None):
            sent_embeds.append(embed)

    class DummyBot:
        def get_channel(self, cid):
            return DummyChannel() if cid == 999 else None

        async def fetch_channel(self, cid):
            return self.get_channel(cid)

    bot_provider = DiscordBotNotificationProvider(bot=DummyBot(), channel_id=999)
    assert await bot_provider.send(oversized_alert) is True
    assert len(sent_embeds) == 1
    bot_embed = sent_embeds[0]
    assert len(bot_embed.title) <= 256
    assert bot_embed.description is None or len(bot_embed.description) <= 4096
    assert len(bot_embed.footer.text) <= 2048
    total_bot_chars = (
        len(bot_embed.title)
        + len(bot_embed.description or "")
        + len(bot_embed.footer.text)
        + sum(len(f.name) + len(f.value) for f in bot_embed.fields)
    )
    assert total_bot_chars <= 6000
    for f in bot_embed.fields:
        assert 1 <= len(f.name) <= 256
        assert 1 <= len(f.value) <= 1024

    await webhook_provider.close()


@pytest.mark.asyncio
async def test_discord_webhook_clamps_retry_after_and_skips_final_sleep():
    """RES-PROV-001: Adversarial 429 retry_after values are clamped <= 30s and final attempt never sleeps."""
    import math
    from unittest.mock import AsyncMock, patch

    alert = AlertPayload(ticker="BTCUSDT", action="BUY", price=65000)

    # 1. Adversarial 429 retry_after values
    adversarial_payloads = [
        b'{"retry_after": 999999}',
        b'{"retry_after": Infinity}',
        b'{"retry_after": NaN}',
        b'{"retry_after": -5.0}',
        b'{"retry_after": "not-a-number"}',
    ]
    for raw_body in adversarial_payloads:
        def handler_429(request: httpx.Request, body=raw_body) -> httpx.Response:
            return httpx.Response(status_code=429, content=body, headers={"content-type": "application/json"})

        sleep_calls = []

        async def fake_sleep(delay: float):
            sleep_calls.append(delay)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler_429)) as mock_client:
            provider = DiscordWebhookProvider(webhook_url="https://discord.com/api/webhooks/test", client=mock_client)
            with patch("app.providers.discord.asyncio.sleep", side_effect=fake_sleep):
                ok = await provider.send(alert)
                assert ok is False
                assert len(sleep_calls) == 2
                for d in sleep_calls:
                    assert math.isfinite(d) and 0.0 <= d <= 30.0

    # 2. HTTP 500 exhaustion sleeps exactly twice (not on attempt 3)
    def handler_500(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=500, text="Server Error")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler_500)) as mock_client:
        provider = DiscordWebhookProvider(webhook_url="https://discord.com/api/webhooks/test", client=mock_client)
        with patch("app.providers.discord.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            assert await provider.send(alert) is False
            assert mock_sleep.await_count == 2

    # 3. httpx.ReadTimeout exhaustion sleeps exactly twice (not on attempt 3)
    def handler_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler_timeout)) as mock_client:
        provider = DiscordWebhookProvider(webhook_url="https://discord.com/api/webhooks/test", client=mock_client)
        with patch("app.providers.discord.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            assert await provider.send(alert) is False
            assert mock_sleep.await_count == 2


@pytest.mark.asyncio
async def test_discord_bot_provider_checks_ready_and_retries_transient_errors():
    """RES-PROV-002: DiscordBotNotificationProvider guards is_ready(), retries transient network errors, and fails fast on non-retryable errors."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch
    import aiohttp
    from app.providers.discord import DiscordBotNotificationProvider

    alert = AlertPayload(ticker="BTCUSDT", action="BUY", price=68000, message="Signal")

    # 1. Unready bot returns False immediately without touching channels
    unready_bot = MagicMock()
    unready_bot.is_closed.return_value = False
    unready_bot.is_ready.return_value = False
    provider_unready = DiscordBotNotificationProvider(bot=unready_bot, channel_id=999)
    assert await provider_unready.send(alert) is False
    unready_bot.get_channel.assert_not_called()

    # 2. Transient errors on fetch_channel + channel.send recover on attempt 3
    attempts = {"fetch": 0, "send": 0}
    flaky_channel = MagicMock()

    async def flaky_send(embed=None):
        attempts["send"] += 1
        if attempts["send"] == 1:
            raise asyncio.TimeoutError("gateway timeout")

    flaky_channel.send = AsyncMock(side_effect=flaky_send)

    flaky_bot = MagicMock()
    flaky_bot.is_closed.return_value = False
    flaky_bot.is_ready.return_value = True
    flaky_bot.get_channel.return_value = None

    async def flaky_fetch(cid):
        attempts["fetch"] += 1
        if attempts["fetch"] == 1:
            raise aiohttp.ClientError("proxy 503")
        return flaky_channel

    flaky_bot.fetch_channel = AsyncMock(side_effect=flaky_fetch)
    provider_flaky = DiscordBotNotificationProvider(bot=flaky_bot, channel_id=999)
    with patch("app.providers.discord.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        assert await provider_flaky.send(alert) is True
        assert attempts["fetch"] == 3
        assert attempts["send"] == 2
        assert mock_sleep.await_count == 2

    # 3. Transient failure across all 3 attempts returns False and sleeps only twice
    always_fail_ch = MagicMock()
    always_fail_ch.send = AsyncMock(side_effect=aiohttp.ClientError("still down"))
    fail_bot = MagicMock()
    fail_bot.is_closed.return_value = False
    fail_bot.is_ready.return_value = True
    fail_bot.get_channel.return_value = always_fail_ch

    provider_fail = DiscordBotNotificationProvider(bot=fail_bot, channel_id=999)
    with patch("app.providers.discord.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        assert await provider_fail.send(alert) is False
        assert always_fail_ch.send.await_count == 3
        assert mock_sleep.await_count == 2

    # 4. Non-retryable ValueError fails immediately without retry or sleep
    bad_ch = MagicMock()
    bad_ch.send = AsyncMock(side_effect=ValueError("bad embed"))
    bad_bot = MagicMock()
    bad_bot.is_closed.return_value = False
    bad_bot.is_ready.return_value = True
    bad_bot.get_channel.return_value = bad_ch

    provider_bad = DiscordBotNotificationProvider(bot=bad_bot, channel_id=999)
    with patch("app.providers.discord.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        assert await provider_bad.send(alert) is False
        assert bad_ch.send.await_count == 1
        mock_sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_discord_providers_format_zero_sub_1e8_negative_and_nonfinite_prices():
    """DATA-FORMAT-001: Webhook and Bot providers never emit '$0.', '$-5.', '$nan', or '$inf'."""
    from unittest.mock import AsyncMock, MagicMock
    from app.providers.discord import DiscordBotNotificationProvider

    webhook = DiscordWebhookProvider(webhook_url="https://discord.com/api/webhooks/123/abc")

    # Happy & edge finite values
    expected_cases = [
        (0.0, "`$0.00`"),
        (1e-9, "`$0.000000001`"),
        (1e-15, "`$1.00e-15`"),
        (-5.0, "`-$5.0000`"),
        (0.00012300, "`$0.000123`"),
        (68450.5, "`$68,450.50`"),
    ]
    for raw_price, expected_val in expected_cases:
        alert = AlertPayload(ticker="TEST", action="BUY", price=raw_price)
        body = webhook.build_discord_payload(alert)
        price_fields = [f["value"] for f in body["embeds"][0]["fields"] if f["name"] == "💵 Price"]
        assert price_fields == [expected_val]
        assert price_fields[0] != "`$0.`" and "$-" not in price_fields[0]

    # Adversarial non-finite prices are omitted rather than formatted as '$nan'/'$inf'
    for bad_price in (float("nan"), float("inf"), float("-inf")):
        alert = AlertPayload(ticker="TEST", action="BUY", price=bad_price)
        body = webhook.build_discord_payload(alert)
        assert [f for f in body["embeds"][0]["fields"] if f["name"] == "💵 Price"] == []

    # Same guarantees in DiscordBotNotificationProvider.send
    sent_embeds = []
    ch = MagicMock()
    ch.send = AsyncMock(side_effect=lambda embed=None: sent_embeds.append(embed))
    bot = MagicMock()
    bot.is_closed.return_value = False
    bot.is_ready.return_value = True
    bot.get_channel.return_value = ch

    bot_provider = DiscordBotNotificationProvider(bot=bot, channel_id=999)
    assert await bot_provider.send(AlertPayload(ticker="TEST", action="BUY", price=0.0)) is True
    assert await bot_provider.send(AlertPayload(ticker="TEST", action="BUY", price=1e-9)) is True
    assert await bot_provider.send(AlertPayload(ticker="TEST", action="BUY", price=-5.0)) is True
    assert await bot_provider.send(AlertPayload(ticker="TEST", action="BUY", price=float("nan"))) is True

    assert sent_embeds[0].fields[0].value == "`$0.00`"
    assert sent_embeds[1].fields[0].value == "`$0.000000001`"
    assert sent_embeds[2].fields[0].value == "`-$5.0000`"
    assert [f for f in sent_embeds[3].fields if f.name == "💵 Fiyat"] == []

    await webhook.close()
