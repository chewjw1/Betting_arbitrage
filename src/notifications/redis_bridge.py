"""Notification bridge for delivering opportunities from scanner to Discord bot.

Supports two modes:
1. Redis pub/sub (multi-process: separate scheduler + Discord bot containers)
2. In-process asyncio.Queue (single-process: seedbox / simple deployment)

The scanner publishes opportunities; the Discord bot consumes them.
"""

import asyncio
import json
from datetime import datetime
from decimal import Decimal
from typing import Optional

import structlog

from src.config import get_settings

logger = structlog.get_logger()

# Redis channels (only used in Redis mode)
CHANNEL_OPPORTUNITIES = "arb:opportunities"


class DecimalEncoder(json.JSONEncoder):
    """JSON encoder that handles Decimal types."""

    def default(self, obj):
        if isinstance(obj, Decimal):
            return str(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)


def _serialize_market(market) -> dict:
    """Serialize a MarketData object to a dict."""
    return {
        "platform": market.platform,
        "platform_market_id": market.platform_market_id,
        "title": market.title,
        "url": market.url,
        "yes_price": str(market.yes_price) if market.yes_price else None,
        "no_price": str(market.no_price) if market.no_price else None,
        "yes_bid": str(market.yes_bid) if market.yes_bid else None,
        "yes_ask": str(market.yes_ask) if market.yes_ask else None,
    }


def build_opportunity_data(opp, opportunity_id: str, opp_type: str = "cross_platform") -> dict:
    """Build opportunity data dict from an ArbitrageResult or LogicalArbitrageResult.

    Args:
        opp: ArbitrageResult or LogicalArbitrageResult.
        opportunity_id: Database ID for the opportunity.
        opp_type: Type of opportunity.

    Returns:
        Dict with opportunity data.
    """
    if hasattr(opp, "market_a"):
        # Cross-platform ArbitrageResult
        data = {
            "type": "cross_platform",
            "opportunity_id": opportunity_id,
            "market_a": _serialize_market(opp.market_a),
            "market_b": _serialize_market(opp.market_b),
            "side_a": opp.side_a,
            "side_b": opp.side_b,
            "price_a": str(opp.price_a),
            "price_b": str(opp.price_b),
            "gross_spread": str(opp.gross_spread),
            "gross_profit": str(opp.gross_profit),
            "total_fees": str(opp.total_fees),
            "net_profit": str(opp.net_profit),
            "net_profit_pct": str(opp.net_profit_pct),
            "position_size": str(opp.position_size),
            "is_profitable": opp.is_profitable,
            "notes": opp.notes,
            "slippage_warning": getattr(opp, "slippage_warning", None),
            "effective_net_profit_pct": str(opp.effective_net_profit_pct) if getattr(opp, "effective_net_profit_pct", None) else None,
        }
    elif hasattr(opp, "relationship"):
        # LogicalArbitrageResult
        rel = opp.relationship
        data = {
            "type": "logical",
            "opportunity_id": opportunity_id,
            "market_a": _serialize_market(rel.market_a),
            "market_b": _serialize_market(rel.market_b),
            "relationship_type": rel.relationship_type.value,
            "expected_constraint": rel.expected_constraint,
            "violation_amount": str(opp.violation_amount),
            "net_profit_pct": str(opp.net_profit_pct),
            "estimated_fees": str(opp.estimated_fees),
            "recommended_action": getattr(opp, "recommended_action", None),
            "subtype_display": getattr(opp, "subtype_display", None),
        }
    else:
        data = {
            "type": opp_type,
            "opportunity_id": opportunity_id,
        }

    data["published_at"] = datetime.utcnow().isoformat()
    return data


def serialize_opportunity(opp, opportunity_id: str, opp_type: str = "cross_platform") -> str:
    """Serialize to JSON string (for Redis transport)."""
    return json.dumps(build_opportunity_data(opp, opportunity_id, opp_type), cls=DecimalEncoder)


def deserialize_opportunity(json_str: str) -> dict:
    """Deserialize from JSON string (for Redis transport)."""
    return json.loads(json_str)


# ---------------------------------------------------------------------------
# In-process queue (single-process mode, no Redis needed)
# ---------------------------------------------------------------------------

class InProcessPublisher:
    """Publishes opportunities via asyncio.Queue (same process)."""

    def __init__(self, queue: asyncio.Queue):
        self._queue = queue
        self.logger = logger.bind(component="InProcessPublisher")

    async def connect(self):
        pass

    async def close(self):
        pass

    async def publish(self, opp, opportunity_id: str, opp_type: str = "cross_platform") -> int:
        data = build_opportunity_data(opp, opportunity_id, opp_type)
        await self._queue.put(data)
        self.logger.debug("Published opportunity (in-process)", opportunity_id=opportunity_id)
        return 1


class InProcessSubscriber:
    """Subscribes to opportunities via asyncio.Queue (same process)."""

    def __init__(self, queue: asyncio.Queue):
        self._queue = queue
        self._redis = None  # Compatibility with status check in discord_bot
        self.logger = logger.bind(component="InProcessSubscriber")

    async def connect(self):
        self.logger.info("In-process subscriber ready")

    async def close(self):
        pass

    async def listen(self):
        while True:
            data = await self._queue.get()
            yield data


# ---------------------------------------------------------------------------
# Redis pub/sub (multi-process mode)
# ---------------------------------------------------------------------------

class OpportunityPublisher:
    """Publishes opportunities to Redis (used by scheduler/scanner)."""

    def __init__(self):
        self._redis = None
        self.logger = logger.bind(component="OpportunityPublisher")

    async def connect(self):
        import redis.asyncio as aioredis
        settings = get_settings()
        self._redis = aioredis.from_url(
            settings.redis_url,
            decode_responses=True,
        )
        self.logger.info("Connected to Redis", url=settings.redis_url)

    async def close(self):
        if self._redis:
            await self._redis.close()
            self._redis = None

    async def publish(self, opp, opportunity_id: str, opp_type: str = "cross_platform") -> int:
        if not self._redis:
            self.logger.warning("Redis not connected, skipping publish")
            return 0
        try:
            message = serialize_opportunity(opp, opportunity_id, opp_type)
            receivers = await self._redis.publish(CHANNEL_OPPORTUNITIES, message)
            self.logger.debug(
                "Published opportunity",
                opportunity_id=opportunity_id,
                receivers=receivers,
            )
            return receivers
        except Exception as e:
            self.logger.error("Failed to publish opportunity", error=str(e))
            return 0


class OpportunitySubscriber:
    """Subscribes to opportunities from Redis (used by Discord bot)."""

    def __init__(self):
        self._redis = None
        self._pubsub = None
        self.logger = logger.bind(component="OpportunitySubscriber")

    async def connect(self):
        import redis.asyncio as aioredis
        settings = get_settings()
        self._redis = aioredis.from_url(
            settings.redis_url,
            decode_responses=True,
        )
        self._pubsub = self._redis.pubsub()
        await self._pubsub.subscribe(CHANNEL_OPPORTUNITIES)
        self.logger.info("Subscribed to opportunities channel")

    async def close(self):
        if self._pubsub:
            await self._pubsub.unsubscribe(CHANNEL_OPPORTUNITIES)
            await self._pubsub.close()
            self._pubsub = None
        if self._redis:
            await self._redis.close()
            self._redis = None

    async def listen(self):
        if not self._pubsub:
            return
        async for message in self._pubsub.listen():
            if message["type"] != "message":
                continue
            try:
                data = deserialize_opportunity(message["data"])
                yield data
            except Exception as e:
                self.logger.error("Failed to deserialize opportunity", error=str(e))
