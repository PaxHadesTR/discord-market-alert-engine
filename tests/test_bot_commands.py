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


def _make_mock_interaction(user_id: int = 111, user_name: str = "Trader", channel_id: int = 222):
    interaction = MagicMock()
    interaction.id = 9999
    interaction.user = MagicMock()
    interaction.user.id = user_id
    interaction.user.name = user_name
    interaction.user.mention = f"<@{user_id}>"
    interaction.channel_id = channel_id
    interaction.guild = None
    interaction.guild_id = None
    interaction.response = MagicMock()
    interaction.response.is_done.return_value = False
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


@pytest.mark.asyncio
async def test_list_alerts_handles_zero_current_price():
    """LOGIC-BOT-005: /alert list does not raise ZeroDivisionError when feed returns 0.0."""
    mock_engine = MagicMock()
    alert = AlertRecord(
        id=7,
        user_id="111",
        user_name="Trader",
        channel_id="222",
        guild_id=None,
        symbol="BTCUSDT",
        asset_type="CRYPTO",
        target_price=70000.0,
        initial_price=65000.0,
        direction="ABOVE",
        status="ACTIVE",
        created_at="2026-10-29T20:00:00+00:00",
    )
    mock_engine.db = MagicMock()
    mock_engine.db.get_user_alerts = AsyncMock(return_value=[alert])
    mock_engine.feed = MagicMock()
    mock_engine.feed.get_current_price = AsyncMock(return_value=0.0)

    bot = DiscordAlertBot(engine=mock_engine, settings=Settings(DISCORD_BOT_TOKEN="mock_token"))
    bot._register_commands()

    alert_group = bot.tree.get_command("alert")
    list_cmd = alert_group.get_command("list")
    interaction = _make_mock_interaction(user_id=111)

    await list_cmd.callback(interaction, status="ACTIVE")
    interaction.followup.send.assert_awaited_once()
    embed = interaction.followup.send.call_args.kwargs["embed"]
    assert len(embed.fields) == 1
    assert "Fark:" not in embed.fields[0].value

    await bot.close()


@pytest.mark.asyncio
async def test_watchlist_show_bounds_fields_and_fetches_concurrently():
    """BOT-RES-002: /watchlist show caps at 25 fields with truncation footer, fetches concurrently, isolates errors."""
    import asyncio
    from app.bot.client import MAX_WATCHLIST_ITEMS, WATCHLIST_FETCH_CONCURRENCY

    symbols = [f"SYM{i}" for i in range(30)]
    in_flight = 0
    peak_in_flight = 0

    async def fake_price(sym: str):
        nonlocal in_flight, peak_in_flight
        in_flight += 1
        peak_in_flight = max(peak_in_flight, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        if sym == "SYM3":
            raise RuntimeError("Upstream error")
        return 100.0

    mock_engine = MagicMock()
    mock_engine.db = MagicMock()
    mock_engine.db.get_user_watchlist = AsyncMock(return_value=symbols)
    mock_engine.db.add_to_watchlist = AsyncMock(return_value=True)
    mock_engine.feed = MagicMock()
    mock_engine.feed.get_current_price = fake_price

    bot = DiscordAlertBot(engine=mock_engine, settings=Settings(DISCORD_BOT_TOKEN="mock_token"))
    bot._register_commands()

    wl_group = bot.tree.get_command("watchlist")
    show_cmd = wl_group.get_command("show")
    add_cmd = wl_group.get_command("add")

    interaction = _make_mock_interaction(user_id=111)
    await show_cmd.callback(interaction)

    interaction.followup.send.assert_awaited_once()
    embed = interaction.followup.send.call_args.kwargs["embed"]
    assert len(embed.fields) == MAX_WATCHLIST_ITEMS
    assert "30" in embed.footer.text
    assert peak_in_flight <= WATCHLIST_FETCH_CONCURRENCY
    assert peak_in_flight >= 2

    # /watchlist add enforces 25-item cap and rejects invalid traversal symbols
    interaction_cap = _make_mock_interaction(user_id=111)
    await add_cmd.callback(interaction_cap, symbol="AAPL")
    mock_engine.db.add_to_watchlist.assert_not_awaited()
    assert "limitine" in interaction_cap.followup.send.call_args.kwargs["content"]

    interaction_bad = _make_mock_interaction(user_id=111)
    await add_cmd.callback(interaction_bad, symbol="../../evil?x=1")
    mock_engine.db.add_to_watchlist.assert_not_awaited()
    assert "Geçersiz sembol" in interaction_bad.followup.send.call_args.kwargs["content"]

    await bot.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("malicious", ["@everyone", "@here", "<@&123456>", "<@999999>"])
async def test_public_commands_block_discord_mention_injection(malicious: str):
    """SEC-AUTH-003: /price and /alert set escape mentions and enforce restrictive AllowedMentions."""
    import discord

    mock_engine = MagicMock()
    mock_engine.feed = MagicMock()
    mock_engine.feed.get_current_price = AsyncMock(return_value=None)
    mock_engine.create_user_alert = AsyncMock(return_value=(False, f"Geçersiz fiyat formatı: '{malicious}'", None))

    bot = DiscordAlertBot(engine=mock_engine, settings=Settings(DISCORD_BOT_TOKEN="mock_token"))
    bot._register_commands()

    # 1. /price with malicious symbol
    price_cmd = bot.tree.get_command("price")
    inter_price = _make_mock_interaction(user_id=111)
    await price_cmd.callback(inter_price, symbol=malicious)

    kwargs_price = inter_price.followup.send.call_args.kwargs
    am_price: discord.AllowedMentions = kwargs_price["allowed_mentions"]
    assert am_price.everyone is False
    assert am_price.roles is False
    assert am_price.users is False
    assert malicious not in kwargs_price["content"]

    # 2. /alert set error path with malicious target_price
    alert_group = bot.tree.get_command("alert")
    set_cmd = alert_group.get_command("set")
    inter_set = _make_mock_interaction(user_id=111)
    await set_cmd.callback(inter_set, symbol="BTC", target_price=malicious)

    kwargs_set = inter_set.followup.send.call_args.kwargs
    am_set: discord.AllowedMentions = kwargs_set["allowed_mentions"]
    assert am_set.everyone is False
    assert am_set.roles is False
    assert am_set.users is False
    assert malicious not in kwargs_set["content"]

    await bot.close()


@pytest.mark.asyncio
async def test_alert_action_view_is_bounded_and_stops_on_dismiss():
    """BOT-MEM-001: AlertActionView uses a bounded timeout and stops itself on dismiss to prevent ViewStore leaks."""
    from app.bot.client import AlertActionView

    alert = AlertRecord(
        id=42,
        user_id="111",
        user_name="Trader",
        channel_id="222",
        guild_id=None,
        symbol="BTCUSDT",
        asset_type="CRYPTO",
        target_price=70000.0,
        initial_price=65000.0,
        direction="ABOVE",
        status="TRIGGERED",
        created_at="2026-10-29T20:00:00+00:00",
    )
    mock_engine = MagicMock()
    view = AlertActionView(mock_engine, alert)
    assert view.timeout is not None and view.timeout > 0
    assert view.is_finished() is False

    interaction = _make_mock_interaction(user_id=111)
    await view.dismiss_button.callback(interaction)
    interaction.response.edit_message.assert_awaited_once()
    assert view.is_finished() is True
