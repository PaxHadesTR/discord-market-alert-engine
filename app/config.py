from functools import lru_cache
from typing import Any, List, Optional
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables and .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Server Configuration
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    LOG_LEVEL: str = "INFO"

    # Security: Secret token required for TradingView Webhooks
    WEBHOOK_SECRET: str = "change_me_in_production"

    # Discord Webhook Configuration (for TradingView direct forwarding)
    DISCORD_WEBHOOK_URL: Optional[str] = None
    DISCORD_BOT_USERNAME: str = "TradingView Alert"
    DISCORD_AVATAR_URL: Optional[str] = "https://upload.wikimedia.org/wikipedia/commons/e/e4/TradingView_Logo.svg"

    # Discord Bot Configuration (for interactive Slash Commands and autonomous alerts)
    DISCORD_BOT_TOKEN: Optional[str] = None
    DISCORD_ALERT_CHANNEL_ID: Optional[int] = None
    # Optional HTTP/SOCKS5 proxy (or comma-separated failover list) for connecting to Discord from blocked regions
    DISCORD_PROXY: Optional[str] = None
    # When all configured proxies fail consecutively (e.g. WARP 503), probe direct connection before rotating back
    DISCORD_PROXY_FALLBACK_DIRECT: bool = True
    DISCORD_PROXY_FAILOVER_THRESHOLD: int = 2

    # Autonomous Alert Engine & Limits
    SQLITE_DB_PATH: str = "./data/alerts.db"
    MAX_ACTIVE_ALERTS: int = 100
    MAX_USER_ACTIVE_ALERTS: int = 25
    ALERT_EXPIRY_DAYS: int = 180
    YAHOO_POLL_INTERVAL_SECONDS: float = 20.0
    CACHE_TTL_SECONDS: float = 20.0
    BINANCE_WS_URL: str = "wss://stream.binance.com:9443/ws"

    # Rate Limiting & Queue Settings
    RATE_LIMIT_PER_SECOND: float = 2.0
    MAX_QUEUE_CAPACITY: int = 500
    HTTP_TIMEOUT_SECONDS: float = 10.0

    @field_validator("DISCORD_ALERT_CHANNEL_ID", mode="before")
    @classmethod
    def _coerce_blank_channel_id(cls, value: Any) -> Any:
        """Treat blank DISCORD_ALERT_CHANNEL_ID env values (from .env.example) as None."""
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def is_discord_bot_configured(self) -> bool:
        """Check if a valid, non-placeholder Discord bot token is configured."""
        token = (self.DISCORD_BOT_TOKEN or "").strip().strip('"').strip("'")
        dummy_tokens = {
            "your_discord_bot_token_here",
            "buraya_bot_tokeniniz",
            "none",
            "null",
            "dummy",
            "undefined",
            "false",
        }
        return bool(token and token.lower() not in dummy_tokens)

    @property
    def discord_proxy_list(self) -> List[str]:
        """Parse comma-separated DISCORD_PROXY values into an ordered, deduplicated list."""
        raw = (self.DISCORD_PROXY or "").strip()
        if not raw:
            return []
        proxies: List[str] = []
        for part in raw.split(","):
            cleaned = part.strip().strip('"').strip("'")
            if cleaned and cleaned not in proxies:
                proxies.append(cleaned)
        return proxies


@lru_cache()
def get_settings() -> Settings:
    """Return cached singleton instance of Settings."""
    return Settings()
