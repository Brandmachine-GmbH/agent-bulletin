"""Integration test against a real Redis.

Skips automatically if no Redis is reachable. Uses a throwaway project and cleans up
after itself, so it never touches real data.
"""

import os

import pytest
import redis

from agent_bulletin.store import RedisStore

PROJECT = "_pytest_integration"


@pytest.fixture
def real_store():
    url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    client = redis.Redis.from_url(url, decode_responses=True)
    try:
        client.ping()
    except Exception:
        pytest.skip("no live Redis reachable at REDIS_URL")
    s = RedisStore(client=client)
    s.flush_project(PROJECT)
    yield s
    s.flush_project(PROJECT)  # always clean up


def test_end_to_end_against_real_redis(real_store):
    real_store.post(PROJECT, "backend", "Deploy", "the deploy is frozen")
    real_store.post(PROJECT, "backend", "API", "renamed orders endpoint")

    first = real_store.check_mailbox(PROJECT, "reader")
    assert first["count"] == 2
    assert real_store.check_mailbox(PROJECT, "reader")["count"] == 0  # watermark advanced

    assert real_store.search(PROJECT, "deploy")["count"] == 1
    assert real_store.r.ttl(real_store._k(PROJECT, "msg", "1")) > 0
