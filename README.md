# Context Vault — Phase 1: Capture Experiment

An MCP server that lets your coding agent log project decisions (with reasoning
and transcript citations) as you work. This is the two-week capture experiment:
before building distillation or sharing, find out whether agents reliably call
`log_decision` at the right moments.

## Setup

Requires Python 3.10+.

```bash
pip install mcp
```

### Claude Code

```bash
claude mcp add -s user context-vault -- python /path/to/context-vault/server.py
```

Installing once at user scope (`-s user`) is enough — the vault is scoped per
project automatically, so every project you work in gets its own history without
a per-project install.

Or add to `.mcp.json` in your project root:

```json
{
  "mcpServers": {
    "context-vault": {
      "command": "python",
      "args": ["/path/to/context-vault/server.py"]
    }
  }
}
```

### Cursor (`~/.cursor/mcp.json`) — same JSON shape as above.

## The one instruction that makes it work

Add this to your project's `CLAUDE.md` (or Cursor rules):

> When we make, change, or reverse a meaningful project decision —
> architecture, stack, library choice, approach ruled out, direction change —
> call `log_decision` with the summary, the reasoning, and a verbatim excerpt
> of the conversation where it happened. If it reverses an earlier decision,
> use `supersede_decision` instead. Do not log routine actions or debugging
> steps. At the start of a session, call `get_project_brief` to load context.

## Tools

| Tool | Purpose |
|---|---|
| `log_decision` | Capture a decision + reasoning + transcript citation |
| `supersede_decision` | Record a reversal; old decision kept in timeline |
| `list_decisions` | Timeline view (pass `include_superseded=true` for history) |
| `get_decision` | Full record incl. the citation excerpt |
| `get_project_brief` | The "catch me up" view — all active decisions |

All five operate on the current project's vault only — see [Storage](#storage).

## Tests

```bash
pip install pytest
pytest
```

50 tests, well under a second. They run against a fake vault home and fabricated
project roots, so they can never touch a real vault in `~/.context-vault/`.

The suite is weighted toward the two things that carry the design: which project
a call resolves to (`TestProjectRoot`, `TestVaultPath`, `TestProjectIsolation`)
and the append-only supersede invariant (`TestSupersede`). It was checked against
deliberate mutations — breaking git-root walking, dropping the path hash from the
slug, removing the double-supersede guard, reverting to a single global vault,
and dropping worktree support each turn it red.

## Running the experiment

Work normally for two weeks. Then audit:

1. **Recall** — read back through your sessions: how many real decisions were
   made vs. how many got logged? (`list_decisions` with superseded included)
2. **Precision** — of what got logged, how much is noise?
3. **Citation quality** — do the transcript excerpts actually support the
   summaries?

If recall is poor, the finding is that capture needs a post-hoc distiller pass
over session transcripts, not just in-flow tool calls — that reshapes Phase 2.

## Storage

**One vault per project.** Each project gets its own SQLite file under
`~/.context-vault/projects/`, named `<project>-<hash>.db` — the name half is so
you can find it by eye, the hash half (of the absolute path) is what keeps two
repos called `api` apart. `get_project_brief` only ever returns decisions made
in the current project.

Decisions are never deleted, only superseded — the log is the version history.

### Which project am I in?

Resolved at every call, in this order:

1. `CONTEXT_VAULT_PROJECT` — set it to pin the server to one project explicitly.
2. The nearest git root, walking up from the working directory. Launching the
   server from `my-repo/src/api/` uses the same vault as `my-repo/`.
3. The working directory, if there's no git root above it.

`CONTEXT_VAULT_DB` still overrides everything and pins one exact file, bypassing
per-project scoping — useful for tests, or for reading a vault that lives
somewhere unusual.

### Upgrading from the shared vault

Earlier versions kept every project's decisions in one `~/.context-vault/vault.db`.
That file is left untouched, so a fresh per-project vault will start out empty.
To carry the old history into a project:

```bash
# read the old shared vault without moving it
CONTEXT_VAULT_DB=~/.context-vault/vault.db python /path/to/context-vault/server.py

# or adopt it as this project's vault (run from the project root)
python - <<'PY'
import shutil, sys; sys.path.insert(0, "/path/to/context-vault")
import server
server.db_path().parent.mkdir(parents=True, exist_ok=True)
shutil.copy(server.LEGACY_DB, server.db_path())
print("adopted as", server.db_path())
PY
```

Each vault also records its own project path in a `meta` table, so you can
identify a stray `.db` file:

```bash
sqlite3 ~/.context-vault/projects/some-repo-a3f9c1.db \
  "SELECT value FROM meta WHERE key='project_path'"
```
