"""agent-bulletin - a tiny Redis-backed per-project news feed for agents."""

from .store import (
    ACK_FROM_IMPORTANCE,
    IMPORTANCE_LEVELS,
    RedisStore,
    importance_at_least,
    project_slug,
    normalize_importance,
    requires_ack,
)

__version__ = "0.4.0"
__all__ = [
    "RedisStore",
    "IMPORTANCE_LEVELS",
    "ACK_FROM_IMPORTANCE",
    "normalize_importance",
    "importance_at_least",
    "requires_ack",
    "project_slug",
]
