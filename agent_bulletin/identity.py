"""Resolving which project and which reader a process is acting as.

Shared by the CLI and the MCP server on purpose. When those two halves disagree about a
name, the failures are silent and confusing: the watcher stops recognising the agent's own
posts (so it notifies it about them), `--to-me` never matches, and acks are filed under a
name that has no relation to the reader's mailbox. One resolver, used by both.
"""

import os
import socket
import subprocess
from typing import Optional, Tuple


def _git_project() -> Optional[str]:
    """Repo name from git, or None outside a repo.

    Uses the *common* git dir rather than the toplevel so that linked worktrees resolve to
    the same project as the main checkout. Otherwise `myrepo` and `myrepo-feature-x` are two
    separate feeds and an agent in a worktree posts where nobody is watching.
    """
    try:
        out = subprocess.run(["git", "rev-parse", "--git-common-dir"],
                             capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    path = os.path.abspath(out.stdout.strip())
    # Only a common dir named `.git` sits inside the repo it belongs to. A submodule's is
    # `.git/modules/<name>` (whose parent would make every submodule "modules"), and a bare
    # repo's is the repo itself; for those the checkout's own top level is the right name.
    if os.path.basename(path) == ".git":
        return os.path.basename(os.path.dirname(path)) or None
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return None
    if top.returncode != 0 or not top.stdout.strip():
        return None
    return os.path.basename(top.stdout.strip()) or None


def resolve_project(explicit: Optional[str] = None) -> str:
    """Project from the argument, then $BULLETIN_PROJECT, then the git repo, then the cwd."""
    return (explicit or os.environ.get("BULLETIN_PROJECT") or _git_project()
            or os.path.basename(os.getcwd()) or "default")


def resolve_agent(explicit: Optional[str] = None) -> str:
    """Reader name from the argument, then $BULLETIN_AGENT, then host + directory.

    Stability matters more than beauty: `am:{p}:seen:{agent}` is keyed by the exact string,
    so a renamed agent is a brand new reader with an empty watermark. The host fallback is
    the weak link (macOS renames hosts on network collisions), which is why callers should
    warn when it is used: see `agent_source`.
    """
    explicit = explicit or os.environ.get("BULLETIN_AGENT")
    if explicit:
        return explicit
    host = socket.gethostname().split(".")[0]
    return f"{host}-{os.path.basename(os.getcwd()) or 'default'}"


def agent_source(explicit: Optional[str] = None) -> str:
    """Where the agent name came from: "argument", "env", or "fallback"."""
    if explicit:
        return "argument"
    if os.environ.get("BULLETIN_AGENT"):
        return "env"
    return "fallback"


def identity(project: Optional[str] = None,
             agent: Optional[str] = None) -> Tuple[str, str]:
    """Both names at once, for callers that need the pair."""
    return resolve_project(project), resolve_agent(agent)
