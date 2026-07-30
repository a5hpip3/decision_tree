"""Context Vault behaviour tests.

Two things carry the design and so get the most coverage: which project a call
resolves to, and the append-only supersede invariant.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import sqlite3
import subprocess

import pytest

import server
from conftest import unwrap

log_decision = unwrap(server.log_decision)
supersede_decision = unwrap(server.supersede_decision)
list_decisions = unwrap(server.list_decisions)
get_decision = unwrap(server.get_decision)
get_project_brief = unwrap(server.get_project_brief)


def decision_id(result: str) -> int:
    """Pull the id out of a tool's confirmation string."""
    match = re.search(r"#(\d+)", result)
    assert match, f"no decision id in {result!r}"
    return int(match.group(1))


def log(summary: str = "Use SQLite", reasoning: str = "One file, no daemon") -> int:
    return decision_id(log_decision(summary, reasoning, f"user: {summary}?\nagent: yes"))


# --------------------------------------------------------------------------
# project_root — which project a call belongs to
# --------------------------------------------------------------------------


class TestProjectRoot:
    def test_git_root_from_project_root(self, vault):
        repo = vault.project("repo")
        vault.enter(repo)
        assert server.project_root() == repo

    def test_git_root_from_subdirectory(self, vault):
        """Launching from a subdirectory must resolve to the repo, not the cwd."""
        repo = vault.project("repo")
        nested = repo / "src" / "api"
        nested.mkdir(parents=True)
        vault.enter(nested)
        assert server.project_root() == repo

    def test_git_marker_as_file_is_a_root(self, vault):
        """Worktrees and submodules have a `.git` file, not a directory.

        Enters a subdirectory deliberately: from the root itself the cwd
        fallback returns the same path, so the test would pass even if the
        marker were ignored entirely.
        """
        repo = vault.project("worktree", git_as_file=True)
        nested = repo / "src"
        nested.mkdir()
        vault.enter(nested)
        assert server.project_root() == repo

    def test_falls_back_to_cwd_without_git(self, vault):
        plain = vault.project("plain", git=False)
        vault.enter(plain)
        assert server.project_root() == plain

    def test_nearest_git_root_wins(self, vault):
        """An inner repo is its own project, not part of the outer one."""
        outer = vault.project("outer")
        inner = vault.project("inner", parent=outer)
        vault.enter(inner)
        assert server.project_root() == inner

    def test_env_override_beats_git_root(self, vault, monkeypatch):
        repo = vault.project("repo")
        pinned = vault.project("pinned", git=False)
        vault.enter(repo)
        monkeypatch.setenv("CONTEXT_VAULT_PROJECT", str(pinned))
        assert server.project_root() == pinned

    def test_env_override_expands_user(self, vault, monkeypatch):
        monkeypatch.setenv("CONTEXT_VAULT_PROJECT", "~/somewhere")
        assert "~" not in str(server.project_root())

    @pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
    def test_fabricated_git_marker_matches_real_git_init(self, vault, tmp_path):
        """Guards the fake `.git` the other tests rely on against real git."""
        real = tmp_path / "real-repo"
        real.mkdir()
        subprocess.run(["git", "init", "-q", str(real)], check=True)
        nested = real / "deep" / "nested"
        nested.mkdir(parents=True)
        vault.enter(nested)
        assert server.project_root() == real.resolve()


# --------------------------------------------------------------------------
# project_slug / db_path — one file per project
# --------------------------------------------------------------------------


class TestVaultPath:
    def test_vault_lives_under_projects_dir(self, vault):
        vault.enter(vault.project("repo"))
        path = server.db_path()
        assert path.parent == vault.home / "projects"
        assert path.suffix == ".db"

    def test_slug_is_readable_and_sanitised(self, vault):
        vault.enter(vault.project("My Repo!"))
        assert server.db_path().name.startswith("my-repo-")

    def test_slug_strips_leading_and_trailing_punctuation(self, vault):
        vault.enter(vault.project("...repo..."))
        assert server.db_path().name.startswith("repo-")

    def test_unnameable_project_falls_back(self, vault):
        vault.enter(vault.project("!!!"))
        assert server.db_path().name.startswith("project-")

    def test_long_name_is_truncated(self, vault):
        vault.enter(vault.project("z" * 120))
        stem, _, digest = server.db_path().stem.rpartition("-")
        assert len(stem) == 40
        assert len(digest) == 6

    def test_same_project_is_stable_across_calls(self, vault):
        repo = vault.project("repo")
        nested = repo / "src"
        nested.mkdir()

        vault.enter(repo)
        from_root = server.db_path()
        vault.enter(nested)
        assert server.db_path() == from_root

    def test_same_name_different_path_gets_different_vault(self, vault, tmp_path):
        """The hash is what actually separates two repos called the same thing."""
        a = vault.project("api", parent=tmp_path / "one")
        b = vault.project("api", parent=tmp_path / "two")

        vault.enter(a)
        path_a = server.db_path()
        vault.enter(b)
        path_b = server.db_path()

        assert path_a != path_b
        assert path_a.stem.rpartition("-")[0] == path_b.stem.rpartition("-")[0] == "api"

    def test_db_env_override_pins_exact_file(self, vault, monkeypatch, tmp_path):
        vault.enter(vault.project("repo"))
        pinned = tmp_path / "pinned.db"
        monkeypatch.setenv("CONTEXT_VAULT_DB", str(pinned))
        assert server.db_path() == pinned

    def test_db_env_override_expands_user(self, vault, monkeypatch):
        monkeypatch.setenv("CONTEXT_VAULT_DB", "~/pinned.db")
        assert "~" not in str(server.db_path())


# --------------------------------------------------------------------------
# Isolation — the point of the whole change
# --------------------------------------------------------------------------


class TestProjectIsolation:
    def test_decisions_do_not_leak_between_projects(self, vault):
        alpha, beta = vault.project("alpha"), vault.project("beta")

        vault.enter(alpha)
        log("Use SQLite", "One file, no daemon")

        vault.enter(beta)
        brief = get_project_brief()
        assert "SQLite" not in brief
        assert "no recorded history" in brief

        vault.enter(alpha)
        assert "SQLite" in get_project_brief()

    def test_ids_are_independent_per_project(self, vault):
        alpha, beta = vault.project("alpha"), vault.project("beta")
        vault.enter(alpha)
        assert log("Alpha first") == 1
        assert log("Alpha second") == 2
        vault.enter(beta)
        assert log("Beta first") == 1

    def test_subdirectory_shares_the_repo_vault(self, vault):
        repo = vault.project("repo")
        nested = repo / "src"
        nested.mkdir()

        vault.enter(repo)
        log("Use SQLite")
        vault.enter(nested)
        assert "SQLite" in get_project_brief()

    def test_each_project_gets_its_own_file(self, vault):
        vault.enter(vault.project("alpha"))
        log("Alpha")
        vault.enter(vault.project("beta"))
        log("Beta")
        assert len(list((vault.home / "projects").glob("*.db"))) == 2


# --------------------------------------------------------------------------
# log_decision
# --------------------------------------------------------------------------


class TestLogDecision:
    def test_returns_id_and_summary(self, vault):
        vault.enter(vault.project())
        result = log_decision("Use SQLite", "One file", "verbatim excerpt")
        assert result == "Logged decision #1: Use SQLite"

    def test_persists_all_fields(self, vault):
        vault.enter(vault.project())
        log_decision("Use SQLite", "One file, no daemon", "user: db?\nagent: sqlite")
        record = get_decision(1)
        assert "Use SQLite" in record
        assert "One file, no daemon" in record
        assert "agent: sqlite" in record

    def test_ids_increment(self, vault):
        vault.enter(vault.project())
        assert [log("One"), log("Two"), log("Three")] == [1, 2, 3]

    def test_creates_vault_directory_on_first_write(self, vault):
        vault.enter(vault.project())
        assert not (vault.home / "projects").exists()
        log("First decision")
        assert server.db_path().exists()


# --------------------------------------------------------------------------
# supersede_decision — the append-only invariant
# --------------------------------------------------------------------------


class TestSupersede:
    def test_links_both_directions(self, vault):
        vault.enter(vault.project())
        old = log("Use SQLite")
        new = decision_id(
            supersede_decision(old, "Use Postgres", "Outgrew SQLite", "excerpt")
        )

        assert f"Supersedes: #{old}" in get_decision(new)
        assert f"SUPERSEDED by #{new}" in get_decision(old)

    def test_old_decision_is_kept_not_deleted(self, vault):
        vault.enter(vault.project())
        old = log("Use SQLite")
        supersede_decision(old, "Use Postgres", "Outgrew it", "excerpt")

        assert "SQLite" not in list_decisions()
        assert "SQLite" in list_decisions(include_superseded=True)

    def test_superseded_decision_leaves_the_brief(self, vault):
        vault.enter(vault.project())
        old = log("Use SQLite")
        supersede_decision(old, "Use Postgres", "Outgrew it", "excerpt")

        brief = get_project_brief()
        assert "Postgres" in brief
        assert "SQLite" not in brief
        assert "1 superseded decision(s)" in brief

    def test_rejects_superseding_twice(self, vault):
        vault.enter(vault.project())
        old = log("Use SQLite")
        new = decision_id(supersede_decision(old, "Use Postgres", "Outgrew it", "x"))

        result = supersede_decision(old, "Use MySQL", "Changed mind again", "x")
        assert result.startswith("Error:")
        assert f"already superseded by #{new}" in result

    def test_rejects_unknown_id(self, vault):
        vault.enter(vault.project())
        result = supersede_decision(999, "Use Postgres", "why", "excerpt")
        assert result.startswith("Error: no decision #999")

    def test_failed_supersede_writes_nothing(self, vault):
        vault.enter(vault.project())
        log("Use SQLite")
        supersede_decision(999, "Use Postgres", "why", "excerpt")
        assert "Postgres" not in list_decisions(include_superseded=True)

    def test_chain_of_supersedes(self, vault):
        vault.enter(vault.project())
        first = log("Use SQLite")
        second = decision_id(supersede_decision(first, "Use Postgres", "grew", "x"))
        supersede_decision(second, "Use DynamoDB", "grew again", "x")

        brief = get_project_brief()
        assert "DynamoDB" in brief
        assert "Postgres" not in brief
        assert "2 superseded decision(s)" in brief

    def test_supersede_is_scoped_to_the_project(self, vault):
        """An id from another project must not be reachable."""
        vault.enter(vault.project("alpha"))
        log("Alpha decision")
        vault.enter(vault.project("beta"))
        assert supersede_decision(1, "Beta reversal", "why", "x").startswith("Error:")


# --------------------------------------------------------------------------
# Read tools
# --------------------------------------------------------------------------


class TestReadTools:
    def test_list_is_newest_first(self, vault):
        vault.enter(vault.project())
        log("Oldest")
        log("Newest")
        assert list_decisions().index("Newest") < list_decisions().index("Oldest")

    def test_brief_is_oldest_first(self, vault):
        """The brief reads as a narrative, so it runs the other way."""
        vault.enter(vault.project())
        log("Oldest")
        log("Newest")
        brief = get_project_brief()
        assert brief.index("Oldest") < brief.index("Newest")

    def test_brief_names_the_project(self, vault):
        vault.enter(vault.project("my-service"))
        log("Use SQLite")
        assert "# Project brief — my-service" in get_project_brief()

    def test_list_hides_excerpt_and_reasoning(self, vault):
        vault.enter(vault.project())
        log_decision("Use SQLite", "SECRET_REASONING", "SECRET_EXCERPT")
        listing = list_decisions()
        assert "Use SQLite" in listing
        assert "SECRET_REASONING" not in listing
        assert "SECRET_EXCERPT" not in listing

    def test_get_decision_shows_excerpt(self, vault):
        vault.enter(vault.project())
        log_decision("Use SQLite", "One file", "user: which db?\nagent: sqlite")
        assert "user: which db?" in get_decision(1)

    def test_get_decision_rejects_unknown_id(self, vault):
        vault.enter(vault.project())
        assert get_decision(999).startswith("Error: no decision #999")

    def test_empty_messages_name_the_project(self, vault):
        vault.enter(vault.project("lonely"))
        assert "lonely" in list_decisions()
        assert "lonely" in get_project_brief()


# --------------------------------------------------------------------------
# Migration from the pre-per-project shared vault
# --------------------------------------------------------------------------


class TestLegacyVaultHint:
    def test_hint_appears_when_shared_vault_exists(self, vault):
        vault.create_legacy()
        vault.enter(vault.project())
        assert str(vault.legacy_db) in get_project_brief()
        assert str(vault.legacy_db) in list_decisions()

    def test_no_hint_without_a_shared_vault(self, vault):
        vault.enter(vault.project())
        assert "CONTEXT_VAULT_DB" not in get_project_brief()

    def test_no_hint_once_a_project_has_decisions(self, vault):
        vault.create_legacy()
        vault.enter(vault.project())
        log("Use SQLite")
        assert str(vault.legacy_db) not in get_project_brief()

    def test_no_hint_when_db_is_pinned(self, vault, monkeypatch, tmp_path):
        """Pinning CONTEXT_VAULT_DB is how you read the old vault — don't nag."""
        vault.create_legacy()
        vault.enter(vault.project())
        monkeypatch.setenv("CONTEXT_VAULT_DB", str(tmp_path / "pinned.db"))
        assert str(vault.legacy_db) not in get_project_brief()


# --------------------------------------------------------------------------
# Schema and meta
# --------------------------------------------------------------------------


class TestStorage:
    def test_records_its_project_path(self, vault):
        repo = vault.enter(vault.project("repo"))
        log("Use SQLite")
        with sqlite3.connect(server.db_path()) as conn:
            stored = conn.execute(
                "SELECT value FROM meta WHERE key = 'project_path'"
            ).fetchone()[0]
        assert stored == str(repo)

    def test_project_path_reflects_creation_not_reading(self, vault, monkeypatch):
        """A vault read from elsewhere keeps the path it was created under."""
        repo = vault.enter(vault.project("repo"))
        log("Use SQLite")
        created_at_path = server.db_path()

        vault.enter(vault.project("somewhere-else"))
        monkeypatch.setenv("CONTEXT_VAULT_DB", str(created_at_path))
        get_project_brief()

        with sqlite3.connect(created_at_path) as conn:
            stored = conn.execute(
                "SELECT value FROM meta WHERE key = 'project_path'"
            ).fetchone()[0]
        assert stored == str(repo)

    def test_connect_is_idempotent(self, vault):
        vault.enter(vault.project())
        for _ in range(3):
            server.connect().close()
        with sqlite3.connect(server.db_path()) as conn:
            count = conn.execute("SELECT COUNT(*) FROM meta").fetchone()[0]
        assert count == 1

    def test_opens_an_existing_vault_without_clobbering(self, vault):
        vault.enter(vault.project())
        log("Use SQLite")
        server.connect().close()
        assert "SQLite" in list_decisions()


# --------------------------------------------------------------------------
# MCP surface
# --------------------------------------------------------------------------


def test_all_tools_are_registered():
    """Guards against a tool silently losing its @mcp.tool() decorator."""
    tools = {tool.name for tool in asyncio.run(server.mcp.list_tools())}
    assert tools == {
        "log_decision",
        "supersede_decision",
        "list_decisions",
        "get_decision",
        "get_project_brief",
    }


def test_tool_descriptions_are_populated():
    """Docstrings are the prompt an agent reads — an empty one is a real bug."""
    for tool in asyncio.run(server.mcp.list_tools()):
        assert tool.description, f"{tool.name} has no description"
