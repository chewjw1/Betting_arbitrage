"""Data source health and status routes."""

import asyncio
import ssl
import urllib.request
import json
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from src.database import Market, Opportunity, Price, get_async_session

logger = structlog.get_logger()
router = APIRouter(prefix="/health", tags=["health"])


class DataSourceStatus(BaseModel):
    """Status of a single data source."""
    name: str
    status: str  # ok, error, disabled, stale
    last_check: datetime
    markets_count: Optional[int] = None
    response_time_ms: Optional[int] = None
    error: Optional[str] = None


class ScanStatus(BaseModel):
    """Status of the last scan."""
    last_scan: Optional[datetime] = None
    markets_count: int = 0
    opportunities_found: int = 0


class HealthStatusResponse(BaseModel):
    """Full health status response."""
    timestamp: datetime
    overall_status: str  # healthy, degraded, unhealthy
    data_sources: list[DataSourceStatus]
    scan_status: ScanStatus


# Keep in-memory status for scheduler updates (same-process only)
_scan_status = ScanStatus()


def update_scan_status(
    scan_type: str,
    markets_by_platform: dict,
    opportunities_found: int
):
    """Called by scheduler to update scan status (in-memory, same process only)."""
    global _scan_status

    total_markets = sum(len(m) for m in markets_by_platform.values())
    _scan_status.last_scan = datetime.utcnow()
    _scan_status.markets_count = total_markets
    _scan_status.opportunities_found = opportunities_found


async def get_scan_status_from_db(session: AsyncSession) -> ScanStatus:
    """Derive scan status from the database (works across processes)."""
    scan_status = ScanStatus()

    all_platforms = ["kalshi", "polymarket", "predictit", "draftkings"]

    # Count total markets
    count_query = (
        select(func.count(Market.id))
        .where(Market.platform.in_(all_platforms))
    )
    result = await session.execute(count_query)
    scan_status.markets_count = result.scalar() or 0

    # Get last price update time
    last_price_query = (
        select(func.max(Price.timestamp))
        .join(Market, Price.market_id == Market.id)
        .where(Market.platform.in_(all_platforms))
    )
    last_price_result = await session.execute(last_price_query)
    scan_status.last_scan = last_price_result.scalar()

    # Count recent opportunities (last 24h)
    opp_query = (
        select(func.count(Opportunity.id))
        .where(Opportunity.detected_at >= datetime.utcnow() - timedelta(hours=24))
    )
    opp_result = await session.execute(opp_query)
    scan_status.opportunities_found = opp_result.scalar() or 0

    return scan_status


async def check_predictit() -> DataSourceStatus:
    """Check PredictIt API health."""
    start = datetime.utcnow()
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        req = urllib.request.Request("https://www.predictit.org/api/marketdata/all/")

        loop = asyncio.get_event_loop()

        def fetch():
            with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
                return json.loads(resp.read().decode())

        data = await loop.run_in_executor(None, fetch)
        markets = data.get("markets", [])
        contracts = sum(len(m.get("contracts", [])) for m in markets)

        elapsed = (datetime.utcnow() - start).total_seconds() * 1000

        return DataSourceStatus(
            name="predictit",
            status="ok",
            last_check=datetime.utcnow(),
            markets_count=contracts,
            response_time_ms=int(elapsed),
        )
    except Exception as e:
        return DataSourceStatus(
            name="predictit",
            status="error",
            last_check=datetime.utcnow(),
            error=str(e)[:100],
        )


async def check_polymarket() -> DataSourceStatus:
    """Check Polymarket API health."""
    start = datetime.utcnow()
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        req = urllib.request.Request(
            "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=100"
        )
        req.add_header("User-Agent", "Mozilla/5.0")

        loop = asyncio.get_event_loop()

        def fetch():
            with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
                return json.loads(resp.read().decode())

        data = await loop.run_in_executor(None, fetch)
        count = len(data) if isinstance(data, list) else 0

        elapsed = (datetime.utcnow() - start).total_seconds() * 1000

        return DataSourceStatus(
            name="polymarket",
            status="ok",
            last_check=datetime.utcnow(),
            markets_count=count,
            response_time_ms=int(elapsed),
        )
    except Exception as e:
        return DataSourceStatus(
            name="polymarket",
            status="error",
            last_check=datetime.utcnow(),
            error=str(e)[:100],
        )


async def check_kalshi() -> DataSourceStatus:
    """Check Kalshi API health."""
    start = datetime.utcnow()
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        req = urllib.request.Request(
            "https://api.elections.kalshi.com/trade-api/v2/markets?limit=100&status=open"
        )

        loop = asyncio.get_event_loop()

        def fetch():
            with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
                return json.loads(resp.read().decode())

        data = await loop.run_in_executor(None, fetch)
        count = len(data.get("markets", []))

        elapsed = (datetime.utcnow() - start).total_seconds() * 1000

        return DataSourceStatus(
            name="kalshi",
            status="ok",
            last_check=datetime.utcnow(),
            markets_count=count,
            response_time_ms=int(elapsed),
        )
    except Exception as e:
        return DataSourceStatus(
            name="kalshi",
            status="error",
            last_check=datetime.utcnow(),
            error=str(e)[:100],
        )


async def check_scraper_status_from_db(
    name: str,
    session: AsyncSession,
) -> DataSourceStatus:
    """Check scraper status from the database."""
    # Check if we have any markets from this platform
    count_query = (
        select(func.count(Market.id))
        .where(Market.platform == name)
    )
    result = await session.execute(count_query)
    market_count = result.scalar() or 0

    # Check last price update
    last_update_query = (
        select(func.max(Price.timestamp))
        .join(Market, Price.market_id == Market.id)
        .where(Market.platform == name)
    )
    last_result = await session.execute(last_update_query)
    last_update = last_result.scalar()

    if market_count == 0 and last_update is None:
        return DataSourceStatus(
            name=name,
            status="disabled",
            last_check=datetime.utcnow(),
            markets_count=0,
            error="No data collected yet",
        )

    # If last update was more than 10 minutes ago, mark as stale
    if last_update:
        age = (datetime.utcnow() - last_update).total_seconds()
        if age > 600:
            return DataSourceStatus(
                name=name,
                status="stale",
                last_check=last_update,
                markets_count=market_count,
                error=f"Last update {int(age/60)} minutes ago",
            )

    return DataSourceStatus(
        name=name,
        status="ok",
        last_check=last_update or datetime.utcnow(),
        markets_count=market_count,
    )


@router.get("/status", response_model=HealthStatusResponse)
async def get_health_status(
    session: AsyncSession = Depends(get_async_session),
):
    """Get detailed health status of all data sources."""

    # Check API sources in parallel
    api_checks = await asyncio.gather(
        check_predictit(),
        check_polymarket(),
        check_kalshi(),
        return_exceptions=True,
    )

    data_sources = []
    for check in api_checks:
        if isinstance(check, Exception):
            data_sources.append(DataSourceStatus(
                name="unknown",
                status="error",
                last_check=datetime.utcnow(),
                error=str(check),
            ))
        else:
            data_sources.append(check)

    # Add DraftKings status from database
    dk_status = await check_scraper_status_from_db("draftkings", session)
    data_sources.append(dk_status)

    # Get scan status from database
    scan_status = await get_scan_status_from_db(session)

    # Determine overall status
    ok_count = sum(1 for ds in data_sources if ds.status == "ok")
    error_count = sum(1 for ds in data_sources if ds.status == "error")
    disabled_count = sum(1 for ds in data_sources if ds.status == "disabled")

    # Don't count disabled scrapers against health
    active_sources = len(data_sources) - disabled_count
    if active_sources == 0:
        overall = "unhealthy"
    elif error_count == 0 or ok_count >= 2:
        overall = "healthy" if error_count == 0 else "degraded"
    else:
        overall = "unhealthy"

    return HealthStatusResponse(
        timestamp=datetime.utcnow(),
        overall_status=overall,
        data_sources=data_sources,
        scan_status=scan_status,
    )


@router.get("/sources")
async def get_sources_quick(
    session: AsyncSession = Depends(get_async_session),
):
    """Quick check of data sources from database."""
    scan_status = await get_scan_status_from_db(session)

    return {
        "timestamp": datetime.utcnow().isoformat(),
        "last_scan": scan_status.last_scan.isoformat() if scan_status.last_scan else None,
        "markets_count": scan_status.markets_count,
        "opportunities_found": scan_status.opportunities_found,
    }
