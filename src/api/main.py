"""FastAPI application main entry point."""

from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
import structlog

from src.api.routes import markets, opportunities, stats, health
from src.config import get_settings
from src.database import init_db

logger = structlog.get_logger()
settings = get_settings()

# Path to templates
TEMPLATES_DIR = Path(__file__).parent / "templates"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    # Startup
    logger.info("Starting API server")
    await init_db()
    yield
    # Shutdown
    logger.info("Shutting down API server")


app = FastAPI(
    title="Prediction Market Arbitrage API",
    description="API for viewing and managing prediction market arbitrage opportunities",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://jfk21.phoebe.usbx.me:44495",
        "https://jfk21.phoebe.usbx.me:44495",
        "http://localhost:44495",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(opportunities.router, prefix="/api/v1")
app.include_router(markets.router, prefix="/api/v1")
app.include_router(stats.router, prefix="/api/v1")
app.include_router(health.router, prefix="/api/v1")


@app.get("/", response_class=HTMLResponse, tags=["dashboard"])
async def dashboard():
    """Serve the dashboard HTML page."""
    dashboard_path = TEMPLATES_DIR / "dashboard.html"
    if dashboard_path.exists():
        return HTMLResponse(content=dashboard_path.read_text())
    return HTMLResponse(content="<h1>Dashboard not found</h1>", status_code=404)


@app.get("/api", tags=["root"])
async def api_root():
    """API root endpoint."""
    return {
        "name": "Prediction Market Arbitrage API",
        "version": "0.1.0",
        "docs": "/docs",
        "dashboard": "/",
    }


@app.get("/health", tags=["health"])
async def health_check():
    """Quick health check. For detailed status, use /api/v1/health/status."""
    return {
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "detail": "Use /api/v1/health/status for full platform health",
    }


def run_api():
    """Run the API server."""
    import uvicorn

    uvicorn.run(
        "src.api.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=True,
    )


if __name__ == "__main__":
    run_api()
