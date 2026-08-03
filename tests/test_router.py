"""The router endpoint: one connector, project chosen per call.

A pinned /p/<project>/mcp connector cannot be talked out of its project. The
router has none, so the caller must name one — and naming a project that does
not exist is treated as a typo unless creation is explicit.
"""

from __future__ import annotations

import asyncio

import pytest

import http_app
import server
from conftest import unwrap
from test_http import Server, run

log_decision = unwrap(server.log_decision)
list_decisions = unwrap(server.list_decisions)
get_project_brief = unwrap(server.get_project_brief)
list_projects = unwrap(server.list_projects)


def hosted(name, fn, *args, **kw):
    # Not called `project`: the tools take a project kwarg and it would collide.
    token = server.REMOTE_PROJECT.set(name)
    try:
        return fn(*args, **kw)
    finally:
        server.REMOTE_PROJECT.reset(token)


def as_router(fn, *args, **kw):
    token = server.ROUTER.set(True)
    try:
        return fn(*args, **kw)
    finally:
        server.ROUTER.reset(token)


class TestPinnedConnectorsAreUnchanged:
    def test_project_argument_is_refused_when_pinned(self, vault):
        """The URL decides; an argument must not quietly override it."""
        result = hosted("alpha", log_decision, "x", "y", "z", project="beta")
        assert result.startswith("Error:")
        assert "pinned to alpha" in result

    def test_project_argument_is_refused_over_stdio(self, vault):
        vault.enter(vault.project("repo"))
        result = log_decision("x", "y", "z", project="somewhere")
        assert result.startswith("Error:")
        assert "working directory" in result

    def test_omitting_it_still_works(self, vault):
        assert hosted("alpha", log_decision, "x", "y", "z").startswith("Logged")


class TestRouterRequiresAProject:
    def test_missing_project_is_refused_and_lists_the_real_ones(self, vault):
        hosted("alpha", log_decision, "seed", "y", "z")
        result = as_router(log_decision, "x", "y", "z")
        assert result.startswith("Error:")
        assert "project=<name>" in result
        assert "alpha" in result

    def test_invalid_name_is_refused(self, vault):
        result = as_router(log_decision, "x", "y", "z", project="Not Valid")
        assert result.startswith("Error:")
        assert server.PROJECT_NAME.pattern in result

    def test_writes_to_the_named_project(self, vault):
        hosted("alpha", log_decision, "seed", "y", "z")
        assert as_router(log_decision, "router wrote this", "y", "z",
                         project="alpha").startswith("Logged")
        assert "router wrote this" in hosted("alpha", list_decisions)

    def test_reads_are_scoped_too(self, vault):
        hosted("alpha", log_decision, "alpha decision", "y", "z")
        hosted("beta", log_decision, "beta decision", "y", "z")
        assert "alpha decision" in as_router(get_project_brief, project="alpha")
        assert "beta decision" not in as_router(get_project_brief, project="alpha")


class TestUnknownProject:
    def test_typo_is_refused_with_the_known_names(self, vault):
        hosted("hopscotch", log_decision, "seed", "y", "z")
        result = as_router(log_decision, "x", "y", "z", project="hopscoth")
        assert result.startswith("Error:")
        assert "hopscotch" in result, "should suggest the real name"
        assert "create=true" in result

    def test_typo_does_not_create_a_vault(self, vault):
        hosted("hopscotch", log_decision, "seed", "y", "z")
        as_router(log_decision, "x", "y", "z", project="hopscoth")
        assert server.remote_projects() == ["hopscotch"]

    def test_create_starts_a_new_project(self, vault):
        assert as_router(log_decision, "first", "y", "z",
                         project="brand-new", create=True).startswith("Logged")
        assert "brand-new" in server.remote_projects()

    def test_create_is_not_needed_for_an_existing_project(self, vault):
        hosted("alpha", log_decision, "seed", "y", "z")
        assert as_router(log_decision, "x", "y", "z",
                         project="alpha").startswith("Logged")


class TestListProjects:
    def test_reports_counts_and_clusters(self, vault):
        hosted("alpha", log_decision, "one", "y", "z", cluster="Engine")
        hosted("alpha", log_decision, "two", "y", "z", cluster="Landing")
        hosted("beta", log_decision, "solo", "y", "z")

        listed = list_projects()
        assert "alpha (2 active" in listed
        assert "Engine" in listed and "Landing" in listed
        assert "beta (1 active" in listed

    def test_says_so_when_there_are_none(self, vault):
        assert list_projects() == "No hosted projects with decisions yet."

    def test_retired_decisions_are_not_counted_as_active(self, vault):
        hosted("alpha", log_decision, "kept", "y", "z")
        hosted("alpha", log_decision, "dropped", "y", "z")
        hosted("alpha", unwrap(server.retire_decision), 2, "misfiled")
        assert "alpha (1 active" in list_projects()


class TestRoutingOverHttp:
    def _post(self, path, body, headers=None, **build_kw):
        async def scenario():
            import httpx2

            async with Server(http_app.build_app(**build_kw)) as srv:
                async with httpx2.AsyncClient() as client:
                    return await client.post(
                        f"http://127.0.0.1:{srv.port}{path}",
                        json=body,
                        headers={
                            "Content-Type": "application/json",
                            "Accept": "application/json, text/event-stream",
                            **(headers or {}),
                        },
                    )

        return run(scenario())

    INIT = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "t", "version": "1"}},
    }

    def test_router_path_is_served(self, vault):
        assert self._post("/mcp", self.INIT).status_code == 200

    def test_pinned_path_still_served(self, vault):
        assert self._post("/p/alpha/mcp", self.INIT).status_code == 200

    def test_router_still_requires_auth(self, vault):
        assert self._post("/mcp", self.INIT, token="secret").status_code == 401

    def test_index_advertises_both_endpoints(self, vault):
        async def scenario():
            import httpx2

            async with Server(http_app.build_app()) as srv:
                async with httpx2.AsyncClient() as client:
                    return await client.get(f"http://127.0.0.1:{srv.port}/")

        body = run(scenario()).json()
        assert body["connect"] == "/p/<project>/mcp"
        assert body["router"] == "/mcp"


class TestRouterResourceUrls:
    def test_router_resource_is_the_bare_mcp_url(self):
        scope = {"scheme": "http", "headers": [(b"host", b"v.example.com"),
                                               (b"x-forwarded-proto", b"https")]}
        assert http_app.resource_url_for(scope, None) == "https://v.example.com/mcp"
        assert http_app.metadata_url_for(scope, None).endswith(
            "/.well-known/oauth-protected-resource/mcp")

    def test_pinned_resource_unchanged(self):
        scope = {"scheme": "http", "headers": [(b"host", b"v.example.com"),
                                               (b"x-forwarded-proto", b"https")]}
        assert http_app.resource_url_for(scope, "alpha") == "https://v.example.com/p/alpha/mcp"


class TestEmptyProjectsAreDeprioritised:
    """An empty vault is indistinguishable from an accidental one, and this
    text is read by an agent deciding where to write."""

    def test_suggestions_lead_with_projects_that_have_decisions(self, vault):
        hosted("real-work", log_decision, "a decision", "y", "z")
        hosted("leftover", server.connect).close()  # exists, empty

        suggestion = server.suggest_projects()
        assert suggestion.startswith("real-work")
        assert "leftover" not in suggestion
        assert "1 empty" in suggestion

    def test_error_messages_use_the_same_suggestion(self, vault):
        hosted("real-work", log_decision, "a decision", "y", "z")
        hosted("leftover", server.connect).close()
        result = as_router(log_decision, "x", "y", "z", project="typo")
        assert "real-work" in result
        assert "leftover" not in result

    def test_list_projects_puts_empties_in_a_footnote(self, vault):
        hosted("real-work", log_decision, "a decision", "y", "z")
        hosted("leftover", server.connect).close()
        listed = list_projects()
        assert "- real-work (1 active" in listed
        assert "(1 empty: leftover)" in listed
        assert "- leftover" not in listed

    def test_an_empty_project_is_still_writable_without_create(self, vault):
        """Deprioritised in the listing, but it exists, so it is not a typo."""
        hosted("leftover", server.connect).close()
        assert as_router(log_decision, "x", "y", "z",
                         project="leftover").startswith("Logged")
