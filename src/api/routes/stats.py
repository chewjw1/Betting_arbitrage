"""Statistics routes."""

from datetime import datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas import StatsResponse
from src.database import Opportunity, get_async_session

router = APIRouter(prefix="/stats", tags=["statistics"])


@router.get("", response_model=StatsResponse)
async def get_stats(
    days: int = Query(30, ge=1, le=365, description="Number of days to include"),
    session: AsyncSession = Depends(get_async_session),
):
    """Get dashboard statistics."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    # Total opportunities in period
    total_query = select(func.count()).where(
        Opportunity.detected_at >= cutoff
    )
    total = await session.scalar(total_query) or 0

    # Opportunities today
    today_query = select(func.count()).where(
        Opportunity.detected_at >= today_start
    )
    today_count = await session.scalar(today_query) or 0

    # Acted vs passed
    acted_query = select(func.count()).where(
        and_(
            Opportunity.detected_at >= cutoff,
            Opportunity.user_acted == True,
        )
    )
    acted_count = await session.scalar(acted_query) or 0

    passed_query = select(func.count()).where(
        and_(
            Opportunity.detected_at >= cutoff,
            Opportunity.user_acted == False,
        )
    )
    passed_count = await session.scalar(passed_query) or 0

    # Potential profit (sum of net_profit_pct for all opportunities)
    potential_profit_query = select(
        func.coalesce(func.sum(Opportunity.net_profit_pct), 0)
    ).where(Opportunity.detected_at >= cutoff)
    potential_profit = await session.scalar(potential_profit_query) or Decimal("0")

    # Actual profit (sum of actual_profit for acted opportunities)
    actual_profit_query = select(
        func.sum(Opportunity.actual_profit)
    ).where(
        and_(
            Opportunity.detected_at >= cutoff,
            Opportunity.user_acted == True,
            Opportunity.actual_profit.isnot(None),
        )
    )
    actual_profit = await session.scalar(actual_profit_query)

    # Average net profit %
    avg_profit_query = select(
        func.coalesce(func.avg(Opportunity.net_profit_pct), 0)
    ).where(Opportunity.detected_at >= cutoff)
    avg_profit = await session.scalar(avg_profit_query) or Decimal("0")

    # Top platforms
    platform_query = select(
        Opportunity.platform_a,
        func.count().label("count"),
    ).where(
        Opportunity.detected_at >= cutoff
    ).group_by(
        Opportunity.platform_a
    ).order_by(
        func.count().desc()
    ).limit(5)

    platform_result = await session.execute(platform_query)
    top_platforms = [
        {"platform": row[0], "count": row[1]}
        for row in platform_result.all()
    ]

    # Opportunities by day (last 14 days)
    fourteen_days_ago = datetime.utcnow() - timedelta(days=14)
    daily_query = select(
        func.date(Opportunity.detected_at).label("date"),
        func.count().label("count"),
        func.avg(Opportunity.net_profit_pct).label("avg_profit"),
    ).where(
        Opportunity.detected_at >= fourteen_days_ago
    ).group_by(
        func.date(Opportunity.detected_at)
    ).order_by(
        func.date(Opportunity.detected_at)
    )

    daily_result = await session.execute(daily_query)
    opportunities_by_day = [
        {
            "date": str(row[0]),
            "count": row[1],
            "avg_profit": float(row[2]) if row[2] else 0,
        }
        for row in daily_result.all()
    ]

    return StatsResponse(
        total_opportunities=total,
        opportunities_today=today_count,
        opportunities_acted=acted_count,
        opportunities_passed=passed_count,
        total_potential_profit=potential_profit,
        total_actual_profit=actual_profit,
        average_net_profit_pct=avg_profit,
        top_platforms=top_platforms,
        opportunities_by_day=opportunities_by_day,
    )
