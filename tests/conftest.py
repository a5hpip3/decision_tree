"""Shared fixtures for the Context Vault suite.

Every test runs against a fake vault home and fabricated project roots, so
nothing here can read or write a real vault under ~/.context-vault/.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402


def unwrap(tool):
    """The plain callable behind an @mcp.tool()-decorated function.

    mcp 2.x registers the tool and hands back the original function; other SDK
    versions return a wrapper keeping it on `.fn`. server.py supports both, so
    the tests have to as well.
    """
    return getattr(tool, "fn", tool)


class Vault:
    """Builds throwaway projects and points the server at a throwaway home."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, home: Path):
        self._tmp = tmp_path
        self._mp = monkeypatch
        self.home = home

    def project(
        self,
        name: str = "repo",
        *,
        git: bool = True,
        parent: Path | None = None,
        git_as_file: bool = False,
    ) -> Path:
        """Create a project root.

        The git marker is fabricated rather than shelling out to `git init` —
        project_root() only tests that `.git` exists. `git_as_file=True` mimics
        a worktree or submodule, where `.git` is a file, not a directory.
        (test_fabricated_git_marker_matches_real_git_init checks the fake is
        faithful to the real thing.)
        """
        root = (parent or self._tmp / "work") / name
        root.mkdir(parents=True)
        if git:
            marker = root / ".git"
            if git_as_file:
                marker.write_text("gitdir: /elsewhere/.git/worktrees/wt\n")
            else:
                marker.mkdir()
        return root.resolve()

    def enter(self, path: Path) -> Path:
        """chdir into a directory, as an MCP client would launch the server."""
        self._mp.chdir(path)
        return Path(path).resolve()

    @property
    def legacy_db(self) -> Path:
        return self.home / "vault.db"

    def create_legacy(self) -> Path:
        """Materialise the pre-per-project shared vault."""
        self.home.mkdir(parents=True, exist_ok=True)
        self.legacy_db.touch()
        return self.legacy_db


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Vault:
    home = tmp_path / "home" / ".context-vault"
    monkeypatch.setattr(server, "VAULT_HOME", home)
    monkeypatch.setattr(server, "LEGACY_DB", home / "vault.db")
    monkeypatch.delenv("CONTEXT_VAULT_DB", raising=False)
    monkeypatch.delenv("CONTEXT_VAULT_PROJECT", raising=False)

    # Park the cwd somewhere with no git root above it so every test has to opt
    # into a project explicitly and none inherit the checkout running the suite.
    neutral = tmp_path / "neutral"
    neutral.mkdir()
    monkeypatch.chdir(neutral)

    return Vault(tmp_path, monkeypatch, home)
