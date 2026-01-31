"""Opportunity routes."""

from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.api.schemas import (
    OpportunityListResponse,
    OpportunityResponse,
    OpportunityUpdate,
)
from src.database import Opportunity, Market, get_async_session

router = APIRouter(prefix="/opportunities", tags=["opportunities"])


def opportunity_to_response(opp: Opportunity) -> OpportunityResponse:
    """Convert Opportunity model to response schema."""
    return OpportunityResponse(
        id=opp.id,
        matched_market_id=opp.matched_market_id,
        opportunity_type=opp.opportunity_type,
        platform_a=opp.platform_a,
        market_a_id=opp.market_a_id,
        side_a=opp.side_a,
        price_a=opp.price_a,
        platform_b=opp.platform_b,
        market_b_id=opp.market_b_id,
        side_b=opp.side_b,
        price_b=opp.price_b,
        gross_spread=opp.gross_spread,
        estimated_fees=opp.estimated_fees,
        net_profit_pct=opp.net_profit_pct,
        detected_at=opp.detected_at,
        expired_at=opp.expired_at,
        status=opp.status,
        user_acted=opp.user_acted,
        user_action_at=opp.user_action_at,
        user_notes=opp.user_notes,
        position_size=opp.position_size,
        actual_profit=opp.actual_profit,
        actual_fees=opp.actual_fees,
        market_a_title=opp.market_a.title if opp.market_a else None,
        market_b_title=opp.market_b.title if opp.market_b else None,
        market_a_url=opp.market_a.url if opp.market_a else None,
        market_b_url=opp.market_b.url if opp.market_b else None,
    )


@router.get("", response_model=OpportunityListResponse)
async def list_opportunities(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[str] = Query(None, description="Filter by status"),
    platform: Optional[str] = Query(None, description="Filter by platform"),
    user_acted: Optional[bool] = Query(None, description="Filter by user action"),
    min_profit_pct: Optional[float] = Query(None, description="Minimum net profit %"),
    days: Optional[int] = Query(None, description="Limit to last N days"),
    session: AsyncSession = Depends(get_async_session),
):
    """List opportunities with filtering and pagination."""
    # Build query
    query = select(Opportunity).options(
        selectinload(Opportunity.market_a),
        selectinload(Opportunity.market_b),
    )

    # Apply filters
    conditions = []

    if status:
        conditions.append(Opportunity.status == status)

    if platform:
        conditions.append(
            (Opportunity.platform_a == platform) | (Opportunity.platform_b == platform)
        )

    if user_acted is not None:
        conditions.append(Opportunity.user_acted == user_acted)

    if min_profit_pct is not None:
        conditions.append(Opportunity.net_profit_pct >= min_profit_pct)

    if days:
        cutoff = datetime.utcnow() - timedelta(days=days)
        conditions.append(Opportunity.detected_at >= cutoff)

    if conditions:
        query = query.where(and_(*conditions))

    # Get total count
    count_query = select(func.count()).select_from(
        query.subquery()
    )
    total = await session.scalar(count_query) or 0

    # Add pagination and ordering
    query = query.order_by(Opportunity.detected_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)

    result = await session.execute(query)
    opportunities = result.scalars().all()

    return OpportunityListResponse(
        items=[opportunity_to_response(opp) for opp in opportunities],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size,
    )


@router.get("/{opportunity_id}", response_model=OpportunityResponse)
async def get_opportunity(
    opportunity_id: UUID,
    session: AsyncSession = Depends(get_async_session),
):
    """Get a specific opportunity by ID."""
    query = (
        select(Opportunity)
        .options(
            selectinload(Opportunity.market_a),
            selectinload(Opportunity.market_b),
        )
        .where(Opportunity.id == opportunity_id)
    )

    result = await session.execute(query)
    opportunity = result.scalar_one_or_none()

    if not opportunity:
        raise HTTPException(status_code=404, detail="Opportunity not found")

    return opportunity_to_response(opportunity)


@router.patch("/{opportunity_id}", response_model=OpportunityResponse)
async def update_opportunity(
    opportunity_id: UUID,
    update: OpportunityUpdate,
    session: AsyncSession = Depends(get_async_session),
):
    """Update an opportunity (e.g., mark as acted upon)."""
    query = (
        select(Opportunity)
        .options(
            selectinload(Opportunity.market_a),
            selectinload(Opportunity.market_b),
        )
        .where(Opportunity.id == opportunity_id)
    )

    result = await session.execute(query)
    opportunity = result.scalar_one_or_none()

    if not opportunity:
        raise HTTPException(status_code=404, detail="Opportunity not found")

    # Update fields
    update_data = update.model_dump(exclude_unset=True)

    for field, value in update_data.items():
        setattr(opportunity, field, value)

    # Set action timestamp if marking as acted
    if update.user_acted is not None and opportunity.user_action_at is None:
        opportunity.user_action_at = datetime.utcnow()

    await session.commit()
    await session.refresh(opportunity)

    return opportunity_to_response(opportunity)
