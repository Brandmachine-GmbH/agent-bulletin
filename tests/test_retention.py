"""Retention: everything that points at a message must go when the message goes.

Message hashes always expired, but the feed, thread and search-index keys around them did
not, so a quiet project's leftovers stayed forever and the web UI kept listing projects
whose every message was long gone.
"""

DAY = 86400


def _ttl(store, *parts):
    return store.r.ttl(store._k(*parts))


def test_derived_keys_expire_with_the_message(store):
    store.post("p", "a", "hello", "world")
    for parts in (("p", "feed"), ("p", "thread", "1"), ("p", "idx", "hello")):
        assert 29 * DAY < _ttl(store, *parts) <= 30 * DAY, parts


def test_a_short_lived_post_never_shortens_a_key(store):
    store.post("p", "a", "long", "shared")
    store.post("p", "a", "brief", "shared", ttl_days=1)
    assert _ttl(store, "p", "feed") > 29 * DAY
    assert _ttl(store, "p", "idx", "shared") > 29 * DAY
    assert _ttl(store, "p", "idx", "brief") <= DAY


def test_a_project_whose_messages_expired_is_not_listed(store):
    store.post("gone", "a", "s", "b")
    store.post("live", "a", "s", "b")
    store.r.delete(store._k("gone", "msg", "1"))  # the message expired
    listed = {p["slug"]: p["message_count"] for p in store.list_projects()["projects"]}
    assert listed == {"live": 1}


def test_the_count_ignores_expired_messages(store):
    store.post("p", "a", "one", "b")
    store.post("p", "a", "two", "b")
    store.r.delete(store._k("p", "msg", "1"))
    assert store.list_projects()["projects"][0]["message_count"] == 1


def _legacy(store, p, msg_id, token):
    """Leftovers as old code wrote them: derived keys with no expiry."""
    store.r.zadd(store._k(p, "feed"), {msg_id: int(msg_id)})
    store.r.zadd(store._k(p, "thread", msg_id), {msg_id: int(msg_id)})
    store.r.sadd(store._k(p, "idx", token), msg_id)
    store.r.zadd("am:projects", {p: 1.0})


def test_prune_clears_leftovers_and_bounds_the_survivors(store):
    _legacy(store, "old", "1", "orphan")  # its message is long gone
    store.post("keep", "a", "alive", "b")
    store.r.persist(store._k("keep", "idx", "alive"))  # as if written before the TTL fix
    _legacy(store, "keep", "99", "alive")  # a dead id inside a live index set

    dry = store.prune(dry_run=True)
    assert dry["keys_deleted"] == 4 and store.r.exists(store._k("old", "idx", "orphan"))

    counts = store.prune()
    assert counts["registry_dropped"] == 1
    assert not store.r.keys("am:old:*")
    assert store.r.smembers(store._k("keep", "idx", "alive")) == {"1"}
    assert _ttl(store, "keep", "idx", "alive") > 29 * DAY
    assert store.r.zrange(store._k("keep", "feed"), 0, -1) == ["1"]
    assert [p["slug"] for p in store.list_projects()["projects"]] == ["keep"]
    assert store.prune()["ids_removed"] == 0  # rerunning is a no-op
