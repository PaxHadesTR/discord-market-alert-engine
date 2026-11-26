import asyncio
import json
import time
from unittest.mock import patch
import pytest
from app.market_data.binance import BinanceMarketFeed
from app.market_data.feed import UnifiedPriceFeed


@pytest.mark.asyncio
async def test_binance_ws_reader_not_stalled_by_slow_callback():
    """BINANCE-CONC-001: Slow alert callback on one ticker never stalls ws.recv() or other ticker cache updates."""
    frame1 = json.dumps([
        {"s": "SLOWUSDT", "c": "100.0"},
        {"s": "BTCUSDT", "c": "68000.0"},
    ])
    frame2 = json.dumps({"s": "ETHUSDT", "c": "3500.0"})

    frames = [frame1, frame2]
    recv_timestamps = []
    block_event = asyncio.Event()

    class _FakeWS:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def recv(self):
            if frames:
                msg = frames.pop(0)
                recv_timestamps.append(time.monotonic())
                return msg
            await block_event.wait()
            raise asyncio.CancelledError

    binance = BinanceMarketFeed(dispatch_concurrency=4, max_dispatch_queue=64)
    feed = UnifiedPriceFeed(binance_feed=binance)

    slow_started = asyncio.Event()
    slow_release = asyncio.Event()

    async def slow_listener(symbol: str, price: float):
        if symbol == "SLOWUSDT":
            slow_started.set()
            await slow_release.wait()

    feed.add_listener(slow_listener)

    with patch("app.market_data.binance.websockets.connect", return_value=_FakeWS()):
        t0 = time.monotonic()
        binance.start_stream(feed._handle_price_tick)

        # Wait for slow listener to start blocking on SLOWUSDT
        await asyncio.wait_for(slow_started.wait(), timeout=2.0)

        # While SLOWUSDT is still blocked indefinitely, frame2 must already be drained
        # and BTCUSDT + ETHUSDT must already be updated in UnifiedPriceFeed.price_cache
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if "BTCUSDT" in feed.price_cache and "ETHUSDT" in feed.price_cache and len(recv_timestamps) == 2:
                break
            await asyncio.sleep(0.01)

        elapsed = time.monotonic() - t0
        assert len(recv_timestamps) == 2
        assert feed.price_cache["BTCUSDT"][0] == 68000.0
        assert feed.price_cache["ETHUSDT"][0] == 3500.0
        assert elapsed < 1.0  # Did not wait for slow callback

        # Stop cleanly cancels and drains all background workers even while slow callback is suspended
        await binance.stop()
        assert binance._task is None
        assert binance._dispatch_workers == []
        assert binance._dispatch_queue is None


@pytest.mark.asyncio
async def test_yahoo_start_polling_reuses_existing_client():
    """YAHOO-MEM-001: start_polling reuses an already-open AsyncClient instead of leaking it."""
    import httpx
    from app.market_data.yahoo import YahooFinanceFeed

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"chart": {"result": [{"meta": {"regularMarketPrice": 225.5}}]}})

    feed = YahooFinanceFeed(poll_interval_seconds=60.0)
    feed._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    existing_client = feed._client

    price = await feed.fetch_price("AAPL")
    assert price == 225.5
    assert feed._client is existing_client

    async def get_syms():
        return []

    async def on_update(sym, p):
        pass

    feed.start_polling(get_syms, on_update)
    assert feed._client is existing_client
    assert not existing_client.is_closed

    await feed.stop()
    assert existing_client.is_closed
    assert feed._client is None


@pytest.mark.asyncio
async def test_yahoo_poll_loop_bounds_concurrency():
    """YAHOO-RES-001: _poll_loop bounds concurrent outbound requests via Semaphore while updating all symbols."""
    from app.market_data.yahoo import YahooFinanceFeed

    max_limit = 5
    feed = YahooFinanceFeed(poll_interval_seconds=60.0, max_concurrent_requests=max_limit)

    in_flight = 0
    peak_in_flight = 0
    updated = {}
    all_done = asyncio.Event()
    symbols = [f"SYM{i}" for i in range(30)]

    async def fake_fetch_price(symbol: str):
        nonlocal in_flight, peak_in_flight
        in_flight += 1
        peak_in_flight = max(peak_in_flight, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return 100.0 + int(symbol.replace("SYM", ""))

    feed.fetch_price = fake_fetch_price

    async def get_syms():
        return symbols

    async def on_update(sym: str, price: float):
        updated[sym] = price
        if len(updated) == len(symbols):
            all_done.set()

    feed.start_polling(get_syms, on_update)
    await asyncio.wait_for(all_done.wait(), timeout=5.0)
    await feed.stop()

    assert len(updated) == 30
    assert peak_in_flight <= max_limit
    assert peak_in_flight >= 2


@pytest.mark.asyncio
async def test_yahoo_rejects_url_traversal_and_allows_valid_symbols():
    """F-b688e6beb214: Malformed/traversal symbols are rejected without HTTP calls; valid Yahoo symbols succeed."""
    import httpx
    from app.market_data.yahoo import YahooFinanceFeed

    requested_paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        return httpx.Response(200, json={"chart": {"result": [{"meta": {"regularMarketPrice": 123.45}}]}})

    feed = YahooFinanceFeed()
    feed._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    # Adversarial symbols must never trigger outbound requests
    malicious = [
        "../../v10/finance/quoteSummary/AAPL",
        "AAPL?crumb=evil&foo=bar",
        "AAPL#fragment",
        "AAPL/extra",
        "AA PL",
        "AAPL\r\nX-Injected: 1",
        "..",
        "A" * 25,
    ]
    for bad in malicious:
        assert await feed.fetch_price(bad) is None
        assert await feed._fetch_direct_price(bad) is None
    assert requested_paths == []

    # Valid Yahoo Finance catalog symbols must continue working
    valid = ["^NDX", "GC=F", "USDTRY=X", "DX-Y.NYB", "THYAO.IS", "000001.SS"]
    for sym in valid:
        price = await feed.fetch_price(sym)
        assert price == 123.45

    assert len(requested_paths) == len(valid)
    for path in requested_paths:
        assert path.startswith("/v8/finance/chart/")

    await feed.stop()


@pytest.mark.asyncio
async def test_yahoo_fetch_price_regular_close_fallback_bist_and_errors():
    """TEST-COVERAGE-003: YahooFinanceFeed covers regularMarketPrice, close fallback, BIST .IS fallback, 404/500/timeout."""
    import httpx
    from app.market_data.yahoo import YahooFinanceFeed

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/AAPL"):
            return httpx.Response(200, json={"chart": {"result": [{"meta": {"regularMarketPrice": 215.75}}]}})
        if path.endswith("/CLOSEONLY"):
            return httpx.Response(
                200,
                json={
                    "chart": {
                        "result": [
                            {
                                "meta": {},
                                "indicators": {"quote": [{"close": [10.0, None, 12.5, None]}]},
                            }
                        ]
                    }
                },
            )
        if path.endswith("/THYAO"):
            return httpx.Response(404, json={"chart": {"result": None}})
        if path.endswith("/THYAO.IS"):
            return httpx.Response(200, json={"chart": {"result": [{"meta": {"regularMarketPrice": 312.4}}]}})
        if path.endswith("/SERVERERR") or path.endswith("/SERVERERR.IS"):
            return httpx.Response(500, text="Internal Server Error")
        if path.endswith("/TIMEOUT") or path.endswith("/TIMEOUT.IS"):
            raise httpx.ReadTimeout("timed out")
        return httpx.Response(404)

    feed = YahooFinanceFeed()
    feed._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    assert await feed.fetch_price("AAPL") == 215.75
    assert await feed.fetch_price("CLOSEONLY") == 12.5
    # BIST .IS fallback when direct THYAO returns 404
    assert await feed.fetch_price("THYAO") == 312.4
    # HTTP 500 & timeout degrade gracefully to None
    assert await feed.fetch_price("SERVERERR") is None
    assert await feed.fetch_price("TIMEOUT") is None
    assert await feed.fetch_price("UNKNOWN99") is None

    await feed.stop()


@pytest.mark.asyncio
async def test_binance_rest_fetch_price_happy_and_error_paths():
    """TEST-COVERAGE-003: BinanceMarketFeed REST fetch_price handles 200, 400/500, and network timeouts."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        sym = request.url.params.get("symbol")
        if sym == "BTCUSDT":
            return httpx.Response(200, json={"symbol": "BTCUSDT", "price": "68420.50"})
        if sym == "ERRUSDT":
            return httpx.Response(500, text="Binance error")
        if sym == "TIMEOUTUSDT":
            raise httpx.ReadTimeout("timeout")
        return httpx.Response(400, json={"code": -1121, "msg": "Invalid symbol."})

    feed = BinanceMarketFeed()
    feed._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    assert await feed.fetch_price("btcusdt") == 68420.50
    assert await feed.fetch_price("ERRUSDT") is None
    assert await feed.fetch_price("TIMEOUTUSDT") is None
    assert await feed.fetch_price("INVALID") is None

    await feed.stop()


@pytest.mark.asyncio
async def test_unified_feed_crypto_routing_and_none_cache_behavior():
    """V6-COVERAGE-002: UnifiedPriceFeed routes CRYPTO to binance, caches valid quotes, and never caches None."""
    from unittest.mock import AsyncMock, MagicMock

    binance = MagicMock()
    binance.fetch_price = AsyncMock(return_value=68500.0)
    yahoo = MagicMock()
    yahoo.fetch_price = AsyncMock(return_value=210.0)

    feed = UnifiedPriceFeed(binance_feed=binance, yahoo_feed=yahoo, cache_ttl_seconds=20.0)

    # (a) CRYPTO symbol ('BTC' -> 'BTCUSDT') routes to binance.fetch_price (not yahoo) and caches result
    price = await feed.get_current_price("BTC")
    assert price == 68500.0
    binance.fetch_price.assert_awaited_once_with("BTCUSDT")
    yahoo.fetch_price.assert_not_awaited()
    assert feed.price_cache["BTCUSDT"][0] == 68500.0

    # Cached hit avoids a second network call
    assert await feed.get_current_price("btc") == 68500.0
    assert binance.fetch_price.await_count == 1

    # (b) None from binance or yahoo does not populate cache and does not overwrite an expired entry with None
    binance.fetch_price = AsyncMock(return_value=None)
    assert await feed.get_current_price("ETH") is None
    assert "ETHUSDT" not in feed.price_cache

    # Expire existing BTCUSDT entry and ensure None does not overwrite it
    feed.price_cache["BTCUSDT"] = (68500.0, time.time() - 60.0)
    assert await feed.get_current_price("BTCUSDT") is None
    assert feed.price_cache["BTCUSDT"][0] == 68500.0

    yahoo.fetch_price = AsyncMock(return_value=None)
    assert await feed.get_current_price("AAPL") is None
    assert "AAPL" not in feed.price_cache


@pytest.mark.asyncio
async def test_binance_ws_loop_reconnect_backoff_escalates_and_resets():
    """V6-COVERAGE-002: BinanceMarketFeed._ws_loop escalates backoff (2.0 -> 3.0), delivers ticks, and resets backoff to 2.0 on reconnect."""
    attempts = 0
    sleep_delays = []
    received_ticks = []
    binance = BinanceMarketFeed()
    binance._running = True

    class _FakeWS:
        def __init__(self, frames_to_emit):
            self._frames = list(frames_to_emit)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def recv(self):
            if self._frames:
                return self._frames.pop(0)
            raise ConnectionError("post-recovery drop")

    def fake_connect(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts in (1, 2):
            raise ConnectionError(f"connect failed #{attempts}")
        if attempts == 3:
            return _FakeWS([json.dumps({"s": "BTCUSDT", "c": "69100.5"})])
        # Stop cleanly after observing the post-recovery backoff reset
        binance._running = False
        raise asyncio.CancelledError

    async def fake_sleep(delay: float):
        sleep_delays.append(delay)

    async def on_tick(sym: str, price: float):
        received_ticks.append((sym, price))

    with patch("app.market_data.binance.websockets.connect", side_effect=fake_connect), \
         patch("app.market_data.binance.asyncio.sleep", side_effect=fake_sleep):
        await binance._ws_loop(on_tick)

    assert received_ticks == [("BTCUSDT", 69100.5)]
    # First two connect errors escalate 2.0 -> 3.0; after attempt 3 succeeds & drops, backoff resets to 2.0
    assert sleep_delays == [2.0, 3.0, 2.0]


@pytest.mark.asyncio
async def test_unified_feed_rejects_non_positive_and_non_finite_prices():
    """DATA-CACHE-001: UnifiedPriceFeed never caches or dispatches <=0, NaN, Inf, or bool prices."""
    from unittest.mock import AsyncMock, MagicMock

    binance = MagicMock()
    yahoo = MagicMock()
    feed = UnifiedPriceFeed(binance_feed=binance, yahoo_feed=yahoo, cache_ttl_seconds=20.0)
    received = []

    async def listener(sym: str, price: float):
        received.append((sym, price))

    feed.add_listener(listener)

    # Adversarial upstream ticks must neither populate cache nor notify listeners
    for bad_price in (0.0, -5.0, float("nan"), float("inf"), float("-inf"), True, False):
        await feed._handle_price_tick("BTCUSDT", bad_price)
    assert "BTCUSDT" not in feed.price_cache
    assert received == []

    # Adversarial get_current_price responses must not be cached and must return None
    for bad_price in (0.0, -10.0, float("nan"), float("inf"), float("-inf"), True):
        binance.fetch_price = AsyncMock(return_value=bad_price)
        assert await feed.get_current_price("BTC") is None
        assert "BTCUSDT" not in feed.price_cache

    # Subsequent valid upstream quote succeeds and caches cleanly
    binance.fetch_price = AsyncMock(return_value=69500.25)
    assert await feed.get_current_price("BTC") == 69500.25
    assert feed.price_cache["BTCUSDT"][0] == 69500.25

    # Valid tick updates cache and notifies listener
    await feed._handle_price_tick("BTCUSDT", 70100.5)
    assert feed.price_cache["BTCUSDT"][0] == 70100.5
    assert received == [("BTCUSDT", 70100.5)]


@pytest.mark.asyncio
async def test_unified_feed_price_cache_enforces_lru_capacity_and_ttl_eviction():
    """MEM-FEED-001: UnifiedPriceFeed.price_cache stays bounded by max_cache_size and prunes expired entries."""
    from unittest.mock import AsyncMock, MagicMock

    binance = MagicMock()
    binance.fetch_price = AsyncMock(return_value=10.0)
    yahoo = MagicMock()
    feed = UnifiedPriceFeed(
        binance_feed=binance,
        yahoo_feed=yahoo,
        cache_ttl_seconds=20.0,
        max_cache_size=3,
    )

    # Insert 5 distinct symbols -> LRU evicts oldest entries to stay <= 3
    for i in range(5):
        await feed._handle_price_tick(f"COIN{i}USDT", 100.0 + i)

    assert len(feed.price_cache) == 3
    assert list(feed.price_cache.keys()) == ["COIN2USDT", "COIN3USDT", "COIN4USDT"]

    # Touch COIN2USDT via get_current_price -> promotes it to MRU so COIN3USDT is evicted next
    assert await feed.get_current_price("COIN2USDT") == 102.0
    await feed._handle_price_tick("COIN5USDT", 105.0)
    assert list(feed.price_cache.keys()) == ["COIN4USDT", "COIN2USDT", "COIN5USDT"]

    # Expired oldest entry is pruned on subsequent write even below capacity
    feed.price_cache["COIN4USDT"] = (104.0, time.time() - 120.0)
    await feed._handle_price_tick("COIN5USDT", 106.0)
    assert "COIN4USDT" not in feed.price_cache
    assert "COIN5USDT" in feed.price_cache
