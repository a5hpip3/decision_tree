"""Context Vault — Phase 1 capture experiment.

A minimal MCP server that lets a coding agent log project decisions (with
reasoning and a verbatim transcript excerpt) as they happen. Decisions are
never deleted, only superseded — the append-only log is the version history.

Storage: single SQLite file at ~/.context-vault/vault.db, overridable with
the CONTEXT_VAULT_DB environment variable.
"""

import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as Server

mcp = Server(
    name="context-vault",
    instructions=(
        "Log meaningful project decisions (architecture, stack, approach "
        "ruled out, direction change) with log_decision as they happen. "
        "Use supersede_decision when a decision reverses an earlier one. "
        "Call get_project_brief at the start of a session to load context."
    ),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    summary       TEXT NOT NULL,
    reasoning     TEXT NOT NULL,
    excerpt       TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    supersedes    INTEGER REFERENCES decisions(id),
    superseded_by INTEGER REFERENCES decisions(id)
);
"""


def db_path() -> Path:
    override = os.environ.get("CONTEXT_VAULT_DB")
    if override:
        return Path(override)
    return Path.home() / ".context-vault" / "vault.db"


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def format_decision(row: sqlite3.Row, full: bool = False) -> str:
    status = f"SUPERSEDED by #{row['superseded_by']}" if row["superseded_by"] else "active"
    lines = [f"#{row['id']} [{status}] {row['created_at']}"]
    lines.append(f"  Decision: {row['summary']}")
    if full:
        lines.append(f"  Reasoning: {row['reasoning']}")
        if row["supersedes"]:
            lines.append(f"  Supersedes: #{row['supersedes']}")
        lines.append(f"  Citation excerpt:\n    {row['excerpt']}")
    return "\n".join(lines)


@mcp.tool()
def log_decision(summary: str, reasoning: str, excerpt: str) -> str:
    """Log a meaningful project decision at the moment it is made.

    Use for architecture choices, stack/library picks, approaches ruled out,
    and direction changes — not routine actions or debugging steps. If this
    decision reverses an earlier one, use supersede_decision instead.

    Args:
        summary: One-sentence statement of the decision.
        reasoning: Why this was decided; alternatives considered and why
            they were rejected.
        excerpt: Verbatim, self-contained excerpt of the conversation where
            the decision happened — this is the citation.
    """
    with closing(connect()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO decisions (summary, reasoning, excerpt, created_at) VALUES (?, ?, ?, ?)",
            (summary, reasoning, excerpt, now()),
        )
    return f"Logged decision #{cur.lastrowid}: {summary}"


@mcp.tool()
def supersede_decision(decision_id: int, summary: str, reasoning: str, excerpt: str) -> str:
    """Record a decision that reverses or replaces an earlier one.

    The old decision is kept in the timeline and marked superseded — nothing
    is deleted. The new decision links back to what it replaced.

    Args:
        decision_id: The id of the decision being reversed or replaced.
        summary: One-sentence statement of the new decision.
        reasoning: Why the earlier decision no longer holds.
        excerpt: Verbatim excerpt of the conversation where the reversal
            happened.
    """
    with closing(connect()) as conn, conn:
        old = conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
        if old is None:
            return f"Error: no decision #{decision_id}. Use list_decisions to find the right id."
        if old["superseded_by"]:
            return (
                f"Error: decision #{decision_id} was already superseded by "
                f"#{old['superseded_by']}. Supersede that one instead."
            )
        cur = conn.execute(
            "INSERT INTO decisions (summary, reasoning, excerpt, created_at, supersedes)"
            " VALUES (?, ?, ?, ?, ?)",
            (summary, reasoning, excerpt, now(), decision_id),
        )
        conn.execute(
            "UPDATE decisions SET superseded_by = ? WHERE id = ?",
            (cur.lastrowid, decision_id),
        )
    return (
        f"Logged decision #{cur.lastrowid}, superseding #{decision_id} "
        f"({old['summary']})"
    )


@mcp.tool()
def list_decisions(include_superseded: bool = False) -> str:
    """List the decision timeline, newest first.

    Args:
        include_superseded: Include superseded decisions to see the full
            history, not just the current state.
    """
    query = "SELECT * FROM decisions"
    if not include_superseded:
        query += " WHERE superseded_by IS NULL"
    query += " ORDER BY id DESC"
    with closing(connect()) as conn:
        rows = conn.execute(query).fetchall()
    if not rows:
        return "No decisions logged yet."
    return "\n".join(format_decision(r) for r in rows)


@mcp.tool()
def get_decision(decision_id: int) -> str:
    """Get the full record of one decision, including its citation excerpt.

    Args:
        decision_id: The id of the decision to fetch.
    """
    with closing(connect()) as conn:
        row = conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
    if row is None:
        return f"Error: no decision #{decision_id}. Use list_decisions to find the right id."
    return format_decision(row, full=True)


@mcp.tool()
def get_project_brief() -> str:
    """Catch me up: all active decisions with reasoning, oldest first.

    Call this at the start of a session to load the current state of the
    project. Superseded decisions are excluded; use list_decisions with
    include_superseded=true to see how the project got here.
    """
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM decisions WHERE superseded_by IS NULL ORDER BY id"
        ).fetchall()
        superseded = conn.execute(
            "SELECT COUNT(*) FROM decisions WHERE superseded_by IS NOT NULL"
        ).fetchone()[0]
    if not rows:
        return "No decisions logged yet — this project has no recorded history."
    parts = ["# Project brief — active decisions\n"]
    for row in rows:
        parts.append(f"- #{row['id']} ({row['created_at']}): {row['summary']}")
        parts.append(f"  Why: {row['reasoning']}")
    if superseded:
        parts.append(
            f"\n({superseded} superseded decision(s) in history — "
            "list_decisions with include_superseded=true to see them.)"
        )
    return "\n".join(parts)


if __name__ == "__main__":
    mcp.run()
