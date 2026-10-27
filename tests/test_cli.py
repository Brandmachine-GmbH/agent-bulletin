"""Tests for the watch loop itself.

The store-level invariants were already covered; every failure found in review lived in
the loop instead (a cursor that could never advance again, one bad project silencing the
rest, a reconnect re-delivering everything). None of that is reachable through `since()`
alone, so these drive `_drain` and `cmd_watch` directly.
"""

import argparse

import pytest
import redis

from agent_bulletin import cli
from agent_bulletin.store import project_slug


@pytest.fixture
def emitted(monkeypatch):
    """Capture what the watcher would send to the session."""
    lines = []
    monkeypatch.setattr(cli, "_emit", lines.append)
    monkeypatch.setattr(cli, "_warn", lambda m: None)
    return lines


def _handler(sink):
    return lambda messages: sink.extend(m["subject"] for m in messages)


# ------------------------------------------------------------------- draining
def test_drain_delivers_and_advances(store, emitted):
    store.post("p", "a", "one", "b")
    store.post("p", "a", "two", "b")
    seen, cursors = [], {"p": 0}
    cli._drain(store, "p", cursors, _handler(seen))
    assert seen == ["one", "two"]
    assert cursors["p"] == 2
    cli._drain(store, "p", cursors, _handler(seen))
    assert seen == ["one", "two"]  # nothing re-delivered


def test_a_failure_mid_drain_redelivers_at_most_one_page(store, monkeypatch, emitted):
    """The cursor is committed per page, so a drop after page 1 does not replay page 1."""
    for i in range(5):
        store.post("p", "a", f"s{i}", "b")
    monkeypatch.setattr(cli, "_PAGE", 2)
    calls = {"n": 0}
    real_since = store.since

    def flaky(project, after_id=0, limit=cli._PAGE):
        calls["n"] += 1
        if calls["n"] == 3:  # fails while fetching the third page
            raise redis.ConnectionError("dropped")
        return real_since(project, after_id, limit)

    monkeypatch.setattr(store, "since", flaky)
    seen, cursors = [], {"p": 0}
    with pytest.raises(redis.ConnectionError):
        cli._drain(store, "p", cursors, _handler(seen))
    assert seen == ["s0", "s1", "s2", "s3"]
    assert cursors["p"] == 4  # progress kept: the retry resumes, it does not restart

    monkeypatch.setattr(store, "since", real_since)
    cli._drain(store, "p", cursors, _handler(seen))
    assert seen == ["s0", "s1", "s2", "s3", "s4"]  # exactly one copy of each


def test_a_reset_id_sequence_does_not_deafen_the_watch(store, emitted):
    """After a flush the new ids start below the cursor, and `newer than` would otherwise
    never be satisfied again for the rest of the session."""
    for i in range(3):
        store.post("p", "a", f"old{i}", "b")
    cursors = {"p": 3}
    store.flush_project("p")
    store.post("p", "a", "after the flush", "b")  # gets id 1 again
    seen = []
    cli._drain(store, "p", cursors, _handler(seen))
    assert seen == ["after the flush"]


def test_a_flushed_project_restarts_the_cursor(store, emitted):
    # No id counter at all means the project was flushed: its next post is #1 again, and a
    # cursor left at 7 would hide the first seven posts of the new sequence.
    cursors = {"nothing": 7}
    cli._drain(store, "nothing", cursors, _handler([]))
    assert cursors["nothing"] == 0
    seen = []
    store.post("nothing", "a", "first again", "b")
    cli._drain(store, "nothing", cursors, _handler(seen))
    assert seen == ["first again"]


def test_an_expired_newest_post_is_not_a_reset(store, emitted):
    # The newest post can expire before older ones (ttl_days is per message) and be removed
    # from the feed lazily. That once read as an id reset and replayed the whole feed.
    store.post("p", "a", "old", "b")
    store.post("p", "a", "short-lived", "b", ttl_days=1)
    cursors = {"p": 2}
    store.r.delete(store._k("p", "msg", "2"))
    store.latest("p")  # any reader lazily drops the dangling id
    seen = []
    cli._drain(store, "p", cursors, _handler(seen))
    assert seen == [] and cursors["p"] == 2


# ------------------------------------------------------------------ coalescing
def test_a_burst_is_summarized_rather_than_flooding_the_session(emitted):
    """Every line is a notification and the host stops a monitor that sends too many, so a
    batch of changes must not be able to kill the channel announcing it."""
    msgs = [{"id": str(i), "from": "a", "to": [], "subject": f"s{i}", "body": "b",
             "importance": "normal"} for i in range(1, 21)]
    msgs[4]["importance"] = "halt"
    cli._emit_batch(msgs, project="p", agent="me")
    assert len(emitted) == 2                      # the halt in full, then one summary
    assert "HALT" in emitted[0]
    assert "19 further new messages" in emitted[1]
    assert "check_mailbox" in emitted[1]


def test_a_small_batch_is_not_coalesced(emitted):
    msgs = [{"id": str(i), "from": "a", "to": [], "subject": f"s{i}", "body": "b",
             "importance": "normal"} for i in range(3)]
    cli._emit_batch(msgs, project="p", agent="me")
    assert len(emitted) == 3


# --------------------------------------------------------------- arming cursor
def test_arming_catches_up_from_this_readers_own_watermark(store):
    """Seeding at the feed tail would mean anything posted while no session was open is
    never notified at all, which is the normal case for one session at a time."""
    store.post("p", "a", "read already", "b")
    store.check_mailbox("p", "me", bootstrap="all")      # watermark now 1
    store.post("p", "a", "posted while away", "b")
    assert cli._initial_cursor(store, "p", "me", None) == 1
    page = store.since("p", 1)
    assert [m["subject"] for m in page["messages"]] == ["posted while away"]


def test_arming_is_capped_so_a_stale_reader_is_not_flooded(store, monkeypatch):
    monkeypatch.setattr(cli, "_MAX_CATCHUP", 2)
    store.post("p", "a", "s0", "b")
    store.check_mailbox("p", "me", bootstrap="all")
    for i in range(1, 21):
        store.post("p", "a", f"s{i}", "b")
    assert store.since("p", cli._initial_cursor(store, "p", "me", None))["count"] == 2


def test_a_reader_that_has_never_checked_starts_clean(store):
    for i in range(5):
        store.post("p", "a", f"s{i}", "b")
    assert cli._initial_cursor(store, "p", "stranger", None) == 5


def test_explicit_backlog_still_wins(store):
    for i in range(5):
        store.post("p", "a", f"s{i}", "b")
    store.check_mailbox("p", "me", bootstrap="all")
    assert cli._initial_cursor(store, "p", "me", 0) == 5   # 0 means "from now on"
    assert cli._initial_cursor(store, "p", "me", 2) == 3


# -------------------------------------------------------------------- parsing
def test_projects_that_slug_alike_are_watched_once():
    """Otherwise each spelling carries its own cursor and delivers its own copy."""
    args = cli.build_parser().parse_args(
        ["watch", "--project", "my_project", "--project", "My Project",
         "--project", "my-project"])
    resolved = list(dict.fromkeys(project_slug(p) for p in args.project))
    assert resolved == ["my-project"]


@pytest.mark.parametrize("given,expected", [(0, cli._MIN_INTERVAL), (-1, cli._MIN_INTERVAL),
                                            (5, 5)])
def test_the_poll_interval_has_a_floor(given, expected):
    """At 0 the loop stops polling and starts spinning (80% of a core); at a negative value
    redis-py raises a bare ValueError out of settimeout."""
    assert max(cli._MIN_INTERVAL, float(given)) == expected


def test_broken_pipe_exits_quietly(monkeypatch):
    """The host closing stdout is a normal end, not a crash to dump a traceback for."""
    monkeypatch.setattr(cli, "build_parser", lambda: _parser_raising(BrokenPipeError))
    assert cli.main([]) == 0
    monkeypatch.setattr(cli, "build_parser", lambda: _parser_raising(KeyboardInterrupt))
    assert cli.main([]) == 0


def _parser_raising(exc):
    parser = argparse.ArgumentParser()

    def boom(_argv=None):
        ns = argparse.Namespace()
        ns.func = lambda *_: (_ for _ in ()).throw(exc())
        return ns

    parser.parse_args = boom
    return parser


def test_one_bad_project_does_not_silence_the_others(store, monkeypatch, emitted):
    """A permanently broken feed must not abort every other project's drain, every tick,
    for the life of the session."""
    store.post("bad", "a", "unreachable", "b")
    store.post("good", "a", "delivered", "b")
    real_since = store.since

    def selective(project, after_id=0, limit=200):
        if project == "bad":
            raise redis.ResponseError("WRONGTYPE")
        return real_since(project, after_id, limit)

    monkeypatch.setattr(store, "since", selective)
    seen, cursors = [], {"bad": 0, "good": 0}
    failed = cli._drain_all(store, ["bad", "good"], cursors, lambda p: _handler(seen))
    assert failed == ["bad"]
    assert seen == ["delivered"]          # the healthy project still got through
    assert cursors["good"] == 1


def test_the_first_project_cannot_starve_the_rest(store, monkeypatch, emitted):
    """Ordering matters: the broken feed is drained first here."""
    store.post("zzz", "a", "still delivered", "b")
    real_since = store.since
    monkeypatch.setattr(store, "since", lambda project, after_id=0, limit=200: (
        (_ for _ in ()).throw(redis.ResponseError("boom")) if project == "aaa"
        else real_since(project, after_id, limit)))
    seen = []
    cli._drain_all(store, ["aaa", "zzz"], {"aaa": 0, "zzz": 0}, lambda p: _handler(seen))
    assert seen == ["still delivered"]


def test_a_legacy_importance_word_still_posts(store, monkeypatch, capsys):
    # argparse `choices` used to reject these outright, losing the post.
    monkeypatch.setattr(cli, "RedisStore", lambda: store)
    args = cli.build_parser().parse_args(
        ["post", "--project", "p", "--from", "a", "--subject", "s", "--body", "b",
         "--importance", "urgent"])
    assert args.func(args) == 0
    out = capsys.readouterr().out
    assert '"importance": "halt"' in out and "importance_note" in out


def test_a_failed_presence_read_does_not_fail_a_committed_post(store, monkeypatch, capsys):
    import redis as redis_mod
    monkeypatch.setattr(cli, "RedisStore", lambda: store)
    monkeypatch.setattr(store, "watchers", lambda p: (_ for _ in ()).throw(
        redis_mod.ConnectionError("blip")))
    args = cli.build_parser().parse_args(
        ["post", "--project", "p", "--from", "a", "--subject", "s", "--body", "b"])
    assert args.func(args) == 0
    assert "could not be read" in capsys.readouterr().out
    assert store.latest("p")["count"] == 1
