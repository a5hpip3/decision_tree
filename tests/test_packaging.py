"""Guards that the container ships every module the app imports.

A module missing from the Dockerfile's COPY passes every other test — the
failure only appears when the container starts and the import blows up, which
looks like a failed health check rather than a missing file. That is exactly
how oauth.py shipped broken once.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINTS = ("http_app.py", "server.py")


def local_modules(path: Path) -> set[str]:
    """Top-level module names imported by `path` that are local .py files."""
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return {n for n in names if (ROOT / f"{n}.py").exists()}


def transitive_local_modules() -> set[str]:
    seen: set[str] = set()
    queue = [Path(e).stem for e in ENTRYPOINTS]
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        queue.extend(local_modules(ROOT / f"{name}.py") - seen)
    return seen


def copied_files() -> set[str]:
    dockerfile = (ROOT / "Dockerfile").read_text()
    copied: set[str] = set()
    for line in dockerfile.splitlines():
        match = re.match(r"\s*COPY\s+(.+?)\s+\./?\s*$", line)
        if match:
            copied.update(part.strip() for part in match.group(1).split())
    return copied


def test_dockerfile_copies_every_imported_module():
    required = {f"{name}.py" for name in transitive_local_modules()}
    missing = required - copied_files()
    assert not missing, (
        f"Dockerfile does not COPY {sorted(missing)} — the container will "
        "fail at import time, which surfaces as a failed health check."
    )


def test_entrypoints_are_themselves_copied():
    assert set(ENTRYPOINTS) <= copied_files()


@pytest.mark.parametrize("name", sorted(transitive_local_modules()))
def test_module_is_importable_without_optional_env(name, monkeypatch):
    """Import must not depend on deployment-specific configuration."""
    for var in (
        "CONTEXT_VAULT_TOKEN",
        "CONTEXT_VAULT_OAUTH_ISSUER",
        "CONTEXT_VAULT_ALLOWED_HOSTS",
        "CONTEXT_VAULT_HOME",
        "CONTEXT_VAULT_DB",
        "CONTEXT_VAULT_PROJECT",
    ):
        monkeypatch.delenv(var, raising=False)
    __import__(name)


def test_requirements_cover_third_party_imports():
    """Every third-party top-level import appears in requirements.txt."""
    requirements = (ROOT / "requirements.txt").read_text().lower()
    stdlib_ok = {"jwt": "pyjwt", "anyio": "mcp", "starlette": "starlette"}
    for name in transitive_local_modules():
        for imported in local_modules_third_party(ROOT / f"{name}.py"):
            pkg = stdlib_ok.get(imported, imported)
            assert pkg.lower() in requirements, (
                f"{imported} imported by {name}.py but no matching entry in "
                "requirements.txt"
            )


def local_modules_third_party(path: Path) -> set[str]:
    """Imported top-level names that are neither stdlib nor local files."""
    import sys

    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return {
        n
        for n in names
        if not (ROOT / f"{n}.py").exists() and n not in sys.stdlib_module_names
    }
