"""connect_hosted — write a repo's .mcp.json so it uses the hosted vault."""

from __future__ import annotations

import json

import onboarding
import server
from conftest import unwrap

connect_hosted = unwrap(server.connect_hosted)
log_decision = unwrap(server.log_decision)

HOST = "https://vault.example.com"


class TestNormaliseHost:
    def test_adds_scheme(self):
        assert onboarding.normalise_host("vault.example.com") == HOST

    def test_strips_trailing_slash(self):
        assert onboarding.normalise_host("https://vault.example.com/") == HOST

    def test_tolerates_a_pasted_connector_url(self):
        pasted = "https://vault.example.com/p/some-project/mcp"
        assert onboarding.normalise_host(pasted) == HOST

    def test_empty_stays_empty(self):
        assert onboarding.normalise_host("   ") == ""


class TestConnectHosted:
    def test_writes_a_usable_config(self, vault, monkeypatch):
        root = vault.enter(vault.project("my_repo"))
        monkeypatch.delenv(onboarding.HOSTED_URL_ENV, raising=False)

        result = connect_hosted(host=HOST)
        assert "Created" in result

        config = json.loads((root / ".mcp.json").read_text())
        entry = config["mcpServers"]["decisiontree"]
        assert entry["type"] == "http"
        assert entry["url"] == f"{HOST}/p/my_repo/mcp"
        assert entry["headers"]["Authorization"] == "Bearer ${CONTEXT_VAULT_TOKEN}"

    def test_never_inlines_a_real_token(self, vault, monkeypatch):
        """The file is meant to be committed."""
        root = vault.enter(vault.project())
        monkeypatch.setenv(onboarding.TOKEN_ENV, "super-secret-value")
        connect_hosted(host=HOST)
        assert "super-secret-value" not in (root / ".mcp.json").read_text()

    def test_uses_the_declared_project_name(self, vault):
        root = vault.enter(vault.project("ugly_dir"))
        onboarding.write_declared_name(root, "pretty-name")
        connect_hosted(host=HOST)
        config = json.loads((root / ".mcp.json").read_text())
        assert config["mcpServers"]["decisiontree"]["url"].endswith("/p/pretty-name/mcp")

    def test_explicit_project_wins(self, vault):
        root = vault.enter(vault.project("my_repo"))
        connect_hosted(host=HOST, project="chosen")
        config = json.loads((root / ".mcp.json").read_text())
        assert config["mcpServers"]["decisiontree"]["url"].endswith("/p/chosen/mcp")

    def test_host_from_environment(self, vault, monkeypatch):
        root = vault.enter(vault.project())
        monkeypatch.setenv(onboarding.HOSTED_URL_ENV, HOST)
        connect_hosted()
        assert HOST in (root / ".mcp.json").read_text()

    def test_without_a_host_it_says_how(self, vault, monkeypatch):
        vault.enter(vault.project())
        monkeypatch.delenv(onboarding.HOSTED_URL_ENV, raising=False)
        result = connect_hosted()
        assert result.startswith("Error:")
        assert onboarding.HOSTED_URL_ENV in result

    def test_preserves_other_servers(self, vault):
        """A repo may already depend on other MCP servers."""
        root = vault.enter(vault.project())
        (root / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"other": {"type": "http", "url": "https://x/mcp"}}})
        )
        connect_hosted(host=HOST)
        servers = json.loads((root / ".mcp.json").read_text())["mcpServers"]
        assert set(servers) == {"other", "decisiontree"}
        assert servers["other"]["url"] == "https://x/mcp"

    def test_preserves_unrelated_top_level_keys(self, vault):
        root = vault.enter(vault.project())
        (root / ".mcp.json").write_text(json.dumps({"somethingElse": {"keep": True}}))
        connect_hosted(host=HOST)
        config = json.loads((root / ".mcp.json").read_text())
        assert config["somethingElse"] == {"keep": True}

    def test_rerunning_is_idempotent(self, vault):
        vault.enter(vault.project())
        connect_hosted(host=HOST)
        second = connect_hosted(host=HOST)
        assert "already points at" in second

    def test_changing_host_updates_in_place(self, vault):
        root = vault.enter(vault.project())
        connect_hosted(host=HOST)
        result = connect_hosted(host="https://other.example.com")
        assert "Updated" in result
        config = json.loads((root / ".mcp.json").read_text())
        assert "other.example.com" in config["mcpServers"]["decisiontree"]["url"]

    def test_malformed_existing_file_is_left_alone(self, vault):
        root = vault.enter(vault.project())
        (root / ".mcp.json").write_text("{ not json")
        result = connect_hosted(host=HOST)
        assert result.startswith("Error:")
        assert (root / ".mcp.json").read_text() == "{ not json"

    def test_rejects_invalid_project_name(self, vault):
        vault.enter(vault.project())
        assert connect_hosted(host=HOST, project="Not Valid").startswith("Error:")

    def test_refuses_when_already_hosted(self, vault):
        token = server.REMOTE_PROJECT.set("hosted-project")
        try:
            result = connect_hosted(host=HOST)
        finally:
            server.REMOTE_PROJECT.reset(token)
        assert "already talks to the hosted server" in result

    def test_local_history_is_untouched(self, vault):
        """Connecting to hosted must not disturb what is already captured."""
        vault.enter(vault.project())
        log_decision("Logged locally", "why", "x")
        before = server.db_path()
        connect_hosted(host=HOST)
        assert server.db_path() == before
        assert "Logged locally" in unwrap(server.get_project_brief)()


class TestSetupMentionsIt:
    def test_local_report_offers_connect_hosted(self, vault):
        vault.enter(vault.project())
        assert "connect_hosted" in unwrap(server.setup)()
