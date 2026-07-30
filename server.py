"""Context Vault — Phase 1 capture experiment.

A minimal MCP server that lets a coding agent log project decisions (with
reasoning and a verbatim transcript excerpt) as they happen. Decisions are
never deleted, only superseded — the append-only log is the version history.

Storage: one SQLite file per project under ~/.context-vault/projects/, so a
vault only ever surfaces the decisions made in the project it belongs to.
"""

import hashlib
import os
import re
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
        "Call get_project_brief at the start of a session to load context. "
        "The vault is scoped to the current project — it holds only this "
        "project's decisions, and other projects cannot see them."
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

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

VAULT_HOME = Path.home() / ".context-vault"
LEGACY_DB = VAULT_HOME / "vault.db"


def project_root() -> Path:
    """The directory that identifies the current project.

    CONTEXT_VAULT_PROJECT wins if set. Otherwise walk up from the working
    directory looking for a git root — `.git` is a directory in a normal
    clone but a file in a worktree or submodule, so test for existence, not
    for a directory. Falls back to the working directory itself.
    """
    override = os.environ.get("CONTEXT_VAULT_PROJECT")
    if override:
        return Path(override).expanduser().resolve()
    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if (candidate / ".git").exists():
            return candidate
    return cwd


def project_slug(root: Path) -> str:
    """A readable, collision-free filename stem for a project root.

    The name half is for humans browsing ~/.context-vault/projects/; the hash
    half is what actually keeps two same-named repos apart.
    """
    name = re.sub(r"[^a-z0-9._-]+", "-", root.name.lower()).strip("-.") or "project"
    digest = hashlib.sha256(str(root).encode()).hexdigest()[:6]
    return f"{name[:40]}-{digest}"


def db_path() -> Path:
    override = os.environ.get("CONTEXT_VAULT_DB")
    if override:
        return Path(override).expanduser()
    return VAULT_HOME / "projects" / f"{project_slug(project_root())}.db"


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    with conn:
        # Records which project a hashed filename belongs to. INSERT OR IGNORE
        # so the value reflects where the vault was created, not wherever it
        # happens to be read from later.
        conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('project_path', ?)",
            (str(project_root()),),
        )
    return conn


def empty_vault_note() -> str:
    """Hint shown on an empty vault when a pre-per-project vault exists."""
    if os.environ.get("CONTEXT_VAULT_DB") or not LEGACY_DB.exists():
        return ""
    return (
        f"\n\n(Vaults used to be shared across all projects. The old shared vault "
        f"is still at {LEGACY_DB} — set CONTEXT_VAULT_DB to that path to read it, "
        f"or copy it to {db_path()} to adopt its history as this project's.)"
    )


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
    """List this project's decision timeline, newest first.

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
        return f"No decisions logged yet for {project_root().name}.{empty_vault_note()}"
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
    """Catch me up: this project's active decisions with reasoning, oldest first.

    Call this at the start of a session to load the current state of the
    project. Superseded decisions are excluded; use list_decisions with
    include_superseded=true to see how the project got here.
    """
    root = project_root()
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM decisions WHERE superseded_by IS NULL ORDER BY id"
        ).fetchall()
        superseded = conn.execute(
            "SELECT COUNT(*) FROM decisions WHERE superseded_by IS NOT NULL"
        ).fetchone()[0]
    if not rows:
        return (
            f"No decisions logged yet — {root.name} has no recorded history."
            f"{empty_vault_note()}"
        )
    parts = [f"# Project brief — {root.name}\n", "Active decisions:\n"]
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
