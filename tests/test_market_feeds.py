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
