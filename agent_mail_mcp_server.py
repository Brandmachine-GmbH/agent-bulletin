"""agent-mail MCP Server.

A tiny Redis-backed per-project news feed for AI agents. Agents POST short messages
to a project, and CHECK a project's mailbox to catch up on what's new since they last
looked. Organization is by project, not by recipient: anyone reading project X sees the
whole stream for X. `from`/`to` are metadata, not routing.

See motivation.md and architecture.md for the design.
"""

from typing import Optional

from fastmcp import FastMCP

from agent_mail.store import DEFAULT_TTL_DAYS, RedisStore

mcp = FastMCP("agent-mail")

_store: Optional[RedisStore] = None


def get_store() -> RedisStore:
    """Get or create the Redis-backed store, with a clear error if Redis is unreachable."""
    global _store
    if _store is not None:
        return _store
    try:
        store = RedisStore()
        store.ping()
    except Exception as e:
        raise RuntimeError(
            f"Could not connect to Redis: {e}. "
            "Make sure redis-server is running and REDIS_URL is correct "
            "(default: redis://localhost:6379/0)."
        )
    _store = store
    return _store


# ============================================================================
# MCP Tools
# ============================================================================

@mcp.tool()
def post_message(
    project: str,
    from_agent: str,
    subject: str,
    body: str,
    to: Optional[list[str]] = None,
    thread_id: Optional[str] = None,
    importance: str = "normal",
    ttl_days: int = DEFAULT_TTL_DAYS,
) -> dict:
    """Post a message to a project's news feed.

    This is how an agent announces something to everyone working on a project
    (e.g. "renamed the /orders endpoint", "deploy is frozen until 3pm").

    Args:
        project: The project / channel name (free-form, e.g. "frontend"). Agents that
                 want to talk to each other must agree on the same project string.
        from_agent: Who is posting (free-form name, e.g. "backend-agent"). Metadata only.
        subject: Short headline for the message.
        body: The message content (markdown is fine).
        to: Optional list of agent names being mentioned/targeted. Metadata only - it does
            NOT restrict who can read the message; everyone reading the project sees it.
        thread_id: Optional id to attach this message to an existing thread. Omit to start
                   a new thread (the thread id becomes this message's id).
        importance: "low" | "normal" | "high" (free-form; default "normal").
        ttl_days: Days until the message auto-expires (default 30).

    Returns:
        {"success": bool, "message_id": str, "thread_id": str, "project": str, "error": str}
    """
    try:
        result = get_store().post(
            project, from_agent, subject, body,
            to=to, thread_id=thread_id, importance=importance, ttl_days=ttl_days,
        )
        return {"success": True, **result}
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
def check_mailbox(
    project: str,
    agent: str,
    limit: int = 20,
    mark_seen: bool = True,
) -> dict:
    """Check a project's mailbox: get the messages this agent has not seen yet.

    This is the primary verb. It returns the oldest-unseen messages first (so you catch
    up in order) and then advances this agent's "last seen" marker. A second call right
    after returns nothing new. If there is a backlog larger than `limit`, repeated calls
    drain it page by page.

    Args:
        project: The project / channel to check.
        agent: The reader's name. Each agent has its own independent "seen" position per
               project, so different agents catch up independently.
        limit: Max messages to return this call (default 20).
        mark_seen: If True (default), advance this agent's seen-marker to the last message
                   returned. Set False to peek without marking anything seen.

    Returns:
        {"success": bool, "count": int, "messages": [summary...], "error": str}
        Each summary has: id, thread_id, from, to, subject, importance, created_at,
        created_at_iso, snippet. Use read_message for the full body.
    """
    try:
        result = get_store().check_mailbox(project, agent, limit=limit, mark_seen=mark_seen)
        return {"success": True, **result}
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
def read_message(project: str, message_id: str) -> dict:
    """Read one full message (including its complete body) by id.

    Args:
        project: The project the message belongs to.
        message_id: The message id (as returned by post_message / check_mailbox / search).

    Returns:
        {"success": bool, "message": {...full message...}, "error": str}
    """
    try:
        msg = get_store().read(project, message_id)
        if msg is None:
            return {
                "success": False,
                "error": f"Message '{message_id}' not found in project '{project}' "
                         "(it may have expired).",
            }
        return {"success": True, "message": msg}
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
def search_messages(project: str, query: str, limit: int = 20) -> dict:
    """Full-text search within a project's messages.

    Matches messages whose subject or body contain ALL of the query's words (token AND).
    Results are newest-first.

    Args:
        project: The project to search.
        query: Words to search for (e.g. "deploy orders"). Case-insensitive.
        limit: Max results (default 20).

    Returns:
        {"success": bool, "count": int, "messages": [summary...], "error": str}
    """
    try:
        result = get_store().search(project, query, limit=limit)
        return {"success": True, **result}
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
def get_thread(project: str, thread_id: str) -> dict:
    """Get every message in a thread, in chronological order (oldest first).

    Args:
        project: The project the thread belongs to.
        thread_id: The thread id (a message's thread_id, as seen in summaries).

    Returns:
        {"success": bool, "count": int, "messages": [...full messages...], "error": str}
    """
    try:
        result = get_store().get_thread(project, thread_id)
        return {"success": True, **result}
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
def list_projects(limit: int = 50) -> dict:
    """List projects that have activity, most-recently-active first.

    Useful for discovering where there is news without knowing the project name in advance.

    Args:
        limit: Max projects to return (default 50).

    Returns:
        {"success": bool, "count": int,
         "projects": [{"slug", "last_activity", "last_activity_iso", "message_count"}...],
         "error": str}
    """
    try:
        result = get_store().list_projects(limit=limit)
        return {"success": True, **result}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ============================================================================
# Server Entry Point
# ============================================================================

if __name__ == "__main__":
    # FastMCP handles stdio transport automatically
    mcp.run()
