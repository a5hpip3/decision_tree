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
claude mcp add -s user decisiontree -- python /path/to/context-vault/server.py
```

Installing once at user scope (`-s user`) is enough — the vault is scoped per
project automatically, so every project you work in gets its own history without
a per-project install.

Or add to `.mcp.json` in your project root:

```json
{
  "mcpServers": {
    "decisiontree": {
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
    "decisiontree-myproject": {
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

### Two ways to address a project

| Endpoint | Project comes from | Suits |
|---|---|---|
| `/p/<project>/mcp` | the URL, fixed | Claude Code, one repo per connector |
| `/mcp` (router) | a `project` argument on each call | chat, where one connector serves every project |

A connector is account-level configuration, so a pinned URL means one project
for every conversation that uses it. That is right for a repo and wrong for
chat, where the subject changes between messages. On the router, `list_projects`
returns the real names so the agent can ask which one rather than guess.

The two do not blur into each other. A pinned connector **refuses** a `project`
argument rather than honouring it — silently writing somewhere other than the
URL says is exactly the misfiling this design exists to prevent. On the router
an unrecognised name is refused with the known ones listed, because a typo is
far more likely than a new project; `create=true` starts one deliberately.

Empty vaults are demoted to a footnote in every list an agent sees. An empty
project is indistinguishable from one created by accident, and crowding the
suggestions with them makes a wrong choice more likely.

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
claude mcp add -s user decisiontree \
  -e CONTEXT_VAULT_HOSTED_URL=https://<your-host> \
  -- /path/to/.venv/bin/python /path/to/server.py
```

Then, in any repo you want on the shared history, ask for `connect_hosted`. It
writes `.mcp.json` at the repo root:

```json
{
  "mcpServers": {
    "decisiontree": {
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

**Export the token before connecting.** Claude Code expands `${VAR}` from its
own environment; if the variable is unset it loads the config anyway and sends
the literal text `Bearer ${CONTEXT_VAULT_TOKEN}`. The server detects that and
says so by name rather than failing as an unparseable JWT. Note that a
GUI-launched app does not read your shell profile — launch the client from a
shell where the variable is set, or use `claude mcp add -s local` with the
literal token instead (stored in `~/.claude.json`, never committed).

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
> steps.
>
> Connect it up: when the decision builds on an earlier one, pass
> `derives_from` with that decision's id. Give it a short `cluster` label —
> reuse the labels already in the project rather than inventing new ones — and
> set `source` to chat, code, pr or doc, plus `ref` for the PR, file:line or
> ticket it came from. A decision with no parent and no cluster is an isolated
> dot; the history is only useful as a connected tree.
>
> At the start of a session, call `get_project_brief` to load context.

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

## Decision context

Beyond the summary, reasoning and excerpt, each decision carries optional
context. All of it is optional — capture predates these fields and a required
argument would break every agent already logging.

| Field | Purpose |
|---|---|
| `derives_from` | Id of the decision this builds on. **This is what makes the history a tree** rather than a flat list |
| `cluster` | Short theme reused across the project ("Landing page", "Engine contracts") |
| `source` | Where it was decided: `chat`, `code`, `pr`, `doc` — a closed set, so it stays a usable filter |
| `ref` | The artifact: PR number, `file:line`, ticket id, document section |
| `author` | Who made the call, when known |

`derives_from` is validated against the current project and rejected if it
points at nothing — an edge to a missing decision is worse than no edge.
`source` is rejected unless it is one of the four, because a mistyped surface
would silently vanish from the filter rather than fail. `supersede_decision`
inherits the cluster of the decision it replaces.

`cluster` and `source` appear in `list_decisions` output so an agent can see
the labels already in use before inventing another.

### How agents learn about these fields

Nothing is required beyond `summary`, `reasoning` and `excerpt` — a required
argument would break every existing caller, and it could only force a field to
be *filled*, not filled well. The fields are encouraged in four places instead:

1. **The tool description** — the full docstring, delivered on `tools/list`.
2. **The server `instructions`**, sent in the initialize handshake.
3. **`setup`**, as a tool and a prompt.
4. **The response to `log_decision` itself**, which is the one that arrives
   while the agent can still act:

```
Logged decision #13: Layer 6 caching…
  No cluster set — labels already in use: Engine contracts, Landing page
  No derives_from set — if this builds on one of these, say which: #12 …, #11 …
```

That hint only appears when it has something concrete to offer. The first
decision in a project has no siblings to derive from and no labels to reuse, so
it gets the plain confirmation. Retired decisions are never offered as parents.

## Read API

A Railway volume mounts to exactly one service, so the web front-end cannot
open these SQLite files itself — it reads them through a JSON API on this
service.

| Route | Returns |
|---|---|
| `GET /api/projects` | every hosted project with per-status counts, cluster labels and last activity |
| `GET /api/projects/{name}/decisions` | that project's decisions, plus the graph edges |

Read-only by design: writes stay on the MCP tools, where the docstrings that
shape how agents log live. Anything other than `GET` returns `405`.

Edges are computed server-side, so the client never needs to know that a
derivation and a reversal are stored in different columns:

```json
{"from": 7, "to": 3, "kind": "derives"}
{"from": 9, "to": 7, "kind": "supersedes"}
```

Retired decisions are excluded unless `?include_retired=true`. An edge whose
target was filtered out is dropped rather than left dangling.

Only hosted vaults under `$CONTEXT_VAULT_HOME/remote/` are served — local
vaults are keyed to somebody's laptop and never exposed. An unknown project
name returns `404` **without creating a vault**, which matters because opening
one would otherwise conjure an empty project for every typo'd URL.

Auth reuses `CONTEXT_VAULT_TOKEN`. A JWT is accepted only when a single fixed
`CONTEXT_VAULT_OAUTH_AUDIENCE` is configured, since these routes are
cross-project and have no per-project resource URL to check an audience
against.

## Web front-end (`web/`)

A separate Railway service, because a volume mounts to one service and this one
holds no data — it reads the vault through the [read API](#read-api).

```
browser --session cookie--> decisiontree-web --bearer token--> vault API
```

The vault token lives only in the web service. It is never sent to the browser,
never embedded in a page, and the proxy forwards only `GET` to two fixed path
shapes — an open proxy carrying a credential would be worse than no auth at all.

| Env var | Purpose |
|---|---|
| `AUTH0_ISSUER` | e.g. `https://decisiontree.us.auth0.com/` |
| `AUTH0_CLIENT_ID` / `AUTH0_CLIENT_SECRET` | a **Regular Web Application** in Auth0 (confidential client) |
| `VAULT_API_URL` | the vault service's base URL |
| `VAULT_API_TOKEN` | `CONTEXT_VAULT_TOKEN` from the vault service |
| `SESSION_SECRET` | signs session cookies |
| `WEB_ALLOWED_EMAILS` | comma-separated addresses permitted to sign in |
| `SESSION_INSECURE_COOKIE` | `true` only for local http development |

**`WEB_ALLOWED_EMAILS` fails closed.** With it unset, every sign-in is refused.
An Auth0 tenant will happily let a stranger sign up through a social
connection, and what is behind this door is decision reasoning and verbatim
transcript excerpts. A refused login reports the address it refused, so a wrong
account is distinguishable from a missing allowlist entry.

Sessions are signed, not encrypted — the browser can read the payload — so the
session holds identity only and is tested to contain no credential.

Auth0 needs the callback URL `https://<web-host>/auth/callback` registered on
that application.

### The Context Graph

`web/static/` is the ported design: a left rail (projects, search, status /
surface / cluster filters) and a force-directed canvas with pan, zoom and a
414px detail panel.

`layout()` in `app.js` is ported from the design's `graph()` and is faithful on
purpose — the same virtual `__root` → cluster → decision hierarchy, the same
spring weights and ideal lengths, the same 460 cooling iterations. Those
constants are what make it look like the design rather than a generic force
graph.

Two adaptations the real data forced:

- **Cards are a fixed 214×96**, the size the layout reserves. The design's
  titles are one-liners; real summaries are paragraphs, and an auto-height card
  overlaps its neighbours. Titles clamp to three lines and the full text lives
  in the panel.
- **Reasoning and excerpts wrap with `overflow-wrap:anywhere`**, because they
  carry file paths, identifiers and stack traces with nothing to break on.

Fields the vault does not carry yet are hidden rather than faked: with no
clusters or sources, those rail sections simply do not render. A filter over a
column that is always null is worse than no filter.

**Nodes can be dragged.** The force layout is a starting arrangement, not a
verdict — drag a card and it stays where you put it, with its edges following.
A drag under 4px counts as a click, so selecting a node still works. Positions
are kept per project in `localStorage`, because an arrangement that vanished on
the next filter change or reload would not be worth making. `RESET` appears
once anything has moved and returns to the computed layout.

During a drag the card and its edges are moved directly rather than
re-rendering: a full repaint per mouse move would rebuild the DOM, and
re-running the force simulation would fight the drag by pulling the node back.

Note the front-end has no automated tests — it is verified by rendering real
vault data in a browser.

### Deploying the web service

```bash
railway up web --path-as-root --ci --service decisiontree-web
```

**`--path-as-root` is not optional.** `railway up` archives from the git root,
not the working directory, so running it from inside `web/` uploads the whole
repository and Railway builds the *root* Dockerfile — deploying a second copy
of the vault service under the web service's domain. It passes its health check
and looks fine; the giveaway is `/login` returning `Not Found` and `/` serving
the vault's JSON index.

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
