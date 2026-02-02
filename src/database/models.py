"""SQLAlchemy database models."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for all models."""

    pass


class Market(Base):
    """Markets from each prediction platform."""

    __tablename__ = "markets"
    __table_args__ = (UniqueConstraint("platform", "platform_market_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    platform: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    platform_market_id: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    resolution_criteria: Mapped[Optional[str]] = mapped_column(Text)
    category: Mapped[Optional[str]] = mapped_column(String(100))
    end_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(50), default="open")
    url: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow
    )

    # Relationships
    prices: Mapped[list["Price"]] = relationship(back_populates="market")

    def __repr__(self) -> str:
        return f"<Market {self.platform}:{self.platform_market_id} '{self.title[:50]}'>"


class Price(Base):
    """Price snapshots for markets."""

    __tablename__ = "prices"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    market_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("markets.id"), nullable=False, index=True
    )
    yes_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4))
    no_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4))
    yes_volume: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2))
    no_volume: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2))
    bid_yes: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4))
    ask_yes: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4))
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, index=True
    )

    # Relationships
    market: Mapped["Market"] = relationship(back_populates="prices")

    def __repr__(self) -> str:
        return f"<Price market={self.market_id} yes={self.yes_price} no={self.no_price}>"


class MatchedMarket(Base):
    """Markets matched across platforms (same event)."""

    __tablename__ = "matched_markets"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    market_a_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("markets.id"), nullable=False
    )
    market_b_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("markets.id"), nullable=False
    )
    match_confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), default=0)
    match_method: Mapped[str] = mapped_column(String(50), default="automatic")
    resolution_compatible: Mapped[Optional[bool]] = mapped_column(Boolean)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow
    )

    # Relationships
    market_a: Mapped["Market"] = relationship(foreign_keys=[market_a_id])
    market_b: Mapped["Market"] = relationship(foreign_keys=[market_b_id])
    opportunities: Mapped[list["Opportunity"]] = relationship(back_populates="matched_market")

    def __repr__(self) -> str:
        return f"<MatchedMarket {self.market_a_id} <-> {self.market_b_id}>"


class Opportunity(Base):
    """Detected arbitrage opportunities."""

    __tablename__ = "opportunities"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    matched_market_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("matched_markets.id")
    )
    opportunity_type: Mapped[str] = mapped_column(
        String(50), default="cross_platform"
    )  # cross_platform, logical, cross_platform_logical
    opportunity_subtype: Mapped[Optional[str]] = mapped_column(
        String(50)
    )  # Detailed subtype (e.g., championship_vs_playoffs, deadline_inconsistency)
    opportunity_subtype_display: Mapped[Optional[str]] = mapped_column(
        String(100)
    )  # Human-readable subtype name

    # Platform A details
    platform_a: Mapped[str] = mapped_column(String(50), nullable=False)
    market_a_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("markets.id"), nullable=False
    )
    side_a: Mapped[str] = mapped_column(String(10), nullable=False)  # yes or no
    price_a: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)

    # Platform B details
    platform_b: Mapped[str] = mapped_column(String(50), nullable=False)
    market_b_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("markets.id"), nullable=False
    )
    side_b: Mapped[str] = mapped_column(String(10), nullable=False)
    price_b: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)

    # Calculations
    gross_spread: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    estimated_fees: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    net_profit_pct: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)

    # Per-platform fee breakdowns
    fee_a_entry: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4))
    fee_a_profit: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4))
    fee_a_total: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4))
    fee_b_entry: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4))
    fee_b_profit: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4))
    fee_b_total: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4))

    # Status
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, index=True
    )
    expired_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        String(50), default="active"
    )  # active, expired, executed

    # User action
    user_acted: Mapped[Optional[bool]] = mapped_column(Boolean)
    user_action_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    user_notes: Mapped[Optional[str]] = mapped_column(Text)

    # Actual results (if executed)
    position_size: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2))
    actual_profit: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2))
    actual_fees: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2))

    # Relationships
    matched_market: Mapped[Optional["MatchedMarket"]] = relationship(
        back_populates="opportunities"
    )
    market_a: Mapped["Market"] = relationship(foreign_keys=[market_a_id])
    market_b: Mapped["Market"] = relationship(foreign_keys=[market_b_id])
    notifications: Mapped[list["Notification"]] = relationship(back_populates="opportunity")

    def __repr__(self) -> str:
        return f"<Opportunity {self.platform_a}/{self.platform_b} net={self.net_profit_pct}%>"


class PriceHistory(Base):
    """Historical price tracking for analysis.

    Stores every price update to analyze:
    - How long arbitrage windows stay open
    - Price movement patterns
    - Best times to scan
    """

    __tablename__ = "price_history"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    platform: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    platform_market_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    market_title: Mapped[str] = mapped_column(Text, nullable=False)

    # Prices
    yes_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4))
    no_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4))
    yes_bid: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4))
    yes_ask: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4))

    # Volume & Liquidity
    volume_24h: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2))
    total_volume: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2))
    open_interest: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2))

    # Timestamp
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, index=True
    )

    def __repr__(self) -> str:
        return f"<PriceHistory {self.platform}:{self.platform_market_id} yes={self.yes_price} @ {self.recorded_at}>"


class Notification(Base):
    """Discord notification log."""

    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("opportunities.id"), nullable=False
    )
    discord_message_id: Mapped[Optional[str]] = mapped_column(String(50))
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow
    )
    response_received_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    response: Mapped[Optional[str]] = mapped_column(
        String(50)
    )  # acted, passed, no_response

    # Relationships
    opportunity: Mapped["Opportunity"] = relationship(back_populates="notifications")

    def __repr__(self) -> str:
        return f"<Notification {self.opportunity_id} response={self.response}>"
