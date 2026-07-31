"""Onboarding: say which project you're in before anything is captured."""

from __future__ import annotations

import asyncio
import json

import onboarding
import server
from conftest import unwrap

setup = unwrap(server.setup)
_name_project = unwrap(server.name_project)


def name_project(*args, **kwargs):
    return asyncio.run(_name_project(*args, **kwargs))
log_decision = unwrap(server.log_decision)
get_project_brief = unwrap(server.get_project_brief)


class TestSuggestName:
    def test_derives_a_valid_name(self, vault):
        for raw, expected in [
            ("My Repo", "my-repo"),
            ("rapid_manufacturing", "rapid_manufacturing"),
            ("UPPER", "upper"),
            ("...dots...", "dots"),
        ]:
            root = vault.project(raw)
            suggested = onboarding.suggest_name(root)
            assert suggested == expected
            assert server.PROJECT_NAME.match(suggested)

    def test_unnameable_directory_still_yields_valid_name(self, vault):
        suggested = onboarding.suggest_name(vault.project("!!!"))
        assert server.PROJECT_NAME.match(suggested)


class TestDeclaration:
    def test_absent_by_default(self, vault):
        assert onboarding.read_declared_name(vault.project()) is None

    def test_round_trips_json(self, vault):
        root = vault.project()
        onboarding.write_declared_name(root, "my-project")
        assert onboarding.read_declared_name(root) == "my-project"
        assert json.loads((root / ".context-vault").read_text())["project"] == "my-project"

    def test_accepts_a_bare_name(self, vault):
        root = vault.project()
        (root / ".context-vault").write_text("plain-name\n")
        assert onboarding.read_declared_name(root) == "plain-name"

    def test_rejects_an_invalid_declared_name(self, vault):
        root = vault.project()
        (root / ".context-vault").write_text("Not A Valid Name")
        assert onboarding.read_declared_name(root) is None

    def test_empty_file_is_ignored(self, vault):
        root = vault.project()
        (root / ".context-vault").write_text("   ")
        assert onboarding.read_declared_name(root) is None

    def test_declared_name_titles_the_brief(self, vault):
        root = vault.project("ugly_dir_name")
        vault.enter(root)
        onboarding.write_declared_name(root, "pretty-name")
        log_decision("A decision", "why", "x")
        assert "# Project brief — pretty-name" in get_project_brief()

    def test_declaring_a_name_does_not_orphan_history(self, vault):
        """The local vault stays keyed to the path, so history survives naming."""
        root = vault.project()
        vault.enter(root)
        log_decision("Logged before naming", "why", "x")
        before = server.db_path()

        name_project("renamed-later")

        assert server.db_path() == before
        assert "Logged before naming" in get_project_brief()


class TestNameProject:
    def test_writes_the_declaration(self, vault):
        root = vault.enter(vault.project())
        result = name_project("chosen-name")
        assert "chosen-name" in result
        assert onboarding.read_declared_name(root) == "chosen-name"

    def test_rejects_invalid_names(self, vault):
        vault.enter(vault.project())
        result = name_project("Not Valid!")
        assert result.startswith("Error:")
        assert server.PROJECT_NAME.pattern in result

    def test_refuses_over_http(self, vault):
        token = server.REMOTE_PROJECT.set("hosted-project")
        try:
            result = name_project("something")
        finally:
            server.REMOTE_PROJECT.reset(token)
        assert "connector URL" in result

    def test_without_a_name_and_no_client_explains_how(self, vault):
        vault.enter(vault.project("some_repo"))
        result = name_project()
        assert "some_repo" in result
        assert ".context-vault" in result


class TestSetupReport:
    def test_names_the_project_and_the_vault_file(self, vault):
        root = vault.enter(vault.project("my_repo"))
        report = setup()
        assert "my_repo" in report
        assert str(server.db_path()) in report
        assert "local (stdio)" in report

    def test_explains_capture_is_required(self, vault):
        vault.enter(vault.project())
        report = setup()
        assert "required" in report.lower()
        assert "log_decision" in report
        assert "CLAUDE.md" in report

    def test_offers_the_hosted_command_with_the_right_name(self, vault):
        root = vault.enter(vault.project("my_repo"))
        onboarding.write_declared_name(root, "chosen")
        assert "/p/chosen/mcp" in setup()

    def test_hosted_report_points_at_the_connector_url(self, vault):
        token = server.REMOTE_PROJECT.set("hosted-project")
        try:
            report = setup()
        finally:
            server.REMOTE_PROJECT.reset(token)
        assert "hosted HTTP" in report
        assert "hosted-project" in report
        assert "/p/<project>/mcp" in report

    def test_counts_reflect_the_vault(self, vault):
        vault.enter(vault.project())
        assert "0 active decision(s), 0 total" in setup()
        log_decision("One", "why", "x")
        assert "1 active decision(s), 1 total" in setup()


class TestFirstRunNudge:
    def test_empty_vault_points_at_setup(self, vault):
        vault.enter(vault.project())
        assert "call `setup`" in get_project_brief()

    def test_nudge_stops_once_anything_is_logged(self, vault):
        vault.enter(vault.project())
        log_decision("First", "why", "x")
        assert "call `setup`" not in get_project_brief()

    def test_no_nudge_when_history_exists_but_none_active(self, vault):
        """A vault whose decisions were all retired is not a first run."""
        vault.enter(vault.project())
        log_decision("Misfiled", "why", "x")
        unwrap(server.retire_decision)(1, "wrong project")
        brief = get_project_brief()
        assert "call `setup`" not in brief
        assert "retired" in brief
