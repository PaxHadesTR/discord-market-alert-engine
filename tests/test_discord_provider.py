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
