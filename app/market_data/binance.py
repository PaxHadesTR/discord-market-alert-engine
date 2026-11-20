import asyncio
import json
import logging
from typing import Callable, Coroutine, List, Optional, Tuple
import httpx
import websockets

logger = logging.getLogger(__name__)


class BinanceMarketFeed:
    """Asynchronous Binance market data feed combining REST snapshot and real-time WebSocket stream."""

    def __init__(
        self,
        ws_url: str = "wss://stream.binance.com:9443/ws/!miniTicker@arr",
        rest_api_url: str = "https://api.binance.com/api/v3/ticker/price",
        timeout_seconds: float = 8.0,
        max_dispatch_queue: int = 4096,
        dispatch_concurrency: int = 16,
    ) -> None:
        self.ws_url = ws_url
        self.rest_api_url = rest_api_url
        self.timeout = timeout_seconds
        self.max_dispatch_queue = max(1, max_dispatch_queue)
        self.dispatch_concurrency = max(1, dispatch_concurrency)
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._dispatch_queue: Optional[asyncio.Queue[Tuple[str, float]]] = None
        self._dispatch_workers: List[asyncio.Task] = []
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Return cached AsyncClient or recreate if closed."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def fetch_price(self, symbol: str) -> Optional[float]:
        """Fetch immediate spot price snapshot from Binance REST API."""
        symbol_clean = symbol.upper().strip()
        try:
            client = await self._get_client()
            response = await client.get(self.rest_api_url, params={"symbol": symbol_clean})
            if response.status_code == 200:
                data = response.json()
                return float(data["price"])
            logger.warning("Binance REST API returned status %d for symbol %s", response.status_code, symbol_clean)
            return None
        except Exception as e:
            logger.error("Failed fetching Binance price for %s: %s", symbol_clean, e)
            return None

    def start_stream(self, on_price_update: Callable[[str, float], Coroutine[None, None, None]]) -> None:
        """Start background WebSocket consumer to stream real-time price updates for all crypto symbols."""
        if self._running:
            return
        self._running = True
        self._dispatch_queue = asyncio.Queue(maxsize=self.max_dispatch_queue)
        self._dispatch_workers = [
            asyncio.create_task(
                self._dispatch_worker(on_price_update),
                name=f"binance_tick_dispatcher_{idx}",
            )
            for idx in range(self.dispatch_concurrency)
        ]
        self._task = asyncio.create_task(self._ws_loop(on_price_update), name="binance_ws_stream")
        logger.info("Started Binance WebSocket background stream on %s", self.ws_url)

    def _enqueue_tick(self, symbol: str, price: float) -> None:
        """Non-blocking bounded enqueue; evicts oldest stale tick on saturation to prevent WS stalls."""
        if self._dispatch_queue is None:
            return
        if self._dispatch_queue.full():
            try:
                self._dispatch_queue.get_nowait()
                self._dispatch_queue.task_done()
            except asyncio.QueueEmpty:
                pass
        try:
            self._dispatch_queue.put_nowait((symbol, price))
        except asyncio.QueueFull:
            logger.debug("Binance tick dispatch queue saturated; dropping tick for %s", symbol)

    async def _dispatch_worker(
        self,
        on_price_update: Callable[[str, float], Coroutine[None, None, None]],
    ) -> None:
        """Background worker invoking price update callbacks without blocking WebSocket reads."""
        assert self._dispatch_queue is not None
        while self._running:
            try:
                symbol, price = await self._dispatch_queue.get()
            except asyncio.CancelledError:
                break
            try:
                await on_price_update(symbol, price)
            except asyncio.CancelledError:
                break
            except Exception as err:
                logger.debug("Error dispatching Binance tick for %s: %s", symbol, err)
            finally:
                self._dispatch_queue.task_done()

    async def stop(self) -> None:
        """Stop WebSocket background stream, dispatch workers, and close HTTP client."""
        self._running = False
        tasks_to_cancel: List[asyncio.Task] = []
        if self._task:
            tasks_to_cancel.append(self._task)
            self._task = None
        if self._dispatch_workers:
            tasks_to_cancel.extend(self._dispatch_workers)
            self._dispatch_workers = []

        for task in tasks_to_cancel:
            task.cancel()
        if tasks_to_cancel:
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)

        if self._dispatch_queue is not None:
            while not self._dispatch_queue.empty():
                try:
                    self._dispatch_queue.get_nowait()
                    self._dispatch_queue.task_done()
                except asyncio.QueueEmpty:
                    break
            self._dispatch_queue = None

        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
        logger.info("Binance market feed stopped.")

    async def _ws_loop(self, on_price_update: Callable[[str, float], Coroutine[None, None, None]]) -> None:
        """Resilient WebSocket loop with automatic reconnect and exponential backoff."""
        backoff = 2.0
        while self._running:
            try:
                logger.debug("Connecting to Binance WebSocket %s...", self.ws_url)
                async with websockets.connect(self.ws_url, ping_interval=20, ping_timeout=20) as ws:
                    logger.info("Connected to Binance WebSocket market stream.")
                    backoff = 2.0  # Reset backoff on successful connection

                    while self._running:
                        msg = await ws.recv()
                        try:
                            data = json.loads(msg)
                            # !miniTicker@arr emits a list of ticker objects
                            if isinstance(data, list):
                                for item in data:
                                    s = item.get("s")
                                    c = item.get("c")
                                    if s and c:
                                        price = float(c)
                                        if self._dispatch_queue is not None:
                                            self._enqueue_tick(s.upper(), price)
                                        else:
                                            await on_price_update(s.upper(), price)
                            elif isinstance(data, dict):
                                s = data.get("s")
                                c = data.get("c")
                                if s and c:
                                    price = float(c)
                                    if self._dispatch_queue is not None:
                                        self._enqueue_tick(s.upper(), price)
                                    else:
                                        await on_price_update(s.upper(), price)
                        except Exception as parse_err:
                            logger.debug("Error processing miniTicker tick: %s", parse_err)

            except asyncio.CancelledError:
                break
            except Exception as e:
                if not self._running:
                    break
                logger.warning("Binance WebSocket disconnected (%s). Reconnecting in %.1fs...", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 30.0)
