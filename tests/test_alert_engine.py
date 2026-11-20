import pytest
import pytest_asyncio
from app.config import Settings
from app.database.db import AlertDatabase, AlertRecord
from app.market_data.feed import UnifiedPriceFeed
from app.services.alert_engine import AutonomousAlertEngine


class DummyMarketFeed(UnifiedPriceFeed):
    def __init__(self):
        super().__init__()
        self.mock_prices = {
            "BTCUSDT": 65000.0,
            "THYAO.IS": 300.0,
        }

    async def get_current_price(self, raw_symbol: str):
        from app.market_data.symbols import classify_and_normalize
        norm, _ = classify_and_normalize(raw_symbol)
        return self.mock_prices.get(norm, 100.0)


@pytest_asyncio.fixture
async def setup_engine(tmp_path):
    db_file = str(tmp_path / "test_engine.db")
    db = AlertDatabase(db_path=db_file)
    await db.init_db()

    feed = DummyMarketFeed()
    settings = Settings(MAX_ACTIVE_ALERTS=5, ALERT_EXPIRY_DAYS=30)

    triggered_alerts = []

    async def on_trigger(alert: AlertRecord, current_price: float):
        triggered_alerts.append((alert, current_price))

    engine = AutonomousAlertEngine(
        db=db,
        price_feed=feed,
        settings=settings,
        trigger_callback=on_trigger,
    )

    return engine, triggered_alerts, feed


@pytest.mark.asyncio
async def test_create_alert_direction_above(setup_engine):
    engine, triggered, feed = setup_engine

    # Current BTC price is 65000, target is 68000 -> direction should be ABOVE
    success, msg, record = await engine.create_user_alert(
        user_id="u1",
        user_name="Pax",
        channel_id="c1",
        guild_id="g1",
        raw_symbol="btcusdt",
        target_price=68000.0,
    )
    assert success is True
    assert record is not None
    assert record.symbol == "BTCUSDT"
    assert record.direction == "ABOVE"
    assert record.initial_price == 65000.0


@pytest.mark.asyncio
async def test_create_alert_direction_below(setup_engine):
    engine, triggered, feed = setup_engine

    # Current BTC price is 65000, target is 60000 -> direction should be BELOW
    success, msg, record = await engine.create_user_alert(
        user_id="u1",
        user_name="Pax",
        channel_id="c1",
        guild_id="g1",
        raw_symbol="btcusdt",
        target_price=60000.0,
    )
    assert success is True
    assert record.direction == "BELOW"


@pytest.mark.asyncio
async def test_alert_quota_enforcement(setup_engine):
    engine, triggered, feed = setup_engine
    # Max alerts set to 5 in fixture
    for i in range(5):
        s, m, r = await engine.create_user_alert(
            user_id="u1", user_name="Pax", channel_id="c1", guild_id="g1",
            raw_symbol="btcusdt", target_price=70000.0 + i,
        )
        assert s is True

    # 6th should be rejected
    s, m, r = await engine.create_user_alert(
        user_id="u1", user_name="Pax", channel_id="c1", guild_id="g1",
        raw_symbol="btcusdt", target_price=80000.0,
    )
    assert s is False
    assert "kotası dolu" in m


@pytest.mark.asyncio
async def test_price_tick_triggers_alert(setup_engine):
    engine, triggered, feed = setup_engine

    # Create ABOVE alert at 68000
    await engine.create_user_alert(
        user_id="u1", user_name="Pax", channel_id="c1", guild_id="g1",
        raw_symbol="btcusdt", target_price=68000.0,
    )

    # Tick at 67000: should NOT trigger
    await engine._on_price_tick("BTCUSDT", 67000.0)
    assert len(triggered) == 0

    # Tick at 68500: should trigger!
    await engine._on_price_tick("BTCUSDT", 68500.0)
    assert len(triggered) == 1
    rec, price = triggered[0]
    assert rec.symbol == "BTCUSDT"
    assert price == 68500.0

    # Another tick should not re-trigger because it's already TRIGGERED in DB
    await engine._on_price_tick("BTCUSDT", 69000.0)
    assert len(triggered) == 1


@pytest.mark.asyncio
async def test_percentage_target_price(setup_engine):
    engine, triggered, feed = setup_engine
    # Current BTC price is 65000. Target +10% -> 71500
    success, msg, record = await engine.create_user_alert(
        user_id="u_pct", user_name="Pax", channel_id="c1", guild_id="g1",
        raw_symbol="btcusdt", target_price="+10%",
    )
    assert success is True
    assert record.target_price == 71500.0
    assert record.direction == "ABOVE"
    assert "(+10% hedefi)" in record.note

    # Target -5% -> 61750
    success2, msg2, record2 = await engine.create_user_alert(
        user_id="u_pct", user_name="Pax", channel_id="c1", guild_id="g1",
        raw_symbol="btcusdt", target_price="-5%",
    )
    assert success2 is True
    assert record2.target_price == 61750.0
    assert record2.direction == "BELOW"
    assert "(-5% hedefi)" in record2.note


@pytest.mark.asyncio
async def test_invalid_target_prices(setup_engine):
    engine, triggered, feed = setup_engine
    # Negative target
    s, m, r = await engine.create_user_alert(
        user_id="u1", user_name="Pax", channel_id="c1", guild_id="g1",
        raw_symbol="btcusdt", target_price="-500",
    )
    assert s is False
    assert "0'dan büyük" in m

    # Zero target
    s0, m0, r0 = await engine.create_user_alert(
        user_id="u1", user_name="Pax", channel_id="c1", guild_id="g1",
        raw_symbol="btcusdt", target_price="0",
    )
    assert s0 is False

    # Comma string target (Turkish format: 66000,50)
    sc, mc, rc = await engine.create_user_alert(
        user_id="u1", user_name="Pax", channel_id="c1", guild_id="g1",
        raw_symbol="btcusdt", target_price="66000,50",
    )
    assert sc is True
    assert rc.target_price == 66000.50


@pytest.mark.asyncio
async def test_per_user_quota(tmp_path):
    db_file = str(tmp_path / "test_user_quota.db")
    db = AlertDatabase(db_path=db_file)
    await db.init_db()

    feed = DummyMarketFeed()
    settings = Settings(MAX_ACTIVE_ALERTS=50, MAX_USER_ACTIVE_ALERTS=3)
    engine = AutonomousAlertEngine(db=db, price_feed=feed, settings=settings)

    for i in range(3):
        s, m, r = await engine.create_user_alert(
            user_id="greedy_user", user_name="Greedy", channel_id="c1", guild_id="g1",
            raw_symbol="btcusdt", target_price=70000 + i,
        )
        assert s is True

    # 4th for greedy_user should fail
    s4, m4, r4 = await engine.create_user_alert(
        user_id="greedy_user", user_name="Greedy", channel_id="c1", guild_id="g1",
        raw_symbol="btcusdt", target_price=80000,
    )
    assert s4 is False
    assert "Kişisel aktif alarm" in m4

    # Another user should still be able to create an alert
    so, mo, ro = await engine.create_user_alert(
        user_id="other_user", user_name="Other", channel_id="c1", guild_id="g1",
        raw_symbol="btcusdt", target_price=80000,
    )
    assert so is True


@pytest.mark.asyncio
async def test_price_feed_cache_freshness():
    """TEST-TTL-001: Verify cache hit before TTL and live refetch + cache update after TTL expires."""
    import asyncio
    import time
    from unittest.mock import AsyncMock

    feed = UnifiedPriceFeed(cache_ttl_seconds=0.1)
    feed.yahoo.fetch_price = AsyncMock(return_value=225.0)
    feed.price_cache["AAPL"] = (220.0, time.time())

    # Immediately should return cached without calling upstream fetch_price
    p1 = await feed.get_current_price("AAPL")
    assert p1 == 220.0
    feed.yahoo.fetch_price.assert_not_awaited()

    # Wait for TTL to expire
    await asyncio.sleep(0.15)

    # After TTL expiry, get_current_price must refetch live price and refresh cache
    p2 = await feed.get_current_price("AAPL")
    assert p2 == 225.0
    feed.yahoo.fetch_price.assert_awaited_once_with("AAPL")
    assert feed.price_cache["AAPL"][0] == 225.0


@pytest.mark.asyncio
async def test_on_price_tick_skips_sqlite_for_inactive_symbols(setup_engine, monkeypatch):
    """ENGINE-DB-001: _on_price_tick skips SQLite queries for symbols without active alerts and stays synced."""
    from datetime import datetime, timezone, timedelta
    from unittest.mock import MagicMock
    import aiosqlite

    engine, triggered, feed = setup_engine
    await engine._refresh_active_symbols()

    connect_spy = MagicMock(wraps=aiosqlite.connect)
    monkeypatch.setattr(aiosqlite, "connect", connect_spy)

    # Inactive symbols -> zero SQLite connections
    await engine._on_price_tick("DOGEUSDT", 0.15)
    await engine._on_price_tick("SHIBUSDT", 0.00002)
    connect_spy.assert_not_called()

    # Add alert -> BTCUSDT indexed
    ok, _, rec = await engine.create_user_alert(
        user_id="u1", user_name="Pax", channel_id="c1", guild_id="g1",
        raw_symbol="BTCUSDT", target_price=70000.0,
    )
    assert ok is True
    assert "BTCUSDT" in engine._active_symbols

    # Direct DB cancel_alert (like DiscordAlertBot) removes BTCUSDT from index
    assert await engine.db.cancel_alert(rec.id, user_id="u1") is True
    assert "BTCUSDT" not in engine._active_symbols
    connect_spy.reset_mock()
    await engine._on_price_tick("BTCUSDT", 71000.0)
    connect_spy.assert_not_called()

    # Add another alert, trigger & reset -> index tracks both transitions
    ok2, _, rec2 = await engine.create_user_alert(
        user_id="u1", user_name="Pax", channel_id="c1", guild_id="g1",
        raw_symbol="BTCUSDT", target_price=70000.0,
    )
    assert ok2 is True
    await engine._on_price_tick("BTCUSDT", 71000.0)
    assert len(triggered) == 1
    assert "BTCUSDT" not in engine._active_symbols

    assert await engine.db.reset_alert_to_active(rec2.id) is True
    assert "BTCUSDT" in engine._active_symbols

    # Direct clear_user_alerts removes from index
    assert await engine.db.clear_user_alerts(user_id="u1", status="ACTIVE") == 1
    assert "BTCUSDT" not in engine._active_symbols

    # expire_stale_alerts removes from index
    aid = await engine.db.add_alert("u2", "U2", "c1", None, "ETHUSDT", "CRYPTO", 4000, 3500, "ABOVE")
    assert "ETHUSDT" in engine._active_symbols
    old_ts = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    async with aiosqlite.connect(engine.db.db_path) as conn:
        await conn.execute("UPDATE alerts SET created_at = ? WHERE id = ?;", (old_ts, aid))
        await conn.commit()
    assert await engine.db.expire_stale_alerts(max_age_days=180) == 1
    assert "ETHUSDT" not in engine._active_symbols


@pytest.mark.asyncio
async def test_alerts_not_lost_when_callback_missing_or_not_ready(setup_engine):
    """ENGINE-STARTUP-001: Alerts remain ACTIVE when trigger_callback is None or bot owner is not ready."""
    engine, triggered, feed = setup_engine
    ok, _, rec = await engine.create_user_alert(
        user_id="u1", user_name="Pax", channel_id="c1", guild_id="g1",
        raw_symbol="BTCUSDT", target_price=70000.0,
    )
    assert ok is True

    # 1. trigger_callback is None -> alert must remain ACTIVE
    engine.trigger_callback = None
    await engine._on_price_tick("BTCUSDT", 75000.0)
    still_active = await engine.db.get_alert_by_id(rec.id)
    assert still_active.status == "ACTIVE"

    # 2. Bound callback whose owner has is_closed() == False and is_ready() == False -> must remain ACTIVE
    class _StubBot:
        def __init__(self, ready: bool, closed: bool = False):
            self._ready = ready
            self._closed = closed
            self.calls = []

        def is_closed(self) -> bool:
            return self._closed

        def is_ready(self) -> bool:
            return self._ready

        async def dispatch(self, alert, price):
            self.calls.append((alert.id, price))
            return True

    unready_bot = _StubBot(ready=False, closed=False)
    engine.trigger_callback = unready_bot.dispatch
    await engine._on_price_tick("BTCUSDT", 75000.0)
    assert unready_bot.calls == []
    assert (await engine.db.get_alert_by_id(rec.id)).status == "ACTIVE"

    # 3. Once bot becomes ready -> alert triggers and dispatches
    unready_bot._ready = True
    await engine._on_price_tick("BTCUSDT", 75000.0)
    assert unready_bot.calls == [(rec.id, 75000.0)]
    assert (await engine.db.get_alert_by_id(rec.id)).status == "TRIGGERED"


@pytest.mark.asyncio
async def test_concurrent_create_user_alert_respects_quotas(tmp_path):
    """DATA-RACE-001: Concurrent create_user_alert calls with async price delay never exceed user/global quotas."""
    import asyncio

    class SlowMarketFeed(DummyMarketFeed):
        async def get_current_price(self, raw_symbol: str):
            await asyncio.sleep(0.03)
            return await super().get_current_price(raw_symbol)

    db = AlertDatabase(db_path=str(tmp_path / "race_test.db"))
    await db.init_db()
    feed = SlowMarketFeed()
    settings = Settings(MAX_ACTIVE_ALERTS=5, MAX_USER_ACTIVE_ALERTS=3)
    engine = AutonomousAlertEngine(db=db, price_feed=feed, settings=settings)

    # Launch 10 simultaneous alert creations across 2 users (6 for u1, 4 for u2)
    tasks = [
        engine.create_user_alert(
            user_id="u1", user_name="User1", channel_id="c1", guild_id="g1",
            raw_symbol="BTCUSDT", target_price=70000.0 + i,
        )
        for i in range(6)
    ] + [
        engine.create_user_alert(
            user_id="u2", user_name="User2", channel_id="c1", guild_id="g1",
            raw_symbol="BTCUSDT", target_price=71000.0 + i,
        )
        for i in range(4)
    ]
    results = await asyncio.gather(*tasks)

    u1_ok = sum(1 for ok, _, _ in results[:6] if ok)
    u2_ok = sum(1 for ok, _, _ in results[6:] if ok)
    assert u1_ok <= 3
    assert u2_ok <= 3
    assert u1_ok + u2_ok == 5
    assert await db.count_active_alerts(user_id="u1") == u1_ok
    assert await db.count_active_alerts() == 5


@pytest.mark.asyncio
async def test_non_finite_and_invalid_prices_rejected(setup_engine):
    """F-07a823ab39eb: Reject inf, -inf, nan, +inf%, infk, 1e309, and non-positive current_price."""
    from app.services.alert_engine import parse_target_price_input

    engine, _, feed = setup_engine

    bad_inputs = [
        float("inf"),
        float("-inf"),
        float("nan"),
        "inf",
        "-inf",
        "nan",
        "+inf%",
        "-inf%",
        "nan%",
        "infk",
        "1e309",
        True,
    ]
    for bad in bad_inputs:
        val, err = parse_target_price_input(bad, 65000.0)
        assert val is None, f"Expected rejection for {bad!r}, got {val!r}"
        assert err is not None

        ok, msg, rec = await engine.create_user_alert(
            user_id="u1", user_name="Pax", channel_id="c1", guild_id="g1",
            raw_symbol="BTCUSDT", target_price=bad,
        )
        assert ok is False
        assert rec is None

    # Zero or negative current_price from feed must also be rejected
    feed.mock_prices["BTCUSDT"] = 0.0
    ok_zero, _, rec_zero = await engine.create_user_alert(
        user_id="u1", user_name="Pax", channel_id="c1", guild_id="g1",
        raw_symbol="BTCUSDT", target_price=70000.0,
    )
    assert ok_zero is False
    assert rec_zero is None
    assert await engine.db.count_active_alerts() == 0
