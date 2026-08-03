"""Optional context fields: derives_from, cluster, source, ref, author.

These exist so the history forms a graph rather than a flat list. Everything
is optional — capture predates them, and a required argument would break every
agent already logging.
"""

from __future__ import annotations

import sqlite3

import pytest

import server
from conftest import unwrap

log_decision = unwrap(server.log_decision)
supersede_decision = unwrap(server.supersede_decision)
list_decisions = unwrap(server.list_decisions)
get_decision = unwrap(server.get_decision)


def log(summary="A decision", **kw) -> int:
    result = log_decision(summary, "because", "excerpt", **kw)
    assert result.startswith("Logged"), result
    return int(result.split("#")[1].split(":")[0])


class TestBackwardCompatible:
    def test_all_fields_are_optional(self, vault):
        """The original three-argument call must keep working untouched."""
        vault.enter(vault.project())
        assert log_decision("Use SQLite", "simple", "x") == "Logged decision #1: Use SQLite"

    def test_omitted_fields_store_null_not_empty_string(self, vault):
        vault.enter(vault.project())
        log()
        with sqlite3.connect(server.db_path()) as conn:
            row = conn.execute(
                "SELECT derives_from, cluster, source, ref, author FROM decisions"
            ).fetchone()
        assert row == (None, None, None, None, None)

    def test_blank_strings_are_stored_as_null(self, vault):
        vault.enter(vault.project())
        log(cluster="   ", source="", ref="  ", author="")
        with sqlite3.connect(server.db_path()) as conn:
            row = conn.execute("SELECT cluster, source, ref, author FROM decisions").fetchone()
        assert row == (None, None, None, None)


class TestDerivesFrom:
    def test_links_to_an_earlier_decision(self, vault):
        vault.enter(vault.project())
        first = log("Foundation")
        second = log("Builds on it", derives_from=first)
        assert f"Derives from: #{first}" in get_decision(second)

    def test_rejects_an_unknown_parent(self, vault):
        """An edge to nowhere is worse than no edge."""
        vault.enter(vault.project())
        result = log_decision("Orphan", "why", "x", derives_from=999)
        assert result.startswith("Error: no decision #999")

    def test_nothing_is_written_when_the_parent_is_invalid(self, vault):
        vault.enter(vault.project())
        log_decision("Orphan", "why", "x", derives_from=999)
        assert "Orphan" not in list_decisions()

    def test_zero_means_unset(self, vault):
        """The default is 0 rather than None so the schema stays MCP-friendly."""
        vault.enter(vault.project())
        did = log("No parent", derives_from=0)
        assert "Derives from" not in get_decision(did)

    def test_parent_is_scoped_to_the_project(self, vault):
        vault.enter(vault.project("alpha"))
        log("Alpha decision")
        vault.enter(vault.project("beta"))
        assert log_decision("Beta", "why", "x", derives_from=1).startswith("Error:")

    def test_chains_are_allowed(self, vault):
        vault.enter(vault.project())
        a = log("One")
        b = log("Two", derives_from=a)
        c = log("Three", derives_from=b)
        assert f"Derives from: #{b}" in get_decision(c)


class TestSource:
    @pytest.mark.parametrize("source", ["chat", "code", "pr", "doc"])
    def test_accepts_known_surfaces(self, vault, source):
        vault.enter(vault.project())
        did = log(source=source)
        assert f"({source})" in list_decisions()

    def test_rejects_anything_else(self, vault):
        """A mistyped surface would vanish from the filter rather than error."""
        vault.enter(vault.project())
        result = log_decision("x", "y", "z", source="slack")
        assert result.startswith("Error: source must be one of")
        assert "slack" in result


class TestClusterAndLabels:
    def test_cluster_shows_in_the_timeline(self, vault):
        """Visible in list_decisions so an agent reuses labels already in use."""
        vault.enter(vault.project())
        log(cluster="Landing page")
        assert "[Landing page]" in list_decisions()

    def test_author_and_ref_show_in_the_full_record(self, vault):
        vault.enter(vault.project())
        did = log(ref="PR #218 · tutor/ladder.ts", author="Devon L.")
        record = get_decision(did)
        assert "Reference: PR #218 · tutor/ladder.ts" in record
        assert "Author: Devon L." in record

    @pytest.mark.parametrize(
        "field,limit", [("cluster", server.MAX_CLUSTER), ("ref", server.MAX_REF), ("author", server.MAX_AUTHOR)]
    )
    def test_overlong_values_are_rejected(self, vault, field, limit):
        vault.enter(vault.project())
        result = log_decision("x", "y", "z", **{field: "z" * (limit + 1)})
        assert result.startswith(f"Error: {field} is longer than")


class TestSupersedeCarriesContext:
    def test_inherits_the_cluster_it_replaces(self, vault):
        vault.enter(vault.project())
        old = log("Original", cluster="Engine")
        result = supersede_decision(old, "Replacement", "changed", "x")
        new = int(result.split("#")[1].split(",")[0])

        with sqlite3.connect(server.db_path()) as conn:
            cluster = conn.execute(
                "SELECT cluster FROM decisions WHERE id = ?", (new,)
            ).fetchone()[0]
        assert cluster == "Engine"

    def test_explicit_cluster_overrides_inheritance(self, vault):
        vault.enter(vault.project())
        old = log("Original", cluster="Engine")
        supersede_decision(old, "Replacement", "changed", "x", cluster="Landing page")
        assert "[Landing page]" in list_decisions()

    def test_validates_its_own_source(self, vault):
        vault.enter(vault.project())
        old = log("Original")
        assert supersede_decision(old, "New", "why", "x", source="slack").startswith("Error:")

    def test_invalid_context_leaves_the_old_decision_untouched(self, vault):
        vault.enter(vault.project())
        old = log("Original")
        supersede_decision(old, "New", "why", "x", source="slack")
        assert "Original" in list_decisions(), "must not have been marked superseded"


class TestMigration:
    def test_existing_vault_gains_the_columns_and_keeps_data(self, vault):
        """Vaults written before these fields must upgrade in place."""
        vault.enter(vault.project())
        path = server.db_path()
        path.parent.mkdir(parents=True, exist_ok=True)

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
            " VALUES ('Legacy', 'old reasoning', 'old excerpt', '2026-01-01T00:00:00+00:00')"
        )
        old.commit()
        old.close()

        assert "Legacy" in list_decisions()
        assert "old reasoning" in get_decision(1)

        columns = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(decisions)")}
        assert {"derives_from", "cluster", "source", "ref", "author"} <= columns

        # And the new fields work on the upgraded vault.
        second = log("Follow-up", derives_from=1, cluster="Engine", source="pr")
        assert "Derives from: #1" in get_decision(second)


class TestCreatedAt:
    """An explicit timestamp exists for importing history recorded elsewhere."""

    def test_defaults_to_now(self, vault):
        vault.enter(vault.project())
        did = log()
        with sqlite3.connect(server.db_path()) as conn:
            stamp = conn.execute(
                "SELECT created_at FROM decisions WHERE id = ?", (did,)
            ).fetchone()[0]
        assert stamp.startswith("20")

    def test_preserves_an_explicit_timestamp(self, vault):
        vault.enter(vault.project())
        did = log(created_at="2026-08-02T18:53:22+00:00")
        with sqlite3.connect(server.db_path()) as conn:
            stamp = conn.execute(
                "SELECT created_at FROM decisions WHERE id = ?", (did,)
            ).fetchone()[0]
        assert stamp == "2026-08-02T18:53:22+00:00"

    def test_naive_timestamps_are_treated_as_utc(self, vault):
        vault.enter(vault.project())
        did = log(created_at="2026-08-02T18:53:22")
        with sqlite3.connect(server.db_path()) as conn:
            stamp = conn.execute(
                "SELECT created_at FROM decisions WHERE id = ?", (did,)
            ).fetchone()[0]
        assert stamp == "2026-08-02T18:53:22+00:00"

    def test_other_offsets_are_normalised_to_utc(self, vault):
        vault.enter(vault.project())
        did = log(created_at="2026-08-02T20:53:22+02:00")
        with sqlite3.connect(server.db_path()) as conn:
            stamp = conn.execute(
                "SELECT created_at FROM decisions WHERE id = ?", (did,)
            ).fetchone()[0]
        assert stamp == "2026-08-02T18:53:22+00:00"

    def test_rejects_a_malformed_timestamp(self, vault):
        vault.enter(vault.project())
        result = log_decision("x", "y", "z", created_at="last tuesday")
        assert result.startswith("Error: created_at")
        assert "ISO 8601" in result

    def test_rejects_a_future_timestamp(self, vault):
        """A future date would sort above everything in the timeline."""
        vault.enter(vault.project())
        result = log_decision("x", "y", "z", created_at="2099-01-01T00:00:00+00:00")
        assert result.startswith("Error: created_at")
        assert "future" in result

    def test_nothing_is_written_when_the_timestamp_is_rejected(self, vault):
        vault.enter(vault.project())
        log_decision("Rejected", "y", "z", created_at="nonsense")
        assert "Rejected" not in list_decisions()

    def test_ordering_follows_the_imported_dates(self, vault):
        """The point of the field: a real timeline after an import."""
        vault.enter(vault.project())
        log("Older", created_at="2026-01-01T00:00:00+00:00")
        log("Newer", created_at="2026-06-01T00:00:00+00:00")
        with sqlite3.connect(server.db_path()) as conn:
            rows = [
                r[0]
                for r in conn.execute("SELECT summary FROM decisions ORDER BY created_at")
            ]
        assert rows == ["Older", "Newer"]


class TestCaptureHints:
    """log_decision nudges toward the fields that make a graph — but only when
    it has something concrete to offer."""

    def test_first_decision_gets_no_hint(self, vault):
        """Nothing to derive from and no labels to reuse: stay quiet."""
        vault.enter(vault.project())
        assert log_decision("First", "why", "x") == "Logged decision #1: First"

    def test_suggests_labels_already_in_use(self, vault):
        vault.enter(vault.project())
        log("Has one", cluster="Engine contracts")
        log("Has another", cluster="Landing page")
        result = log_decision("Missing a cluster", "why", "x")
        assert "No cluster set" in result
        assert "Engine contracts" in result
        assert "Landing page" in result

    def test_no_cluster_hint_when_one_is_given(self, vault):
        vault.enter(vault.project())
        log("Has one", cluster="Engine")
        result = log_decision("Also has one", "why", "x", cluster="Engine")
        assert "No cluster set" not in result

    def test_generic_cluster_hint_once_a_project_has_a_few(self, vault):
        """No labels exist anywhere yet — the case that needs starting."""
        vault.enter(vault.project())
        for i in range(3):
            log(f"Decision {i}")
        result = log_decision("Fourth", "why", "x")
        assert "No cluster set" in result
        assert "groups related" in result

    def test_suggests_recent_decisions_to_derive_from(self, vault):
        vault.enter(vault.project())
        first = log("Earlier decision")
        result = log_decision("Follow-up", "why", "x")
        assert "No derives_from set" in result
        assert f"#{first} Earlier decision" in result

    def test_no_derives_hint_when_a_parent_is_given(self, vault):
        vault.enter(vault.project())
        first = log("Earlier decision")
        result = log_decision("Follow-up", "why", "x", derives_from=first)
        assert "No derives_from set" not in result

    def test_hints_list_at_most_three_recent(self, vault):
        vault.enter(vault.project())
        for i in range(6):
            log(f"Decision {i}", cluster="Engine")
        result = log_decision("Latest", "why", "x", cluster="Engine")
        assert result.count("#") - 1 <= 3, result

    def test_hint_does_not_offer_the_decision_itself(self, vault):
        vault.enter(vault.project())
        log("Earlier")
        result = log_decision("This one", "why", "x")
        new_id = int(result.split("#")[1].split(":")[0])
        assert f"#{new_id} This one" not in result

    def test_retired_decisions_are_not_offered_as_parents(self, vault):
        vault.enter(vault.project())
        first = log("Misfiled decision")
        unwrap(server.retire_decision)(first, "wrong project")
        result = log_decision("Next", "why", "x")
        assert "Misfiled decision" not in result
