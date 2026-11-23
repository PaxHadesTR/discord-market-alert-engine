import asyncio
import logging
import math
from typing import Any, Dict, List, Optional
import httpx
from app.models.alert import AlertPayload
from app.providers.base import BaseNotificationProvider

logger = logging.getLogger(__name__)

MAX_EMBED_TITLE = 256
MAX_EMBED_DESCRIPTION = 4096
MAX_EMBED_FIELD_NAME = 256
MAX_EMBED_FIELD_VALUE = 1024
MAX_EMBED_FOOTER = 2048
MAX_EMBED_TOTAL = 6000


def _truncate_str(text: str, max_length: int) -> str:
    """Truncate text with ellipsis so len(result) <= max_length."""
    s = str(text)
    if len(s) <= max_length:
        return s
    if max_length <= 3:
        return s[:max_length]
    return s[: max_length - 3].rstrip() + "..."


def _wrap_code_value(value: Any, max_length: int = MAX_EMBED_FIELD_VALUE) -> str:
    """Wrap a field value in inline backticks while strictly respecting max_length."""
    raw = str(value).strip()
    if not raw:
        raw = "-"
    inner_max = max(1, max_length - 2)
    return f"`{_truncate_str(raw, inner_max)}`"


def _enforce_embed_limits(
    title: str,
    description: Optional[str],
    fields: List[Dict[str, Any]],
    footer_text: str,
) -> tuple[str, Optional[str], List[Dict[str, Any]], str]:
    """Clamp all Discord embed parts and enforce the 6000-character total embed budget."""
    safe_title = _truncate_str(title, MAX_EMBED_TITLE)
    safe_footer = _truncate_str(footer_text, MAX_EMBED_FOOTER)
    safe_desc = _truncate_str(description, MAX_EMBED_DESCRIPTION) if description else None

    safe_fields: List[Dict[str, Any]] = []
    for f in fields:
        name = _truncate_str(str(f.get("name") or "-").strip() or "-", MAX_EMBED_FIELD_NAME)
        val = _truncate_str(str(f.get("value") or "-").strip() or "-", MAX_EMBED_FIELD_VALUE)
        safe_fields.append({"name": name, "value": val, "inline": bool(f.get("inline", False))})

    def _total_chars(desc: Optional[str], flds: List[Dict[str, Any]]) -> int:
        return (
            len(safe_title)
            + len(safe_footer)
            + (len(desc) if desc else 0)
            + sum(len(item["name"]) + len(item["value"]) for item in flds)
        )

    # First trim description if over total budget
    if safe_desc and _total_chars(safe_desc, safe_fields) > MAX_EMBED_TOTAL:
        non_desc_len = _total_chars(None, safe_fields)
        allowed_desc = max(0, MAX_EMBED_TOTAL - non_desc_len)
        if allowed_desc >= 64:
            safe_desc = _truncate_str(safe_desc, allowed_desc)
        else:
            # Reserve modest description room and trim fields below
            safe_desc = _truncate_str(safe_desc, min(len(safe_desc), 512))

    # If still over budget, progressively trim field values (keeping backticks closed if wrapped)
    if _total_chars(safe_desc, safe_fields) > MAX_EMBED_TOTAL and safe_fields:
        excess = _total_chars(safe_desc, safe_fields) - MAX_EMBED_TOTAL
        for item in reversed(safe_fields):
            if excess <= 0:
                break
            val = item["value"]
            min_len = 8 if (val.startswith("`") and val.endswith("`")) else 4
            reducible = max(0, len(val) - min_len)
            if reducible > 0:
                cut = min(reducible, excess)
                new_len = len(val) - cut
                if val.startswith("`") and val.endswith("`"):
                    item["value"] = _wrap_code_value(val[1:-1], new_len)
                else:
                    item["value"] = _truncate_str(val, new_len)
                excess = _total_chars(safe_desc, safe_fields) - MAX_EMBED_TOTAL

    # If still over budget, trim field names then drop trailing fields as last resort
    if _total_chars(safe_desc, safe_fields) > MAX_EMBED_TOTAL:
        for item in reversed(safe_fields):
            if _total_chars(safe_desc, safe_fields) <= MAX_EMBED_TOTAL:
                break
            if len(item["name"]) > 32:
                item["name"] = _truncate_str(item["name"], 32)
        while safe_fields and _total_chars(safe_desc, safe_fields) > MAX_EMBED_TOTAL:
            safe_fields.pop()

    # Final guard on description if even without fields it exceeds budget
    if safe_desc and _total_chars(safe_desc, safe_fields) > MAX_EMBED_TOTAL:
        allowed_desc = max(0, MAX_EMBED_TOTAL - _total_chars(None, safe_fields))
        safe_desc = _truncate_str(safe_desc, allowed_desc) if allowed_desc > 0 else None

    return safe_title, safe_desc, safe_fields, safe_footer


class DiscordWebhookProvider(BaseNotificationProvider):
    """Dispatches TradingView alerts to Discord channels using Discord Webhook API."""

    def __init__(
        self,
        webhook_url: str,
        bot_username: str = "TradingView Alert",
        avatar_url: Optional[str] = None,
        timeout_seconds: float = 10.0,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.webhook_url = webhook_url
        self.bot_username = bot_username
        self.avatar_url = avatar_url
        self.timeout = timeout_seconds
        self._external_client = client is not None
        self._client = client or httpx.AsyncClient(timeout=self.timeout)

    async def send(self, alert: AlertPayload) -> bool:
        """Construct Discord Embed and POST to the configured Webhook URL."""
        if not self.webhook_url:
            logger.warning("Discord webhook URL is not configured. Alert skipped.")
            return False

        payload = self.build_discord_payload(alert)

        max_retries = 3
        backoff = 1.0

        for attempt in range(1, max_retries + 1):
            try:
                response = await self._client.post(self.webhook_url, json=payload)

                if response.status_code in (200, 204):
                    logger.info("Successfully dispatched alert to Discord: %s %s", alert.ticker, alert.action)
                    return True

                elif response.status_code == 429:
                    retry_after = 2.0
                    try:
                        rate_data = response.json()
                        raw_retry = rate_data.get("retry_after", 2.0)
                        if not isinstance(raw_retry, bool):
                            parsed_retry = float(raw_retry)
                            if math.isfinite(parsed_retry) and parsed_retry >= 0:
                                retry_after = parsed_retry
                    except Exception:
                        pass
                    retry_after = min(retry_after, 30.0)
                    logger.warning("Discord 429 Rate Limit hit. Backing off for %.2fs (Attempt %d/%d)", retry_after, attempt, max_retries)
                    if attempt < max_retries:
                        await asyncio.sleep(retry_after)

                elif response.status_code >= 500:
                    logger.warning("Discord server error (HTTP %d). Retrying in %.2fs...", response.status_code, backoff)
                    if attempt < max_retries:
                        await asyncio.sleep(backoff)
                    backoff *= 2

                else:
                    logger.error("Failed to send alert to Discord (HTTP %d): %s", response.status_code, response.text)
                    return False

            except (httpx.RequestError, httpx.TimeoutException) as exc:
                logger.warning("Network error while dispatching to Discord: %s. Retrying in %.2fs...", exc, backoff)
                if attempt < max_retries:
                    await asyncio.sleep(backoff)
                backoff *= 2
            except Exception as e:
                logger.error("Unexpected error in Discord dispatch: %s", e, exc_info=True)
                return False

        logger.error("Exceeded maximum retries (%d) dispatching alert %s to Discord.", max_retries, alert.ticker)
        return False

    def build_discord_payload(self, alert: AlertPayload) -> Dict[str, Any]:
        """Generate structured Discord webhook JSON body containing rich Embeds."""
        # Emoji and Title formatting
        if alert.is_bullish:
            icon = "🟢"
            direction = f"BUY / {alert.normalized_action}"
        elif alert.is_bearish:
            icon = "🔴"
            direction = f"SELL / {alert.normalized_action}"
        else:
            icon = "🔔"
            direction = alert.normalized_action

        title = f"{icon} {alert.ticker} - {direction}"

        fields: List[Dict[str, Any]] = []

        if alert.price is not None:
            # Format price nicely
            if alert.price >= 1.0:
                price_str = f"${alert.price:,.2f}" if alert.price > 10 else f"${alert.price:.4f}"
            else:
                price_str = f"${alert.price:.8f}".rstrip("0")
            fields.append({"name": "💵 Price", "value": f"`{price_str}`", "inline": True})

        if alert.strategy:
            fields.append({"name": "🎯 Strategy", "value": _wrap_code_value(alert.strategy), "inline": True})

        if alert.timeframe:
            fields.append({"name": "⏱ Timeframe", "value": _wrap_code_value(alert.timeframe), "inline": True})

        # Add custom extra data fields (up to 5)
        if alert.extra_data:
            for k, v in list(alert.extra_data.items())[:5]:
                field_name = _truncate_str(f"🔹 {str(k).capitalize()}", MAX_EMBED_FIELD_NAME)
                fields.append({"name": field_name, "value": _wrap_code_value(v), "inline": True})

        description = None
        if alert.message:
            description = f"> {alert.message}"

        footer_text = "TradingView Alert Dispatcher • Coolify"
        title, description, fields, footer_text = _enforce_embed_limits(
            title=title,
            description=description,
            fields=fields,
            footer_text=footer_text,
        )

        embed = {
            "title": title,
            "description": description,
            "color": alert.embed_color,
            "fields": fields,
            "timestamp": alert.timestamp,
            "footer": {
                "text": footer_text,
            },
        }

        body: Dict[str, Any] = {
            "username": self.bot_username,
            "embeds": [embed],
        }

        if self.avatar_url:
            body["avatar_url"] = self.avatar_url

        return body

    async def close(self) -> None:
        """Close the underlying HTTP client session if owned."""
        if not self._external_client and not self._client.is_closed:
            await self._client.aclose()


class DiscordBotNotificationProvider(BaseNotificationProvider):
    """Dispatches TradingView alerts directly to a Discord channel using the active Discord Bot client."""

    def __init__(self, bot: Any, channel_id: int):
        self.bot = bot
        self.channel_id = channel_id

    async def send(self, alert: AlertPayload) -> bool:
        if not self.bot or not self.channel_id:
            return False

        if getattr(self.bot, "is_closed", None) and self.bot.is_closed():
            logger.warning("DiscordBotNotificationProvider: Bot client is closed/reconnecting. Skipping dispatch.")
            return False

        try:
            channel = self.bot.get_channel(self.channel_id)
            if not channel:
                channel = await self.bot.fetch_channel(self.channel_id)
            if not channel:
                logger.error("DiscordBotNotificationProvider: Channel %s not found.", self.channel_id)
                return False

            import discord
            is_bullish = alert.is_bullish
            is_bearish = alert.is_bearish
            icon = "🟢" if is_bullish else ("🔴" if is_bearish else "🔔")
            direction = f"BUY / {alert.normalized_action}" if is_bullish else (
                f"SELL / {alert.normalized_action}" if is_bearish else alert.normalized_action
            )

            raw_fields: List[Dict[str, Any]] = []
            if alert.price is not None:
                if alert.price >= 1.0:
                    price_str = f"${alert.price:,.2f}" if alert.price > 10 else f"${alert.price:.4f}"
                else:
                    price_str = f"${alert.price:.8f}".rstrip("0")
                raw_fields.append({"name": "💵 Fiyat", "value": _wrap_code_value(price_str), "inline": True})
            if alert.strategy:
                raw_fields.append({"name": "🎯 Strateji", "value": _wrap_code_value(alert.strategy), "inline": True})
            if alert.timeframe:
                raw_fields.append({"name": "⏱ Zaman Dilimi", "value": _wrap_code_value(alert.timeframe), "inline": True})

            if alert.extra_data:
                for k, v in list(alert.extra_data.items())[:5]:
                    raw_fields.append({
                        "name": _truncate_str(f"🔹 {str(k).capitalize()}", MAX_EMBED_FIELD_NAME),
                        "value": _wrap_code_value(v),
                        "inline": True,
                    })

            title, description, bounded_fields, footer_text = _enforce_embed_limits(
                title=f"{icon} {alert.ticker} - {direction}",
                description=f"> {alert.message}" if alert.message else None,
                fields=raw_fields,
                footer_text="TradingView Alert Dispatcher • Discord Bot",
            )

            embed = discord.Embed(
                title=title,
                description=description,
                color=alert.embed_color,
            )
            for item in bounded_fields:
                embed.add_field(name=item["name"], value=item["value"], inline=item["inline"])
            embed.set_footer(text=footer_text)
            await channel.send(embed=embed)
            logger.info("Successfully dispatched TradingView alert via Bot to channel %s", self.channel_id)
            return True
        except Exception as e:
            logger.error("Failed dispatching alert via Discord Bot: %s", e)
            return False

    async def close(self) -> None:
        pass
