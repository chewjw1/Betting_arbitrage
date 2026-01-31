"""Application configuration using Pydantic Settings."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Database
    database_url: str = Field(
        default="postgresql+asyncpg://betting:betting_password@localhost:5432/betting_arbitrage",
        description="Async database URL",
    )
    database_url_sync: str = Field(
        default="postgresql://betting:betting_password@localhost:5432/betting_arbitrage",
        description="Sync database URL for migrations",
    )

    # Redis
    redis_url: str = Field(default="redis://localhost:6379/0")

    # Kalshi API
    kalshi_api_key: str = Field(default="")
    kalshi_private_key_path: Path = Field(default=Path("./kalshi_private_key.pem"))
    kalshi_api_host: str = Field(default="https://api.elections.kalshi.com")

    # Polymarket API
    polymarket_api_host: str = Field(default="https://clob.polymarket.com")
    polymarket_gamma_host: str = Field(default="https://gamma-api.polymarket.com")
    polymarket_api_key: str = Field(default="")
    polymarket_api_secret: str = Field(default="")
    polymarket_passphrase: str = Field(default="")

    # PredictIt API
    predictit_api_host: str = Field(default="https://www.predictit.org")

    # DraftKings (scraping)
    draftkings_base_url: str = Field(default="https://predictions.draftkings.com")
    apify_api_token: str = Field(default="", description="Apify API token for DraftKings scraping")

    # FanDuel (scraping)
    fanduel_base_url: str = Field(default="https://www.fanduel.com/predicts")

    # Interactive Brokers
    ibkr_username: str = Field(default="")
    ibkr_password: str = Field(default="")
    ibkr_account_id: str = Field(default="")

    # Discord
    discord_bot_token: str = Field(default="")
    discord_channel_id: int = Field(default=0)
    discord_guild_id: int = Field(default=0)

    # Arbitrage Settings
    min_net_spread_pct: float = Field(
        default=1.0,
        description="Minimum net profit percentage to trigger an alert",
    )
    max_position_size: float = Field(
        default=500.0,
        description="Maximum dollars per side of an arbitrage position",
    )
    api_poll_interval_seconds: int = Field(
        default=60,
        description="How often to poll API-based platforms (Kalshi, Polymarket, PredictIt)",
    )
    scrape_poll_interval_seconds: int = Field(
        default=180,
        description="How often to poll scraping-based platforms (DraftKings, FanDuel, IBKR)",
    )
    # Deduplication: How long to suppress repeat notifications for same opportunity (seconds)
    notification_cooldown_seconds: int = Field(
        default=300,
        description="Don't re-notify for same market pair within this window (5 min default)",
    )

    # API Settings
    api_host: str = Field(default="0.0.0.0")
    api_port: int = Field(default=8000)

    # Logging
    log_level: str = Field(default="INFO")


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


# Platform fee configurations
PLATFORM_FEES = {
    "kalshi": {
        "trading_fee_pct": 0.012,  # 1.2% per trade
        "profit_fee_pct": 0.02,  # 2% on profits
        "deposit_fee": 0,
        "withdrawal_fee": 0,
    },
    "polymarket": {
        "trading_fee_pct": 0.0001,  # 0.01% for US
        "profit_fee_pct": 0,
        "deposit_fee": 0,
        "withdrawal_fee": 0,
    },
    "polymarket_intl": {
        "trading_fee_pct": 0,
        "profit_fee_pct": 0,
        "deposit_fee": 0,
        "withdrawal_fee": 0,
    },
    "predictit": {
        "trading_fee_pct": 0,
        "profit_fee_pct": 0.10,  # 10% on profits
        "withdrawal_pct": 0.05,  # 5% withdrawal fee
        "deposit_fee": 0,
        "withdrawal_fee": 0,
    },
    "manifold": {
        "trading_fee_pct": 0,
        "profit_fee_pct": 0,
        "deposit_fee": 0,
        "withdrawal_fee": 0,
    },
    "draftkings": {
        # DraftKings uses CME contracts, fees similar to CME
        "trading_fee_pct": 0.01,  # ~1% estimated
        "profit_fee_pct": 0,
        "deposit_fee": 0,
        "withdrawal_fee": 0,
    },
    "fanduel": {
        # FanDuel Predicts uses CME contracts
        "trading_fee_pct": 0.01,  # ~1% estimated
        "profit_fee_pct": 0,
        "deposit_fee": 0,
        "withdrawal_fee": 0,
    },
    "ibkr": {
        # IBKR ForecastTrader - zero commission
        "trading_fee_pct": 0,
        "profit_fee_pct": 0,
        "deposit_fee": 0,
        "withdrawal_fee": 0,
    },
    "coinbase": {
        # Coinbase uses Kalshi contracts - same fee structure
        "trading_fee_pct": 0.012,
        "profit_fee_pct": 0.02,
        "deposit_fee": 0,
        "withdrawal_fee": 0,
    },
    "robinhood": {
        # Robinhood uses Kalshi contracts - fees included in spread
        "trading_fee_pct": 0,  # Built into spread
        "profit_fee_pct": 0,
        "deposit_fee": 0,
        "withdrawal_fee": 0,
    },
}
