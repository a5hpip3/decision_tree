"""First-run setup: say where decisions will land, before any are logged.

The failure this exists to prevent is silent misrouting — a client pointed at
one project while you work in another, with nothing on screen to reveal it
until the wrong vault fills up. So the setup report leads with the resolved
project and the exact file or URL being written to.

Naming is interactive where the client supports elicitation and falls back to
plain instructions where it doesn't, which is most clients today.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import server

# A repo can declare its canonical name here. It sets the *label* and the
# hosted URL to use — never the local vault filename, which stays keyed to the
# absolute path so declaring a name later cannot orphan existing history.
DECLARATION_FILE = ".context-vault"

CAPTURE_INSTRUCTION = """When we make, change, or reverse a meaningful project \
decision — architecture, stack, library choice, approach ruled out, direction \
change — call `log_decision` with the summary, the reasoning, and a verbatim \
excerpt of the conversation where it happened. If it reverses an earlier \
decision, use `supersede_decision` instead. Do not log routine actions or \
debugging steps. At the start of a session, call `get_project_brief` to load \
context."""


MCP_CONFIG_FILE = ".mcp.json"
HOSTED_URL_ENV = "CONTEXT_VAULT_HOSTED_URL"
TOKEN_ENV = "CONTEXT_VAULT_TOKEN"


def declaration_path(root: Path) -> Path:
    return root / DECLARATION_FILE


def normalise_host(raw: str) -> str:
    """Accept a bare hostname or a full URL, with or without an /p/... path."""
    host = raw.strip().rstrip("/")
    if not host:
        return ""
    if "://" not in host:
        host = f"https://{host}"
    # Tolerate someone pasting a whole connector URL.
    return re.sub(r"/p/[^/]+/mcp$", "", host).rstrip("/")


def hosted_entry(base: str, project: str) -> dict:
    """The .mcp.json entry for this project's hosted vault.

    The token is written as a ${VAR} reference, never inlined — .mcp.json is
    meant to be committed, and Claude Code expands the variable at load time.
    """
    return {
        "type": "http",
        "url": f"{base}/p/{project}/mcp",
        "headers": {"Authorization": f"Bearer ${{{TOKEN_ENV}}}"},
    }


def merge_mcp_config(root: Path, entry: dict, name: str = "context-vault") -> tuple[Path, str]:
    """Add or update one server in the repo's .mcp.json, preserving the rest.

    Returns (path, action) where action is created, updated or unchanged. Other
    servers in the file are never touched — a repo may already depend on them.
    """
    path = root / MCP_CONFIG_FILE
    config: dict = {}
    if path.exists():
        try:
            config = json.loads(path.read_text() or "{}")
        except ValueError as exc:
            raise ValueError(f"{path} is not valid JSON ({exc}); leaving it alone") from exc
        if not isinstance(config, dict):
            raise ValueError(f"{path} does not contain a JSON object; leaving it alone")

    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError(f"{path} has a non-object mcpServers key; leaving it alone")

    previous = servers.get(name)
    if previous == entry:
        return path, "unchanged"

    servers[name] = entry
    path.write_text(json.dumps(config, indent=2) + "\n")
    return path, ("updated" if previous is not None else "created")


def read_declared_name(root: Path) -> str | None:
    """The project name this repo declares, if any."""
    path = declaration_path(root)
    if not path.exists():
        return None
    try:
        raw = path.read_text().strip()
    except OSError:
        return None
    if not raw:
        return None
    if raw.startswith("{"):
        try:
            raw = str(json.loads(raw).get("project", "")).strip()
        except (ValueError, AttributeError):
            return None
    return raw if server.PROJECT_NAME.match(raw) else None


def write_declared_name(root: Path, name: str) -> Path:
    path = declaration_path(root)
    path.write_text(json.dumps({"project": name}, indent=2) + "\n")
    return path


def suggest_name(root: Path) -> str:
    """A valid hosted project name derived from the directory name."""
    candidate = re.sub(r"[^a-z0-9._-]+", "-", root.name.lower()).strip("-.")
    candidate = candidate[:64] or "project"
    if not server.PROJECT_NAME.match(candidate):
        candidate = f"p{candidate}"[:64]
    return candidate


def vault_state() -> dict:
    """Everything the report needs about where this call will write."""
    remote = server.REMOTE_PROJECT.get()
    root = None if remote else server.project_root()
    declared = read_declared_name(root) if root else None

    with server.closing(server.connect()) as conn:
        total = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        active = conn.execute(
            f"SELECT COUNT(*) FROM decisions WHERE {server.ACTIVE}"
        ).fetchone()[0]

    return {
        "hosted": remote is not None,
        "project": remote or (declared or (root.name if root else "unknown")),
        "root": root,
        "declared": declared,
        "suggested": suggest_name(root) if root else None,
        "vault": server.db_path(),
        "total": total,
        "active": active,
    }


def report(state: dict) -> str:
    """The setup walkthrough, tailored to how this client is connected."""
    lines = ["# Context Vault — setup", ""]

    transport = "hosted HTTP" if state["hosted"] else "local (stdio)"
    lines += [
        "```",
        f"Connected over : {transport}",
        f"Project        : {state['project']}",
    ]
    if not state["hosted"]:
        source = (
            f"declared in {DECLARATION_FILE}"
            if state["declared"]
            else "from the git root directory name"
        )
        lines.append(f"                 ({source})")
        lines.append(f"Working dir    : {state['root']}")
    lines += [
        f"Writing to     : {state['vault']}",
        f"Status         : {state['active']} active decision(s), "
        f"{state['total']} total",
        "```",
        "",
    ]

    lines += ["## 1. Is that the right project?", ""]
    if state["hosted"]:
        lines += [
            f"Decisions logged here go to **{state['project']}**, taken from the "
            "connector URL. If that is not this project, change the "
            "`/p/<project>/mcp` segment in the connector settings before "
            "logging anything — decisions filed against the wrong project have "
            "to be moved and retired afterwards.",
        ]
    else:
        lines += [
            f"The project is resolved from the git root, so every repo gets its "
            f"own vault automatically. To pin a different name, call "
            f"`name_project`, or create a `{DECLARATION_FILE}` file containing "
            f'`{{"project": "your-name"}}`.',
        ]
    lines.append("")

    lines += ["## 2. Where should the history live?", ""]
    if state["hosted"]:
        lines += [
            "This client already writes to the shared hosted vault, so the same "
            "history is visible from every other client pointed at the same URL.",
        ]
    else:
        name = state["declared"] or state["suggested"]
        lines += [
            "Right now this is a **local** vault on this machine only.",
            "",
            "To share one history with the Claude apps instead, call "
            f"`connect_hosted`. It writes `{MCP_CONFIG_FILE}` for this repo "
            f"pointing at `/p/{name}/mcp`, with the token as a "
            f"`${{{TOKEN_ENV}}}` reference rather than inlined, so the file is "
            "safe to commit and teammates get the server on opening the repo.",
            "",
            f"Only this repo is affected; every other one keeps capturing "
            "locally.",
        ]
    lines.append("")

    lines += [
        "## 3. Turn on capture — required",
        "",
        "Nothing is recorded automatically. Add this to the project's "
        "`CLAUDE.md` (or Cursor rules) so the agent knows when to log:",
        "",
        "> " + CAPTURE_INSTRUCTION.replace("\n", "\n> "),
        "",
        "## 4. Verify",
        "",
        "Ask for `get_project_brief`. It should name "
        f"**{state['project']}** and list what is in the vault.",
    ]
    return "\n".join(lines)


def nudge(state: dict) -> str:
    """Appended to an empty vault's response, once, to point at setup."""
    if state["total"]:
        return ""
    return (
        f"\n\n(First run for {state['project']} — call `setup` to confirm this "
        "is the right project and see how to turn on capture.)"
    )
