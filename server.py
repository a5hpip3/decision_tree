"""Context Vault — Phase 1 capture experiment.

A minimal MCP server that lets a coding agent log project decisions (with
reasoning and a verbatim transcript excerpt) as they happen. Decisions are
never deleted, only superseded — the append-only log is the version history.

Storage: one SQLite file per project under ~/.context-vault/projects/, so a
vault only ever surfaces the decisions made in the project it belongs to.

Run over stdio (the default) and the project is the local git root. Run over
HTTP via http_app.py and there is no filesystem to read, so the project comes
from the request URL instead — see REMOTE_PROJECT below.
"""

import contextvars
import hashlib
import os
import re
import sqlite3
import dataclasses
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as Server

mcp = Server(
    name="decisiontree",
    instructions=(
        "Log meaningful project decisions (architecture, stack, approach "
        "ruled out, direction change) with log_decision as they happen. "
        "Set derives_from when a decision builds on an earlier one, and give "
        "it a cluster label reused across the project — an unconnected, "
        "unlabelled decision is far less useful later. "
        "Use supersede_decision when a decision reverses an earlier one. "
        "Call get_project_brief at the start of a session to load context. "
        "The vault is scoped to the current project — it holds only this "
        "project's decisions, and other projects cannot see them."
    ),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    summary        TEXT NOT NULL,
    reasoning      TEXT NOT NULL,
    excerpt        TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    supersedes     INTEGER REFERENCES decisions(id),
    superseded_by  INTEGER REFERENCES decisions(id),
    retired_at     TEXT,
    retired_reason TEXT,
    derives_from   INTEGER REFERENCES decisions(id),
    cluster        TEXT,
    source         TEXT,
    ref            TEXT,
    author         TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Who may reach this project. Written and seeded now, enforced later: getting
-- ownership recorded before anything depends on it means the change that turns
-- on enforcement cannot also be the change that decides who owns what.
--
-- A row can exist before its subject is known. An owner invites an email
-- address, and the token subject is filled in the first time that person
-- actually signs in — so `subject` is null until claimed, and `email` is null
-- for anyone added by subject alone.
CREATE TABLE IF NOT EXISTS members (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    subject  TEXT UNIQUE,
    email    TEXT UNIQUE,
    role     TEXT NOT NULL,
    added_at TEXT NOT NULL,
    added_by TEXT
);
"""

ROLES = ("owner", "member", "viewer")

# What each role can reach. Superseding is deliberately not an owner-only act:
# replacing a decision is how a team's thinking moves on, and needing
# permission to do it would push people back into arguing in chat. Retiring
# says a decision should never have been recorded at all, which on somebody
# else's entry is an owner's call.
RANK = {"viewer": 1, "member": 2, "owner": 3}
NEEDS = {"read": 1, "write": 2, "admin": 3}

# Columns added after the first release. CREATE TABLE IF NOT EXISTS silently
# leaves an existing table alone, so they have to be added explicitly.
ADDED_COLUMNS = (
    ("retired_at", "TEXT"),
    ("retired_reason", "TEXT"),
    ("derives_from", "INTEGER"),
    ("cluster", "TEXT"),
    ("source", "TEXT"),
    ("ref", "TEXT"),
    ("author", "TEXT"),
)

# Where the decision was made. Kept small on purpose: a free-text field would
# fragment into synonyms and stop being a usable filter.
SOURCES = ("chat", "code", "pr", "doc")

MAX_CLUSTER = 60
MAX_REF = 200
MAX_AUTHOR = 80

# A decision is shown by default only while it is neither replaced nor retired.
ACTIVE = "superseded_by IS NULL AND retired_at IS NULL"

VAULT_HOME = Path(
    os.environ.get("CONTEXT_VAULT_HOME") or Path.home() / ".context-vault"
).expanduser()
LEGACY_DB = VAULT_HOME / "vault.db"

# Set per-request by http_app when serving over HTTP, where there is no project
# filesystem to inspect. Unset under stdio, which resolves the project locally.
REMOTE_PROJECT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "remote_project", default=None
)

# True when the request arrived on the project-less router endpoint, where the
# caller names the project per call instead of the URL fixing it. A pinned
# connector cannot be talked out of its project; the router has none to start
# with, so every tool has to be told.
ROUTER: contextvars.ContextVar[bool] = contextvars.ContextVar("router", default=False)


@dataclasses.dataclass(frozen=True)
class Identity:
    """Who is making this call, according to a credential the server verified.

    `subject` is the token's `sub`: stable, opaque, and always present.
    `email` is only there once the tenant adds it as a custom claim, because an
    Auth0 access token for a custom API carries sub/aud/iss/scope and nothing
    else. Everything here therefore has to work with subject alone.
    """

    subject: str
    email: str = ""

    @property
    def label(self) -> str:
        """How this person is recorded as the author of a decision."""
        # Truncated rather than rejected: an author the server derived itself
        # must never turn into a validation error the caller cannot act on.
        return (self.email or self.subject)[:MAX_AUTHOR]


# Set per request by http_app once a token verifies. None over stdio.
IDENTITY: contextvars.ContextVar[Identity | None] = contextvars.ContextVar(
    "identity", default=None
)

# Set by http_app when the deployment verifies credentials at all.
VERIFIES: contextvars.ContextVar[bool] = contextvars.ContextVar("verifies", default=False)


def gated() -> bool:
    """Whether this call is subject to membership.

    True as soon as somebody is identified, and true on any deployment that
    checks credentials — where a missing identity means the caller got in
    without one, which must not then be read as permission.

    False only where there is nobody to identify and nothing to identify them
    with: a local run, where the vault is a file belonging to whoever is
    reading it.
    """
    return IDENTITY.get() is not None or VERIFIES.get()

# Auth0 will not emit a bare `email` claim in an access token, so a tenant
# Action has to add a namespaced one. Both are read: the namespaced claim is
# what production will send, the plain one keeps tests and any other issuer
# straightforward.
EMAIL_CLAIM = "https://decisiontree/email"


def identity_from_claims(claims) -> Identity | None:
    """Build an Identity from verified token claims, or None if unusable.

    A token with no subject cannot attribute anything, and inventing a
    placeholder would put an unattributable decision in a shared log under a
    name that looks real.
    """
    subject = str(claims.get("sub") or "").strip()
    if not subject:
        return None
    email = ""
    for key in (EMAIL_CLAIM, "email"):
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            email = value.strip().lower()
            break
    return Identity(subject=subject, email=email)


def attributed_author(supplied: str) -> str:
    """Who to record as the author of a decision.

    A verified credential outranks whatever the client sent. `author` is an
    ordinary tool argument, so the model on the other end can put any name in
    it — harmless in a private vault, but in a shared project it is a byline
    anyone can forge. When the caller authenticated, the token is the answer
    and the supplied value is dropped rather than merged, so there is exactly
    one story about where attribution comes from.
    """
    identity = IDENTITY.get()
    return identity.label if identity is not None else supplied

# A remote project name becomes a filename, so it is validated rather than
# sanitised — anything not matching this is rejected at the edge, never coerced.
PROJECT_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def remote_projects() -> list[str]:
    """Names of the hosted vaults on this machine's volume."""
    directory = VAULT_HOME / "remote"
    if not directory.is_dir():
        return []
    return sorted(
        path.stem for path in directory.glob("*.db") if PROJECT_NAME.match(path.stem)
    )


def project_activity(names: list[str] | None = None) -> list[tuple[str, int]]:
    """(name, active count) for the given hosted vaults, or every one."""
    out = []
    for name in (remote_projects() if names is None else names):
        token = REMOTE_PROJECT.set(name)
        try:
            with closing(connect()) as conn:
                out.append(
                    (name, conn.execute(f"SELECT COUNT(*) FROM decisions WHERE {ACTIVE}").fetchone()[0])
                )
        finally:
            REMOTE_PROJECT.reset(token)
    return out


def suggest_projects() -> str:
    """Project names worth offering, with empty vaults reduced to a count.

    An empty vault is indistinguishable from a typo that was created by
    accident, and listing them all crowds out the projects someone actually
    means — this text is read by an agent choosing where to write.
    """
    activity = project_activity(visible_projects())
    named = [name for name, active in activity if active]
    empty = len(activity) - len(named)
    if not named:
        return "(none with decisions yet)"
    text = ", ".join(named)
    if empty:
        text += f" (plus {empty} empty)"
    return text


@contextmanager
def scoped(project: str = "", create: bool = False, need: str = "read"):
    """Bind the vault this call should act on; yields an error string or None.

    Over stdio and on a pinned connector the project is already decided, and an
    explicit one is refused rather than honoured — silently writing somewhere
    other than the URL says is the misfiling this design exists to prevent.
    On the router the project is required, because nothing else supplies it.

    This is also where access is decided, because it is the one path every tool
    goes through: a check somewhere else is a check a new tool can forget.
    `need` is what the tool is about to do — see NEEDS.
    """
    pinned = REMOTE_PROJECT.get()

    if not ROUTER.get():
        if project:
            where = f"this connector is pinned to {pinned}" if pinned else (
                "this client resolves the project from the working directory"
            )
            yield (
                f"Error: {where}, so the project argument does not apply. Omit it, "
                "or connect to the router endpoint (/mcp) to choose a project per call."
            )
        else:
            yield authorise(need)
        return

    if not project:
        yield (
            "Error: this connector is not tied to a project, so this call needs "
            f"project=<name>. Known projects: {suggest_projects()}. "
            "Ask which one before guessing."
        )
        return

    if not PROJECT_NAME.match(project):
        yield f"Error: {project!r} is not a valid project name ({PROJECT_NAME.pattern})."
        return

    fresh = project not in remote_projects()
    if fresh and not create:
        yield (
            f"Error: no project named {project!r}. Known projects: "
            f"{suggest_projects()}. Pass create=true to start a new one — but "
            "check for a typo first."
        )
        return

    token = REMOTE_PROJECT.set(project)
    try:
        if fresh:
            claim_new_project()
        yield authorise(need)
    finally:
        REMOTE_PROJECT.reset(token)


def claim_membership(conn: sqlite3.Connection, identity: "Identity") -> str | None:
    """The caller's role in the bound project, or None if they have none.

    Membership can be recorded before the person has ever signed in: an owner
    invites an address, and only the address is known at that point. The first
    time someone arrives with a token that matches, their subject is written
    into the row and every later call is matched on subject alone.

    Matching on email and then pinning to a subject — rather than matching on
    email every time — matters because an address can be reassigned inside a
    company, while a subject cannot. Once a row is claimed it belongs to that
    account and a later holder of the address does not inherit it.
    """
    row = conn.execute(
        "SELECT role FROM members WHERE subject = ?", (identity.subject,)
    ).fetchone()
    if row:
        return row["role"]
    if not identity.email:
        return None
    # Compared lowercased rather than trusting the stored form: an address is
    # case-insensitive in practice, and an invite typed with a capital would
    # otherwise sit there never matching the person it was meant for.
    row = conn.execute(
        "SELECT id, role FROM members WHERE lower(email) = ? AND subject IS NULL",
        (identity.email,),
    ).fetchone()
    if row is None:
        return None
    with writing(conn):
        # Re-checked in the WHERE clause: two of this person's clients can
        # arrive at once, and the second must not overwrite the first.
        conn.execute(
            "UPDATE members SET subject = ? WHERE id = ? AND subject IS NULL",
            (identity.subject, row["id"]),
        )
    return row["role"]


def add_owner(conn: sqlite3.Connection, identity: "Identity") -> None:
    """Make the caller the owner of a project they just created."""
    conn.execute(
        "INSERT OR IGNORE INTO members (subject, email, role, added_at, added_by)"
        " SELECT ?, ?, 'owner', ?, 'creator'"
        " WHERE NOT EXISTS (SELECT 1 FROM members)",
        (identity.subject, identity.email or None, now()),
    )


def claim_new_project() -> None:
    """Give a newly created project to whoever asked for it.

    Only reached through create=true on the router, which is somebody saying
    they mean to start a project. A pinned connector deliberately does not
    create: the name comes from a URL, so anything that auto-created would let
    a caller find out which projects exist by seeing which ones refuse them,
    and would keep turning typos into empty phantom projects.
    """
    identity = IDENTITY.get()
    if identity is None:
        return
    with closing(connect()) as conn, writing(conn):
        add_owner(conn, identity)


def authorise(need: str) -> str | None:
    """Whether the caller may do `need` in the bound project; None if they may.

    Only hosted projects are gated. Over stdio the vault is a file in the
    caller's own checkout, and there is no second party to protect it from.
    """
    if REMOTE_PROJECT.get() is None or not gated():
        return None

    identity = IDENTITY.get()
    if identity is None:
        # Nothing reaches a hosted project without saying who it is. There used
        # to be a shared token that did, which is the same as saying every
        # project was readable by anyone holding one secret.
        return (
            "Error: this project needs you to be signed in. Reconnect the "
            "connector so it can authenticate you."
        )

    if REMOTE_PROJECT.get() not in remote_projects():
        return unknown_project(REMOTE_PROJECT.get())

    with closing(connect()) as conn:
        role = claim_membership(conn, identity)

    if role is None:
        # Deliberately the same answer as a project that does not exist. This
        # server is multi-tenant, so confirming that a name is real to someone
        # with no access to it leaks one project's existence to another's team.
        return unknown_project(project_label())
    if RANK[role] < NEEDS[need]:
        return (
            f"Error: you are a {role} on {project_label()!r}, which cannot "
            f"{PERMISSION_VERB[need]}. Ask an owner to change your role."
        )
    return None


def unknown_project(name: str) -> str:
    """The one answer for 'no such project' and 'not yours' alike."""
    return (
        f"Error: no project named {name!r}. Known projects: {suggest_projects()}."
    )


PERMISSION_VERB = {
    "read": "read it",
    "write": "log or change decisions",
    "admin": "retire other people's decisions or manage access",
}


def may_act_on(row: sqlite3.Row) -> str | None:
    """Whether the caller may retire someone else's decision.

    A member retires their own mistakes; declaring that somebody else's
    decision should never have been recorded is an owner's call.
    """
    if REMOTE_PROJECT.get() is None or not gated():
        return None
    identity = IDENTITY.get()
    if identity is None:
        return None
    with closing(connect()) as conn:
        role = claim_membership(conn, identity)
    if role is None:
        return None          # authorise() has already refused this caller
    if RANK[role] >= NEEDS["admin"]:
        return None
    if row["author"] and row["author"] == identity.label:
        return None
    return (
        f"Error: decision #{row['id']} was logged by "
        f"{row['author'] or 'someone else'}. Only an owner can retire another "
        "person's decision — supersede it instead if it is simply out of date."
    )


def visible_projects() -> list[str]:
    """Hosted projects the caller is a member of."""
    if not gated():
        return remote_projects()
    identity = IDENTITY.get()
    if identity is None:
        return []
    names = remote_projects()
    out = []
    for name in names:
        token = REMOTE_PROJECT.set(name)
        try:
            with closing(connect()) as conn:
                if claim_membership(conn, identity) is not None:
                    out.append(name)
        finally:
            REMOTE_PROJECT.reset(token)
    return out


def project_root() -> Path:
    """The directory that identifies the current project.

    CONTEXT_VAULT_PROJECT wins if set. Otherwise walk up from the working
    directory looking for a git root — `.git` is a directory in a normal
    clone but a file in a worktree or submodule, so test for existence, not
    for a directory. Falls back to the working directory itself.
    """
    override = os.environ.get("CONTEXT_VAULT_PROJECT")
    if override:
        return Path(override).expanduser().resolve()
    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if (candidate / ".git").exists():
            return candidate
    return cwd


def project_slug(root: Path) -> str:
    """A readable, collision-free filename stem for a project root.

    The name half is for humans browsing ~/.context-vault/projects/; the hash
    half is what actually keeps two same-named repos apart.
    """
    name = re.sub(r"[^a-z0-9._-]+", "-", root.name.lower()).strip("-.") or "project"
    digest = hashlib.sha256(str(root).encode()).hexdigest()[:6]
    return f"{name[:40]}-{digest}"


def db_path() -> Path:
    # A remote project outranks CONTEXT_VAULT_DB deliberately: that env var
    # pins one file, which in a multi-tenant HTTP process would silently
    # collapse every project into a shared vault.
    remote = REMOTE_PROJECT.get()
    if remote is not None:
        return VAULT_HOME / "remote" / f"{remote}.db"
    override = os.environ.get("CONTEXT_VAULT_DB")
    if override:
        return Path(override).expanduser()
    return VAULT_HOME / "projects" / f"{project_slug(project_root())}.db"


def project_identity() -> str:
    """Stable identifier recorded in each vault's meta table."""
    remote = REMOTE_PROJECT.get()
    return f"remote:{remote}" if remote is not None else str(project_root())


def project_label() -> str:
    """Human-readable project name for briefs and empty-vault messages."""
    remote = REMOTE_PROJECT.get()
    if remote is not None:
        return remote
    # Imported lazily: onboarding reads this module, so a top-level import
    # would be circular.
    import onboarding

    root = project_root()
    return onboarding.read_declared_name(root) or root.name


# Long enough to outlast a competing write, short enough that a caller gets an
# answer rather than a hung tool call.
BUSY_TIMEOUT_MS = 5000


@contextmanager
def writing(conn: sqlite3.Connection):
    """A write transaction that takes its lock before the first read.

    Every write path here reads before it writes — is this decision already
    superseded, already retired — and then acts on what it read. SQLite's
    default deferred transaction takes no lock until the write itself, so two
    callers can both pass the same check before either of them writes. Taking
    the write lock up front is what makes those checks mean anything once more
    than one person is logging into a project.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None turns off the implicit transactions sqlite3 would
    # otherwise open, so writing() above is the only thing that begins one and
    # its BEGIN IMMEDIATE can never collide with one already in flight.
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    enable_wal(conn)
    conn.executescript(SCHEMA)
    migrate(conn)
    bootstrap(conn)
    return conn


def enable_wal(conn: sqlite3.Connection) -> None:
    """Put the file in WAL mode, so readers are not stuck behind a writer.

    Journal mode is a property of the file rather than of the connection, so
    this only has to succeed once per vault, ever — hence the check before the
    switch, which is what keeps every later connection off the write path.

    The switch needs a lock nobody else holds and, unlike an ordinary write, is
    not covered by busy_timeout: while someone is mid-write it fails at once.
    That failure is benign and is swallowed. Either the file is already WAL, or
    the caller currently writing set it, or the next one to find it idle will.
    Correctness under contention rests on BEGIN IMMEDIATE and the busy timeout;
    WAL is a concurrency improvement layered on top of them, not a load-bearing
    part of them.
    """
    if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal":
        return
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        pass


def bootstrap(conn: sqlite3.Connection) -> None:
    """One-time setup for a vault, skipped without taking a write lock.

    connect() runs on every call, reads included. Opening a write transaction
    each time would put every reader in the queue behind whoever happens to be
    logging a decision — which is exactly what WAL was turned on to avoid. So
    the checks are reads, and the lock is taken only when there is something to
    write, which is once in a vault's life.
    """
    needs_meta = (
        conn.execute("SELECT 1 FROM meta WHERE key = 'project_path'").fetchone() is None
    )
    owner = (os.environ.get("CONTEXT_VAULT_OWNER_EMAIL") or "").strip().lower()
    # Seeding only ever fires on a vault with nobody on it, so it can never
    # override a membership list somebody has actually edited.
    needs_owner = bool(owner) and not conn.execute("SELECT 1 FROM members LIMIT 1").fetchone()
    if not needs_meta and not needs_owner:
        return

    with writing(conn):
        if needs_meta:
            # Records which project a hashed filename belongs to. INSERT OR
            # IGNORE so the value reflects where the vault was created, not
            # wherever it happens to be read from later.
            conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('project_path', ?)",
                (project_identity(),),
            )
        if needs_owner:
            # Enforcement lands in a later stage, and the moment it does a
            # project with no members is a project nobody can reach, including
            # whoever has been using it all along. Re-checked inside the lock
            # because another caller may have seeded it since.
            conn.execute(
                "INSERT OR IGNORE INTO members (subject, email, role, added_at, added_by)"
                " SELECT NULL, ?, 'owner', ?, 'bootstrap'"
                " WHERE NOT EXISTS (SELECT 1 FROM members)",
                (owner, now()),
            )


def history_note(superseded: int, retired: int) -> str:
    """Trailing note about decisions held back from the brief."""
    counts = []
    if superseded:
        counts.append(f"{superseded} superseded decision(s)")
    if retired:
        counts.append(f"{retired} retired (filed in error)")
    if not counts:
        return ""
    return (
        f"\n({' and '.join(counts)} in history — "
        "list_decisions with include_superseded=true to see them.)"
    )


def empty_vault_note() -> str:
    """Hint shown on an empty vault when a pre-per-project vault exists."""
    # Meaningless to a remote client: it can't read the server's filesystem.
    if REMOTE_PROJECT.get() is not None:
        return ""
    if os.environ.get("CONTEXT_VAULT_DB") or not LEGACY_DB.exists():
        return ""
    return (
        f"\n\n(Vaults used to be shared across all projects. The old shared vault "
        f"is still at {LEGACY_DB} — set CONTEXT_VAULT_DB to that path to read it, "
        f"or copy it to {db_path()} to adopt its history as this project's.)"
    )


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing vault up to the current schema.

    Vaults are long-lived files on disk and predate columns added later, so a
    missing column is normal, not an error.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(decisions)")}
    missing = [(name, decl) for name, decl in ADDED_COLUMNS if name not in existing]
    if not missing:
        return
    with writing(conn):
        for name, decl in missing:
            conn.execute(f"ALTER TABLE decisions ADD COLUMN {name} {decl}")


def capture_hints(conn: sqlite3.Connection, new_id: int, cluster: str, parent) -> str:
    """Nudge toward the fields that turn a list of decisions into a graph.

    The agent reads its own tool result, so this is the one place feedback
    arrives while it can still act on it — and it carries the project's actual
    vocabulary rather than repeating generic advice from the docstring.

    Only fires when it has something concrete to say: the first decision in a
    project has no siblings to derive from and no labels to reuse, so it gets
    the plain confirmation.
    """
    hints = []

    if not cluster.strip():
        labels = [
            r["cluster"]
            for r in conn.execute(
                "SELECT DISTINCT cluster FROM decisions"
                f" WHERE cluster IS NOT NULL AND id != ? AND {ACTIVE}"
                " ORDER BY cluster",
                (new_id,),
            )
        ]
        if labels:
            hints.append(
                "No cluster set — labels already in use: " + ", ".join(labels[:6])
            )
        else:
            total = conn.execute(
                f"SELECT COUNT(*) FROM decisions WHERE {ACTIVE}"
            ).fetchone()[0]
            if total >= 3:
                hints.append(
                    "No cluster set — a short shared label groups related "
                    "decisions and makes the history readable"
                )

    if parent is None:
        recent = list(
            conn.execute(
                f"SELECT id, summary FROM decisions WHERE id != ? AND {ACTIVE}"
                " ORDER BY id DESC LIMIT 3",
                (new_id,),
            )
        )
        if recent:
            listed = ", ".join(
                f"#{r['id']} {r['summary'][:48]}{'…' if len(r['summary']) > 48 else ''}"
                for r in recent
            )
            hints.append(
                "No derives_from set — if this builds on one of these, say which: "
                + listed
            )

    return "".join(f"\n  {h}" for h in hints)


def normalise_created_at(value: str) -> tuple[str | None, str | None]:
    """Resolve the timestamp for a new decision; returns (stamp, error).

    Blank means now, which is the normal case. An explicit value exists for
    importing history recorded elsewhere, so it is validated rather than
    trusted: a future timestamp would sort a decision above everything and a
    malformed one would break the timeline it exists to preserve.
    """
    if not value.strip():
        return now(), None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None, (
            f"Error: created_at {value!r} is not an ISO 8601 timestamp "
            "(expected e.g. 2026-08-02T18:53:22+00:00)."
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if parsed > datetime.now(timezone.utc) + timedelta(minutes=1):
        return None, f"Error: created_at {value!r} is in the future."
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds"), None


def check_context(
    conn: sqlite3.Connection,
    derives_from: int | None,
    cluster: str,
    source: str,
    ref: str,
    author: str,
) -> str | None:
    """Validate the optional context fields; return an error string or None.

    Every one of these is optional. Rejecting a bad value is better than
    storing it: a mistyped source silently disappears from the surface filter,
    and a derives_from pointing at nothing draws an edge to nowhere.
    """
    if derives_from is not None:
        row = conn.execute(
            "SELECT id FROM decisions WHERE id = ?", (derives_from,)
        ).fetchone()
        if row is None:
            return (
                f"Error: no decision #{derives_from} in this project to derive "
                "from. Use list_decisions to find the right id, or omit "
                "derives_from."
            )
    if source and source not in SOURCES:
        return f"Error: source must be one of {', '.join(SOURCES)} (got {source!r})."
    for value, limit, name in (
        (cluster, MAX_CLUSTER, "cluster"),
        (ref, MAX_REF, "ref"),
        (author, MAX_AUTHOR, "author"),
    ):
        if len(value) > limit:
            return f"Error: {name} is longer than {limit} characters."
    return None


def context_columns(
    derives_from: int | None, cluster: str, source: str, ref: str, author: str
) -> tuple:
    """Values for the context columns, with blank strings stored as NULL."""
    return (
        derives_from,
        cluster.strip() or None,
        source.strip() or None,
        ref.strip() or None,
        author.strip() or None,
    )


def format_decision(row: sqlite3.Row, full: bool = False) -> str:
    if row["retired_at"]:
        status = "RETIRED"
    elif row["superseded_by"]:
        status = f"SUPERSEDED by #{row['superseded_by']}"
    else:
        status = "active"
    tags = " ".join(
        part
        for part in (
            f"[{row['cluster']}]" if row["cluster"] else "",
            f"({row['source']})" if row["source"] else "",
        )
        if part
    )
    lines = [f"#{row['id']} [{status}] {row['created_at']}{' ' + tags if tags else ''}"]
    lines.append(f"  Decision: {row['summary']}")
    if row["retired_at"]:
        lines.append(f"  Retired {row['retired_at']}: {row['retired_reason']}")
    if full:
        lines.append(f"  Reasoning: {row['reasoning']}")
        if row["derives_from"]:
            lines.append(f"  Derives from: #{row['derives_from']}")
        if row["supersedes"]:
            lines.append(f"  Supersedes: #{row['supersedes']}")
        for label, key in (("Author", "author"), ("Reference", "ref")):
            if row[key]:
                lines.append(f"  {label}: {row[key]}")
        lines.append(f"  Citation excerpt:\n    {row['excerpt']}")
    return "\n".join(lines)


@mcp.tool()
def log_decision(
    summary: str,
    reasoning: str,
    excerpt: str,
    derives_from: int = 0,
    cluster: str = "",
    source: str = "",
    ref: str = "",
    author: str = "",
    created_at: str = "",
    project: str = "",
    create: bool = False,
) -> str:
    """Log a meaningful project decision at the moment it is made.

    Use for architecture choices, stack/library picks, approaches ruled out,
    and direction changes — not routine actions or debugging steps. If this
    decision reverses an earlier one, use supersede_decision instead.

    Args:
        summary: One-sentence statement of the decision.
        reasoning: Why this was decided; alternatives considered and why
            they were rejected.
        excerpt: Verbatim, self-contained excerpt of the conversation where
            the decision happened — this is the citation.
        derives_from: Id of the decision this one builds on, if any. Set it
            whenever the reasoning refers back to an earlier decision — "given
            we already chose X", "extends", "the follow-up to". This is what
            connects the history into a tree instead of a flat list, so prefer
            setting it over leaving it blank. Use supersede_decision instead
            when the new decision *reverses* the earlier one.
        cluster: Short theme this belongs to, reused across decisions in the
            project — e.g. "Auth", "Content pipeline", "Landing page". Check
            list_decisions for the labels already in use before inventing one.
        source: Where it was decided: chat, code, pr, or doc.
        ref: Pointer to the artifact — a PR number, file:line, ticket id, or
            document section.
        author: Who made the call, if known. Ignored when the connection
            is authenticated — the credential says who you are, and a
            byline the caller types is one the caller can forge.
        created_at: Leave empty. The server stamps the time. Set it only when
            importing a decision that was recorded somewhere else and whose
            real date matters, as an ISO 8601 timestamp.
        project: Which project to act on. Required only on the router
            connector (a URL with no /p/<project>/ in it); omit it everywhere
            else. Call list_projects first and ask which one rather than
            guessing.
        create: Start a project that does not exist yet. Only set this when you
            mean to; an unrecognised name is far more often a typo.
    """
    with scoped(project, create, need="write") as problem:
        return problem or _write_decision(
            summary, reasoning, excerpt, derives_from, cluster, source, ref,
            author, created_at,
        )


def _write_decision(
    summary, reasoning, excerpt, derives_from, cluster, source, ref, author, created_at
) -> str:
    """Insert one decision into whichever vault is currently bound."""
    parent = derives_from or None
    author = attributed_author(author)
    with closing(connect()) as conn, writing(conn):
        problem = check_context(conn, parent, cluster, source, ref, author)
        if problem:
            return problem
        stamp, problem = normalise_created_at(created_at)
        if problem:
            return problem
        cur = conn.execute(
            "INSERT INTO decisions (summary, reasoning, excerpt, created_at,"
            " derives_from, cluster, source, ref, author)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (summary, reasoning, excerpt, stamp)
            + context_columns(parent, cluster, source, ref, author),
        )
        hints = capture_hints(conn, cur.lastrowid, cluster, parent)
    return f"Logged decision #{cur.lastrowid}: {summary}{hints}"


@mcp.tool()
def supersede_decision(
    decision_id: int,
    summary: str,
    reasoning: str,
    excerpt: str,
    cluster: str = "",
    source: str = "",
    ref: str = "",
    author: str = "",
    project: str = "",
) -> str:
    """Record a decision that reverses or replaces an earlier one.

    The old decision is kept in the timeline and marked superseded — nothing
    is deleted. The new decision links back to what it replaced.

    Args:
        decision_id: The id of the decision being reversed or replaced.
        summary: One-sentence statement of the new decision.
        reasoning: Why the earlier decision no longer holds.
        excerpt: Verbatim excerpt of the conversation where the reversal
            happened.
        cluster: Theme this belongs to. Defaults to the cluster of the
            decision being replaced, which is almost always right.
        source: Where it was decided: chat, code, pr, or doc.
        ref: Pointer to the artifact — PR number, file:line, ticket, section.
        author: Who made the call, if known. Ignored when the connection
            is authenticated — the credential says who you are, and a
            byline the caller types is one the caller can forge.
        project: Which project to act on. Required only on the router
            connector (a URL with no /p/<project>/ in it); omit it everywhere
            else. Call list_projects first if you are unsure of the name.
    """
    with scoped(project, need="write") as problem:
        return problem or _supersede_decision(decision_id, summary, reasoning, excerpt, cluster, source, ref, author)


def _supersede_decision(decision_id, summary, reasoning, excerpt, cluster, source, ref, author) -> str:
    author = attributed_author(author)
    with closing(connect()) as conn, writing(conn):
        old = conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
        if old is None:
            return f"Error: no decision #{decision_id}. Use list_decisions to find the right id."
        if old["superseded_by"]:
            return (
                f"Error: decision #{decision_id} was already superseded by "
                f"#{old['superseded_by']}. Supersede that one instead."
            )
        if old["retired_at"]:
            return (
                f"Error: decision #{decision_id} was retired as filed in error "
                f"({old['retired_reason']}). A retired decision is not part of "
                "this project's history, so there is nothing to supersede."
            )
        # A replacement almost always belongs to the same theme as what it
        # replaces, so inherit rather than making the caller repeat it.
        cluster = cluster or (old["cluster"] or "")
        problem = check_context(conn, None, cluster, source, ref, author)
        if problem:
            return problem
        cur = conn.execute(
            "INSERT INTO decisions (summary, reasoning, excerpt, created_at, supersedes,"
            " derives_from, cluster, source, ref, author)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (summary, reasoning, excerpt, now(), decision_id)
            + context_columns(None, cluster, source, ref, author),
        )
        conn.execute(
            "UPDATE decisions SET superseded_by = ? WHERE id = ?",
            (cur.lastrowid, decision_id),
        )
    return (
        f"Logged decision #{cur.lastrowid}, superseding #{decision_id} "
        f"({old['summary']})"
    )


@mcp.tool()
def retire_decision(decision_id: int, reason: str, project: str = "") -> str:
    """Mark a decision as filed in error, without deleting it.

    Use only when a decision does not belong in this project at all — for
    example it was logged against the wrong project. It leaves the brief and
    the default timeline but stays in the full history with the reason
    attached. Nothing is destroyed.

    This is not for decisions that turned out to be wrong or were changed —
    that is what supersede_decision is for, and a superseded decision remains
    a real part of this project's story.

    Args:
        decision_id: The id of the decision to retire.
        reason: Why it does not belong here, e.g. where it was moved to.
        project: Which project to act on. Required only on the router
            connector (a URL with no /p/<project>/ in it); omit it everywhere
            else. Call list_projects first if you are unsure of the name.
    """
    with scoped(project, need="write") as problem:
        return problem or _retire_decision(decision_id, reason)


def _retire_decision(decision_id, reason) -> str:
    with closing(connect()) as conn, writing(conn):
        row = conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
        if row is None:
            return f"Error: no decision #{decision_id}. Use list_decisions to find the right id."
        denied = may_act_on(row)
        if denied:
            return denied
        if row["retired_at"]:
            return (
                f"Error: decision #{decision_id} was already retired "
                f"({row['retired_at']}: {row['retired_reason']})."
            )
        conn.execute(
            "UPDATE decisions SET retired_at = ?, retired_reason = ? WHERE id = ?",
            (now(), reason, decision_id),
        )
    return f"Retired decision #{decision_id}: {row['summary']}"


@mcp.tool()
def list_decisions(include_superseded: bool = False, project: str = "") -> str:
    """List this project's decision timeline, newest first.

    Args:
        include_superseded: Include superseded decisions to see the full
            history, not just the current state.
        project: Which project to act on. Required only on the router
            connector (a URL with no /p/<project>/ in it); omit it everywhere
            else. Call list_projects first if you are unsure of the name.
    """
    with scoped(project) as problem:
        return problem or _list_decisions(include_superseded)


def _list_decisions(include_superseded) -> str:
    query = "SELECT * FROM decisions"
    if not include_superseded:
        query += f" WHERE {ACTIVE}"
    query += " ORDER BY id DESC"
    with closing(connect()) as conn:
        rows = conn.execute(query).fetchall()
    if not rows:
        return f"No decisions logged yet for {project_label()}.{empty_vault_note()}"
    return "\n".join(format_decision(r) for r in rows)


@mcp.tool()
def get_decision(decision_id: int, project: str = "") -> str:
    """Get the full record of one decision, including its citation excerpt.

    Args:
        decision_id: The id of the decision to fetch.
        project: Which project to act on. Required only on the router
            connector (a URL with no /p/<project>/ in it); omit it everywhere
            else. Call list_projects first if you are unsure of the name.
    """
    with scoped(project) as problem:
        return problem or _get_decision(decision_id)


def _get_decision(decision_id) -> str:
    with closing(connect()) as conn:
        row = conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
    if row is None:
        return f"Error: no decision #{decision_id}. Use list_decisions to find the right id."
    return format_decision(row, full=True)


@mcp.tool()
def get_project_brief(project: str = "") -> str:
    """Catch me up: this project's active decisions with reasoning, oldest first.

    Call this at the start of a session to load the current state of the
    project. Superseded decisions are excluded; use list_decisions with
    include_superseded=true to see how the project got here.

    Args:
        project: Which project to act on. Required only on the router
            connector (a URL with no /p/<project>/ in it); omit it everywhere
            else. Call list_projects first if you are unsure of the name.
    """
    with scoped(project) as problem:
        return problem or _get_project_brief()


def _get_project_brief() -> str:
    label = project_label()
    with closing(connect()) as conn:
        rows = conn.execute(
            f"SELECT * FROM decisions WHERE {ACTIVE} ORDER BY id"
        ).fetchall()
        superseded = conn.execute(
            "SELECT COUNT(*) FROM decisions"
            " WHERE superseded_by IS NOT NULL AND retired_at IS NULL"
        ).fetchone()[0]
        retired = conn.execute(
            "SELECT COUNT(*) FROM decisions WHERE retired_at IS NOT NULL"
        ).fetchone()[0]
    history = history_note(superseded, retired)
    if not rows:
        # Nothing at all in the vault means this is a first contact, and the
        # cheapest moment to catch a client pointed at the wrong project. A
        # vault whose decisions were all superseded or retired is not that.
        if not superseded and not retired:
            return (
                f"No decisions logged yet — {label} has no recorded history."
                f"\n\n(First run for {label} — call `setup` to confirm this is "
                "the right project and see how to turn on capture.)"
                f"{empty_vault_note()}"
            )
        # Everything here was superseded or retired, so this is emphatically
        # not a first run — say what is being held back instead.
        return f"No active decisions for {label}.{history}{empty_vault_note()}"
    parts = [f"# Project brief — {label}\n", "Active decisions:\n"]
    for row in rows:
        parts.append(f"- #{row['id']} ({row['created_at']}): {row['summary']}")
        parts.append(f"  Why: {row['reasoning']}")
    if history:
        parts.append(history)
    return "\n".join(parts)


@mcp.tool()
def list_projects() -> str:
    """List the hosted projects and how much is in each.

    Use this before logging on a connector that is not tied to a project, so
    the project argument is a real name rather than a guess — and show the list
    to the user and ask which one if it is not obvious from the conversation.
    """
    activity = dict(project_activity(visible_projects()))
    names = [name for name, active in activity.items() if active]
    empty = [name for name, active in activity.items() if not active]
    if not names:
        return "No hosted projects with decisions yet."

    lines = []
    for name in names:
        token = REMOTE_PROJECT.set(name)
        try:
            with closing(connect()) as conn:
                active = conn.execute(
                    f"SELECT COUNT(*) FROM decisions WHERE {ACTIVE}"
                ).fetchone()[0]
                latest = conn.execute(
                    f"SELECT MAX(created_at) FROM decisions WHERE {ACTIVE}"
                ).fetchone()[0]
                clusters = [
                    r[0]
                    for r in conn.execute(
                        f"SELECT DISTINCT cluster FROM decisions"
                        f" WHERE cluster IS NOT NULL AND {ACTIVE} ORDER BY cluster"
                    )
                ]
        finally:
            REMOTE_PROJECT.reset(token)
        detail = f"{active} active"
        if latest:
            detail += f", last {latest[:10]}"
        if clusters:
            detail += f" — {', '.join(clusters[:5])}"
        lines.append(f"- {name} ({detail})")
    text = "Hosted projects:\n" + "\n".join(lines)
    if empty:
        text += f"\n\n({len(empty)} empty: {', '.join(empty)})"
    return text


@mcp.tool()
def setup() -> str:
    """Show where this client is connected and how to finish setting it up.

    Call this on first use, or whenever it is not obvious which project the
    decisions being logged will belong to.
    """
    import onboarding

    return onboarding.report(onboarding.vault_state())


@mcp.tool()
async def name_project(name: str = "", ctx=None) -> str:
    """Set the canonical name for this project, written to a .context-vault file.

    The name is what the hosted connector URL uses and what briefs are titled
    with. It does not move any existing local vault: local history stays keyed
    to the directory's path, so naming a project later never loses decisions.

    Args:
        name: The name to use. Must match ^[a-z0-9][a-z0-9._-]{0,63}$. Leave
            empty to be asked interactively, where the client supports it.
    """
    import onboarding

    if REMOTE_PROJECT.get() is not None:
        return (
            "This client is connected over HTTP, where the project comes from "
            "the connector URL. Change the /p/<project>/mcp segment in the "
            "connector settings instead."
        )

    root = project_root()
    suggested = onboarding.suggest_name(root)

    if not name:
        name = await _ask_for_name(ctx, suggested)
        if not name:
            return (
                f"Not set. Call name_project with a name, or create "
                f"{onboarding.declaration_path(root)} containing "
                f'{{"project": "{suggested}"}}. Suggested: {suggested}'
            )

    if not PROJECT_NAME.match(name):
        return (
            f"Error: {name!r} is not a valid project name. It must match "
            f"{PROJECT_NAME.pattern} — lowercase letters, digits, dot, "
            f"underscore or hyphen, starting with a letter or digit. "
            f"Suggested: {suggested}"
        )

    path = onboarding.write_declared_name(root, name)
    return (
        f"Project named {name!r} (written to {path}). Briefs will use this "
        f"name, and the hosted connector URL for it would be "
        f"/p/{name}/mcp. Existing local history is unaffected."
    )


async def _ask_for_name(ctx, suggested: str) -> str:
    """Ask the user via MCP elicitation; empty string if that isn't possible.

    Elicitation is optional in the protocol and unsupported by most clients
    today, so every failure path here is expected rather than exceptional.
    """
    if ctx is None:
        return ""
    try:
        import mcp.types as types

        connection = ctx.connection
        if not connection.check_capability(
            types.ClientCapabilities(elicitation=types.ElicitationCapability())
        ):
            return ""
        result = await connection.send_request(
            types.ElicitRequest(
                method="elicitation/create",
                params=types.ElicitRequestFormParams(
                    message=(
                        "What should this project be called? This names its "
                        "decision history and its hosted connector URL."
                    ),
                    requestedSchema={
                        "type": "object",
                        "properties": {
                            "project": {
                                "type": "string",
                                "title": "Project name",
                                "description": (
                                    "Lowercase letters, digits, . _ - "
                                    f"(suggested: {suggested})"
                                ),
                                "default": suggested,
                            }
                        },
                        "required": ["project"],
                    },
                ),
            ),
            result_type=types.ElicitResult,
        )
        if result.action != "accept" or not result.content:
            return ""
        return str(result.content.get("project", "")).strip()
    except Exception:
        # Any transport, capability or validation problem means "ask in text".
        return ""


@mcp.tool()
def connect_hosted(host: str = "", project: str = "") -> str:
    """Point this repo at the hosted vault by writing its .mcp.json.

    Writes a project-scoped MCP config so this repo's decisions go to the
    shared hosted server instead of a local vault — the same history the
    Claude apps see. The token is written as a ${CONTEXT_VAULT_TOKEN}
    reference rather than inlined, so the file is safe to commit.

    Args:
        host: The hosted server, e.g. https://vault.example.com. Defaults to
            the CONTEXT_VAULT_HOSTED_URL environment variable.
        project: Project name for the URL. Defaults to this repo's declared
            name, else one derived from the directory name.
    """
    import onboarding

    if REMOTE_PROJECT.get() is not None:
        return (
            "This client already talks to the hosted server — there is nothing "
            "to connect. Run this from a client using the local stdio server."
        )

    base = onboarding.normalise_host(host or os.environ.get(onboarding.HOSTED_URL_ENV, ""))
    if not base:
        return (
            "Error: no hosted server known. Pass host=\"https://your-host\", or "
            f"set {onboarding.HOSTED_URL_ENV} in this MCP server's environment "
            "so it works everywhere without arguments."
        )

    root = project_root()
    name = project or onboarding.read_declared_name(root) or onboarding.suggest_name(root)
    if not PROJECT_NAME.match(name):
        return f"Error: {name!r} is not a valid project name ({PROJECT_NAME.pattern})."

    try:
        path, action = onboarding.merge_mcp_config(root, onboarding.hosted_entry(base, name))
    except (OSError, ValueError) as exc:
        return f"Error: {exc}"

    if action == "unchanged":
        return f"{path} already points at {base}/p/{name}/mcp — nothing to change."
    return (
        f"{action.capitalize()} {path}, pointing this repo at "
        f"{base}/p/{name}/mcp.\n\n"
        f"Next: export {onboarding.TOKEN_ENV} in your shell profile (the file "
        "references it rather than storing it, so it is safe to commit), then "
        "restart Claude Code in this repo and approve the project-scoped "
        "server when prompted. Existing local history for this repo stays "
        "where it is."
    )


@mcp.prompt(
    name="setup",
    title="DecisionTree setup",
    description="Show which project this is connected to and how to finish setup.",
)
def setup_prompt() -> str:
    import onboarding

    return onboarding.report(onboarding.vault_state())


if __name__ == "__main__":
    mcp.run()
