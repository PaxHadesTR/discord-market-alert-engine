import asyncio
from collections import OrderedDict
import logging
import re
import time
from typing import Callable, Coroutine, List, Optional, Tuple
from urllib.parse import quote as url_quote
import httpx

logger = logging.getLogger(__name__)

VALID_YAHOO_SYMBOL_RE = re.compile(r"^[A-Z0-9.\-=^]{1,20}$")


def is_valid_yahoo_symbol(symbol: str) -> bool:
    """Validate that a normalized symbol matches strict allowlist without path traversal."""
    cleaned = (symbol or "").upper().strip()
    if not cleaned or ".." in cleaned:
        return False
    return bool(VALID_YAHOO_SYMBOL_RE.fullmatch(cleaned))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

_ALLOWED_SEARCH_QUOTE_TYPES = {
    "EQUITY",
    "ETF",
    "INDEX",
    "FUTURE",
    "CURRENCY",
    "CRYPTOCURRENCY",
}


class YahooFinanceFeed:
    """Async client for fetching stock, Forex, and commodity prices via Yahoo Finance API."""

    def __init__(
        self,
        poll_interval_seconds: float = 20.0,
        timeout_seconds: float = 8.0,
        max_concurrent_requests: int = 8,
        search_cache_ttl_seconds: float = 900.0,
        search_cache_max_size: int = 256,
    ) -> None:
        self.poll_interval = poll_interval_seconds
        self.timeout = timeout_seconds
        self.max_concurrent_requests = max(1, max_concurrent_requests)
        self.search_cache_ttl = search_cache_ttl_seconds
        self.search_cache_max_size = search_cache_max_size
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._client: Optional[httpx.AsyncClient] = None
        # Bounded LRU + TTL cache: query_upper -> (timestamp, [(label, symbol), ...])
        self._search_cache: "OrderedDict[str, Tuple[float, List[Tuple[str, str]]]]" = OrderedDict()

    async def fetch_price(self, symbol: str) -> Optional[float]:
        """Fetch latest price snapshot for non-crypto symbols (BIST, US Equities, Forex, Gold)."""
        symbol_clean = (symbol or "").upper().strip()
        if not is_valid_yahoo_symbol(symbol_clean):
            logger.warning("Rejected invalid Yahoo Finance symbol: %r", symbol)
            return None
        price = await self._fetch_direct_price(symbol_clean)
        if price is not None:
            return price

        # If not found and symbol has no extension/special marker, attempt BIST fallback (.IS)
        if (
            "." not in symbol_clean
            and "=" not in symbol_clean
            and "^" not in symbol_clean
            and "-" not in symbol_clean
        ):
            bist_symbol = f"{symbol_clean}.IS"
            logger.debug("Attempting BIST fallback lookup for %s -> %s", symbol_clean, bist_symbol)
            bist_price = await self._fetch_direct_price(bist_symbol)
            if bist_price is not None:
                return bist_price

        return None

    async def search_symbols(self, query: str, limit: int = 8) -> List[Tuple[str, str]]:
        """Search global equities, ETFs, indices, futures, and FX via Yahoo Finance Search API.

        Uses a bounded in-memory LRU/TTL cache and never raises on network errors so Discord
        autocomplete remains responsive even under degraded connectivity.
        """
        clean = query.strip().upper()
        if len(clean) < 2:
            return []

        now = time.time()
        cached = self._search_cache.get(clean)
        if cached is not None:
            cached_at, cached_results = cached
            if (now - cached_at) <= self.search_cache_ttl:
                self._search_cache.move_to_end(clean)
                return cached_results[:limit]
            self._search_cache.pop(clean, None)

        url = "https://query1.finance.yahoo.com/v1/finance/search"
        params = {
            "q": clean,
            "quotesCount": max(1, min(limit, 15)),
            "newsCount": 0,
            "enableFuzzyQuery": "false",
        }

        try:
            client = await self._get_client()
            response = await client.get(url, params=params, timeout=min(self.timeout, 1.5))
            if response.status_code != 200:
                logger.debug("Yahoo search returned status %d for query '%s'", response.status_code, clean)
                return []

            data = response.json()
            quotes = data.get("quotes", [])
            results: List[Tuple[str, str]] = []
            seen = set()

            for item in quotes:
                symbol = (item.get("symbol") or "").strip().upper()
                quote_type = (item.get("quoteType") or "").strip().upper()
                if not symbol or not is_valid_yahoo_symbol(symbol) or quote_type not in _ALLOWED_SEARCH_QUOTE_TYPES:
                    continue
                if symbol in seen:
                    continue
                seen.add(symbol)

                name = (item.get("shortname") or item.get("longname") or "").strip()
                exch = (item.get("exchDisp") or item.get("exchange") or "").strip()
                if name and exch:
                    label = f"{symbol} ({name} • {exch})"
                elif name:
                    label = f"{symbol} ({name})"
                elif exch:
                    label = f"{symbol} ({exch})"
                else:
                    label = symbol

                results.append((label[:100], symbol[:100]))
                if len(results) >= limit:
                    break

            self._search_cache[clean] = (now, results)
            self._search_cache.move_to_end(clean)
            while len(self._search_cache) > self.search_cache_max_size:
                self._search_cache.popitem(last=False)

            return results
        except Exception as e:
            logger.debug("Yahoo symbol search failed for '%s': %s", clean, e)
            return []

    async def _get_client(self) -> httpx.AsyncClient:
        """Return cached AsyncClient with browser headers, recreating if closed."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(headers=HEADERS, timeout=self.timeout)
        return self._client

    async def _fetch_direct_price(self, symbol_clean: str) -> Optional[float]:
        if not is_valid_yahoo_symbol(symbol_clean):
            logger.warning("Blocked malformed Yahoo Finance symbol in _fetch_direct_price: %r", symbol_clean)
            return None

        encoded_symbol = url_quote(symbol_clean, safe="=^.-")
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded_symbol}"
        params = {"interval": "1m", "range": "1d"}

        try:
            client = await self._get_client()
            response = await client.get(url, params=params)
            if response.status_code == 200:
                data = response.json()
                result = data.get("chart", {}).get("result")
                if result and len(result) > 0:
                    meta = result[0].get("meta", {})
                    price = meta.get("regularMarketPrice")
                    if price is not None:
                        return float(price)

                    # Fallback to last close indicator
                    quote = result[0].get("indicators", {}).get("quote", [{}])[0]
                    closes = quote.get("close", [])
                    valid_closes = [c for c in closes if c is not None]
                    if valid_closes:
                        return float(valid_closes[-1])

            logger.debug("Yahoo Finance returned status %d for symbol %s", response.status_code, symbol_clean)
            return None
        except Exception as e:
            logger.debug("Failed fetching Yahoo price for %s: %s", symbol_clean, e)
            return None

    def start_polling(
        self,
        get_active_symbols: Callable[[], Coroutine[None, None, List[str]]],
        on_price_update: Callable[[str, float], Coroutine[None, None, None]],
    ) -> None:
        """Start periodic background polling task for active non-crypto symbols."""
        if self._running:
            return
        self._running = True
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(headers=HEADERS, timeout=self.timeout)
        self._task = asyncio.create_task(
            self._poll_loop(get_active_symbols, on_price_update),
            name="yahoo_finance_poller",
        )
        logger.info("Started Yahoo Finance polling background worker (interval: %.1fs).", self.poll_interval)

    async def stop(self) -> None:
        """Stop polling background worker."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
        logger.info("Yahoo Finance feed stopped.")

    async def _poll_loop(
        self,
        get_active_symbols: Callable[[], Coroutine[None, None, List[str]]],
        on_price_update: Callable[[str, float], Coroutine[None, None, None]],
    ) -> None:
        semaphore = asyncio.Semaphore(self.max_concurrent_requests)

        async def _bounded_poll(symbol: str) -> None:
            async with semaphore:
                await self._poll_symbol(symbol, on_price_update)

        while self._running:
            try:
                symbols = await get_active_symbols()
                if symbols:
                    logger.debug("Polling Yahoo Finance for %d active symbols: %s", len(symbols), symbols)
                    tasks = [_bounded_poll(s) for s in set(symbols)]
                    await asyncio.gather(*tasks, return_exceptions=True)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Unexpected error in Yahoo polling loop: %s", e, exc_info=True)

            await asyncio.sleep(self.poll_interval)

    async def _poll_symbol(
        self,
        symbol: str,
        on_price_update: Callable[[str, float], Coroutine[None, None, None]],
    ) -> None:
        price = await self.fetch_price(symbol)
        if price is not None:
            await on_price_update(symbol, price)
