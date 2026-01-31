"""Market routes."""

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas import MarketResponse, PriceResponse
from src.database import Market, Price, get_async_session

router = APIRouter(prefix="/markets", tags=["markets"])


@router.get("", response_model=list[MarketResponse])
async def list_markets(
    platform: Optional[str] = Query(None, description="Filter by platform"),
    status: Optional[str] = Query(None, description="Filter by status"),
    search: Optional[str] = Query(None, description="Search in title"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_async_session),
):
    """List markets with filtering."""
    query = select(Market)

    if platform:
        query = query.where(Market.platform == platform)

    if status:
        query = query.where(Market.status == status)

    if search:
        query = query.where(Market.title.ilike(f"%{search}%"))

    query = query.order_by(Market.updated_at.desc())
    query = query.offset(offset).limit(limit)

    result = await session.execute(query)
    markets = result.scalars().all()

    return [MarketResponse.model_validate(m) for m in markets]


@router.get("/{market_id}", response_model=MarketResponse)
async def get_market(
    market_id: UUID,
    session: AsyncSession = Depends(get_async_session),
):
    """Get a specific market by ID."""
    query = select(Market).where(Market.id == market_id)
    result = await session.execute(query)
    market = result.scalar_one_or_none()

    if not market:
        raise HTTPException(status_code=404, detail="Market not found")

    return MarketResponse.model_validate(market)


@router.get("/{market_id}/prices", response_model=list[PriceResponse])
async def get_market_prices(
    market_id: UUID,
    limit: int = Query(100, ge=1, le=1000),
    session: AsyncSession = Depends(get_async_session),
):
    """Get price history for a market."""
    # Verify market exists
    market_query = select(Market).where(Market.id == market_id)
    market_result = await session.execute(market_query)
    if not market_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Market not found")

    # Get prices
    query = (
        select(Price)
        .where(Price.market_id == market_id)
        .order_by(Price.timestamp.desc())
        .limit(limit)
    )

    result = await session.execute(query)
    prices = result.scalars().all()

    return [PriceResponse.model_validate(p) for p in prices]


@router.get("/platforms/list", response_model=list[str])
async def list_platforms(
    session: AsyncSession = Depends(get_async_session),
):
    """Get list of all platforms with markets."""
    query = select(Market.platform).distinct()
    result = await session.execute(query)
    return [row[0] for row in result.all()]
