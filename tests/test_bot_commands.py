from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from app.bot.client import DiscordAlertBot
from app.config import Settings
from app.database.db import AlertRecord


@pytest.mark.asyncio
async def test_dispatch_alert_notification_handles_zero_initial_price_and_unready_bot():
    """BOT-RES-001: Zero initial_price does not raise ZeroDivisionError; unready bot skips dispatch safely."""
    mock_engine = MagicMock()
    settings = Settings(DISCORD_BOT_TOKEN="mock_token", DISCORD_ALERT_CHANNEL_ID=12345)
    bot = DiscordAlertBot(engine=mock_engine, settings=settings)

    legacy_alert = AlertRecord(
        id=99,
        user_id="111",
        user_name="LegacyUser",
        channel_id="12345",
        guild_id=None,
        symbol="BTCUSDT",
        asset_type="CRYPTO",
        target_price=70000.0,
        initial_price=0.0,
        direction="ABOVE",
        status="TRIGGERED",
        created_at="2026-10-29T20:00:00+00:00",
    )

    # 1. Unready bot (is_closed() == False, is_ready() == False) returns False without calling channel/user APIs
    with patch.object(bot, "is_closed", return_value=False), \
         patch.object(bot, "is_ready", return_value=False), \
         patch.object(bot, "get_channel") as mock_get_ch:
        res_unready = await bot.dispatch_alert_notification(legacy_alert, 71000.0)
        assert res_unready is False
        mock_get_ch.assert_not_called()

    # 2. Ready bot handles initial_price=0.0 cleanly without ZeroDivisionError
    fake_channel = MagicMock()
    fake_channel.send = AsyncMock()
    with patch.object(bot, "is_closed", return_value=False), \
         patch.object(bot, "is_ready", return_value=True), \
         patch.object(bot, "get_channel", return_value=fake_channel):
        res_ok = await bot.dispatch_alert_notification(legacy_alert, 71000.0)
        assert res_ok is True
        fake_channel.send.assert_awaited_once()
        sent_embed = fake_channel.send.call_args.kwargs["embed"]
        assert "BTCUSDT" in sent_embed.description

    await bot.close()
