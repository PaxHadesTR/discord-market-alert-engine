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


@pytest.mark.asyncio
async def test_alert_action_buttons_persist_across_bot_restarts(tmp_path):
    """LOGIC-BOT-004: setup_hook registers DynamicItems encoding alert.id that resolve records after restart."""
    import re
    from app.bot.client import AlertActionView, DismissAlertButton, RearmAlertButton
    from app.database.db import AlertDatabase

    db = AlertDatabase(str(tmp_path / "persist_buttons.db"))
    await db.init_db()
    aid = await db.add_alert(
        user_id="111", user_name="Trader", channel_id="222", guild_id=None,
        symbol="BTCUSDT", asset_type="CRYPTO", target_price=70000.0, initial_price=65000.0, direction="ABOVE",
    )
    record = await db.get_alert_by_id(aid)

    mock_engine = MagicMock()
    mock_engine.db = db

    # 1. AlertActionView encodes alert.id in button custom_ids
    view = AlertActionView(mock_engine, record)
    assert view.rearm_button.custom_id == f"btn_rearm:{aid}"
    assert view.dismiss_button.custom_id == f"btn_dismiss:{aid}"

    # 2. Fresh DiscordAlertBot after restart registers dynamic items in setup_hook
    fresh_bot = DiscordAlertBot(engine=mock_engine, settings=Settings(DISCORD_BOT_TOKEN="mock_token"))
    with patch.object(fresh_bot.tree, "sync", AsyncMock(return_value=[])):
        await fresh_bot.setup_hook()

    # 3. Dynamic RearmAlertButton resolves record from SQLite on fresh bot and opens RearmAlertModal
    match = re.fullmatch(r"btn_rearm:(?P<id>[0-9]+)", f"btn_rearm:{aid}")
    inter_rearm = _make_mock_interaction(user_id=111)
    inter_rearm.client = fresh_bot
    rearm_item = await RearmAlertButton.from_custom_id(inter_rearm, MagicMock(), match)
    await rearm_item.callback(inter_rearm)
    inter_rearm.response.send_modal.assert_awaited_once()

    # 4. Deleted/missing alert ID is handled gracefully with ephemeral message
    match_missing = re.fullmatch(r"btn_rearm:(?P<id>[0-9]+)", "btn_rearm:999999")
    inter_missing = _make_mock_interaction(user_id=111)
    inter_missing.client = fresh_bot
    missing_item = await RearmAlertButton.from_custom_id(inter_missing, MagicMock(), match_missing)
    await missing_item.callback(inter_missing)
    inter_missing.response.send_message.assert_awaited_once()
    assert inter_missing.response.send_message.call_args.kwargs["ephemeral"] is True

    # 5. Dynamic DismissAlertButton works without an in-memory ViewStore entry
    match_dismiss = re.fullmatch(r"btn_dismiss:(?P<id>[0-9]+)", f"btn_dismiss:{aid}")
    inter_dismiss = _make_mock_interaction(user_id=111)
    inter_dismiss.client = fresh_bot
    dismiss_item = await DismissAlertButton.from_custom_id(inter_dismiss, MagicMock(), match_dismiss)
    await dismiss_item.callback(inter_dismiss)
    inter_dismiss.response.edit_message.assert_awaited_once()

    await fresh_bot.close()


@pytest.mark.asyncio
async def test_alert_action_view_enforces_owner_authorization():
    """SEC-AUTH-004: Non-owner users cannot rearm or dismiss another user's AlertActionView buttons."""
    from app.bot.client import AlertActionView

    alert = AlertRecord(
        id=55,
        user_id="111",
        user_name="Owner",
        channel_id="222",
        guild_id="333",
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

    # Non-owner attempts rearm -> blocked ephemerally, no modal opened
    inter_other_rearm = _make_mock_interaction(user_id=999, user_name="Intruder")
    await view.rearm_button.callback(inter_other_rearm)
    inter_other_rearm.response.send_modal.assert_not_awaited()
    inter_other_rearm.response.send_message.assert_awaited_once()
    assert inter_other_rearm.response.send_message.call_args.kwargs["ephemeral"] is True

    # Non-owner attempts dismiss -> blocked ephemerally, message not edited, view still active
    inter_other_dismiss = _make_mock_interaction(user_id=999, user_name="Intruder")
    await view.dismiss_button.callback(inter_other_dismiss)
    inter_other_dismiss.response.edit_message.assert_not_awaited()
    inter_other_dismiss.response.send_message.assert_awaited_once()
    assert inter_other_dismiss.response.send_message.call_args.kwargs["ephemeral"] is True
    assert view.is_finished() is False

    # Owner succeeds for both rearm and dismiss
    inter_owner_rearm = _make_mock_interaction(user_id=111, user_name="Owner")
    await view.rearm_button.callback(inter_owner_rearm)
    inter_owner_rearm.response.send_modal.assert_awaited_once()

    inter_owner_dismiss = _make_mock_interaction(user_id=111, user_name="Owner")
    await view.dismiss_button.callback(inter_owner_dismiss)
    inter_owner_dismiss.response.edit_message.assert_awaited_once()
    assert view.is_finished() is True


@pytest.mark.asyncio
async def test_slash_commands_and_rearm_modal_happy_paths():
    """TEST-COVERAGE-003: Cover /alert set, /alert delete, /alert clear, /price, /stats, /help, and RearmAlertModal."""
    from app.bot.client import RearmAlertModal

    record = AlertRecord(
        id=10,
        user_id="111",
        user_name="Trader",
        channel_id="222",
        guild_id=None,
        symbol="BTCUSDT",
        asset_type="CRYPTO",
        target_price=72000.0,
        initial_price=68000.0,
        direction="ABOVE",
        status="ACTIVE",
        created_at="2026-10-29T20:00:00+00:00",
        note="Breakout",
    )

    mock_engine = MagicMock()
    mock_engine.create_user_alert = AsyncMock(return_value=(True, "OK", record))
    mock_engine.db = MagicMock()
    mock_engine.db.cancel_alert = AsyncMock(return_value=True)
    mock_engine.db.clear_user_alerts = AsyncMock(return_value=2)
    mock_engine.db.get_database_stats = AsyncMock(
        return_value={"active_alerts": 3, "triggered_alerts": 5, "watchlist_items": 4}
    )
    mock_engine.feed = MagicMock()
    mock_engine.feed.get_current_price = AsyncMock(return_value=68000.0)

    bot = DiscordAlertBot(engine=mock_engine, settings=Settings(DISCORD_BOT_TOKEN="mock_token"))
    bot._register_commands()

    # /alert set happy path
    alert_group = bot.tree.get_command("alert")
    inter_set = _make_mock_interaction(user_id=111)
    await alert_group.get_command("set").callback(inter_set, symbol="BTC", target_price="72000", note="Breakout", dm=False)
    inter_set.followup.send.assert_awaited_once()
    assert "BTCUSDT" in inter_set.followup.send.call_args.kwargs["embed"].title

    # /alert delete happy path
    inter_del = _make_mock_interaction(user_id=111)
    await alert_group.get_command("delete").callback(inter_del, alert_id=10)
    mock_engine.db.cancel_alert.assert_awaited_once_with(alert_id=10, user_id="111")

    # /alert clear happy path
    inter_clr = _make_mock_interaction(user_id=111)
    await alert_group.get_command("clear").callback(inter_clr, scope="TRIGGERED")
    mock_engine.db.clear_user_alerts.assert_awaited_once_with(user_id="111", status="TRIGGERED")

    # /price happy path
    inter_price = _make_mock_interaction(user_id=111)
    await bot.tree.get_command("price").callback(inter_price, symbol="BTC")
    assert "BTC" in inter_price.followup.send.call_args.kwargs["embed"].title

    # /stats and /help happy paths
    inter_stats = _make_mock_interaction(user_id=111)
    await bot.tree.get_command("stats").callback(inter_stats)
    assert len(inter_stats.followup.send.call_args.kwargs["embed"].fields) == 6

    inter_help = _make_mock_interaction(user_id=111)
    await bot.tree.get_command("help").callback(inter_help)
    assert inter_help.followup.send.call_args.kwargs["ephemeral"] is True

    # RearmAlertModal submission
    modal = RearmAlertModal(mock_engine, record)
    modal.target_price._value = "75000"
    modal.note._value = "Next leg"
    inter_modal = _make_mock_interaction(user_id=111)
    await modal.on_submit(inter_modal)
    inter_modal.followup.send.assert_awaited_once()
    assert "yeniden aktif edildi" in inter_modal.followup.send.call_args.kwargs["content"]

    await bot.close()


@pytest.mark.asyncio
async def test_sec_valid_001_bounds_note_symbol_and_legacy_embed_fields():
    """SEC-VALID-001: Oversized notes/symbols are clamped and legacy 1500-char DB notes never exceed Discord embed limits."""
    oversized_note = "N" * 2000
    oversized_symbol = "S" * 3000
    legacy_note = "L" * 1500

    legacy_record = AlertRecord(
        id=88,
        user_id="111",
        user_name="Trader",
        channel_id="222",
        guild_id=None,
        symbol="BTCUSDT",
        asset_type="CRYPTO",
        target_price=72000.0,
        initial_price=68000.0,
        direction="ABOVE",
        status="ACTIVE",
        created_at="2026-10-29T20:00:00+00:00",
        note=legacy_note,
    )

    captured_create_kwargs = {}

    async def fake_create_user_alert(**kwargs):
        captured_create_kwargs.update(kwargs)
        rec = AlertRecord(
            id=89,
            user_id=kwargs["user_id"],
            user_name=kwargs["user_name"],
            channel_id=kwargs["channel_id"],
            guild_id=kwargs["guild_id"],
            symbol=kwargs["raw_symbol"],
            asset_type="CRYPTO",
            target_price=72000.0,
            initial_price=68000.0,
            direction="ABOVE",
            status="ACTIVE",
            created_at="2026-10-29T20:00:00+00:00",
            note=kwargs["note"],
        )
        return True, "OK", rec

    mock_engine = MagicMock()
    mock_engine.create_user_alert = AsyncMock(side_effect=fake_create_user_alert)
    mock_engine.db = MagicMock()
    mock_engine.db.get_user_alerts = AsyncMock(return_value=[legacy_record])
    mock_engine.feed = MagicMock()
    mock_engine.feed.get_current_price = AsyncMock(return_value=None)

    bot = DiscordAlertBot(engine=mock_engine, settings=Settings(DISCORD_BOT_TOKEN="mock_token", DISCORD_ALERT_CHANNEL_ID=222))
    bot._register_commands()

    # 1. /alert set truncates 2000-char note to <= 150 chars before DB persistence and bounds embed fields
    alert_group = bot.tree.get_command("alert")
    inter_set = _make_mock_interaction(user_id=111)
    await alert_group.get_command("set").callback(
        inter_set, symbol="BTCUSDT", target_price="72000", note=oversized_note, dm=False
    )
    assert len(captured_create_kwargs["note"]) <= 150
    set_embed = inter_set.followup.send.call_args.kwargs["embed"]
    assert len(set_embed.title) <= 256
    for f in set_embed.fields:
        assert len(f.name) <= 256
        assert len(f.value) <= 1024

    # 2. /alert set error and /price error with 3000-char symbol stay <= 2000 chars
    mock_engine.create_user_alert = AsyncMock(return_value=(False, f"Hata: {oversized_symbol}", None))
    inter_set_err = _make_mock_interaction(user_id=111)
    await alert_group.get_command("set").callback(inter_set_err, symbol=oversized_symbol, target_price="72000")
    assert len(inter_set_err.followup.send.call_args.kwargs["content"]) <= 2000

    inter_price_err = _make_mock_interaction(user_id=111)
    await bot.tree.get_command("price").callback(inter_price_err, symbol=oversized_symbol)
    assert len(inter_price_err.followup.send.call_args.kwargs["content"]) <= 2000

    # 3. Legacy 1500-char DB note in /alert list and dispatch_alert_notification stays strictly <= 1024 chars
    inter_list = _make_mock_interaction(user_id=111)
    await alert_group.get_command("list").callback(inter_list, status="ACTIVE")
    list_embed = inter_list.followup.send.call_args.kwargs["embed"]
    for f in list_embed.fields:
        assert len(f.name) <= 256
        assert len(f.value) <= 1024

    fake_channel = MagicMock()
    fake_channel.send = AsyncMock()
    with patch.object(bot, "is_closed", return_value=False), \
         patch.object(bot, "is_ready", return_value=True), \
         patch.object(bot, "get_channel", return_value=fake_channel):
        delivered = await bot.dispatch_alert_notification(legacy_record, 73000.0)
        assert delivered is True
        sent_embed = fake_channel.send.call_args.kwargs["embed"]
        assert len(sent_embed.title) <= 256
        assert len(sent_embed.description) <= 4096
        for f in sent_embed.fields:
            assert len(f.name) <= 256
            assert len(f.value) <= 1024

    await bot.close()


@pytest.mark.asyncio
async def test_rearm_modal_truncates_oversized_alert_notes(tmp_path):
    """F-2d63a0765cef: RearmAlertModal truncates alert.note default to max_length=150."""
    from app.bot.client import RearmAlertModal
    from app.database.db import AlertDatabase
    from app.services.alert_engine import AutonomousAlertEngine

    # 1. Direct AlertRecord with 200-char note and 145-char + percentage suffix note
    for raw_note in ("A" * 200, ("B" * 145) + " (+5% hedefi)"):
        rec = AlertRecord(
            id=1,
            user_id="111",
            user_name="Trader",
            channel_id="222",
            guild_id=None,
            symbol="BTCUSDT",
            asset_type="CRYPTO",
            target_price=72000.0,
            initial_price=68000.0,
            direction="ABOVE",
            status="TRIGGERED",
            created_at="2026-10-29T20:00:00+00:00",
            note=raw_note,
        )
        modal = RearmAlertModal(MagicMock(), rec)
        assert modal.note.default == raw_note[:150]
        assert len(modal.note.default) <= 150

    # 2. Integration with create_user_alert appending '(+5% hedefi)'
    db = AlertDatabase(str(tmp_path / "rearm_note.db"))
    await db.init_db()
    feed = MagicMock()
    feed.get_current_price = AsyncMock(return_value=68000.0)
    feed.add_listener = MagicMock()
    feed.set_active_equity_supplier = MagicMock()
    engine = AutonomousAlertEngine(db=db, price_feed=feed, settings=Settings())

    ok, _, created = await engine.create_user_alert(
        user_id="111",
        user_name="Trader",
        channel_id="222",
        guild_id=None,
        raw_symbol="BTCUSDT",
        target_price="+5%",
        note="C" * 145,
    )
    assert ok is True and created is not None
    modal_int = RearmAlertModal(engine, created)
    assert len(modal_int.note.default) <= 150
    assert modal_int.note.default == created.note[:150]


@pytest.mark.asyncio
async def test_list_alerts_skips_inactive_dedupes_and_fetches_concurrently():
    """RES-BOT-002: /alert list skips non-ACTIVE lookups, dedupes symbols, bounds concurrency, and isolates errors."""
    import asyncio
    from app.bot.client import WATCHLIST_FETCH_CONCURRENCY

    # 1. Non-ACTIVE alerts never invoke feed.get_current_price
    inactive_alerts = [
        AlertRecord(
            id=idx,
            user_id="111",
            user_name="Trader",
            channel_id="222",
            guild_id=None,
            symbol=f"SYM{idx}",
            asset_type="CRYPTO",
            target_price=100.0,
            initial_price=90.0,
            direction="ABOVE",
            status=st,
            created_at="2026-10-29T20:00:00+00:00",
        )
        for idx, st in enumerate(("TRIGGERED", "CANCELLED", "EXPIRED"), start=1)
    ]

    mock_engine = MagicMock()
    mock_engine.db = MagicMock()
    mock_engine.db.get_user_alerts = AsyncMock(return_value=inactive_alerts)
    mock_engine.feed = MagicMock()
    mock_engine.feed.get_current_price = AsyncMock(return_value=95.0)

    bot = DiscordAlertBot(engine=mock_engine, settings=Settings(DISCORD_BOT_TOKEN="mock_token"))
    bot._register_commands()
    list_cmd = bot.tree.get_command("alert").get_command("list")

    inter_inactive = _make_mock_interaction(user_id=111)
    await list_cmd.callback(inter_inactive, status="ALL")
    mock_engine.feed.get_current_price.assert_not_awaited()
    assert len(inter_inactive.followup.send.call_args.kwargs["embed"].fields) == 3

    # 2. 15 ACTIVE alerts across 10 distinct symbols (5 duplicates), 1 symbol raises RuntimeError
    active_symbols = [f"COIN{i}USDT" for i in range(10)] + [f"COIN{i}USDT" for i in range(5)]
    active_alerts = [
        AlertRecord(
            id=100 + idx,
            user_id="111",
            user_name="Trader",
            channel_id="222",
            guild_id=None,
            symbol=sym,
            asset_type="CRYPTO",
            target_price=120.0,
            initial_price=100.0,
            direction="ABOVE",
            status="ACTIVE",
            created_at="2026-10-29T20:00:00+00:00",
        )
        for idx, sym in enumerate(active_symbols)
    ]

    in_flight = 0
    peak_in_flight = 0
    fetched = []

    async def fake_price(sym: str):
        nonlocal in_flight, peak_in_flight
        fetched.append(sym)
        in_flight += 1
        peak_in_flight = max(peak_in_flight, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        if sym == "COIN3USDT":
            raise RuntimeError("Upstream error")
        return 110.0

    mock_engine.db.get_user_alerts = AsyncMock(return_value=active_alerts)
    mock_engine.feed.get_current_price = fake_price

    inter_active = _make_mock_interaction(user_id=111)
    await list_cmd.callback(inter_active, status="ACTIVE")
    embed = inter_active.followup.send.call_args.kwargs["embed"]
    assert len(embed.fields) == 15
    assert len(fetched) == 10
    assert len(set(fetched)) == 10
    assert 1 < peak_in_flight <= WATCHLIST_FETCH_CONCURRENCY

    await bot.close()


def test_format_price_handles_zero_sub_1e8_and_nonfinite():
    """LOGIC-FORMAT-003: format_price never emits malformed '$0.' for zero, sub-1e-8, or non-finite values."""
    from app.bot.client import format_price

    assert format_price(0.0) == "$0.00"
    assert format_price(1e-9) == "$0.000000001"
    assert not format_price(1e-9).endswith(".")
    assert not format_price(1e-15).endswith(".")
    assert format_price(0.00001230) == "$0.0000123"
    assert format_price(1.5) == "$1.5000"
    assert format_price(68450.5) == "$68,450.50"
    assert format_price(None) == "*Alınamadı*"
    assert format_price(float("nan")) == "*Alınamadı*"
    assert format_price(float("inf")) == "*Alınamadı*"
    assert format_price(float("-inf")) == "*Alınamadı*"


@pytest.mark.asyncio
async def test_dispatch_alert_notification_dm_retry_fallback_and_channel_failures():
    """V6-COVERAGE-001: Cover DM success, DM transient retry, DM->channel fallback, fetch_channel error, and channel retry exhaustion."""
    import aiohttp

    mock_engine = MagicMock()
    settings = Settings(DISCORD_BOT_TOKEN="mock_token", DISCORD_ALERT_CHANNEL_ID=12345)
    bot = DiscordAlertBot(engine=mock_engine, settings=settings)

    dm_alert = AlertRecord(
        id=201,
        user_id="111",
        user_name="Trader",
        channel_id="12345",
        guild_id=None,
        symbol="BTCUSDT",
        asset_type="CRYPTO",
        target_price=70000.0,
        initial_price=65000.0,
        direction="ABOVE",
        status="TRIGGERED",
        created_at="2026-10-29T20:00:00+00:00",
        notify_dm=True,
    )
    ch_alert = AlertRecord(
        id=202,
        user_id="111",
        user_name="Trader",
        channel_id="12345",
        guild_id=None,
        symbol="ETHUSDT",
        asset_type="CRYPTO",
        target_price=3500.0,
        initial_price=3200.0,
        direction="ABOVE",
        status="TRIGGERED",
        created_at="2026-10-29T20:00:00+00:00",
        notify_dm=False,
    )

    with patch.object(bot, "is_closed", return_value=False), \
         patch.object(bot, "is_ready", return_value=True), \
         patch("app.bot.client.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:

        # (a) notify_dm=True happy path delivers via user.send without calling channel.send
        dm_user = MagicMock()
        dm_user.send = AsyncMock()
        ch_unused = MagicMock()
        ch_unused.send = AsyncMock()
        with patch.object(bot, "get_user", return_value=dm_user), \
             patch.object(bot, "get_channel", return_value=ch_unused):
            assert await bot.dispatch_alert_notification(dm_alert, 70500.0) is True
            dm_user.send.assert_awaited_once()
            ch_unused.send.assert_not_awaited()

        # (b) notify_dm=True transient aiohttp.ClientError retries and succeeds on attempt 2
        dm_attempts = 0

        async def flaky_dm_send(**kwargs):
            nonlocal dm_attempts
            dm_attempts += 1
            if dm_attempts == 1:
                raise aiohttp.ClientError("transient proxy error")

        flaky_user = MagicMock()
        flaky_user.send = AsyncMock(side_effect=flaky_dm_send)
        mock_sleep.reset_mock()
        with patch.object(bot, "get_user", return_value=flaky_user), \
             patch.object(bot, "get_channel", return_value=ch_unused):
            assert await bot.dispatch_alert_notification(dm_alert, 70500.0) is True
            assert dm_attempts == 2
            assert mock_sleep.await_count == 1
            ch_unused.send.assert_not_awaited()

        # (c) notify_dm=True non-retryable failure (e.g., DM blocked / Forbidden) falls back to channel.send
        blocked_user = MagicMock()
        blocked_user.send = AsyncMock(side_effect=RuntimeError("Forbidden: Cannot send messages to this user"))
        fallback_channel = MagicMock()
        fallback_channel.send = AsyncMock()
        with patch.object(bot, "get_user", return_value=blocked_user), \
             patch.object(bot, "get_channel", return_value=fallback_channel):
            assert await bot.dispatch_alert_notification(dm_alert, 70500.0) is True
            blocked_user.send.assert_awaited_once()
            fallback_channel.send.assert_awaited_once()

        # (d) get_channel returning None + fetch_channel raising returns False
        with patch.object(bot, "get_channel", return_value=None), \
             patch.object(bot, "fetch_channel", new_callable=AsyncMock, side_effect=RuntimeError("Not Found")):
            assert await bot.dispatch_alert_notification(ch_alert, 3600.0) is False

        # (e) channel.send failing all 3 aiohttp.ClientError attempts returns False with 2 sleeps
        failing_channel = MagicMock()
        failing_channel.send = AsyncMock(side_effect=aiohttp.ClientError("gateway down"))
        mock_sleep.reset_mock()
        with patch.object(bot, "get_channel", return_value=failing_channel):
            assert await bot.dispatch_alert_notification(ch_alert, 3600.0) is False
            assert failing_channel.send.await_count == 3
            assert mock_sleep.await_count == 2

    await bot.close()


@pytest.mark.asyncio
async def test_command_error_handler_redacts_proxy_credentials_and_hides_raw_exceptions():
    """SEC-SECRETS-001: on_app_command_error redacts proxy userinfo, hides raw exception text, and bounds reply length."""
    from discord import app_commands

    u, p = "proxyacct", "proxypwd99"
    proxy_url = f"http://{u}:{p}@proxy.internal:8080"

    bot = DiscordAlertBot(
        engine=MagicMock(),
        settings=Settings(DISCORD_BOT_TOKEN="mock_token"),
        proxy_override=proxy_url,
    )
    bot._setup_error_handler()

    # 1. Proxy 503 error masks credentials in ephemeral response
    inter_proxy = _make_mock_interaction(user_id=111)
    inter_proxy.command = MagicMock(name="price")
    inter_proxy.command.name = "price"
    await bot.tree.on_error(inter_proxy, app_commands.AppCommandError(f"503 ClientHttpProxyError via {proxy_url}"))

    proxy_msg = inter_proxy.response.send_message.call_args.args[0]
    assert u not in proxy_msg and p not in proxy_msg
    assert "http://***@proxy.internal:8080" in proxy_msg
    assert len(proxy_msg) <= 2000
    assert inter_proxy.response.send_message.call_args.kwargs["ephemeral"] is True

    # 2. Non-proxy error never reflects raw exception internals or oversized strings
    inter_generic = _make_mock_interaction(user_id=111)
    inter_generic.command = MagicMock()
    inter_generic.command.name = "alert"
    inter_generic.response.is_done.return_value = True
    secret_detail = "INTERNAL_DB_SECRET_" + ("X" * 3000)
    await bot.tree.on_error(inter_generic, app_commands.AppCommandError(secret_detail))

    generic_msg = inter_generic.followup.send.call_args.args[0]
    assert "INTERNAL_DB_SECRET_" not in generic_msg
    assert len(generic_msg) <= 2000
    assert inter_generic.followup.send.call_args.kwargs["ephemeral"] is True

    await bot.close()


@pytest.mark.asyncio
async def test_watchlist_add_concurrent_requests_never_exceed_quota(tmp_path):
    """CONC-BOT-001: Concurrent /watchlist add commands serialize quota check + insert and never exceed MAX_WATCHLIST_ITEMS."""
    import asyncio
    from app.bot.client import MAX_WATCHLIST_ITEMS
    from app.database.db import AlertDatabase

    db = AlertDatabase(str(tmp_path / "wl_race.db"))
    await db.init_db()
    # Pre-populate 23 items so only 2 slots remain
    for i in range(MAX_WATCHLIST_ITEMS - 2):
        assert await db.add_to_watchlist("u1", f"SYM{i}", "EQUITY_FOREX") is True

    orig_get = db.get_user_watchlist

    async def slow_get(uid: str):
        items = await orig_get(uid)
        await asyncio.sleep(0.02)
        return items

    db.get_user_watchlist = slow_get

    mock_engine = MagicMock()
    mock_engine.db = db
    mock_engine.feed = MagicMock()
    mock_engine.feed.get_current_price = AsyncMock(return_value=100.0)

    bot = DiscordAlertBot(engine=mock_engine, settings=Settings(DISCORD_BOT_TOKEN="mock_token"))
    bot._register_commands()
    add_cmd = bot.tree.get_command("watchlist").get_command("add")

    # Fire 6 concurrent additions for distinct symbols when only 2 slots remain
    interactions = [_make_mock_interaction(user_id=1) for _ in range(6)]
    await asyncio.gather(
        *[add_cmd.callback(inter, symbol=f"NEW{i}") for i, inter in enumerate(interactions)]
    )

    final_items = await orig_get("1")
    # Wait: pre-populated user was "u1"; run again for user "1" with 23 pre-populated items!
    assert len(final_items) == 6

    # Now test actual cap enforcement for user "99" starting at MAX_WATCHLIST_ITEMS - 2
    for i in range(MAX_WATCHLIST_ITEMS - 2):
        assert await db.add_to_watchlist("99", f"BASE{i}", "EQUITY_FOREX") is True
    cap_interactions = [_make_mock_interaction(user_id=99) for _ in range(6)]
    await asyncio.gather(
        *[add_cmd.callback(inter, symbol=f"CAP{i}") for i, inter in enumerate(cap_interactions)]
    )
    capped_items = await orig_get("99")
    assert len(capped_items) == MAX_WATCHLIST_ITEMS
    added_replies = sum(
        1 for inter in cap_interactions if "takip listenize eklendi" in inter.followup.send.call_args.kwargs["content"]
    )
    rejected_replies = sum(
        1 for inter in cap_interactions if "limitine" in inter.followup.send.call_args.kwargs["content"]
    )
    assert added_replies == 2
    assert rejected_replies == 4

    await bot.close()
