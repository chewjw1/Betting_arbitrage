"""Notification systems.

Lazy imports to avoid requiring discord.py when only using Redis bridge.
"""

from src.notifications.redis_bridge import OpportunityPublisher, OpportunitySubscriber


def __getattr__(name):
    if name == "ArbitrageBot":
        from src.notifications.discord_bot import ArbitrageBot
        return ArbitrageBot
    if name == "format_opportunity_embed":
        from src.notifications.formatters import format_opportunity_embed
        return format_opportunity_embed
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ArbitrageBot",
    "OpportunityPublisher",
    "OpportunitySubscriber",
    "format_opportunity_embed",
]
