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
claude mcp add context-vault -- python /path/to/context-vault/server.py
```

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

Single SQLite file at `~/.context-vault/vault.db` (override with the
`CONTEXT_VAULT_DB` env var). Decisions are never deleted, only superseded —
the log is the version history.
