import asyncio
import logging
import math
import re
import time
from datetime import datetime
from typing import List, Optional
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from app.config import Settings
from app.database.db import AlertRecord
from app.routes.health import _redact_sensitive_text
from app.market_data.symbols import (
    classify_and_normalize,
    get_symbol_autocomplete,
    get_symbol_autocomplete_async,
)
from app.market_data.yahoo import is_valid_yahoo_symbol
from app.services.alert_engine import AutonomousAlertEngine

logger = logging.getLogger(__name__)

MAX_WATCHLIST_ITEMS = 25
WATCHLIST_FETCH_CONCURRENCY = 8
MAX_ALERT_NOTE_LENGTH = 150
MAX_SYMBOL_INPUT_LENGTH = 64
MAX_DISCORD_CONTENT_LENGTH = 2000
MAX_EMBED_TITLE_LENGTH = 256
MAX_EMBED_DESCRIPTION_LENGTH = 4096
MAX_EMBED_FIELD_NAME_LENGTH = 256
MAX_EMBED_FIELD_VALUE_LENGTH = 1024


def _truncate_text(text: str, max_length: int) -> str:
    """Safely truncate text to fit Discord API length budgets."""
    s = str(text)
    if len(s) <= max_length:
        return s
    if max_length <= 3:
        return s[:max_length]
    return s[: max_length - 3].rstrip() + "..."


def _sanitize_mentions(text: str) -> str:
    """Escape @everyone/@here as well as raw <@user>/<@&role> mention syntax."""
    escaped = discord.utils.escape_mentions(str(text))
    return escaped.replace("<@", "<\u200b@")


def format_price(price: Optional[float]) -> str:
    """Format price nicely for both small crypto fractions and large indices."""
    if price is None or isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price):
        return "*Alınamadı*"
    if price == 0:
        return "$0.00"
    abs_price = abs(price)
    sign = "-" if price < 0 else ""
    if abs_price >= 1000.0:
        return f"{sign}${abs_price:,.2f}"
    if abs_price >= 1.0:
        return f"{sign}${abs_price:,.4f}"
    if abs_price >= 1e-8:
        formatted = f"{abs_price:.8f}".rstrip("0").rstrip(".")
        return f"{sign}${formatted}"
    formatted = f"{abs_price:.12f}".rstrip("0").rstrip(".")
    if formatted in ("", "0"):
        return f"{sign}${abs_price:.2e}"
    return f"{sign}${formatted}"


async def safe_defer(
    interaction: discord.Interaction,
    ephemeral: bool = False,
    max_retries: int = 3,
    retry_delay: float = 0.35,
) -> bool:
    """Safely defer interaction response with retry on transient proxy (503/timeout) errors."""
    for attempt in range(1, max_retries + 1):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=ephemeral)
            return True
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            logger.warning(
                "Transient network/proxy error during interaction defer (attempt %d/%d): %s",
                attempt, max_retries, err,
            )
            if attempt < max_retries:
                await asyncio.sleep(retry_delay)
            else:
                logger.error("Exhausted retries deferring interaction %s: %s", interaction.id, err)
                raise
        except Exception:
            raise
    return False


async def safe_followup_send(
    interaction: discord.Interaction,
    max_retries: int = 3,
    retry_delay: float = 0.35,
    **kwargs,
) -> Optional[discord.WebhookMessage]:
    """Safely send followup message with retry on transient proxy errors."""
    kwargs.setdefault("allowed_mentions", discord.AllowedMentions.none())
    for attempt in range(1, max_retries + 1):
        try:
            return await interaction.followup.send(**kwargs)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            logger.warning(
                "Transient network/proxy error during followup send (attempt %d/%d): %s",
                attempt, max_retries, err,
            )
            if attempt < max_retries:
                await asyncio.sleep(retry_delay)
            else:
                logger.error("Exhausted retries sending followup for interaction %s: %s", interaction.id, err)
                raise
    return None


async def symbol_autocomplete_handler(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    """Provide real-time hybrid symbol search suggestions for slash commands.

    Combines instant local ranked catalog (500+ presets), live Binance WebSocket symbols,
    and a fast Yahoo Finance Search fallback for any global stock/ETF/index ticker.
    """
    yahoo_feed = None
    live_crypto_symbols = None

    client = getattr(interaction, "client", None)
    engine = getattr(client, "engine", None) if client is not None else None
    feed = getattr(engine, "feed", None) if engine is not None else None
    if feed is not None:
        yahoo_feed = getattr(feed, "yahoo", None)
        price_cache = getattr(feed, "price_cache", None)
        if isinstance(price_cache, dict) and price_cache:
            live_crypto_symbols = list(price_cache.keys())

    suggestions = await get_symbol_autocomplete_async(
        current,
        yahoo_feed=yahoo_feed,
        live_crypto_symbols=live_crypto_symbols,
    )
    return [app_commands.Choice(name=label[:100], value=val[:100]) for label, val in suggestions[:25]]


class RearmAlertModal(discord.ui.Modal, title="🔄 Alarmı Yeniden Kur"):
    """Interactive modal allowing the user to set a new target price or percent on rearm."""

    target_price = discord.ui.TextInput(
        label="Hedef Fiyat veya Değişim",
        placeholder="Örn: 72000, 150.5, +5%, -3%, 70k",
        required=True,
        max_length=30,
    )
    note = discord.ui.TextInput(
        label="Hatırlatıcı Not (İsteğe Bağlı)",
        placeholder="Örn: Direnç kırılımı, kâr al...",
        required=False,
        max_length=150,
        style=discord.TextStyle.short,
    )

    def __init__(self, engine: AutonomousAlertEngine, alert: AlertRecord):
        super().__init__()
        self.engine = engine
        self.alert = alert
        self.target_price.default = f"{alert.target_price:g}"[:30]
        if alert.note:
            self.note.default = str(alert.note)[:MAX_ALERT_NOTE_LENGTH]

    async def on_submit(self, interaction: discord.Interaction):
        await safe_defer(interaction, ephemeral=True)
        success, msg, new_record = await self.engine.create_user_alert(
            user_id=str(interaction.user.id),
            user_name=interaction.user.name,
            channel_id=str(interaction.channel_id),
            guild_id=str(interaction.guild_id) if interaction.guild else None,
            raw_symbol=self.alert.symbol,
            target_price=self.target_price.value.strip(),
            note=self.note.value.strip() or None,
            notify_dm=self.alert.notify_dm,
        )
        if success and new_record:
            await safe_followup_send(
                interaction,
                content=(
                    f"✅ **{new_record.symbol}** için alarm yeniden aktif edildi!\n"
                    f"🎯 **Hedef:** `{format_price(new_record.target_price)}` | 📊 **Yön:** `{new_record.direction}` | 🆔 ID: `#{new_record.id}`"
                ),
                ephemeral=True,
            )
        else:
            await safe_followup_send(interaction, content=f"❌ Alarm kurulamadı: {msg}", ephemeral=True)


class AlertDeleteDropdown(discord.ui.Select):
    """Dropdown menu to select and delete an alert with a single click."""

    def __init__(self, engine: AutonomousAlertEngine, alerts: List[AlertRecord], user_id: str):
        self.engine = engine
        self.user_id = user_id

        options = []
        for a in alerts[:25]:  # Discord select menu max is 25 items
            options.append(
                discord.SelectOption(
                    label=f"ID #{a.id} • {a.symbol} ({format_price(a.target_price)})",
                    value=str(a.id),
                    description=f"{a.direction} | Başlangıç: {format_price(a.initial_price)}",
                    emoji="🗑",
                )
            )

        super().__init__(
            placeholder="Silmek istediğiniz alarmı seçin...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="select_delete_alert",
        )

    async def callback(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message("❌ Sadece kendi alarmlarınızı silebilirsiniz.", ephemeral=True)
            return

        alert_id = int(self.values[0])
        success = await self.engine.db.cancel_alert(alert_id=alert_id, user_id=self.user_id)
        if success:
            self.disabled = True
            await interaction.response.edit_message(view=self.view)
            await safe_followup_send(interaction, content=f"✅ `#{alert_id}` ID'li alarm başarıyla iptal edildi ve silindi.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Alarm silinirken bir hata oluştu veya zaten silinmiş.", ephemeral=True)


class AlertListView(discord.ui.View):
    """View container for /alert list with delete dropdown."""

    def __init__(self, engine: AutonomousAlertEngine, alerts: List[AlertRecord], user_id: str):
        super().__init__(timeout=120)
        active_alerts = [a for a in alerts if a.status == "ACTIVE"]
        if active_alerts:
            self.add_item(AlertDeleteDropdown(engine, active_alerts, user_id))


class RearmAlertButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"btn_rearm:(?P<id>[0-9]+)",
):
    """Stateless persistent button that resolves AlertRecord from SQLite across bot restarts."""

    def __init__(self, alert_id: int) -> None:
        self.alert_id = alert_id
        super().__init__(
            discord.ui.Button(
                label="🔄 Yeniden Kur",
                style=discord.ButtonStyle.green,
                custom_id=f"btn_rearm:{alert_id}",
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> "RearmAlertButton":
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        engine: Optional[AutonomousAlertEngine] = getattr(view, "engine", None) or getattr(
            interaction.client, "engine", None
        )
        alert: Optional[AlertRecord] = getattr(view, "alert", None)
        if alert is None and engine is not None:
            alert = await engine.db.get_alert_by_id(self.alert_id)
        if engine is None or alert is None:
            await interaction.response.send_message(
                "ℹ️ Bu alarm kaydı artık bulunamadı (silinmiş veya temizlenmiş olabilir).",
                ephemeral=True,
            )
            return
        if str(interaction.user.id) != str(alert.user_id):
            await interaction.response.send_message(
                "❌ Bu alarm sadece sahibi tarafından yönetilebilir.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(RearmAlertModal(engine, alert))


class DismissAlertButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"btn_dismiss:(?P<id>[0-9]+)",
):
    """Stateless persistent dismiss button working even after bot restarts."""

    def __init__(self, alert_id: int) -> None:
        self.alert_id = alert_id
        super().__init__(
            discord.ui.Button(
                label="🗑 Kapat",
                style=discord.ButtonStyle.secondary,
                custom_id=f"btn_dismiss:{alert_id}",
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> "DismissAlertButton":
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        engine: Optional[AutonomousAlertEngine] = getattr(view, "engine", None) or getattr(
            interaction.client, "engine", None
        )
        alert: Optional[AlertRecord] = getattr(view, "alert", None)
        if alert is None and engine is not None:
            alert = await engine.db.get_alert_by_id(self.alert_id)
        if alert is not None and str(interaction.user.id) != str(alert.user_id):
            await interaction.response.send_message(
                "❌ Bu alarm sadece sahibi tarafından yönetilebilir.",
                ephemeral=True,
            )
            return

        if view is not None:
            for child in view.children:
                if hasattr(child, "disabled"):
                    child.disabled = True
            await interaction.response.edit_message(view=view)
            view.stop()
        else:
            disabled_view = discord.ui.View(timeout=0)
            disabled_view.add_item(
                discord.ui.Button(
                    label="🔄 Yeniden Kur",
                    style=discord.ButtonStyle.green,
                    custom_id=f"btn_rearm:{self.alert_id}",
                    disabled=True,
                )
            )
            disabled_view.add_item(
                discord.ui.Button(
                    label="🗑 Kapat",
                    style=discord.ButtonStyle.secondary,
                    custom_id=f"btn_dismiss:{self.alert_id}",
                    disabled=True,
                )
            )
            await interaction.response.edit_message(view=disabled_view)


class AlertActionView(discord.ui.View):
    """Interactive Discord buttons attached to triggered alert messages."""

    def __init__(self, engine: AutonomousAlertEngine, alert: AlertRecord):
        super().__init__(timeout=3600)
        self.engine = engine
        self.alert = alert
        self.rearm_button = RearmAlertButton(alert.id)
        self.dismiss_button = DismissAlertButton(alert.id)
        self.add_item(self.rearm_button)
        self.add_item(self.dismiss_button)


class HelpCategorySelect(discord.ui.Select):
    """Interactive help dropdown switching between topic embeds."""

    def __init__(self):
        options = [
            discord.SelectOption(label="Genel Bakış & Hızlı Başlangıç", value="overview", emoji="🚀"),
            discord.SelectOption(label="Fiyat Alarmları (/alert)", value="alerts", emoji="🎯"),
            discord.SelectOption(label="Fiyat & Takip Listesi (/price, /watchlist)", value="market", emoji="💰"),
            discord.SelectOption(label="Desteklenen Semboller & Örnekler", value="symbols", emoji="📊"),
            discord.SelectOption(label="TradingView Webhook Entegrasyonu", value="tradingview", emoji="🔔"),
        ]
        super().__init__(placeholder="Bir yardım kategorisi seçin...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        cat = self.values[0]
        if cat == "overview":
            embed = discord.Embed(
                title="🚀 TradingView & Autonomous Market Bot Rehberi",
                description=(
                    "Bu bot; Kripto, Borsa İstanbul (BIST), Emtia (Altın, Petrol), Dünya Endeksleri ve Döviz kurlarını "
                    "7/24 otonom olarak takip eden ve TradingView webhook sinyallerini Discord kanalınıza ileten profesyonel bir asistandır."
                ),
                color=0x00D26A,
            )
            embed.add_field(
                name="⚡ Temel Yetenekler",
                value=(
                    "• **Otonom Fiyat Alarmları:** İstediğiniz fiyata veya yüzdesel değişime (`+5%`, `-3%`) alarm kurun.\n"
                    "• **Geniş Varlık Yelpazesi:** 210+ Kripto, 560+ BIST hissesi, 390+ ABD/Global hisse & ETF (`MU`, `SPCX`, `NVDA`, `SMH`, `IBIT`), Altın, Petrol, Döviz ve canlı global arama.\n"
                    "• **Kişisel Takip Listesi:** `/watchlist` ile portföyünüzün anlık değerlerini tek ekranda görün.\n"
                    "• **Özel Mesaj (DM) Desteği:** Alarmları sunucu yerine doğrudan özel mesajla alın.\n"
                    "• **TradingView Köprüsü:** Pine Script ve TradingView strateji sinyallerini anında iletin."
                ),
                inline=False,
            )
        elif cat == "alerts":
            embed = discord.Embed(
                title="🎯 Fiyat Alarmı Komutları (/alert)",
                description="İstediğiniz varlık için anlık fiyat takip alarmları oluşturun ve yönetin:",
                color=0x3399FF,
            )
            embed.add_field(
                name="🟢 /alert set",
                value=(
                    "Yeni bir alarm kurar.\n"
                    "• **Fiyat Örnekleri:** `70000`, `150.5`, `310,50`, `70k`\n"
                    "• **Yüzdesel Hedef:** `+5%` (anlık fiyattan %5 yukarı), `-3%` (%3 düşüş)\n"
                    "• **Parametreler:** `symbol`, `target_price`, `note` (isteğe bağlı), `dm: True` (isteğe bağlı)"
                ),
                inline=False,
            )
            embed.add_field(
                name="📋 /alert list",
                value="Kurduğunuz alarmları, anlık piyasa fiyatını ve hedefe olan mesafeyi listeler. Açılır menüden tek tıkla silebilirsiniz.",
                inline=False,
            )
            embed.add_field(
                name="🗑 /alert clear",
                value="Tetiklenmiş eski alarmlarınızı veya tüm alarmlarınızı topluca temizler.",
                inline=False,
            )
            embed.add_field(
                name="❌ /alert delete",
                value="Belirli bir alarmı ID numarasıyla iptal eder (Örn: `/alert delete alert_id:5`).",
                inline=False,
            )
        elif cat == "market":
            embed = discord.Embed(
                title="💰 Fiyat Sorgulama & Takip Listesi",
                color=0xF1C40F,
            )
            embed.add_field(
                name="🔍 /price",
                value=(
                    "Herhangi bir sembolün güncel fiyatını anında sorgular.\n"
                    "• **Örnekler:** `/price symbol:BTC`, `/price symbol:THYAO`, `/price symbol:GOLD`, `/price symbol:NASDAQ`"
                ),
                inline=False,
            )
            embed.add_field(
                name="⭐ /watchlist show",
                value="Kişisel listenizdeki tüm hisse, kripto ve emtiaların güncel fiyatlarını tablo halinde gösterir.",
                inline=False,
            )
            embed.add_field(
                name="➕ /watchlist add & remove",
                value=(
                    "• `/watchlist add symbol:BTC` -> Takip listenize ekler.\n"
                    "• `/watchlist remove symbol:BTC` -> Listenizden çıkarır."
                ),
                inline=False,
            )
        elif cat == "symbols":
            embed = discord.Embed(
                title="📊 Desteklenen Sembol Formatları",
                description="Sembolleri yazarken Discord otomatik tamamlama (Autocomplete) önerilerini kullanabilirsiniz:",
                color=0x9B59B6,
            )
            embed.add_field(
                name="🪙 Kripto Paralar (Binance + Yahoo)",
                value="`BTC`, `ETH`, `SOL`, `HYPE`, `PEPE`, `SUI`, `AVAX`, `DOGE`, `XRP`, `RENDER`, `TAO`, `NEAR`, `PAXG`, `TON`, `KAS`, `XMR` vb. (Tüm 500+ Binance USDT paritesi canlı desteklenir).",
                inline=False,
            )
            embed.add_field(
                name="🇺🇸 ABD, Global Hisseler & ETF'ler",
                value="`NVDA`, `MU` (Micron), `SPCX` (SpaceX), `AAPL`, `TSLA`, `MSFT`, `PLTR`, `AMD`, `RKLB`, `ASTS`, `OKLO`, `IONQ`, `SMH`, `IBIT`, `RHM.DE`, `MC.PA` vb. (Borsa öneki destekli: `NASDAQ:DASH`, `NYSE:AR`).",
                inline=False,
            )
            embed.add_field(
                name="🇹🇷 Borsa İstanbul (560+ BIST Hissesi)",
                value="`THYAO`, `ASELS`, `GARAN`, `EREGL`, `SISE`, `KCHOL`, `TUPRS`, `DOAS`, `ASTOR`, `TRALT`, `XU100`, `XU030` vb. (Sonuna `.IS` yazmanız gerekmez; `BIST:LINK` desteklenir).",
                inline=False,
            )
            embed.add_field(
                name="🏆 Emtialar & 🌐 Dünya Endeksleri & 💱 Döviz",
                value="`GOLD`/`ALTIN`, `SILVER`/`GUMUS`, `BRENT`, `WTI`, `NATGAS`, `COPPER`, `NASDAQ`, `SP500`, `DJI`, `SOX`, `DAX`, `DXY`, `VIX`, `US10Y`, `USDTRY`, `EURTRY`, `EURUSD` (90+ döviz çifti).",
                inline=False,
            )
        elif cat == "tradingview":
            embed = discord.Embed(
                title="🔔 TradingView Webhook Kurulumu",
                description="TradingView alarmlarınızı bot üzerinden Discord'a yönlendirmek için:",
                color=0x1E88E5,
            )
            embed.add_field(
                name="1. Webhook URL Formatı",
                value="`https://sizin-coolify-domaininiz.com/webhook/GIZLI_TOKENINIZ`\n*(Veya `?secret=GIZLI_TOKENINIZ` şeklinde)*",
                inline=False,
            )
            embed.add_field(
                name="2. TradingView Alarm Mesajı (JSON Şablonu)",
                value=(
                    "```json\n"
                    "{\n"
                    '  "ticker": "{{ticker}}",\n'
                    '  "action": "{{strategy.order.action}}",\n'
                    '  "price": {{close}},\n'
                    '  "strategy": "SuperTrend 15m",\n'
                    '  "timeframe": "{{interval}}",\n'
                    '  "message": "Kesişim sinyali gerçekleşti!"\n'
                    "}\n"
                    "```"
                ),
                inline=False,
            )
            embed.add_field(
                name="3. Düz Metin Desteği",
                value="JSON kullanmak istemiyorsanız TradingView mesajına doğrudan `BTCUSDT BUY 68500 Kırılım` yazabilirsiniz.",
                inline=False,
            )

        embed.set_footer(text="Daha fazla yardım için /help komutunu tekrar çalıştırabilirsiniz.")
        await interaction.response.edit_message(embed=embed, view=self.view)


class HelpView(discord.ui.View):
    """View containing the help category dropdown."""

    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(HelpCategorySelect())


_commands_synced_globally: bool = False


_UNSET = object()


class DiscordAlertBot(commands.Bot):
    """Discord Bot implementation handling Slash Commands, Autocomplete, Watchlist, and Notifications."""

    def __init__(
        self,
        engine: AutonomousAlertEngine,
        settings: Settings,
        proxy_override: object = _UNSET,
    ):
        intents = discord.Intents.default()
        if proxy_override is _UNSET:
            proxy_list = getattr(settings, "discord_proxy_list", None)
            if proxy_list:
                proxy_url = proxy_list[0]
            else:
                proxy_url = (settings.DISCORD_PROXY or "").strip() or None
        else:
            proxy_url = (str(proxy_override).strip() or None) if proxy_override is not None else None
        super().__init__(
            command_prefix="!",
            intents=intents,
            proxy=proxy_url,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.engine = engine
        self.settings = settings
        self.active_proxy = proxy_url
        self.start_time = time.time()
        self._watchlist_lock: Optional[asyncio.Lock] = None
        self._watchlist_lock_loop: Optional[asyncio.AbstractEventLoop] = None
        self.engine.trigger_callback = self.dispatch_alert_notification
        logging.getLogger("discord.gateway").setLevel(logging.INFO)
        logging.getLogger("discord.client").setLevel(logging.INFO)
        if proxy_url:
            logger.info("Discord Bot configured with outbound proxy: %s", proxy_url)
        elif proxy_override is None and getattr(settings, "discord_proxy_list", []):
            logger.info("Discord Bot attempting direct connection (proxy fallback mode).")

    def _get_watchlist_lock(self) -> asyncio.Lock:
        """Return an asyncio.Lock bound to the active event loop for atomic watchlist quota enforcement."""
        loop = asyncio.get_running_loop()
        if self._watchlist_lock is None or self._watchlist_lock_loop is not loop:
            self._watchlist_lock = asyncio.Lock()
            self._watchlist_lock_loop = loop
        return self._watchlist_lock

    async def setup_hook(self) -> None:
        """Register persistent dynamic items, slash command groups, and sync tree."""
        global _commands_synced_globally
        self.add_dynamic_items(RearmAlertButton, DismissAlertButton)
        self._register_commands()
        self._setup_error_handler()
        if not _commands_synced_globally:
            try:
                synced = await self.tree.sync()
                _commands_synced_globally = True
                logger.info("Synced %d Discord Slash Commands.", len(synced))
            except Exception as e:
                logger.warning("Failed syncing slash command tree: %s", e)
        else:
            logger.info("Slash commands already synced globally in this session; skipping remote tree sync.")

    def _setup_error_handler(self) -> None:
        """Global error handler for slash commands."""
        @self.tree.error
        async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
            safe_error = _redact_sensitive_text(error, max_length=1000)
            logger.error(
                "Error executing command %s: %s",
                interaction.command.name if interaction.command else "unknown",
                safe_error,
                exc_info=(safe_error == " ".join(str(error).splitlines()).strip()),
            )
            err_str = str(error)
            is_proxy_err = "503" in err_str or "ClientHttpProxyError" in err_str or "Proxy" in err_str or "Cannot connect to host" in err_str

            if is_proxy_err:
                proxy_label = _redact_sensitive_text(self.active_proxy) if self.active_proxy else "proxy"
                msg = (
                    "⚠️ **Geçici Proxy Bağlantı Hatası (503):**\n"
                    f"> Discord proxy bağlantısında (`{proxy_label}`) anlık bir rota yenilenmesi veya kesinti yaşandı.\n"
                    "> Lütfen **5-10 saniye bekleyip komutu tekrar deneyin**. Sorun devam ederse sunucudaki `spoof-dpi` / `warp` konteynerlerini kontrol edin."
                )
            else:
                msg = "⚠️ Komut işlenirken beklenmeyen bir hata oluştu. Lütfen tekrar deneyin."

            msg = _truncate_text(_sanitize_mentions(msg), MAX_DISCORD_CONTENT_LENGTH)

            try:
                if interaction.response.is_done():
                    await interaction.followup.send(msg, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
                else:
                    await interaction.response.send_message(msg, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            except Exception as send_err:
                logger.debug("Could not deliver error message to Discord user: %s", _redact_sensitive_text(send_err))

    async def on_connect(self) -> None:
        """Invoked when bot establishes connection with Discord Gateway."""
        latency_str = f"{self.latency * 1000:.1f}ms" if self.latency and self.latency > 0 else "N/A"
        logger.info("🔌 Discord Gateway connected (Latency: %s). Waiting for READY event...", latency_str)

    async def on_ready(self) -> None:
        """Invoked when bot successfully logs into Discord Gateway and goes Online."""
        logger.info("🚀 Discord Bot is ONLINE! Logged in as: %s (ID: %s)", self.user, self.user.id if self.user else "N/A")
        try:
            await self.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.watching,
                    name="Piyasalar | /alert | /help",
                ),
                status=discord.Status.online,
            )
        except Exception as e:
            logger.debug("Failed updating presence activity: %s", e)

    async def on_resumed(self) -> None:
        """Invoked when bot resumes a disconnected session with Discord Gateway."""
        logger.info("♻️ Discord Gateway session RESUMED. Resuming normal operations.")

    async def on_disconnect(self) -> None:
        """Invoked when bot loses connection to Discord Gateway."""
        logger.warning("⚡ Discord Gateway DISCONNECTED! Awaiting automatic reconnection or supervisor restart.")

    async def on_error(self, event_method: str, *args, **kwargs) -> None:
        """Catch-all for unhandled exceptions in event listeners."""
        logger.error("❌ Unhandled exception in Discord event '%s'", event_method, exc_info=True)

    def _register_commands(self) -> None:
        """Define all slash commands and command groups."""
        alert_group = app_commands.Group(name="alert", description="Fiyat alarmı kurma, listeleme ve yönetme komutları")
        watchlist_group = app_commands.Group(name="watchlist", description="Kişisel varlık takip listesi komutları")

        # --- /alert set ---
        @alert_group.command(name="set", description="Yeni bir fiyat alarmı kurar (Kripto, BIST, Altın, Döviz, Endeks)")
        @app_commands.describe(
            symbol="İşlem çifti veya sembol (Örn: BTC, THYAO, USDTRY, NASDAQ, GOLD)",
            target_price="Hedef fiyat veya yüzde (Örn: 70000, 150.5, +5%, -3%, 70k)",
            note="Alarm için isteğe bağlı hatırlatıcı not",
            dm="Tetiklendiğinde doğrudan özel mesaj (DM) ile bildirilsin mi? (Varsayılan: Hayır)",
        )
        @app_commands.autocomplete(symbol=symbol_autocomplete_handler)
        async def set_alert(
            interaction: discord.Interaction,
            symbol: app_commands.Range[str, 1, 64],
            target_price: app_commands.Range[str, 1, 64],
            note: Optional[app_commands.Range[str, 1, 150]] = None,
            dm: Optional[bool] = False,
        ):
            await safe_defer(interaction, ephemeral=False)
            bounded_symbol = str(symbol).strip()[:MAX_SYMBOL_INPUT_LENGTH]
            bounded_target = str(target_price).strip()[:MAX_SYMBOL_INPUT_LENGTH]
            bounded_note = note.strip()[:MAX_ALERT_NOTE_LENGTH] if isinstance(note, str) and note.strip() else None

            success, msg, record = await self.engine.create_user_alert(
                user_id=str(interaction.user.id),
                user_name=interaction.user.name,
                channel_id=str(interaction.channel_id),
                guild_id=str(interaction.guild_id) if interaction.guild else None,
                raw_symbol=bounded_symbol,
                target_price=bounded_target,
                note=bounded_note,
                notify_dm=bool(dm),
            )

            if not success or not record:
                safe_msg = _truncate_text(_sanitize_mentions(msg), MAX_DISCORD_CONTENT_LENGTH - 4)
                await safe_followup_send(
                    interaction,
                    content=f"❌ {safe_msg}",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            direction_tr = "🟢 Yükseliş Kırılımı (ABOVE)" if record.direction == "ABOVE" else "🔴 Düşüş Kırılımı (BELOW)"
            dm_badge = " • 📬 DM Bildirimi Aktif" if record.notify_dm else ""
            if record.initial_price > 0 and math.isfinite(record.initial_price):
                pct_distance = ((record.target_price - record.initial_price) / record.initial_price) * 100
                target_field_val = f"`{format_price(record.target_price)}` (`{pct_distance:+.2f}%`)"
            else:
                target_field_val = f"`{format_price(record.target_price)}`"

            embed = discord.Embed(
                title=_truncate_text(f"🎯 Alarm Kuruldu: {record.symbol}", MAX_EMBED_TITLE_LENGTH),
                color=0x00FF88 if record.direction == "ABOVE" else 0xFF3366,
                timestamp=datetime.fromisoformat(record.created_at),
            )
            embed.add_field(name="💵 Güncel Fiyat", value=f"`{format_price(record.initial_price)}`", inline=True)
            embed.add_field(
                name="🎯 Hedef Fiyat",
                value=_truncate_text(target_field_val, MAX_EMBED_FIELD_VALUE_LENGTH),
                inline=True,
            )
            embed.add_field(name="📊 Beklenen Yön", value=f"`{direction_tr}`", inline=False)
            if record.note:
                embed.add_field(
                    name="📝 Not",
                    value=_truncate_text(f"> {record.note}", MAX_EMBED_FIELD_VALUE_LENGTH),
                    inline=False,
                )
            embed.set_footer(text=f"Alarm ID: #{record.id} • {record.asset_type}{dm_badge}")

            await safe_followup_send(
                interaction,
                content=f"{interaction.user.mention}",
                embed=embed,
                allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=[interaction.user]),
            )

        # --- /alert list ---
        @alert_group.command(name="list", description="Kurduğunuz alarmları listeler ve tek tıkla silme menüsü sunar")
        @app_commands.describe(status="Listelenecek alarm durumu (Varsayılan: Sadece aktifler)")
        @app_commands.choices(status=[
            app_commands.Choice(name="🟢 Aktif Alarmlar", value="ACTIVE"),
            app_commands.Choice(name="⚡ Tetiklenmiş Alarmlar", value="TRIGGERED"),
            app_commands.Choice(name="📋 Tüm Alarmlar", value="ALL"),
        ])
        async def list_alerts(interaction: discord.Interaction, status: Optional[str] = "ACTIVE"):
            await safe_defer(interaction, ephemeral=True)
            filter_status = None if status == "ALL" else status
            alerts = await self.engine.db.get_user_alerts(user_id=str(interaction.user.id), status=filter_status)

            if not alerts:
                await safe_followup_send(interaction, content="📭 Bu kategoride kayıtlı alarmınız bulunmamaktadır.", ephemeral=True)
                return

            embed = discord.Embed(
                title=f"📋 Alarmlarınız ({len(alerts)} adet)",
                color=0x3399FF,
            )

            displayed_alerts = alerts[:15]
            active_symbols = list(dict.fromkeys(a.symbol for a in displayed_alerts if a.status == "ACTIVE"))
            prices_by_symbol = {}
            if active_symbols:
                semaphore = asyncio.Semaphore(WATCHLIST_FETCH_CONCURRENCY)

                async def _fetch_alert_price(sym: str) -> Optional[float]:
                    async with semaphore:
                        return await self.engine.feed.get_current_price(sym)

                price_results = await asyncio.gather(
                    *[_fetch_alert_price(sym) for sym in active_symbols],
                    return_exceptions=True,
                )
                for sym, res in zip(active_symbols, price_results):
                    prices_by_symbol[sym] = None if isinstance(res, Exception) else res

            for a in displayed_alerts:
                status_icon = "🟢" if a.status == "ACTIVE" else ("⚡" if a.status == "TRIGGERED" else "⚪")
                arrow = "⬆️" if a.direction == "ABOVE" else "⬇️"
                note_str = f"\n> 📝 *{a.note}*" if a.note else ""
                dm_str = " 📬" if a.notify_dm else ""

                current_price = prices_by_symbol.get(a.symbol) if a.status == "ACTIVE" else None
                if current_price is not None and math.isfinite(current_price) and current_price > 0 and a.status == "ACTIVE":
                    pct_distance = ((a.target_price - current_price) / current_price) * 100
                    dist_str = f" (Fark: `{pct_distance:+.2f}%`)"
                    price_line = f"Anlık: `{format_price(current_price)}`{dist_str} | Başlangıç: `{format_price(a.initial_price)}`"
                else:
                    price_line = f"Başlangıç: `{format_price(a.initial_price)}`"

                embed.add_field(
                    name=_truncate_text(
                        f"{status_icon} ID `#{a.id}`: {a.symbol} {arrow} `{format_price(a.target_price)}`{dm_str}",
                        MAX_EMBED_FIELD_NAME_LENGTH,
                    ),
                    value=_truncate_text(f"Durum: `{a.status}` | {price_line}{note_str}", MAX_EMBED_FIELD_VALUE_LENGTH),
                    inline=False,
                )

            if len(alerts) > 15:
                embed.set_footer(text=f"İlk 15 alarm listeleniyor. Toplam: {len(alerts)}")

            view = AlertListView(self.engine, alerts, str(interaction.user.id))
            await safe_followup_send(interaction, embed=embed, view=view, ephemeral=True)

        # --- /alert clear ---
        @alert_group.command(name="clear", description="Geçmiş veya tüm alarmlarınızı topluca temizler")
        @app_commands.describe(scope="Temizlenecek alarm türü (Varsayılan: Sadece tetiklenmişler)")
        @app_commands.choices(scope=[
            app_commands.Choice(name="⚡ Sadece Tetiklenmiş Alarmları Temizle", value="TRIGGERED"),
            app_commands.Choice(name="🟢 Sadece Aktif Alarmları İptal Et", value="ACTIVE"),
            app_commands.Choice(name="💥 TÜM Alarmlarımı Temizle (Aktif + Tetiklenmiş)", value="ALL"),
        ])
        async def clear_alerts(interaction: discord.Interaction, scope: Optional[str] = "TRIGGERED"):
            await safe_defer(interaction, ephemeral=True)
            deleted_count = await self.engine.db.clear_user_alerts(user_id=str(interaction.user.id), status=scope)
            if deleted_count > 0:
                scope_tr = "Tetiklenmiş" if scope == "TRIGGERED" else ("Aktif" if scope == "ACTIVE" else "Tüm")
                await safe_followup_send(interaction, content=f"🗑 **{deleted_count}** adet {scope_tr} alarm başarıyla temizlendi.", ephemeral=True)
            else:
                await safe_followup_send(interaction, content="ℹ️ Temizlenecek uygun alarm bulunamadı.", ephemeral=True)

        # --- /alert delete ---
        @alert_group.command(name="delete", description="Belirli bir alarmı ID ile iptal eder")
        @app_commands.describe(alert_id="İptal edilecek alarm ID'si (Örn: 5)")
        async def delete_alert(interaction: discord.Interaction, alert_id: int):
            await safe_defer(interaction, ephemeral=True)
            success = await self.engine.db.cancel_alert(alert_id=alert_id, user_id=str(interaction.user.id))
            if success:
                await safe_followup_send(interaction, content=f"🗑 ID `#{alert_id}` numaralı alarm iptal edildi.", ephemeral=True)
            else:
                await safe_followup_send(interaction, content=f"❌ `#{alert_id}` ID'li aktif bir alarmınız bulunamadı.", ephemeral=True)

        # --- /price ---
        @self.tree.command(name="price", description="Herhangi bir sembolün güncel fiyatını sorgular")
        @app_commands.describe(symbol="Sorgulanacak sembol (Örn: BTC, ETH, THYAO, GOLD, NASDAQ, DJI, USDTRY)")
        @app_commands.autocomplete(symbol=symbol_autocomplete_handler)
        async def price_query(interaction: discord.Interaction, symbol: app_commands.Range[str, 1, 64]):
            await safe_defer(interaction, ephemeral=False)
            bounded_symbol = str(symbol).strip()[:MAX_SYMBOL_INPUT_LENGTH]
            norm_symbol, _ = classify_and_normalize(bounded_symbol)
            current_price = await self.engine.feed.get_current_price(bounded_symbol)
            if current_price is None:
                safe_sym = _sanitize_mentions(bounded_symbol)
                content = _truncate_text(f"❌ **{safe_sym}** için anlık fiyat alınamadı.", MAX_DISCORD_CONTENT_LENGTH)
                await safe_followup_send(
                    interaction,
                    content=content,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            display_title = bounded_symbol.upper().strip()
            if display_title != norm_symbol:
                display_title = f"{display_title} ({norm_symbol})"

            embed = discord.Embed(
                title=_truncate_text(f"💰 Fiyat Bilgisi: {display_title}", MAX_EMBED_TITLE_LENGTH),
                color=0x3399FF,
                timestamp=datetime.now(),
            )
            embed.add_field(name="Anlık Değer", value=f"`{format_price(current_price)}`", inline=True)
            await safe_followup_send(interaction, embed=embed)

        # --- /watchlist show ---
        @watchlist_group.command(name="show", description="Kişisel takip listenizdeki tüm varlıkların anlık fiyatlarını gösterir")
        async def watchlist_show(interaction: discord.Interaction):
            await safe_defer(interaction, ephemeral=False)
            symbols = await self.engine.db.get_user_watchlist(str(interaction.user.id))

            if not symbols:
                await safe_followup_send(
                    interaction,
                    content="📭 Takip listeniz boş! `/watchlist add symbol:BTC` komutu ile semboller ekleyebilirsiniz.",
                    ephemeral=True,
                )
                return

            embed = discord.Embed(
                title="⭐ Kişisel Takip Listeniz",
                description="Listenizdeki varlıkların güncel anlık fiyatları:",
                color=0xF1C40F,
                timestamp=datetime.now(),
            )

            displayed_symbols = symbols[:MAX_WATCHLIST_ITEMS]
            semaphore = asyncio.Semaphore(WATCHLIST_FETCH_CONCURRENCY)

            async def _fetch_one(sym: str) -> Optional[float]:
                async with semaphore:
                    return await self.engine.feed.get_current_price(sym)

            results = await asyncio.gather(
                *[_fetch_one(sym) for sym in displayed_symbols],
                return_exceptions=True,
            )

            for sym, res in zip(displayed_symbols, results):
                price = None if isinstance(res, Exception) else res
                embed.add_field(name=f"🔹 {sym}", value=f"`{format_price(price)}`", inline=True)

            if len(symbols) > MAX_WATCHLIST_ITEMS:
                embed.set_footer(
                    text=f"İlk {MAX_WATCHLIST_ITEMS} / {len(symbols)} varlık gösteriliyor (kısaltıldı) • /watchlist"
                )
            else:
                embed.set_footer(text=f"Toplam: {len(symbols)} varlık • /watchlist")
            await safe_followup_send(interaction, embed=embed)

        # --- /watchlist add ---
        @watchlist_group.command(name="add", description="Takip listenize yeni bir sembol ekler")
        @app_commands.describe(symbol="Takip listesine eklenecek sembol (Örn: BTC, ETH, THYAO, GOLD)")
        @app_commands.autocomplete(symbol=symbol_autocomplete_handler)
        async def watchlist_add(interaction: discord.Interaction, symbol: str):
            await safe_defer(interaction, ephemeral=True)
            norm_symbol, asset_type = classify_and_normalize(symbol)
            if not is_valid_yahoo_symbol(norm_symbol):
                await safe_followup_send(
                    interaction,
                    content="❌ Geçersiz sembol formatı. Lütfen geçerli bir sembol girin (örn: BTC, THYAO, GOLD).",
                    ephemeral=True,
                )
                return

            user_id_str = str(interaction.user.id)
            async with self._get_watchlist_lock():
                existing = await self.engine.db.get_user_watchlist(user_id_str)
                if norm_symbol in existing:
                    already_exists = True
                    quota_reached = False
                    success = False
                elif len(existing) >= MAX_WATCHLIST_ITEMS:
                    already_exists = False
                    quota_reached = True
                    success = False
                else:
                    already_exists = False
                    quota_reached = False
                    success = await self.engine.db.add_to_watchlist(user_id_str, norm_symbol, asset_type)

            if already_exists:
                await safe_followup_send(
                    interaction,
                    content=f"ℹ️ **{norm_symbol}** zaten takip listenizde kayıtlı.",
                    ephemeral=True,
                )
                return
            if quota_reached:
                await safe_followup_send(
                    interaction,
                    content=f"❌ Takip listesi limitine ({MAX_WATCHLIST_ITEMS}) ulaştınız. Yeni sembol eklemek için önce `/watchlist remove` kullanın.",
                    ephemeral=True,
                )
                return

            if success:
                price = await self.engine.feed.get_current_price(norm_symbol)
                price_info = f" (Anlık: `{format_price(price)}`)" if price is not None else ""
                await safe_followup_send(
                    interaction,
                    content=f"⭐ **{norm_symbol}** takip listenize eklendi!{price_info}",
                    ephemeral=True,
                )
            else:
                await safe_followup_send(
                    interaction,
                    content=f"ℹ️ **{norm_symbol}** zaten takip listenizde kayıtlı.",
                    ephemeral=True,
                )

        # --- /watchlist remove ---
        @watchlist_group.command(name="remove", description="Takip listenizden bir sembolü kaldırır")
        @app_commands.describe(symbol="Kaldırılacak sembol")
        @app_commands.autocomplete(symbol=symbol_autocomplete_handler)
        async def watchlist_remove(interaction: discord.Interaction, symbol: str):
            await safe_defer(interaction, ephemeral=True)
            norm_symbol, _ = classify_and_normalize(symbol)
            success = await self.engine.db.remove_from_watchlist(str(interaction.user.id), norm_symbol)

            if success:
                await safe_followup_send(interaction, content=f"🗑 **{norm_symbol}** takip listenizden çıkarıldı.", ephemeral=True)
            else:
                await safe_followup_send(interaction, content=f"❌ **{norm_symbol}** takip listenizde bulunamadı.", ephemeral=True)

        # --- /help ---
        @self.tree.command(name="help", description="Bot özellikleri, komutlar ve sembol kullanım rehberi")
        async def help_command(interaction: discord.Interaction):
            await safe_defer(interaction, ephemeral=True)
            embed = discord.Embed(
                title="📖 TradingView & Market Alert Bot Rehberi",
                description=(
                    "Aşağıdaki açılır menüden ilgilendiğiniz kategoriyi seçerek detaylı bilgi edinebilirsiniz:\n\n"
                    "• 🚀 **Genel Bakış & Hızlı Başlangıç**\n"
                    "• 🎯 **Fiyat Alarmları (`/alert`)**\n"
                    "• 💰 **Fiyat & Takip Listesi (`/price`, `/watchlist`)**\n"
                    "• 📊 **Desteklenen Semboller & Örnekler**\n"
                    "• 🔔 **TradingView Webhook Entegrasyonu**"
                ),
                color=0x00D26A,
            )
            embed.set_footer(text="Bir kategori seçmek için aşağıdaki menüyü kullanın.")
            view = HelpView()
            await safe_followup_send(interaction, embed=embed, view=view, ephemeral=True)

        # --- /stats ---
        @self.tree.command(name="stats", description="Sistem durumu, gecikme süresi, aktif alarm sayıları ve istatistikler")
        async def stats_command(interaction: discord.Interaction):
            await safe_defer(interaction, ephemeral=False)
            db_stats = await self.engine.db.get_database_stats()
            uptime_seconds = int(time.time() - self.start_time)
            uptime_hours, rem = divmod(uptime_seconds, 3600)
            uptime_mins, uptime_secs = divmod(rem, 60)

            raw_latency = self.latency
            latency_ms = (
                round(raw_latency * 1000, 1)
                if (
                    isinstance(raw_latency, (int, float))
                    and not isinstance(raw_latency, bool)
                    and math.isfinite(raw_latency)
                    and raw_latency >= 0
                )
                else 0.0
            )

            embed = discord.Embed(
                title="📊 Sistem ve Bot İstatistikleri",
                color=0x3399FF,
                timestamp=datetime.now(),
            )
            embed.add_field(name="🏓 Ağ Gecikmesi (Ping)", value=f"`{latency_ms} ms`", inline=True)
            embed.add_field(name="⏱ Çalışma Süresi (Uptime)", value=f"`{uptime_hours}s {uptime_mins}d {uptime_secs}sn`", inline=True)
            embed.add_field(name="🟢 Aktif Alarmlar", value=f"`{db_stats['active_alerts']}` adet", inline=True)
            embed.add_field(name="⚡ Tetiklenmiş Alarmlar", value=f"`{db_stats['triggered_alerts']}` adet", inline=True)
            embed.add_field(name="⭐ Takip Listesi Varlıkları", value=f"`{db_stats['watchlist_items']}` adet", inline=True)
            embed.add_field(name="🌐 Piyasa Beslemesi", value="`Binance WS + Yahoo Poller`", inline=True)
            embed.set_footer(text="TradingView Price Alert & Market Engine")

            await safe_followup_send(interaction, embed=embed)

        self.tree.add_command(alert_group)
        self.tree.add_command(watchlist_group)

    async def dispatch_alert_notification(self, alert: AlertRecord, current_price: float) -> bool:
        """Send rich embed notification to target channel or directly via DM. Returns True if delivered, False otherwise."""
        if self.is_closed() or not self.is_ready():
            logger.warning("Discord Alert Bot client is closed or not ready. Skipping dispatch for alert #%d.", alert.id)
            return False

        is_bullish = alert.direction == "ABOVE"
        direction_label = "Yükseliş Kırılımı (ABOVE)" if is_bullish else "Düşüş Kırılımı (BELOW)"
        color = 0x00FF88 if is_bullish else 0xFF3366
        icon = "🚀" if is_bullish else "⚠️"
        if alert.initial_price > 0 and math.isfinite(alert.initial_price):
            pct_change = ((current_price - alert.initial_price) / alert.initial_price) * 100
            pct_suffix = f" (`{pct_change:+.2f}%`)"
        else:
            pct_suffix = ""

        embed = discord.Embed(
            title=_truncate_text(f"{icon} FİYAT ALARMI TETİKLENDİ: {alert.symbol}", MAX_EMBED_TITLE_LENGTH),
            description=_truncate_text(f"> **{alert.symbol}** hedef fiyata ulaştı!{pct_suffix}", MAX_EMBED_DESCRIPTION_LENGTH),
            color=color,
            timestamp=datetime.now(),
        )
        embed.add_field(name="🎯 Hedef Fiyat", value=f"`{format_price(alert.target_price)}`", inline=True)
        embed.add_field(name="⚡ Tetiklenme Fiyatı", value=f"`{format_price(current_price)}`", inline=True)
        embed.add_field(name="💵 Başlangıç Fiyatı", value=f"`{format_price(alert.initial_price)}`", inline=True)
        embed.add_field(name="📊 Hareket Yönü", value=f"`{direction_label}`", inline=False)

        if alert.note:
            embed.add_field(
                name="📝 Hatırlatıcı Not",
                value=_truncate_text(f"> {alert.note}", MAX_EMBED_FIELD_VALUE_LENGTH),
                inline=False,
            )

        embed.set_footer(text=f"Alarm ID: #{alert.id} • Coolify Alert Engine")
        view = AlertActionView(self.engine, alert)
        content = f"<@{alert.user_id}>"

        # 1. If notify_dm is requested, attempt Direct Message dispatch first
        if alert.notify_dm:
            for attempt in range(1, 4):
                try:
                    user = self.get_user(int(alert.user_id)) or await self.fetch_user(int(alert.user_id))
                    if user:
                        await user.send(content="🔔 **Fiyat Alarmınız Tetiklendi!**", embed=embed, view=view)
                        logger.info("Successfully dispatched DM alert #%d to user %s", alert.id, alert.user_id)
                        return True
                except (aiohttp.ClientError, asyncio.TimeoutError) as dm_net_err:
                    if attempt < 3:
                        await asyncio.sleep(0.5)
                    else:
                        logger.warning("Failed sending DM to user %s after 3 attempts: %s", alert.user_id, dm_net_err)
                except Exception as dm_err:
                    logger.warning("Failed sending DM to user %s (falling back to channel): %s", alert.user_id, dm_err)
                    break

        # 2. Standard Channel dispatch
        target_channel_id = self.settings.DISCORD_ALERT_CHANNEL_ID or int(alert.channel_id)

        delivered = False
        owner_mention_policy = discord.AllowedMentions(
            everyone=False,
            roles=False,
            users=[discord.Object(id=int(alert.user_id))],
        )
        for attempt in range(1, 4):
            try:
                channel = self.get_channel(target_channel_id)
                if not channel:
                    channel = await self.fetch_channel(target_channel_id)
                if not channel:
                    logger.error("Could not find Discord channel %s to dispatch alert.", target_channel_id)
                    break
                await channel.send(content=content, embed=embed, view=view, allowed_mentions=owner_mention_policy)
                logger.info("Successfully announced alert #%d in Discord channel %s", alert.id, target_channel_id)
                delivered = True
                break
            except (aiohttp.ClientError, asyncio.TimeoutError) as net_err:
                if attempt < 3:
                    logger.warning("Attempt %d/3 failed sending alert to channel %s: %s. Retrying in 0.5s...", attempt, target_channel_id, net_err)
                    await asyncio.sleep(0.5)
                else:
                    logger.error("Failed to send alert message to Discord channel after 3 attempts: %s", net_err, exc_info=True)
            except Exception as e:
                logger.error("Failed to send alert message to Discord channel: %s", e, exc_info=True)
                break

        return delivered
