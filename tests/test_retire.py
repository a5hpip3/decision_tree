"""retire_decision — for a decision filed against the wrong project.

Distinct from supersede: a superseded decision is still part of this project's
story, a retired one never belonged here. Neither deletes anything.
"""

from __future__ import annotations

import sqlite3

import server
from conftest import unwrap

log_decision = unwrap(server.log_decision)
supersede_decision = unwrap(server.supersede_decision)
retire_decision = unwrap(server.retire_decision)
list_decisions = unwrap(server.list_decisions)
get_decision = unwrap(server.get_decision)
get_project_brief = unwrap(server.get_project_brief)


def log(summary: str) -> int:
    result = log_decision(summary, "because", "excerpt")
    return int(result.split("#")[1].split(":")[0])


class TestRetire:
    def test_leaves_the_brief_but_stays_in_history(self, vault):
        vault.enter(vault.project())
        keep = log("Belongs here")
        wrong = log("Belongs to another project")

        assert "another project" in get_project_brief()
        retire_decision(wrong, "misfiled; moved to other-project")

        brief = get_project_brief()
        assert "Belongs here" in brief
        assert "another project" not in brief
        assert "1 retired (filed in error)" in brief

        assert "another project" not in list_decisions()
        full = list_decisions(include_superseded=True)
        assert "another project" in full
        assert "RETIRED" in full

    def test_reason_is_recorded_and_shown(self, vault):
        vault.enter(vault.project())
        did = log("Misfiled decision")
        retire_decision(did, "moved to rapid_manufacturing")
        assert "moved to rapid_manufacturing" in get_decision(did)
        assert "moved to rapid_manufacturing" in list_decisions(include_superseded=True)

    def test_nothing_is_deleted(self, vault):
        vault.enter(vault.project())
        did = log_decision("Misfiled", "REASONING_KEPT", "EXCERPT_KEPT")
        did = int(did.split("#")[1].split(":")[0])
        retire_decision(did, "wrong project")
        record = get_decision(did)
        assert "REASONING_KEPT" in record
        assert "EXCERPT_KEPT" in record

    def test_rejects_unknown_id(self, vault):
        vault.enter(vault.project())
        assert retire_decision(999, "x").startswith("Error: no decision #999")

    def test_rejects_double_retire(self, vault):
        vault.enter(vault.project())
        did = log("Misfiled")
        retire_decision(did, "first reason")
        second = retire_decision(did, "second reason")
        assert second.startswith("Error:")
        assert "already retired" in second
        assert "first reason" in second

    def test_retired_cannot_be_superseded(self, vault):
        vault.enter(vault.project())
        did = log("Misfiled")
        retire_decision(did, "wrong project")
        result = supersede_decision(did, "New", "why", "x")
        assert result.startswith("Error:")
        assert "retired" in result

    def test_retiring_a_superseded_decision_is_allowed(self, vault):
        """Both flags can apply; retirement wins for display."""
        vault.enter(vault.project())
        first = log("Original")
        supersede_decision(first, "Replacement", "changed mind", "x")
        assert retire_decision(first, "whole thread was misfiled").startswith("Retired")
        assert "RETIRED" in get_decision(first)

    def test_counts_do_not_double_count(self, vault):
        vault.enter(vault.project())
        keep = log("Keep")
        a = log("Superseded only")
        supersede_decision(a, "Replacement", "why", "x")
        b = log("Retired only")
        retire_decision(b, "wrong project")

        brief = get_project_brief()
        assert "1 superseded decision(s)" in brief
        assert "1 retired (filed in error)" in brief

    def test_retire_is_scoped_to_the_project(self, vault):
        vault.enter(vault.project("alpha"))
        log("Alpha decision")
        vault.enter(vault.project("beta"))
        assert retire_decision(1, "x").startswith("Error:")


class TestMigration:
    def test_existing_vault_gains_the_columns(self, vault):
        """A vault written before this feature must upgrade in place."""
        repo = vault.enter(vault.project())
        path = server.db_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        # Build a pre-migration vault by hand: the old schema, with data.
        old = sqlite3.connect(path)
        old.executescript(
            """
            CREATE TABLE decisions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                summary       TEXT NOT NULL,
                reasoning     TEXT NOT NULL,
                excerpt       TEXT NOT NULL,
                created_at    TEXT NOT NULL,
                supersedes    INTEGER REFERENCES decisions(id),
                superseded_by INTEGER REFERENCES decisions(id)
            );
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )
        old.execute(
            "INSERT INTO decisions (summary, reasoning, excerpt, created_at)"
            " VALUES ('Legacy decision', 'old reasoning', 'old excerpt', '2026-01-01T00:00:00+00:00')"
        )
        old.commit()
        old.close()

        # Opening it through the server must upgrade without losing anything.
        assert "Legacy decision" in get_project_brief()
        assert "old reasoning" in get_decision(1)

        columns = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(decisions)")}
        assert {"retired_at", "retired_reason"} <= columns

        # And the new capability works on the upgraded vault.
        assert retire_decision(1, "misfiled").startswith("Retired")
        assert "Legacy decision" not in get_project_brief()

    def test_migration_is_idempotent(self, vault):
        vault.enter(vault.project())
        log("First")
        for _ in range(3):
            server.connect().close()
        assert "First" in get_project_brief()
