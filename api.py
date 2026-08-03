"""Read-only JSON API over the hosted vaults.

The web front-end runs as a separate Railway service, and a Railway volume
mounts to exactly one service — so the UI cannot read the SQLite files
directly. It reads them through here.

Read-only on purpose: writes stay on the MCP tools, where the docstrings that
shape how agents log live. This is a projection for display, nothing more.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

import server

# Hosted vaults only. Local ones are named <slug>-<pathhash>.db and belong to
# whoever's laptop they're on; they are never served.
REMOTE_DIR = "remote"


def remote_dir() -> Path:
    return server.VAULT_HOME / REMOTE_DIR


def project_names() -> list[str]:
    """Names of hosted vaults, from the filenames on the volume."""
    directory = remote_dir()
    if not directory.is_dir():
        return []
    names = []
    for path in directory.glob("*.db"):
        name = path.stem
        # A file whose name isn't a valid project can't have been created by
        # this server; skip rather than serve something unroutable.
        if server.PROJECT_NAME.match(name):
            names.append(name)
    return sorted(names)


def exists(name: str) -> bool:
    return bool(server.PROJECT_NAME.match(name)) and name in set(project_names())


@contextmanager
def open_project(name: str):
    """Open one project's vault, with migrations applied.

    Goes through server.connect() via the contextvar rather than opening the
    file directly, so a vault read here is upgraded exactly like one written
    through a tool.
    """
    token = server.REMOTE_PROJECT.set(name)
    try:
        conn = server.connect()
        try:
            yield conn
        finally:
            conn.close()
    finally:
        server.REMOTE_PROJECT.reset(token)


def status_of(row: sqlite3.Row) -> str:
    if row["retired_at"]:
        return "retired"
    if row["superseded_by"]:
        return "superseded"
    return "active"


def decision_json(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "summary": row["summary"],
        "reasoning": row["reasoning"],
        "excerpt": row["excerpt"],
        "created_at": row["created_at"],
        "status": status_of(row),
        "derives_from": row["derives_from"],
        "supersedes": row["supersedes"],
        "superseded_by": row["superseded_by"],
        "retired_at": row["retired_at"],
        "retired_reason": row["retired_reason"],
        "cluster": row["cluster"],
        "source": row["source"],
        "ref": row["ref"],
        "author": row["author"],
    }


def summarise(name: str) -> dict:
    """Counts and labels for the project list, without loading every record."""
    with open_project(name) as conn:
        counts = {"active": 0, "superseded": 0, "retired": 0}
        clusters: set[str] = set()
        last = None
        for row in conn.execute("SELECT * FROM decisions"):
            status = status_of(row)
            counts[status] += 1
            if row["cluster"]:
                clusters.add(row["cluster"])
            # A decision filed in error is not activity: counting it would make
            # a project look freshly worked on because something was cleaned up.
            if status != "retired" and (last is None or row["created_at"] > last):
                last = row["created_at"]
    return {
        "name": name,
        "decisions": sum(counts.values()),
        "active": counts["active"],
        "superseded": counts["superseded"],
        "retired": counts["retired"],
        "clusters": sorted(clusters),
        "last_activity": last,
    }


def projects_payload() -> dict:
    return {"projects": [summarise(name) for name in project_names()]}


def decisions_payload(name: str, include_retired: bool = False) -> dict:
    """One project's decisions, plus the edges the graph draws.

    Edges are computed here rather than in the browser so the front-end never
    has to know that a reversal and a derivation are stored differently.
    """
    with open_project(name) as conn:
        rows = list(conn.execute("SELECT * FROM decisions ORDER BY id"))

    decisions = [decision_json(r) for r in rows if include_retired or not r["retired_at"]]
    present = {d["id"] for d in decisions}

    edges = []
    for decision in decisions:
        for key, kind in (("derives_from", "derives"), ("supersedes", "supersedes")):
            target = decision[key]
            # A retired parent can be filtered out from under an edge; drop the
            # edge rather than pointing at a node the client never received.
            if target and target in present:
                edges.append({"from": decision["id"], "to": target, "kind": kind})

    clusters = sorted({d["cluster"] for d in decisions if d["cluster"]})
    sources = sorted({d["source"] for d in decisions if d["source"]})
    return {
        "project": name,
        "decisions": decisions,
        "edges": edges,
        "clusters": clusters,
        "sources": sources,
    }
