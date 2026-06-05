"""Shared pytest fixtures for agent-bulletin."""

import fakeredis
import pytest

from agent_bulletin.store import RedisStore


@pytest.fixture
def store():
    """A fresh RedisStore backed by an in-memory fake Redis (no server needed).

    RedisStore accepts an injected client, so unit tests run fully in-process and
    isolated - each test gets its own empty fake.
    """
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    return RedisStore(client=client)
