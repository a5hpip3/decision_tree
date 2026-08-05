"""Who may reach a hosted project, and what they may do once they are in.

The vault stopped being single-user the moment a project could be shared, and
these are the properties that make that safe: a stranger sees nothing, a
teammate sees only what they were let into, and the answer to "does this
project exist" does not depend on who is asking.
"""

from __future__ import annotations

from contextlib import closing

import pytest

import server
from conftest import add_member, unwrap
from test_identity import as_identity

log_decision = unwrap(server.log_decision)
supersede_decision = unwrap(server.supersede_decision)
retire_decision = unwrap(server.retire_decision)
list_decisions = unwrap(server.list_decisions)
get_decision = unwrap(server.get_decision)
get_project_brief = unwrap(server.get_project_brief)
list_projects = unwrap(server.list_projects)

OWNER = server.Identity("auth0|owner", "owner@example.com")
MEMBER = server.Identity("auth0|member", "member@example.com")
VIEWER = server.Identity("auth0|viewer", "viewer@example.com")
STRANGER = server.Identity("auth0|stranger", "stranger@example.com")


def hosted(name, identity, fn, *args, **kw):
    """Call a tool as `identity` on a pinned connector for `name`."""
    project = server.REMOTE_PROJECT.set(name)
    try:
        return as_identity(identity, fn, *args, **kw)
    finally:
        server.REMOTE_PROJECT.reset(project)


def as_router(identity, fn, *args, **kw):
    router = server.ROUTER.set(True)
    try:
        return as_identity(identity, fn, *args, **kw)
    finally:
        server.ROUTER.reset(router)


def create_project(name, identity, summary="First"):
    """Start a project the only way that now creates one: explicitly."""
    return as_router(identity, log_decision, project=name, create=True,
                     summary=summary, reasoning="r", excerpt="x")


@pytest.fixture
def team(vault):
    """A project owned by OWNER, with a member, a viewer and a stranger."""
    create_project("acme", OWNER)
    add_member("acme", MEMBER.email, "member")
    add_member("acme", VIEWER.email, "viewer")
    return "acme"


# --------------------------------------------------------------------------
# Getting in at all
# --------------------------------------------------------------------------


class TestAccess:
    def test_creator_becomes_owner(self, vault):
        create_project("acme", OWNER)
        token = server.REMOTE_PROJECT.set("acme")
        try:
            with closing(server.connect()) as conn:
                rows = [tuple(r) for r in conn.execute(
                    "SELECT subject, email, role, added_by FROM members")]
        finally:
            server.REMOTE_PROJECT.reset(token)
        assert rows == [(OWNER.subject, OWNER.email, "owner", "creator")]

    def test_an_existing_project_cannot_be_claimed(self, team):
        """Ownership is taken by creating, never by turning up afterwards."""
        assert "no project named" in hosted(
            team, STRANGER, log_decision, summary="Mine now", reasoning="r", excerpt="x"
        )

    def test_stranger_is_refused_every_tool(self, team):
        for fn, kw in (
            (list_decisions, {}),
            (get_decision, {"decision_id": 1}),
            (get_project_brief, {}),
            (log_decision, {"summary": "s", "reasoning": "r", "excerpt": "x"}),
            (retire_decision, {"decision_id": 1, "reason": "no"}),
        ):
            assert "no project named" in hosted(team, STRANGER, fn, **kw), fn

    def test_refusal_does_not_admit_the_project_exists(self, team):
        """This server is multi-tenant; a name is another team's business.

        'Not allowed' and 'no such project' have to read identically, or the
        difference between them enumerates every project on the box. Echoing
        back the name the caller supplied is not a leak — they already had it.
        """
        forbidden = hosted(team, STRANGER, list_decisions)
        missing = hosted("no-such-project", STRANGER, list_decisions)
        assert forbidden == missing.replace("no-such-project", team)

    def test_refusal_does_not_list_other_teams_projects(self, team):
        create_project("secret-acquisition", OWNER)
        assert "secret-acquisition" not in hosted(team, STRANGER, list_decisions)

    def test_member_gets_in(self, team):
        assert "First" in hosted(team, MEMBER, list_decisions)

    def test_membership_by_email_is_claimed_on_first_arrival(self, team):
        """Invites name an address; only a sign-in can supply the subject."""
        hosted(team, MEMBER, list_decisions)
        token = server.REMOTE_PROJECT.set(team)
        try:
            with closing(server.connect()) as conn:
                row = conn.execute(
                    "SELECT subject FROM members WHERE email = ?", (MEMBER.email,)
                ).fetchone()
        finally:
            server.REMOTE_PROJECT.reset(token)
        assert row["subject"] == MEMBER.subject

    def test_a_claimed_row_does_not_transfer_with_the_address(self, team):
        """Addresses get reassigned inside a company; subjects do not."""
        hosted(team, MEMBER, list_decisions)                    # claims the row
        successor = server.Identity("auth0|newhire", MEMBER.email)
        assert "no project named" in hosted(team, successor, list_decisions)

    def test_matching_is_case_insensitive_on_the_address(self, vault):
        create_project("acme", OWNER)
        add_member("acme", "Mixed.Case@Example.COM", "member")
        shouty = server.identity_from_claims(
            {"sub": "auth0|mixed", server.EMAIL_CLAIM: "MIXED.CASE@example.com"}
        )
        assert "First" in hosted("acme", shouty, list_decisions)

    def test_subjectless_identity_cannot_be_matched(self, team):
        """A token with no email can only match a row already claimed."""
        no_email = server.Identity("auth0|nobody")
        assert "no project named" in hosted(team, no_email, list_decisions)


# --------------------------------------------------------------------------
# What each role may do
# --------------------------------------------------------------------------


class TestRoles:
    def test_viewer_reads(self, team):
        assert "First" in hosted(team, VIEWER, list_decisions)
        assert "First" in hosted(team, VIEWER, get_project_brief)

    def test_viewer_cannot_write(self, team):
        out = hosted(team, VIEWER, log_decision,
                     summary="s", reasoning="r", excerpt="x")
        assert "you are a viewer" in out
        assert "First" in hosted(team, OWNER, list_decisions)  # nothing landed

    def test_member_writes(self, team):
        assert "Logged decision" in hosted(
            team, MEMBER, log_decision, summary="Mine", reasoning="r", excerpt="x"
        )

    def test_member_may_supersede_anyones(self, team):
        """Replacing a decision is how the log moves on, not a privilege."""
        out = hosted(team, MEMBER, supersede_decision, decision_id=1,
                     summary="Better", reasoning="r", excerpt="x")
        assert "superseding #1" in out

    def test_member_may_retire_their_own(self, team):
        hosted(team, MEMBER, log_decision, summary="Mine", reasoning="r", excerpt="x")
        assert "Retired decision #2" in hosted(
            team, MEMBER, retire_decision, decision_id=2, reason="typo"
        )

    def test_member_may_not_retire_someone_elses(self, team):
        """Saying a decision should never have been recorded is an owner call."""
        out = hosted(team, MEMBER, retire_decision, decision_id=1, reason="no")
        assert "Only an owner" in out
        assert "RETIRED" not in hosted(team, OWNER, get_decision, decision_id=1)

    def test_owner_may_retire_anyones(self, team):
        hosted(team, MEMBER, log_decision, summary="Mine", reasoning="r", excerpt="x")
        assert "Retired decision #2" in hosted(
            team, OWNER, retire_decision, decision_id=2, reason="wrong"
        )

    def test_refusal_names_the_role_and_the_way_out(self, team):
        out = hosted(team, VIEWER, log_decision,
                     summary="s", reasoning="r", excerpt="x")
        assert "viewer" in out and "Ask an owner" in out


# --------------------------------------------------------------------------
# Listings
# --------------------------------------------------------------------------


class TestListings:
    def test_list_projects_shows_only_your_own(self, vault):
        create_project("mine", OWNER, summary="Mine")
        create_project("theirs", STRANGER, summary="Theirs")
        out = as_router(OWNER, list_projects)
        assert "mine" in out
        assert "theirs" not in out

    def test_suggestions_do_not_name_other_teams_projects(self, vault):
        create_project("secret-acquisition", OWNER)
        create_project("mine", STRANGER)
        out = as_router(STRANGER, list_decisions, project="typo-project")
        assert "secret-acquisition" not in out

    def test_router_can_reach_a_project_you_belong_to(self, team):
        assert "First" in as_router(MEMBER, list_decisions, project=team)

    def test_router_create_makes_you_the_owner(self, vault):
        out = as_router(MEMBER, log_decision, project="brand-new", create=True,
                        summary="First", reasoning="r", excerpt="x")
        assert "Logged decision" in out
        assert "First" in as_router(MEMBER, list_decisions, project="brand-new")


# --------------------------------------------------------------------------
# What must not change
# --------------------------------------------------------------------------


class TestUngatedPaths:
    def test_local_vaults_are_not_gated(self, vault):
        """Over stdio the vault is a file in your own checkout."""
        vault.enter(vault.project("repo"))
        assert "Logged decision" in log_decision(
            summary="Local", reasoning="r", excerpt="x"
        )
        assert "Local" in list_decisions()

    def test_local_vault_ignores_an_identity(self, vault):
        """A local file has no membership list to consult."""
        vault.enter(vault.project("repo"))
        assert "Logged decision" in as_identity(
            STRANGER, log_decision, summary="Local", reasoning="r", excerpt="x"
        )

    def test_static_token_still_opens_everything(self, team):
        """The remaining bypass, and the reason the token has to go.

        An unauthenticated hosted call is one carrying the shared static token,
        which names a machine rather than a person. It predates membership and
        walks straight past it.
        """
        assert "First" in hosted(team, None, list_decisions)
        assert "Logged decision" in hosted(
            team, None, log_decision, summary="s", reasoning="r", excerpt="x"
        )


class TestPinnedConnectorDoesNotCreate:
    """A pinned URL names a project; it does not conjure one.

    Auto-creating made two problems at once. Existence became probeable — a
    name that refused you existed and a name that let you in did not — and
    every typo in a connector URL left an empty project behind, which is where
    the leftover ones on the hosted volume came from.
    """

    def test_unknown_project_reads_like_a_forbidden_one(self, team):
        assert hosted("no-such-project", STRANGER, list_decisions) == hosted(
            team, STRANGER, list_decisions
        ).replace(team, "no-such-project")

    def test_a_typo_leaves_nothing_behind(self, team):
        hosted("acme-typo", OWNER, log_decision,
               summary="s", reasoning="r", excerpt="x")
        assert "acme-typo" not in server.remote_projects()

    def test_creating_still_works_through_the_router(self, vault):
        assert "Logged decision" in create_project("deliberate", OWNER)
        assert "deliberate" in server.remote_projects()


class TestLegacyVaults:
    """A vault that exists with nobody on it must not be up for grabs.

    Every hosted vault predates membership, and they were seeded from the
    bootstrap variable rather than by being claimed. If an unseeded one can be
    taken by whoever reaches it first, then on a public endpoint it belongs to
    whoever knocks first — which is precisely the trust-on-first-use the
    bootstrap variable exists to avoid.
    """

    def legacy(self, name="orphan"):
        create_project(name, OWNER)
        token = server.REMOTE_PROJECT.set(name)
        try:
            with closing(server.connect()) as conn, server.writing(conn):
                conn.execute("DELETE FROM members")
        finally:
            server.REMOTE_PROJECT.reset(token)
        return name

    def test_a_memberless_project_is_not_claimable(self, vault):
        name = self.legacy()
        assert "no project named" in hosted(
            name, STRANGER, log_decision, summary="Mine", reasoning="r", excerpt="x"
        )

    def test_not_claimable_through_the_router_either(self, vault):
        name = self.legacy()
        out = as_router(STRANGER, log_decision, project=name, create=True,
                        summary="Mine", reasoning="r", excerpt="x")
        assert "no project named" in out

    def test_and_stays_memberless(self, vault):
        name = self.legacy()
        as_router(STRANGER, log_decision, project=name, create=True,
                  summary="Mine", reasoning="r", excerpt="x")
        token = server.REMOTE_PROJECT.set(name)
        try:
            with closing(server.connect()) as conn:
                assert conn.execute("SELECT COUNT(*) FROM members").fetchone()[0] == 0
        finally:
            server.REMOTE_PROJECT.reset(token)
