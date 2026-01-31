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
    poll_interval_seconds: int = Field(
        default=30,
        description="How often to poll for new opportunities",
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
}
