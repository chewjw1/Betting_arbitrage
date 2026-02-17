"""Redis pub/sub bridge for cross-process notification delivery.

The scheduler (collector) and Discord bot run as separate processes.
This module provides the communication layer between them:

- Scheduler publishes opportunities to Redis channel "arb:opportunities"
- Discord bot subscribes and sends Discord messages
- Discord bot publishes user reactions to Redis channel "arb:reactions"
- Scheduler (or bot) updates the database with user responses

Uses JSON serialization over Redis pub/sub for simplicity and reliability.
"""

import json
from datetime import datetime
from decimal import Decimal
from typing import Optional

import redis.asyncio as aioredis
import structlog

from src.config import get_settings

logger = structlog.get_logger()

# Redis channels
CHANNEL_OPPORTUNITIES = "arb:opportunities"
CHANNEL_REACTIONS = "arb:reactions"


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


def serialize_opportunity(opp, opportunity_id: str, opp_type: str = "cross_platform") -> str:
    """Serialize an arbitrage opportunity for Redis transport.

    Args:
        opp: ArbitrageResult or LogicalArbitrageResult.
        opportunity_id: Database ID for the opportunity.
        opp_type: Type of opportunity.

    Returns:
        JSON string.
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
    return json.dumps(data, cls=DecimalEncoder)


def deserialize_opportunity(json_str: str) -> dict:
    """Deserialize an opportunity from Redis.

    Args:
        json_str: JSON string from Redis.

    Returns:
        Dict with opportunity data.
    """
    return json.loads(json_str)


class OpportunityPublisher:
    """Publishes opportunities to Redis (used by scheduler/scanner).

    Usage:
        publisher = OpportunityPublisher()
        await publisher.connect()
        await publisher.publish(opp, opportunity_id)
        await publisher.close()
    """

    def __init__(self):
        self._redis: Optional[aioredis.Redis] = None
        self.logger = logger.bind(component="OpportunityPublisher")

    async def connect(self):
        """Connect to Redis."""
        settings = get_settings()
        self._redis = aioredis.from_url(
            settings.redis_url,
            decode_responses=True,
        )
        self.logger.info("Connected to Redis", url=settings.redis_url)

    async def close(self):
        """Close Redis connection."""
        if self._redis:
            await self._redis.close()
            self._redis = None

    async def publish(self, opp, opportunity_id: str, opp_type: str = "cross_platform") -> int:
        """Publish an opportunity to Redis.

        Args:
            opp: ArbitrageResult or LogicalArbitrageResult.
            opportunity_id: Database ID.
            opp_type: Opportunity type.

        Returns:
            Number of subscribers that received the message.
        """
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
    """Subscribes to opportunities from Redis (used by Discord bot).

    Usage:
        subscriber = OpportunitySubscriber()
        await subscriber.connect()
        async for opp_data in subscriber.listen():
            # Send Discord notification
            ...
        await subscriber.close()
    """

    def __init__(self):
        self._redis: Optional[aioredis.Redis] = None
        self._pubsub: Optional[aioredis.client.PubSub] = None
        self.logger = logger.bind(component="OpportunitySubscriber")

    async def connect(self):
        """Connect to Redis and subscribe to opportunity channel."""
        settings = get_settings()
        self._redis = aioredis.from_url(
            settings.redis_url,
            decode_responses=True,
        )
        self._pubsub = self._redis.pubsub()
        await self._pubsub.subscribe(CHANNEL_OPPORTUNITIES)
        self.logger.info("Subscribed to opportunities channel")

    async def close(self):
        """Close Redis connection."""
        if self._pubsub:
            await self._pubsub.unsubscribe(CHANNEL_OPPORTUNITIES)
            await self._pubsub.close()
            self._pubsub = None
        if self._redis:
            await self._redis.close()
            self._redis = None

    async def listen(self):
        """Async generator that yields opportunity data dicts.

        Yields:
            Dict with opportunity data from Redis.
        """
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


class ReactionPublisher:
    """Publishes user reactions from Discord bot to Redis.

    This allows any process to handle reaction updates (though the bot
    handles it directly via database writes for simplicity).
    """

    def __init__(self, redis_client: aioredis.Redis):
        self._redis = redis_client
        self.logger = logger.bind(component="ReactionPublisher")

    async def publish_reaction(self, opportunity_id: str, response: str, user_id: int):
        """Publish a user reaction to Redis.

        Args:
            opportunity_id: Database opportunity ID.
            response: "acted" or "passed".
            user_id: Discord user ID.
        """
        data = json.dumps({
            "opportunity_id": opportunity_id,
            "response": response,
            "user_id": user_id,
            "timestamp": datetime.utcnow().isoformat(),
        })
        await self._redis.publish(CHANNEL_REACTIONS, data)
