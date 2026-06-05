"""agent-mail - a tiny Redis-backed per-project news feed for agents."""

from .store import RedisStore

__version__ = "0.1.0"
__all__ = ["RedisStore"]
