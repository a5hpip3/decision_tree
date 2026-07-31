"""HTTP transport tests — per-project routing over one connector URL each.

The critical property is that concurrent requests to different projects never
see each other's vault. Everything else here is guarding the edge that decides
which project a request belongs to.
"""

from __future__ import annotations

import asyncio

import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

import http_app
import server

PROJECTS = ["alpha", "beta", "gamma", "delta"]


# --------------------------------------------------------------------------
# Pure path parsing — no server needed
# --------------------------------------------------------------------------


class TestParseProject:
    @pytest.mark.parametrize(
        "path,expected",
        [
            ("/p/alpha/mcp", "alpha"),
            ("/p/my-repo/mcp", "my-repo"),
            ("/p/repo.v2/mcp", "repo.v2"),
            ("/p/a/mcp", "a"),
            ("/p/" + "z" * 64 + "/mcp", "z" * 64),
        ],
    )
    def test_accepts_valid_names(self, path, expected):
        assert http_app.parse_project(path) == expected

    @pytest.mark.parametrize(
        "path",
        [
            "/p//mcp",
            "/p/../mcp",
            "/p/../../etc/passwd/mcp",
            "/p/a/b/mcp",
            "/p/UPPER/mcp",
            "/p/-leading/mcp",
            "/p/.hidden/mcp",
            "/p/sp ace/mcp",
            "/p/semi;colon/mcp",
            "/p/" + "z" * 65 + "/mcp",
            "/p/alpha",
            "/p/alpha/sse",
            "/mcp",
            "/",
        ],
    )
    def test_rejects_everything_else(self, path):
        assert http_app.parse_project(path) is None

    def test_traversal_cannot_escape_the_vault_directory(self, vault):
        """A rejected name must never reach db_path()."""
        assert http_app.parse_project("/p/../../etc/passwd/mcp") is None

    def test_valid_names_stay_inside_the_vault_directory(self, vault):
        token = server.REMOTE_PROJECT.set("alpha")
        try:
            path = server.db_path().resolve()
        finally:
            server.REMOTE_PROJECT.reset(token)
        assert path.parent == (vault.home / "remote").resolve()


# --------------------------------------------------------------------------
# Storage routing via the contextvar
# --------------------------------------------------------------------------


class TestRemoteStorage:
    def test_remote_project_selects_its_own_file(self, vault):
        paths = []
        for name in ("alpha", "beta"):
            token = server.REMOTE_PROJECT.set(name)
            try:
                paths.append(server.db_path())
            finally:
                server.REMOTE_PROJECT.reset(token)
        assert paths[0] != paths[1]
        assert {p.name for p in paths} == {"alpha.db", "beta.db"}

    def test_remote_outranks_pinned_db_env(self, vault, monkeypatch, tmp_path):
        """Otherwise one env var would collapse every tenant into one vault."""
        monkeypatch.setenv("CONTEXT_VAULT_DB", str(tmp_path / "pinned.db"))
        token = server.REMOTE_PROJECT.set("alpha")
        try:
            assert server.db_path().name == "alpha.db"
        finally:
            server.REMOTE_PROJECT.reset(token)

    def test_meta_records_remote_identity(self, vault):
        token = server.REMOTE_PROJECT.set("alpha")
        try:
            assert server.project_identity() == "remote:alpha"
            assert server.project_label() == "alpha"
        finally:
            server.REMOTE_PROJECT.reset(token)

    def test_local_mode_is_unchanged(self, vault):
        """The contextvar being unset must leave stdio behaviour intact."""
        repo = vault.enter(vault.project("repo"))
        assert server.REMOTE_PROJECT.get() is None
        assert server.db_path().parent == vault.home / "projects"
        assert server.project_identity() == str(repo)

    def test_legacy_hint_suppressed_for_remote_clients(self, vault):
        vault.create_legacy()
        token = server.REMOTE_PROJECT.set("alpha")
        try:
            assert server.empty_vault_note() == ""
        finally:
            server.REMOTE_PROJECT.reset(token)


# --------------------------------------------------------------------------
# End-to-end over real HTTP
# --------------------------------------------------------------------------


class Server:
    """A uvicorn instance on an ephemeral port, for the duration of a test."""

    def __init__(self, app):
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="critical")
        self._server = uvicorn.Server(config)
        self._task: asyncio.Task | None = None

    async def __aenter__(self):
        self._task = asyncio.create_task(self._server.serve())
        while not self._server.started:
            await asyncio.sleep(0.02)
        self.port = self._server.servers[0].sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self._server.should_exit = True
        await self._task

    def url(self, project: str) -> str:
        return f"http://127.0.0.1:{self.port}/p/{project}/mcp"


async def call(url: str, tool: str, args: dict | None = None, headers=None) -> str:
    import httpx2

    client = httpx2.AsyncClient(headers=headers) if headers else None
    async with streamable_http_client(url, http_client=client) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            result = await session.call_tool(tool, args or {})
            return result.content[0].text


def run(coro):
    return asyncio.run(coro)


class TestOverHttp:
    def test_decision_logged_via_url_lands_in_that_project(self, vault):
        async def scenario():
            async with Server(http_app.build_app()) as srv:
                await call(
                    srv.url("alpha"),
                    "log_decision",
                    {"summary": "Use SQLite", "reasoning": "simple", "excerpt": "x"},
                )
                return (
                    await call(srv.url("alpha"), "get_project_brief"),
                    await call(srv.url("beta"), "get_project_brief"),
                )

        alpha, beta = run(scenario())
        assert "Use SQLite" in alpha
        assert "# Project brief — alpha" in alpha
        assert "Use SQLite" not in beta
        assert "no recorded history" in beta

    def test_each_project_gets_its_own_file(self, vault):
        async def scenario():
            async with Server(http_app.build_app()) as srv:
                for name in PROJECTS:
                    await call(
                        srv.url(name),
                        "log_decision",
                        {"summary": f"{name} decision", "reasoning": "r", "excerpt": "x"},
                    )

        run(scenario())
        files = sorted(p.name for p in (vault.home / "remote").glob("*.db"))
        assert files == sorted(f"{n}.db" for n in PROJECTS)

    def test_concurrent_projects_do_not_leak(self, vault):
        """The spike's core finding, pinned as a regression test."""

        async def scenario():
            async with Server(http_app.build_app()) as srv:
                await asyncio.gather(
                    *(
                        call(
                            srv.url(name),
                            "log_decision",
                            {
                                "summary": f"{name} decision",
                                "reasoning": "r",
                                "excerpt": "x",
                            },
                        )
                        for _ in range(5)
                        for name in PROJECTS
                    )
                )
                return await asyncio.gather(
                    *(call(srv.url(n), "get_project_brief") for n in PROJECTS)
                )

        briefs = run(scenario())
        for name, brief in zip(PROJECTS, briefs):
            assert f"{name} decision" in brief
            for other in PROJECTS:
                if other != name:
                    assert f"{other} decision" not in brief, f"{other} leaked into {name}"

    def test_invalid_project_is_rejected(self, vault):
        async def scenario():
            import httpx2

            async with Server(http_app.build_app()) as srv:
                base = f"http://127.0.0.1:{srv.port}"
                async with httpx2.AsyncClient() as client:
                    return [
                        (await client.get(f"{base}{p}")).status_code
                        for p in ("/p/UPPER/mcp", "/p/../../etc/mcp", "/p//mcp")
                    ]

        assert all(code == 404 for code in run(scenario()))

    def test_health_and_index_are_open(self, vault):
        async def scenario():
            import httpx2

            async with Server(http_app.build_app(token="secret")) as srv:
                base = f"http://127.0.0.1:{srv.port}"
                async with httpx2.AsyncClient() as client:
                    health = await client.get(f"{base}/healthz")
                    index = await client.get(f"{base}/")
                    return health, index

        health, index = run(scenario())
        assert health.status_code == 200 and health.text == "ok"
        assert index.status_code == 200
        assert index.json()["connect"] == "/p/<project>/mcp"


class TestAuth:
    def test_missing_token_is_rejected(self, vault):
        async def scenario():
            import httpx2

            async with Server(http_app.build_app(token="secret")) as srv:
                async with httpx2.AsyncClient() as client:
                    return await client.post(srv.url("alpha"), json={})

        response = run(scenario())
        assert response.status_code == 401
        # No WWW-Authenticate: Claude would read it as an OAuth challenge and
        # fail with "Couldn't reach the MCP server" instead of an auth error.
        assert "www-authenticate" not in response.headers

    def test_wrong_token_is_rejected(self, vault):
        async def scenario():
            import httpx2

            async with Server(http_app.build_app(token="secret")) as srv:
                async with httpx2.AsyncClient() as client:
                    return await client.post(
                        srv.url("alpha"),
                        json={},
                        headers={"Authorization": "Bearer wrong"},
                    )

        assert run(scenario()).status_code == 401

    def test_correct_token_is_accepted(self, vault):
        async def scenario():
            async with Server(http_app.build_app(token="secret")) as srv:
                return await call(
                    srv.url("alpha"),
                    "get_project_brief",
                    headers={"Authorization": "Bearer secret"},
                )

        assert "no recorded history" in run(scenario())

    def test_no_token_configured_means_open(self, vault):
        async def scenario():
            async with Server(http_app.build_app(token=None)) as srv:
                return await call(srv.url("alpha"), "get_project_brief")

        assert "no recorded history" in run(scenario())
