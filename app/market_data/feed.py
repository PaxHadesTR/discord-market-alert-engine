import asyncio
from collections import OrderedDict
import logging
import math
import time
from typing import Callable, Coroutine, List, Optional, Tuple
from app.market_data.binance import BinanceMarketFeed
from app.market_data.symbols import classify_and_normalize
from app.market_data.yahoo import YahooFinanceFeed

logger = logging.getLogger(__name__)

PriceListener = Callable[[str, float], Coroutine[None, None, None]]


def _is_valid_price(price: object) -> bool:
    """Return True only for finite, strictly positive non-boolean numeric prices."""
    return (
        not isinstance(price, bool)
        and isinstance(price, (int, float))
        and math.isfinite(price)
        and price > 0
    )


class UnifiedPriceFeed:
    """Unified hybrid market feed combining real-time Binance WebSocket and Yahoo Finance Poller."""

    def __init__(
        self,
        binance_feed: Optional[BinanceMarketFeed] = None,
        yahoo_feed: Optional[YahooFinanceFeed] = None,
        cache_ttl_seconds: float = 20.0,
        max_cache_size: int = 5000,
    ) -> None:
        self.binance = binance_feed or BinanceMarketFeed()
        self.yahoo = yahoo_feed or YahooFinanceFeed()
        self.cache_ttl = cache_ttl_seconds
        self.max_cache_size = max(1, int(max_cache_size))
        # Bounded LRU cache: maps symbol -> (price: float, timestamp: float)
        self.price_cache: OrderedDict[str, Tuple[float, float]] = OrderedDict()
        self._listeners: List[PriceListener] = []
        self._active_non_crypto_supplier: Optional[Callable[[], Coroutine[None, None, List[str]]]] = None

    def _set_cached_price(self, symbol: str, price: float, now: float) -> None:
        """Store a validated price in the bounded LRU cache and prune expired oldest entries."""
        self.price_cache[symbol] = (price, now)
        self.price_cache.move_to_end(symbol)

        cutoff = now - self.cache_ttl
        while len(self.price_cache) > 1:
            oldest_key, (_, oldest_ts) = next(iter(self.price_cache.items()))
            if oldest_key != symbol and oldest_ts < cutoff:
                self.price_cache.popitem(last=False)
            else:
                break

        while len(self.price_cache) > self.max_cache_size:
            self.price_cache.popitem(last=False)

    def set_active_equity_supplier(self, supplier: Callable[[], Coroutine[None, None, List[str]]]) -> None:
        """Provide callback to supply active non-crypto symbols for Yahoo polling."""
        self._active_non_crypto_supplier = supplier

    def add_listener(self, listener: PriceListener) -> None:
        """Register a callback to be invoked on every incoming price update."""
        self._listeners.append(listener)

    async def get_current_price(self, raw_symbol: str) -> Optional[float]:
        """Fetch current price for any symbol, consulting cache first, then live snapshot.

        Enforces freshness via cache_ttl to ensure stale quotes are never served.
        """
        normalized, asset_type = classify_and_normalize(raw_symbol)
        now = time.time()

        if normalized in self.price_cache:
            cached_price, cached_at = self.price_cache[normalized]
            if (now - cached_at) <= self.cache_ttl and _is_valid_price(cached_price):
                self.price_cache.move_to_end(normalized)
                return float(cached_price)

        if asset_type == "CRYPTO":
            price = await self.binance.fetch_price(normalized)
        else:
            price = await self.yahoo.fetch_price(normalized)

        if not _is_valid_price(price):
            return None

        valid_price = float(price)
        self._set_cached_price(normalized, valid_price, now)
        return valid_price

    async def _handle_price_tick(self, symbol: str, price: float) -> None:
        """Internal dispatch called whenever Binance or Yahoo reports a price."""
        if not _is_valid_price(price):
            logger.debug("Ignoring invalid upstream price tick %r for %s", price, symbol)
            return

        valid_price = float(price)
        symbol_upper = symbol.upper()
        self._set_cached_price(symbol_upper, valid_price, time.time())

        # Notify listeners concurrently
        if self._listeners:
            tasks = [listener(symbol_upper, valid_price) for listener in self._listeners]
            await asyncio.gather(*tasks, return_exceptions=True)

    def start(self) -> None:
        """Start both Binance WebSocket stream and Yahoo Finance polling worker."""
        self.binance.start_stream(self._handle_price_tick)
        if self._active_non_crypto_supplier:
            self.yahoo.start_polling(self._active_non_crypto_supplier, self._handle_price_tick)
        logger.info("UnifiedPriceFeed started with hybrid market engines.")

    async def stop(self) -> None:
        """Gracefully shutdown market data feeds."""
        await asyncio.gather(
            self.binance.stop(),
            self.yahoo.stop(),
            return_exceptions=True,
        )
        logger.info("UnifiedPriceFeed stopped.")
