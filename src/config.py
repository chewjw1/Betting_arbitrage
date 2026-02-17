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
    kalshi_categories: str = Field(
        default="Politics,Economics,Crypto,Elections,Financials,Climate and Weather,World",
        description="Comma-separated Kalshi categories to fetch (skips Sports, Entertainment, etc.)",
    )
    kalshi_max_markets: int = Field(
        default=2000,
        description="Maximum Kalshi markets to fetch per scan",
    )

    # Polymarket API
    polymarket_api_host: str = Field(default="https://clob.polymarket.com")
    polymarket_gamma_host: str = Field(default="https://gamma-api.polymarket.com")
    polymarket_api_key: str = Field(default="")
    polymarket_api_secret: str = Field(default="")
    polymarket_passphrase: str = Field(default="")

    # PredictIt API
    predictit_api_host: str = Field(default="https://www.predictit.org")

    # DraftKings API
    draftkings_base_url: str = Field(default="https://predictions.draftkings.com")

    # Discord
    discord_bot_token: str = Field(default="")
    discord_channel_id: int = Field(default=0)
    discord_guild_id: int = Field(default=0)

    # Arbitrage Settings
    min_net_spread_pct: float = Field(
        default=0.5,
        description="Minimum net profit percentage to trigger an alert (lowered for structural spreads)",
    )
    max_position_size: float = Field(
        default=500.0,
        description="Maximum dollars per side of an arbitrage position",
    )
    api_poll_interval_seconds: int = Field(
        default=600,
        description="How often to poll API-based platforms (10 min default, allows LLM validation to complete)",
    )
    # Deduplication: How long to suppress repeat notifications for same opportunity (seconds)
    notification_cooldown_seconds: int = Field(
        default=600,
        description="Don't re-notify for same market pair within this window (10 min default)",
    )
    # Matching confidence threshold
    min_match_confidence: float = Field(
        default=0.65,
        description="Minimum fuzzy match confidence for cross-platform market pairing",
    )
    # Logical arbitrage detection
    min_logical_violation_pct: float = Field(
        default=1.5,
        description="Minimum constraint violation percentage for logical arbitrage alerts",
    )
    # Spread persistence: how many scans before marking as structural
    spread_persistence_threshold: int = Field(
        default=3,
        description="Number of scans an opportunity must persist before marking as structural",
    )
    # Resolution window
    max_days_to_resolution: int = Field(
        default=180,
        description="Maximum days until resolution to consider (structural spreads can be longer)",
    )

    # LLM Match Validation (uses GPT-4o-mini)
    openai_api_key: str = Field(default="")
    llm_validation_enabled: bool = Field(
        default=True,
        description="Use GPT-4o-mini to validate fuzzy matches. Requires OPENAI_API_KEY.",
    )
    llm_model: str = Field(
        default="gpt-4o-mini",
        description="OpenAI model for validation ($0.15/$0.60 per M tokens).",
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
