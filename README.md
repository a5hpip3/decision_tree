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

MCP hosts don't inherit your shell, so point `command` at an interpreter that
actually has the SDK installed (a venv's `bin/python`), not bare `python`.

### Claude Desktop

Desktop runs local stdio servers too, but it doesn't launch them from a project
directory — so `project_root()` would fall back to whatever cwd the app uses and
every conversation would share one catch-all vault. Pin the project explicitly,
one entry per project you want to track:

```json
{
  "mcpServers": {
    "context-vault-myproject": {
      "command": "/path/to/.venv/bin/python",
      "args": ["/path/to/context-vault/server.py"],
      "env": { "CONTEXT_VAULT_PROJECT": "/path/to/myproject" }
    }
  }
}
```

## HTTP transport — one connector URL per project

`server.py` speaks stdio, where the project is the local git root. For chat
interfaces there is no project filesystem, so `http_app.py` serves the same five
tools over streamable HTTP and takes the project from the URL:

```
https://<host>/p/<project>/mcp
```

The tool signatures are unchanged — an agent can't misname the project, because
it never names it. You add a connector per project instead.

```bash
pip install -r requirements.txt
CONTEXT_VAULT_TOKEN=$(openssl rand -hex 32) python http_app.py   # :8000
```

| Env var | Purpose |
|---|---|
| `CONTEXT_VAULT_HOME` | Where vaults live (set to the mounted volume in production) |
| `CONTEXT_VAULT_TOKEN` | Bearer token required on `/p/...`; unset means **no auth** |
| `CONTEXT_VAULT_ALLOWED_HOSTS` | Hostnames this is served on, comma-separated; `*` disables the check |
| `PORT` / `HOST` | Listen address (default `0.0.0.0:8000`) |

**`CONTEXT_VAULT_ALLOWED_HOSTS` is required for any hosted deployment.** The MCP
SDK's DNS-rebinding protection allows only `127.0.0.1` by default, so without it
every request returns `421 Invalid Host header` — including the initialize
handshake, which makes the server look broken rather than misconfigured.

Project names are validated against `^[a-z0-9][a-z0-9._-]{0,63}$` and rejected
with a 404 if they don't match — never coerced, since the name becomes a
filename. `/healthz` and `/` stay open for platform health checks.

Served **stateless**: every tool call is an independent SQLite transaction, so
there's no session state worth keeping, and it avoids session affinity if this
ever runs on more than one replica.

### Deploying

```bash
railway init --name context-vault
railway add --service context-vault \
  --variables "CONTEXT_VAULT_TOKEN=$(openssl rand -hex 32)" \
  --variables "CONTEXT_VAULT_HOME=/data"
railway service link context-vault
railway volume add --mount-path /data     # required — without it, redeploys wipe history
railway up --ci
railway domain --port 8000                # then feed the domain back in:
railway variables --set "CONTEXT_VAULT_ALLOWED_HOSTS=<your-domain>"
railway up --ci
```

The `Dockerfile` sets `CONTEXT_VAULT_HOME=/data`, so the volume mount path is
what makes vaults survive a redeploy. It deliberately has no `VOLUME`
instruction — Railway rejects the build with "use Railway Volumes" and manages
the mount itself.

The domain is needed *before* the server will answer, but only exists *after*
the first deploy, so the sequence above deploys twice on purpose.

### OAuth with Auth0

Context Vault never issues tokens. It is a *protected resource*: Auth0 is the
authorization server, and this server verifies the JWTs Auth0 mints.

| Env var | Purpose |
|---|---|
| `CONTEXT_VAULT_OAUTH_ISSUER` | Auth0 tenant URL, e.g. `https://you.us.auth0.com/`. Setting it turns OAuth on |
| `CONTEXT_VAULT_OAUTH_AUDIENCE` | Pin the expected `aud`. Leave unset to require the per-project URL |
| `CONTEXT_VAULT_OAUTH_SCOPES` | Space-separated scopes every token must carry |

With OAuth on, the server:

- serves RFC 9728 metadata at `/.well-known/oauth-protected-resource/p/<project>/mcp`,
  whose `resource` is the exact URL the user entered (Claude rejects it otherwise)
- answers unauthenticated calls with `401` +
  `WWW-Authenticate: Bearer resource_metadata="…"`, pointing straight at that
  document so the client doesn't have to probe for it
- verifies RS256 signatures against the tenant JWKS, checking `iss`, `aud`,
  `exp`, and requiring `exp`/`iat`/`iss`/`aud` to be present

`CONTEXT_VAULT_TOKEN` still works alongside it — either credential is accepted,
so Claude Code can keep using the static token while claude.ai uses OAuth.

#### Auth0 tenant setup

1. **Settings → Advanced**: enable **Resource Parameter Compatibility Profile**.
   Auth0 natively uses an `audience` parameter, while MCP clients send RFC 8707
   `resource`; without this profile Auth0 ignores it and the audience is wrong.
   Enable **Include Issuer in Authorization Responses** too.
2. **Applications → APIs → Create API** with the identifier set to the connector
   URL, e.g. `https://<host>/p/decision-tree/mcp`. Signing algorithm RS256.
3. **Applications → Create Application**, type *Native* or *SPA* (a public
   client, so PKCE and no secret). Add the callback URL
   `https://claude.ai/api/mcp/auth_callback`. Copy the Client ID.
4. In Claude's **Add custom connector** dialog, paste that Client ID into
   **OAuth Client ID** and leave the secret empty.

> **One API per project, or one for all?** Auth0's docs don't state whether the
> `resource` URI is matched to an API identifier exactly or by prefix, and it
> decides the answer. If the match is exact you need one Auth0 API per connector
> URL — leave `CONTEXT_VAULT_OAUTH_AUDIENCE` unset and the per-project URL is
> required as the audience. If one API can cover every project, set
> `CONTEXT_VAULT_OAUTH_AUDIENCE` to that identifier instead. Set up the first
> project and connect it; which case you're in becomes obvious immediately.

### Auth and claude.ai connectors

Anthropic supports several [connector auth types](https://claude.com/docs/connectors/building/authentication).
This server targets two of them, and OAuth is **not** required:

| Mode | How | Status |
|---|---|---|
| `none` | leave `CONTEXT_VAULT_TOKEN` unset | supported; anyone with the URL can read and write the vault |
| `static_headers` | set `CONTEXT_VAULT_TOKEN`, admin enters `Authorization: Bearer <token>` when adding the connector | beta |

OAuth (`oauth_dcr` / `oauth_cimd`) is not implemented. It would be the right
move for a multi-user deployment, since `static_headers` shares one credential
across the whole organization rather than identifying individual users.

The `401` response deliberately omits `WWW-Authenticate`. Claude treats `401` +
`WWW-Authenticate: Bearer` as an OAuth challenge and starts hunting for
protected-resource metadata, so including it turns a simple bad-token error into
a misleading "Couldn't reach the MCP server".

Never put the token in the URL — Anthropic's docs and the MCP spec both
prohibit credentials in query strings, and this server never reads one.

## Onboarding

Run `setup` — as a tool ("run context vault setup") or, in Claude Code and
Desktop, as the `/setup` prompt. It reports what actually matters before
anything is captured:

```
Connected over : local (stdio)
Project        : fresh-project
                 (from the git root directory name)
Writing to     : ~/.context-vault/projects/fresh-project-1e083d.db
Status         : 0 active decision(s), 0 total
```

followed by four steps: confirm the project, choose local or hosted, paste the
capture instruction, verify. A vault with nothing in it also appends a one-line
pointer to `setup` on the first `get_project_brief`.

Leading with the resolved project is deliberate. The one real failure mode seen
so far is a client pointed at the wrong project — decisions land in a vault that
looks fine until you read it. Naming it up front is cheaper than moving and
retiring them afterwards.

### Connecting a repo to the hosted vault

Install the stdio server once at user scope and it is live in every repo, which
means it can configure the hosted connection for you — no URL or token to copy:

```bash
claude mcp add -s user context-vault \
  -e CONTEXT_VAULT_HOSTED_URL=https://<your-host> \
  -- /path/to/.venv/bin/python /path/to/server.py
```

Then, in any repo you want on the shared history, ask for `connect_hosted`. It
writes `.mcp.json` at the repo root:

```json
{
  "mcpServers": {
    "context-vault": {
      "type": "http",
      "url": "https://<your-host>/p/<project>/mcp",
      "headers": { "Authorization": "Bearer ${CONTEXT_VAULT_TOKEN}" }
    }
  }
}
```

The token is a `${VAR}` reference, not a literal — Claude Code expands it at
load time, so **the file is safe to commit** and teammates get the server when
they open the repo. Export `CONTEXT_VAULT_TOKEN` once in your shell profile.

Existing servers and unrelated keys in an existing `.mcp.json` are preserved,
re-running is idempotent, and a malformed file is left untouched rather than
overwritten. Project-scoped servers sit at `⏸ Pending approval` until you run
`claude` in the repo and accept it — a repo cannot approve its own servers.

Connecting to hosted does not move or delete the repo's local vault; that
history stays where it is.

### Naming a project

`name_project` writes `.context-vault` at the repo root:

```json
{ "project": "rapid_manufacturing" }
```

That name titles briefs and is the name to use in a hosted connector URL. It
does **not** move an existing local vault — local history stays keyed to the
directory's absolute path, so naming a project later can never orphan
decisions already logged. Commit the file to share the name with the team.

Called with no argument, `name_project` asks interactively via MCP elicitation
where the client supports it, and otherwise explains how to set it in text —
which is the common path, since few clients implement elicitation today.

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
| `retire_decision` | Mark a decision as filed against the wrong project |
| `list_decisions` | Timeline view (pass `include_superseded=true` for history) |
| `get_decision` | Full record incl. the citation excerpt |
| `get_project_brief` | The "catch me up" view — all active decisions |
| `setup` | Where am I connected, and what's left to configure |
| `name_project` | Pin this repo's canonical name (writes `.context-vault`) |
| `connect_hosted` | Point this repo at the hosted vault (writes `.mcp.json`) |

All six operate on the current project's vault only — see [Storage](#storage).

**Supersede vs retire.** A superseded decision was real and then changed — it
stays part of the project's story. A *retired* decision never belonged here at
all, typically because a client was pointed at the wrong project URL. Retiring
drops it from the brief and the default timeline while keeping the full record,
with a reason attached, under `include_superseded=true`. Neither deletes
anything; the log remains append-only.

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
