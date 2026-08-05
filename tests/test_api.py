"""Read-only JSON API used by the web front-end.

A Railway volume mounts to one service, so the UI cannot open these SQLite
files itself — everything it renders comes through here.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import api
import http_app
import server
from conftest import add_member, api_token, oauth_app, unwrap
from test_http import Server, run

log_decision = unwrap(server.log_decision)
supersede_decision = unwrap(server.supersede_decision)
retire_decision = unwrap(server.retire_decision)


def seed(vault, project, summaries, **kw):
    """Write decisions into a hosted vault the way the MCP tools would."""
    token = server.REMOTE_PROJECT.set(project)
    try:
        ids = [log_decision(s, "because", "excerpt", **kw) for s in summaries]
    finally:
        server.REMOTE_PROJECT.reset(token)
    return [int(r.split("#")[1].split(":")[0]) for r in ids]


def in_project(project, fn, *args, **kw):
    token = server.REMOTE_PROJECT.set(project)
    try:
        return fn(*args, **kw)
    finally:
        server.REMOTE_PROJECT.reset(token)


class TestProjectDiscovery:
    def test_no_projects_when_nothing_hosted(self, vault):
        assert api.project_names() == []
        assert api.projects_payload() == {"projects": []}

    def test_lists_hosted_vaults_sorted(self, vault):
        seed(vault, "zeta", ["z"])
        seed(vault, "alpha", ["a"])
        assert api.project_names() == ["alpha", "zeta"]

    def test_ignores_files_that_are_not_valid_projects(self, vault):
        seed(vault, "real", ["r"])
        (api.remote_dir() / "Not A Project.db").write_text("")
        (api.remote_dir() / "notes.txt").write_text("")
        assert api.project_names() == ["real"]

    def test_local_vaults_are_never_served(self, vault):
        """Local vaults belong to somebody's laptop."""
        vault.enter(vault.project("laptop_repo"))
        log_decision("Local only", "why", "x")
        assert api.project_names() == []

    def test_exists_rejects_traversal(self, vault):
        seed(vault, "real", ["r"])
        assert api.exists("real")
        assert not api.exists("../../etc/passwd")
        assert not api.exists("Real")


class TestSummaries:
    def test_counts_by_status(self, vault):
        ids = seed(vault, "proj", ["one", "two", "three"])
        in_project("proj", supersede_decision, ids[0], "replacement", "why", "x")
        in_project("proj", retire_decision, ids[1], "misfiled")

        summary = api.summarise("proj")
        assert summary["decisions"] == 4  # 3 logged + 1 replacement
        assert summary["active"] == 2
        assert summary["superseded"] == 1
        assert summary["retired"] == 1

    def test_reports_clusters_and_last_activity(self, vault):
        seed(vault, "proj", ["a"], cluster="Engine")
        seed(vault, "proj", ["b"], cluster="Landing page")
        seed(vault, "proj", ["c"], cluster="Engine")
        summary = api.summarise("proj")
        assert summary["clusters"] == ["Engine", "Landing page"]
        assert summary["last_activity"] is not None

    def test_empty_project_summarises_cleanly(self, vault):
        seed(vault, "proj", [])
        # No decisions logged, but the vault file exists.
        in_project("proj", server.connect).close()
        summary = api.summarise("proj")
        assert summary["decisions"] == 0
        assert summary["last_activity"] is None


class TestDecisionsPayload:
    def test_returns_records_with_status(self, vault):
        ids = seed(vault, "proj", ["one", "two"])
        in_project("proj", supersede_decision, ids[0], "replacement", "why", "x")

        payload = api.decisions_payload("proj")
        by_id = {d["id"]: d for d in payload["decisions"]}
        assert by_id[ids[0]]["status"] == "superseded"
        assert by_id[ids[1]]["status"] == "active"

    def test_retired_excluded_by_default(self, vault):
        ids = seed(vault, "proj", ["keep", "drop"])
        in_project("proj", retire_decision, ids[1], "misfiled")

        assert [d["id"] for d in api.decisions_payload("proj")["decisions"]] == [ids[0]]
        included = api.decisions_payload("proj", include_retired=True)["decisions"]
        assert {d["id"] for d in included} == set(ids)

    def test_edges_cover_both_relationship_kinds(self, vault):
        first = seed(vault, "proj", ["root"])[0]
        child = seed(vault, "proj", ["child"], derives_from=first)[0]
        result = in_project("proj", supersede_decision, child, "replacement", "why", "x")
        replacement = int(result.split("#")[1].split(",")[0])

        edges = api.decisions_payload("proj")["edges"]
        assert {"from": child, "to": first, "kind": "derives"} in edges
        assert {"from": replacement, "to": child, "kind": "supersedes"} in edges

    def test_no_edge_points_at_a_filtered_out_decision(self, vault):
        """A retired parent must not leave a dangling edge."""
        parent = seed(vault, "proj", ["parent"])[0]
        child = seed(vault, "proj", ["child"], derives_from=parent)[0]
        in_project("proj", retire_decision, parent, "misfiled")

        payload = api.decisions_payload("proj")
        present = {d["id"] for d in payload["decisions"]}
        assert parent not in present
        for edge in payload["edges"]:
            assert edge["to"] in present, "edge points at a decision not returned"

    def test_collects_clusters_and_sources(self, vault):
        seed(vault, "proj", ["a"], cluster="Engine", source="pr")
        seed(vault, "proj", ["b"], cluster="Landing page", source="chat")
        payload = api.decisions_payload("proj")
        assert payload["clusters"] == ["Engine", "Landing page"]
        assert payload["sources"] == ["chat", "pr"]

    def test_projects_are_isolated(self, vault):
        seed(vault, "alpha", ["alpha decision"])
        seed(vault, "beta", ["beta decision"])
        alpha = api.decisions_payload("alpha")["decisions"]
        assert [d["summary"] for d in alpha] == ["alpha decision"]


class TestOverHttp:
    def _get(self, path, headers=None, **build_kw):
        """One request against a freshly built app.

        The app is rebuilt per call on purpose: it holds an MCP session
        manager that can only be run once, so reusing one instance across two
        uvicorn runs fails startup with SystemExit(3).
        """

        async def scenario():
            import httpx2

            async with Server(http_app.build_app(**build_kw)) as srv:
                async with httpx2.AsyncClient(headers=headers or {}) as client:
                    return await client.get(f"http://127.0.0.1:{srv.port}{path}")

        return run(scenario())

    def test_projects_route(self, vault):
        seed(vault, "proj", ["one"])
        response = self._get("/api/projects")
        assert response.status_code == 200
        assert response.json()["projects"][0]["name"] == "proj"

    def test_decisions_route(self, vault):
        seed(vault, "proj", ["one"])
        response = self._get("/api/projects/proj/decisions")
        assert response.status_code == 200
        assert response.json()["decisions"][0]["summary"] == "one"

    def test_unknown_project_404s_without_creating_a_vault(self, vault):
        """connect() creates the file, so a typo must not conjure a project."""
        seed(vault, "real", ["one"])
        response = self._get("/api/projects/typo/decisions")
        assert response.status_code == 404
        assert api.project_names() == ["real"], "a vault was created for a bad name"

    def test_unknown_route_404s_with_help(self, vault):
        response = self._get("/api/nonsense")
        assert response.status_code == 404
        assert "/api/projects" in json.dumps(response.json())

    def test_writes_are_rejected(self, vault):
        """The API is a projection for display; writes stay on the MCP tools."""

        async def scenario():
            import httpx2

            async with Server(http_app.build_app()) as srv:
                async with httpx2.AsyncClient() as client:
                    return await client.post(f"http://127.0.0.1:{srv.port}/api/projects")

        assert run(scenario()).status_code == 405

    def test_include_retired_query_param(self, vault):
        ids = seed(vault, "proj", ["keep", "drop"])
        in_project("proj", retire_decision, ids[1], "misfiled")
        assert len(self._get("/api/projects/proj/decisions").json()["decisions"]) == 1
        with_retired = self._get("/api/projects/proj/decisions?include_retired=true")
        assert len(with_retired.json()["decisions"]) == 2


class TestApiAuth:
    """The API takes the signed-in person's own token, not one of its own.

    The web service used to hold a shared credential and decide for itself who
    could see what, from a separate allowlist. Forwarding the caller's token
    means the vault applies the same membership it applies to a tool call, so
    there is one rule rather than two that can drift apart.
    """

    def _get(self, path, headers=None, **build_kw):
        async def scenario():
            import httpx2

            async with Server(oauth_app(**build_kw)) as srv:
                async with httpx2.AsyncClient(headers=headers or {}) as client:
                    return await client.get(f"http://127.0.0.1:{srv.port}{path}")

        return run(scenario())

    def bearer(self, srv_port, **claims):
        return {"Authorization": f"Bearer {api_token(srv_port, **claims)}"}

    def test_anonymous_is_refused(self, vault):
        seed(vault, "proj", ["one"])
        assert self._get("/api/projects").status_code == 401
        assert self._get("/api/projects/proj/decisions").status_code == 401
        # An invitation is somebody's, so reading one needs to be somebody.
        assert self._get("/api/invites/proj:abcdefghijklmnop").status_code == 401

    def test_an_unauthenticated_server_refuses_rather_than_crashes(self, vault):
        """A local run has nobody to be, and an invitation belongs to someone.

        Reaching the lookup with no identity at all would fail on the first
        attribute access — a 500 where a 404 is the honest answer.
        """
        from contextlib import closing

        seed(vault, "proj", ["one"])
        # A real, live invitation, so the lookup gets past every early return
        # and actually reaches the point where it wants to know who is asking.
        token = server.REMOTE_PROJECT.set("proj")
        try:
            with closing(server.connect()) as conn, server.writing(conn):
                code = server.mint_invite(conn, "member", 7, "invited@example.com")
        finally:
            server.REMOTE_PROJECT.reset(token)

        async def scenario():
            import httpx2

            async with Server(http_app.build_app()) as srv:
                async with httpx2.AsyncClient() as client:
                    return await client.get(
                        f"http://127.0.0.1:{srv.port}/api/invites/{code}"
                    )

        assert run(scenario()).status_code == 404

    def test_an_opaque_secret_is_refused(self, vault):
        seed(vault, "proj", ["one"])
        assert self._get(
            "/api/projects", {"Authorization": "Bearer secret"}
        ).status_code == 401

    def test_a_members_token_is_accepted(self, vault):
        seed(vault, "proj", ["one"])
        add_member("proj", "member@example.com", "owner")

        async def scenario():
            import httpx2

            async with Server(oauth_app()) as srv:
                token = api_token(srv.port, sub="auth0|m",
                                  **{server.EMAIL_CLAIM: "member@example.com"})
                async with httpx2.AsyncClient(
                    headers={"Authorization": f"Bearer {token}"}
                ) as client:
                    return await client.get(
                        f"http://127.0.0.1:{srv.port}/api/projects"
                    )

        response = run(scenario())
        assert response.status_code == 200
        assert [p["name"] for p in response.json()["projects"]] == ["proj"]

    def test_a_token_without_a_subject_sees_nothing(self, vault):
        """Verified, but nobody. An absent identity is not a wildcard."""
        seed(vault, "proj", ["one"])
        add_member("proj", "member@example.com", "owner")

        async def scenario():
            import httpx2

            async with Server(oauth_app()) as srv:
                token = api_token(srv.port, sub=None)
                async with httpx2.AsyncClient(
                    headers={"Authorization": f"Bearer {token}"}
                ) as client:
                    return await client.get(f"http://127.0.0.1:{srv.port}/api/projects")

        assert run(scenario()).json()["projects"] == []

    def test_a_stranger_sees_no_projects(self, vault):
        seed(vault, "proj", ["one"])
        add_member("proj", "member@example.com", "owner")

        async def scenario():
            import httpx2

            async with Server(oauth_app()) as srv:
                token = api_token(srv.port, sub="auth0|s",
                                  **{server.EMAIL_CLAIM: "stranger@example.com"})
                async with httpx2.AsyncClient(
                    headers={"Authorization": f"Bearer {token}"}
                ) as client:
                    projects = await client.get(
                        f"http://127.0.0.1:{srv.port}/api/projects"
                    )
                    decisions = await client.get(
                        f"http://127.0.0.1:{srv.port}/api/projects/proj/decisions"
                    )
                    return projects, decisions

        projects, decisions = run(scenario())
        assert projects.json()["projects"] == []
        # 404, not 403: a name is another team's business.
        assert decisions.status_code == 404


class TestLastActivity:
    def test_retired_decisions_do_not_count_as_activity(self, vault):
        """A cleanup should not make a project look freshly worked on."""
        # Real work dated in the past; the mistake logged just now, so it is
        # the newest record. (A future created_at is rejected by design, so
        # "newer" has to mean the server's own clock.)
        in_project("proj", log_decision, "real work", "why", "x",
                   created_at="2026-01-01T00:00:00+00:00")
        in_project("proj", log_decision, "later mistake", "why", "x")

        with_mistake = api.summarise("proj")["last_activity"]
        assert not with_mistake.startswith("2026-01-01"), "mistake should be newest"

        in_project("proj", retire_decision, 2, "filed in error")
        after = api.summarise("proj")["last_activity"]

        assert after == "2026-01-01T00:00:00+00:00", "should roll back to the real work"
        assert api.summarise("proj")["retired"] == 1

    def test_all_retired_means_no_last_activity(self, vault):
        ids = seed(vault, "proj", ["only one"])
        in_project("proj", retire_decision, ids[0], "misfiled")
        assert api.summarise("proj")["last_activity"] is None
