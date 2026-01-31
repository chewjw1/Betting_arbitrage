"""FastAPI application main entry point."""

from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import structlog

from src.api.routes import markets, opportunities, stats
from src.api.schemas import HealthResponse
from src.config import get_settings
from src.database import init_db

logger = structlog.get_logger()
settings = get_settings()


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
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(opportunities.router, prefix="/api/v1")
app.include_router(markets.router, prefix="/api/v1")
app.include_router(stats.router, prefix="/api/v1")


@app.get("/", tags=["root"])
async def root():
    """Root endpoint."""
    return {
        "name": "Prediction Market Arbitrage API",
        "version": "0.1.0",
        "docs": "/docs",
    }


@app.get("/health", response_model=HealthResponse, tags=["health"])
async def health_check():
    """Health check endpoint."""
    # TODO: Add actual health checks for database, redis, and collectors
    return HealthResponse(
        status="healthy",
        database="connected",
        redis="connected",
        collectors={
            "kalshi": "ok",
            "polymarket": "ok",
            "predictit": "ok",
        },
        timestamp=datetime.utcnow(),
    )


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
