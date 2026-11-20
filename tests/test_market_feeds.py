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
