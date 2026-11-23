import asyncio
import logging
import math
from typing import Callable, Coroutine, List, Optional, Set, Tuple, Union
from app.config import Settings
from app.database.db import AlertDatabase, AlertRecord
from app.market_data.feed import UnifiedPriceFeed
from app.market_data.symbols import classify_and_normalize

logger = logging.getLogger(__name__)

AlertTriggerCallback = Callable[[AlertRecord, float], Coroutine[None, None, None]]


def parse_target_price_input(
    target_input: Union[float, str],
    current_price: float,
) -> Tuple[Optional[float], Optional[str]]:
    """Parse numeric price or percentage offset (e.g., '+5%', '-3%', '150,5', '70k', '$65,000').

    Returns (calculated_target_price, auto_note_suffix) or (None, error_message).
    """
    if isinstance(target_input, bool):
        return None, "Hedef fiyat 0'dan büyük geçerli bir sayı olmalıdır."

    if isinstance(target_input, (int, float)):
        val = float(target_input)
        if not math.isfinite(val) or val <= 0:
            return None, "Hedef fiyat 0'dan büyük sonlu ve pozitif bir sayı olmalıdır."
        return val, None

    clean = str(target_input).strip()
    if not clean:
        return None, "Hedef fiyat boş bırakılamaz."

    # Strip currency symbols
    for prefix in ("$", "€", "₺", "TL", "USD"):
        if clean.upper().startswith(prefix):
            clean = clean[len(prefix):].strip()
        if clean.upper().endswith(prefix):
            clean = clean[:-len(prefix)].strip()

    # Check for percentage notation: e.g. "+5%", "-3%", "5%"
    if clean.endswith("%"):
        try:
            pct = float(clean[:-1].strip().replace(",", "."))
            if not math.isfinite(pct):
                return None, f"Geçersiz yüzde formatı: '{target_input}'. Örnek: +5% veya -3%"
            if pct == 0:
                return None, "Yüzdesel hedef değişim %0 olamaz."
            calc = current_price * (1.0 + (pct / 100.0))
            if not math.isfinite(calc) or calc <= 0:
                return None, "Hesaplanan hedef fiyat 0 veya altına düştü."
            return calc, f"({pct:+g}% hedefi)"
        except ValueError:
            return None, f"Geçersiz yüzde formatı: '{target_input}'. Örnek: +5% veya -3%"

    # Check for 'k' / 'm' suffixes: e.g. 70k -> 70000, 1.5m -> 1500000
    multiplier = 1.0
    if clean.lower().endswith("k"):
        multiplier = 1000.0
        clean = clean[:-1].strip()
    elif clean.lower().endswith("m"):
        multiplier = 1000000.0
        clean = clean[:-1].strip()

    clean = clean.replace(" ", "").replace(",", ".")
    try:
        val = float(clean) * multiplier
        if not math.isfinite(val) or val <= 0:
            return None, "Hedef fiyat 0'dan büyük sonlu ve pozitif bir sayı olmalıdır."
        return val, None
    except ValueError:
        return None, f"Geçersiz fiyat formatı: '{target_input}'. Örnekler: 70000, 150.5, +5%, -3%, 70k"


class AutonomousAlertEngine:
    """Core autonomous alert manager matching live prices against database alerts and enforcing quotas."""

    def __init__(
        self,
        db: AlertDatabase,
        price_feed: UnifiedPriceFeed,
        settings: Settings,
        trigger_callback: Optional[AlertTriggerCallback] = None,
    ) -> None:
        self.db = db
        self.feed = price_feed
        self.settings = settings
        self.trigger_callback = trigger_callback
        self._running = False
        self._expiry_task: Optional[asyncio.Task] = None
        self._active_symbols: Optional[Set[str]] = None
        self._create_lock: Optional[asyncio.Lock] = None
        self._create_lock_loop: Optional[asyncio.AbstractEventLoop] = None
        self._hook_db_mutations()

        # Wire price feed to evaluate alerts
        self.feed.add_listener(self._on_price_tick)
        self.feed.set_active_equity_supplier(self._get_active_non_crypto_symbols)

    def _get_create_lock(self) -> asyncio.Lock:
        """Return an asyncio.Lock bound to the active event loop for atomic quota+insert."""
        loop = asyncio.get_running_loop()
        if self._create_lock is None or self._create_lock_loop is not loop:
            self._create_lock = asyncio.Lock()
            self._create_lock_loop = loop
        return self._create_lock

    async def _refresh_active_symbols(self) -> Set[str]:
        """Synchronize the in-memory set of symbols that currently have ACTIVE alerts."""
        active = await self.db.get_active_alerts()
        self._active_symbols = {a.symbol.upper().strip() for a in active}
        return self._active_symbols

    def _hook_db_mutations(self) -> None:
        """Keep _active_symbols synchronized even when callers mutate self.db directly."""
        if getattr(self.db, "_active_symbol_hooks_installed", False):
            return

        orig_add = self.db.add_alert
        orig_trigger = self.db.trigger_alert
        orig_reset = self.db.reset_alert_to_active
        orig_cancel = self.db.cancel_alert
        orig_clear = self.db.clear_user_alerts
        orig_expire = self.db.expire_stale_alerts

        async def _wrapped_add(*args, **kwargs):
            alert_id = await orig_add(*args, **kwargs)
            symbol = kwargs.get("symbol") if "symbol" in kwargs else (args[4] if len(args) > 4 else None)
            if symbol is not None and self._active_symbols is not None:
                self._active_symbols.add(str(symbol).upper().strip())
            return alert_id

        async def _wrapped_trigger(*args, **kwargs):
            res = await orig_trigger(*args, **kwargs)
            if res and self._active_symbols is not None:
                await self._refresh_active_symbols()
            return res

        async def _wrapped_reset(*args, **kwargs):
            res = await orig_reset(*args, **kwargs)
            if res and self._active_symbols is not None:
                await self._refresh_active_symbols()
            return res

        async def _wrapped_cancel(*args, **kwargs):
            res = await orig_cancel(*args, **kwargs)
            if res and self._active_symbols is not None:
                await self._refresh_active_symbols()
            return res

        async def _wrapped_clear(*args, **kwargs):
            count = await orig_clear(*args, **kwargs)
            if count and self._active_symbols is not None:
                await self._refresh_active_symbols()
            return count

        async def _wrapped_expire(*args, **kwargs):
            count = await orig_expire(*args, **kwargs)
            if count and self._active_symbols is not None:
                await self._refresh_active_symbols()
            return count

        self.db.add_alert = _wrapped_add
        self.db.trigger_alert = _wrapped_trigger
        self.db.reset_alert_to_active = _wrapped_reset
        self.db.cancel_alert = _wrapped_cancel
        self.db.clear_user_alerts = _wrapped_clear
        self.db.expire_stale_alerts = _wrapped_expire
        setattr(self.db, "_active_symbol_hooks_installed", True)

    async def _get_active_non_crypto_symbols(self) -> List[str]:
        """Fetch all active non-crypto symbols currently registered in database alerts and watchlists."""
        active = await self.db.get_active_alerts()
        alert_symbols = [a.symbol for a in active if a.asset_type == "EQUITY_FOREX"]
        watchlist_symbols = await self.db.get_all_watchlist_symbols(asset_type="EQUITY_FOREX")
        return list(set(alert_symbols + watchlist_symbols))

    def _is_callback_ready(self) -> bool:
        """Return True only when a trigger callback is registered and its owner (if any) is ready."""
        if self.trigger_callback is None:
            return False
        cb_owner = getattr(self.trigger_callback, "__self__", None)
        if cb_owner is not None:
            is_closed = getattr(cb_owner, "is_closed", None)
            if callable(is_closed) and is_closed():
                return False
            is_ready = getattr(cb_owner, "is_ready", None)
            if callable(is_ready) and not is_ready():
                return False
        return True

    async def _on_price_tick(self, symbol: str, current_price: float) -> None:
        """Evaluate active alerts when a price tick arrives."""
        if not math.isfinite(current_price) or current_price <= 0:
            return
        if not self._is_callback_ready():
            return
        norm_symbol = symbol.upper().strip()
        if self._active_symbols is None:
            await self._refresh_active_symbols()
        if norm_symbol not in self._active_symbols:
            return
        active_alerts = await self.db.get_active_alerts(symbol=norm_symbol)
        if not active_alerts:
            return

        for alert in active_alerts:
            should_trigger = False

            if alert.direction == "ABOVE" and current_price >= alert.target_price:
                should_trigger = True
            elif alert.direction == "BELOW" and current_price <= alert.target_price:
                should_trigger = True

            if should_trigger:
                # Atomically mark triggered in DB to prevent duplicate fires
                success = await self.db.trigger_alert(alert.id)
                if success:
                    logger.info(
                        "Alert #%d triggered! %s %s target %.4f reached at %.4f",
                        alert.id, alert.symbol, alert.direction, alert.target_price, current_price,
                    )
                    if self.trigger_callback:
                        try:
                            result = await self.trigger_callback(alert, current_price)
                            if result is False:
                                logger.warning(
                                    "Trigger callback reported dispatch failure for alert #%d; resetting to ACTIVE for retry.",
                                    alert.id,
                                )
                                await self.db.reset_alert_to_active(alert.id)
                        except Exception as e:
                            logger.error(
                                "Error executing alert trigger callback for alert #%d: %s; resetting to ACTIVE for retry.",
                                alert.id,
                                e,
                                exc_info=True,
                            )
                            await self.db.reset_alert_to_active(alert.id)

    async def create_user_alert(
        self,
        user_id: str,
        user_name: str,
        channel_id: str,
        guild_id: Optional[str],
        raw_symbol: str,
        target_price: Union[float, str],
        note: Optional[str] = None,
        notify_dm: bool = False,
    ) -> Tuple[bool, str, Optional[AlertRecord]]:
        """Validate quotas, fetch current price, detect direction, and register alert."""
        # 1. Enforce global quota
        total_active = await self.db.count_active_alerts()
        if total_active >= self.settings.MAX_ACTIVE_ALERTS:
            return (
                False,
                f"Sistem genel alarm kotası dolu! (Aktif: {total_active}/{self.settings.MAX_ACTIVE_ALERTS}). Lütfen eski alarmları temizleyin.",
                None,
            )

        # 2. Enforce per-user quota
        user_active = await self.db.count_active_alerts(user_id=user_id)
        if user_active >= self.settings.MAX_USER_ACTIVE_ALERTS:
            return (
                False,
                f"Kişisel aktif alarm limitinize ({user_active}/{self.settings.MAX_USER_ACTIVE_ALERTS}) ulaştınız! Lütfen `/alert list` veya `/alert clear` ile eski alarmları temizleyin.",
                None,
            )

        # 3. Normalize symbol and classify asset type
        symbol, asset_type = classify_and_normalize(raw_symbol)

        # 4. Retrieve current market price
        current_price = await self.feed.get_current_price(symbol)
        if current_price is None or not math.isfinite(current_price) or current_price <= 0:
            return (
                False,
                f"**{symbol}** sembolü için anlık fiyat alınamadı. Lütfen sembol adını kontrol edin (örn: BTCUSDT, THYAO, USDTRY, GOLD, NASDAQ).",
                None,
            )

        # 5. Parse target price (number or percentage distance)
        calc_target, note_suffix = parse_target_price_input(target_price, current_price)
        if calc_target is None:
            return False, f"❌ {note_suffix}", None

        # Append percentage label to note if provided
        final_note = note
        if note_suffix:
            final_note = f"{note} {note_suffix}".strip() if note else note_suffix

        # 6. Auto-detect direction
        if calc_target > current_price:
            direction = "ABOVE"
        elif calc_target < current_price:
            direction = "BELOW"
        else:
            return (
                False,
                f"Hedef fiyat (${calc_target:,.4f}) şu anki piyasa fiyatına (${current_price:,.4f}) zaten eşit!",
                None,
            )

        # 7. Atomically re-verify quotas and insert alert under lock to eliminate TOCTOU races
        async with self._get_create_lock():
            total_active = await self.db.count_active_alerts()
            if total_active >= self.settings.MAX_ACTIVE_ALERTS:
                return (
                    False,
                    f"Sistem genel alarm kotası dolu! (Aktif: {total_active}/{self.settings.MAX_ACTIVE_ALERTS}). Lütfen eski alarmları temizleyin.",
                    None,
                )

            user_active = await self.db.count_active_alerts(user_id=user_id)
            if user_active >= self.settings.MAX_USER_ACTIVE_ALERTS:
                return (
                    False,
                    f"Kişisel aktif alarm limitinize ({user_active}/{self.settings.MAX_USER_ACTIVE_ALERTS}) ulaştınız! Lütfen `/alert list` veya `/alert clear` ile eski alarmları temizleyin.",
                    None,
                )

            alert_id = await self.db.add_alert(
                user_id=user_id,
                user_name=user_name,
                channel_id=channel_id,
                guild_id=guild_id,
                symbol=symbol,
                asset_type=asset_type,
                target_price=calc_target,
                initial_price=current_price,
                direction=direction,
                note=final_note,
                notify_dm=notify_dm,
            )

        record = await self.db.get_alert_by_id(alert_id)
        return True, "Alarm başarıyla oluşturuldu.", record

    async def start(self) -> None:
        """Start database, price feed, and expiration cleaner."""
        if self._running:
            return
        self._running = True
        await self.db.init_db()
        await self._refresh_active_symbols()
        self.feed.start()
        self._expiry_task = asyncio.create_task(self._expiry_worker(), name="alert_expiry_cleaner")
        logger.info("AutonomousAlertEngine started.")

    async def stop(self) -> None:
        """Gracefully stop background engines."""
        self._running = False
        if self._expiry_task:
            self._expiry_task.cancel()
            try:
                await self._expiry_task
            except asyncio.CancelledError:
                pass
            self._expiry_task = None
        await self.feed.stop()
        logger.info("AutonomousAlertEngine stopped.")

    async def _expiry_worker(self) -> None:
        """Periodically clean up stale alerts older than ALERT_EXPIRY_DAYS."""
        while self._running:
            try:
                await asyncio.sleep(3600)  # Check every hour
                await self.db.expire_stale_alerts(self.settings.ALERT_EXPIRY_DAYS)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in alert expiry worker: %s", e, exc_info=True)
