"""Pydantic schemas for API requests and responses."""

from datetime import datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class MarketBase(BaseModel):
    """Base market schema."""

    platform: str
    platform_market_id: str
    title: str
    description: Optional[str] = None
    category: Optional[str] = None
    end_date: Optional[datetime] = None
    status: str = "open"
    url: Optional[str] = None


class MarketResponse(MarketBase):
    """Market response schema."""

    id: UUID
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class PriceResponse(BaseModel):
    """Price snapshot response."""

    id: UUID
    market_id: UUID
    yes_price: Optional[Decimal] = None
    no_price: Optional[Decimal] = None
    yes_volume: Optional[Decimal] = None
    no_volume: Optional[Decimal] = None
    timestamp: datetime

    class Config:
        from_attributes = True


class FeeBreakdownSchema(BaseModel):
    """Fee breakdown for a single platform."""

    platform: str
    entry_fee: Decimal
    profit_fee: Decimal
    total_fee: Decimal
    fee_pct: Optional[Decimal] = None


class OpportunityBase(BaseModel):
    """Base opportunity schema."""

    opportunity_type: str = "cross_platform"
    opportunity_subtype: Optional[str] = None
    opportunity_subtype_display: Optional[str] = None
    platform_a: str
    market_a_id: UUID
    side_a: str
    price_a: Decimal
    platform_b: str
    market_b_id: UUID
    side_b: str
    price_b: Decimal
    gross_spread: Decimal
    estimated_fees: Decimal
    net_profit_pct: Decimal


class OpportunityResponse(OpportunityBase):
    """Opportunity response schema."""

    id: UUID
    matched_market_id: Optional[UUID] = None
    detected_at: datetime
    expired_at: Optional[datetime] = None
    status: str
    user_acted: Optional[bool] = None
    user_action_at: Optional[datetime] = None
    user_notes: Optional[str] = None
    position_size: Optional[Decimal] = None
    actual_profit: Optional[Decimal] = None
    actual_fees: Optional[Decimal] = None

    # Include market details
    market_a_title: Optional[str] = None
    market_b_title: Optional[str] = None
    market_a_url: Optional[str] = None
    market_b_url: Optional[str] = None

    # Per-platform fee breakdowns
    fee_a_entry: Optional[Decimal] = None
    fee_a_profit: Optional[Decimal] = None
    fee_a_total: Optional[Decimal] = None
    fee_b_entry: Optional[Decimal] = None
    fee_b_profit: Optional[Decimal] = None
    fee_b_total: Optional[Decimal] = None

    # Spread persistence
    first_detected_at: Optional[datetime] = None
    times_seen: int = 1
    spread_persistent: bool = False

    class Config:
        from_attributes = True


class OpportunityUpdate(BaseModel):
    """Schema for updating an opportunity."""

    user_acted: Optional[bool] = None
    user_notes: Optional[str] = None
    position_size: Optional[Decimal] = None
    actual_profit: Optional[Decimal] = None
    actual_fees: Optional[Decimal] = None
    status: Optional[str] = None


class OpportunityListResponse(BaseModel):
    """Paginated list of opportunities."""

    items: list[OpportunityResponse]
    total: int
    page: int
    page_size: int
    total_pages: int


class StatsResponse(BaseModel):
    """Dashboard statistics."""

    total_opportunities: int
    opportunities_today: int
    opportunities_acted: int
    opportunities_passed: int
    total_potential_profit: Decimal
    total_actual_profit: Optional[Decimal] = None
    average_net_profit_pct: Decimal
    top_platforms: list[dict]
    opportunities_by_day: list[dict]


class HealthResponse(BaseModel):
    """Health check response."""

    status: str
    database: str
    redis: str
    collectors: dict[str, str]
    timestamp: datetime
