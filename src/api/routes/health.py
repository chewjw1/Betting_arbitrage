"""Data source health and status routes."""

import asyncio
import ssl
import urllib.request
import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel
import structlog

logger = structlog.get_logger()
router = APIRouter(prefix="/health", tags=["health"])


class DataSourceStatus(BaseModel):
    """Status of a single data source."""
    name: str
    status: str  # ok, error, disabled
    last_check: datetime
    markets_count: Optional[int] = None
    response_time_ms: Optional[int] = None
    error: Optional[str] = None


class ScanStatus(BaseModel):
    """Status of the last scan."""
    last_api_scan: Optional[datetime] = None
    last_scrape_scan: Optional[datetime] = None
    api_markets_count: int = 0
    scrape_markets_count: int = 0
    opportunities_found: int = 0


class HealthStatusResponse(BaseModel):
    """Full health status response."""
    timestamp: datetime
    overall_status: str  # healthy, degraded, unhealthy
    data_sources: list[DataSourceStatus]
    scan_status: ScanStatus


# Store last scan info (updated by scheduler)
_scan_status = ScanStatus()


def update_scan_status(
    scan_type: str,
    markets_by_platform: dict,
    opportunities_found: int
):
    """Called by scheduler to update scan status."""
    global _scan_status

    total_markets = sum(len(m) for m in markets_by_platform.values())

    if scan_type == "api":
        _scan_status.last_api_scan = datetime.utcnow()
        _scan_status.api_markets_count = total_markets
    else:
        _scan_status.last_scrape_scan = datetime.utcnow()
        _scan_status.scrape_markets_count = total_markets

    _scan_status.opportunities_found = opportunities_found


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


def check_scraper_status(name: str) -> DataSourceStatus:
    """Check scraper status (based on last scan, not live check)."""
    # Scrapers are checked based on last successful scan, not live
    # because live checks would be too slow
    global _scan_status

    if _scan_status.last_scrape_scan is None:
        return DataSourceStatus(
            name=name,
            status="disabled",
            last_check=datetime.utcnow(),
            error="No scrape scan recorded yet",
        )

    # If last scrape was more than 10 minutes ago, mark as stale
    age = (datetime.utcnow() - _scan_status.last_scrape_scan).total_seconds()
    if age > 600:  # 10 minutes
        return DataSourceStatus(
            name=name,
            status="stale",
            last_check=_scan_status.last_scrape_scan,
            error=f"Last scan {int(age/60)} minutes ago",
        )

    return DataSourceStatus(
        name=name,
        status="ok",
        last_check=_scan_status.last_scrape_scan,
    )


@router.get("/status", response_model=HealthStatusResponse)
async def get_health_status():
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

    # Add scraper status (not live checked)
    for scraper in ["draftkings", "fanduel", "ibkr"]:
        data_sources.append(check_scraper_status(scraper))

    # Determine overall status
    ok_count = sum(1 for ds in data_sources if ds.status == "ok")
    error_count = sum(1 for ds in data_sources if ds.status == "error")

    if error_count == 0:
        overall = "healthy"
    elif ok_count >= 2:
        overall = "degraded"
    else:
        overall = "unhealthy"

    return HealthStatusResponse(
        timestamp=datetime.utcnow(),
        overall_status=overall,
        data_sources=data_sources,
        scan_status=_scan_status,
    )


@router.get("/sources")
async def get_sources_quick():
    """Quick check of data sources (cached, fast)."""
    global _scan_status

    return {
        "timestamp": datetime.utcnow().isoformat(),
        "last_api_scan": _scan_status.last_api_scan.isoformat() if _scan_status.last_api_scan else None,
        "last_scrape_scan": _scan_status.last_scrape_scan.isoformat() if _scan_status.last_scrape_scan else None,
        "api_markets": _scan_status.api_markets_count,
        "scrape_markets": _scan_status.scrape_markets_count,
        "opportunities_found": _scan_status.opportunities_found,
    }
