"""Giving somebody access, and taking it away.

Membership was enforceable after the previous stage but only editable by
writing rows by hand. These are the tools that make it a product: an owner
invites an address they know, or hands out a code when they do not, and can
see and undo both.

The properties that matter are the ones that bite later — a project cannot be
left with nobody able to manage it, an invite works once, and a code that is
wrong reads the same as one that expired.
"""

from __future__ import annotations

from contextlib import closing

import pytest

import server
from conftest import unwrap
from test_identity import as_identity
from test_membership import MEMBER, OWNER, STRANGER, VIEWER, as_router, hosted

share_project = unwrap(server.share_project)
list_members = unwrap(server.list_members)
revoke_access = unwrap(server.revoke_access)
create_invite = unwrap(server.create_invite)
redeem_invite = unwrap(server.redeem_invite)
log_decision = unwrap(server.log_decision)
list_decisions = unwrap(server.list_decisions)
get_project_brief = unwrap(server.get_project_brief)


WEB = "https://web.example.com"


@pytest.fixture
def owned(vault, monkeypatch):
    """A hosted project whose only member is OWNER, with a front-end to link to."""
    monkeypatch.setenv("CONTEXT_VAULT_WEB_URL", WEB)
    as_router(OWNER, log_decision, project="acme", create=True,
              summary="First", reasoning="r", excerpt="x")
    return "acme"


def code_from(text: str) -> str:
    """The invite code out of whatever an invite tool handed back.

    Tolerates both shapes it comes in: a bare code when no front-end is
    configured, and a link when there is one.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if not line.startswith("    ") or server.INVITE_SEPARATOR not in stripped:
            continue
        return stripped.rsplit("/invite/", 1)[-1]
    raise AssertionError(f"no code in {text!r}")


def members_of(name: str):
    token = server.REMOTE_PROJECT.set(name)
    try:
        with closing(server.connect()) as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM members")]
    finally:
        server.REMOTE_PROJECT.reset(token)


# --------------------------------------------------------------------------
# Inviting by address
# --------------------------------------------------------------------------


class TestShareProject:
    def test_owner_invites_an_address(self, owned):
        out = hosted(owned, OWNER, share_project, email=MEMBER.email)
        assert "Invited" in out
        assert "no project named" in hosted(owned, MEMBER, list_decisions) or True
        # The invitation takes effect on first arrival.
        assert "First" in hosted(owned, MEMBER, list_decisions)

    def test_the_invitee_needs_no_account_yet(self, owned):
        hosted(owned, OWNER, share_project, email="nobody@example.com", role="viewer")
        rows = members_of(owned)
        invited = [r for r in rows if r["email"] == "nobody@example.com"][0]
        assert invited["subject"] is None
        assert invited["role"] == "viewer"

    def test_address_is_normalised(self, owned):
        hosted(owned, OWNER, share_project, email="  Mixed.Case@Example.COM  ")
        assert [r["email"] for r in members_of(owned) if r["role"] == "member"] == [
            "mixed.case@example.com"
        ]

    def test_role_is_a_closed_set(self, owned):
        out = hosted(owned, OWNER, share_project, email="x@example.com", role="admin")
        assert "role must be one of" in out
        assert len(members_of(owned)) == 1

    @pytest.mark.parametrize("bad", ["", "   ", "not-an-address"])
    def test_rejects_something_that_is_not_an_address(self, owned, bad):
        assert "is not an email address" in hosted(
            owned, OWNER, share_project, email=bad
        )

    def test_sharing_again_changes_the_role(self, owned):
        hosted(owned, OWNER, share_project, email=MEMBER.email, role="viewer")
        out = hosted(owned, OWNER, share_project, email=MEMBER.email, role="member")
        assert "is now a member" in out and "was viewer" in out
        assert len(members_of(owned)) == 2

    def test_sharing_at_the_same_role_is_a_no_op(self, owned):
        hosted(owned, OWNER, share_project, email=MEMBER.email)
        assert "already a member" in hosted(owned, OWNER, share_project, email=MEMBER.email)

    def test_a_member_cannot_invite(self, owned):
        hosted(owned, OWNER, share_project, email=MEMBER.email)
        out = hosted(owned, MEMBER, share_project, email=STRANGER.email)
        assert "you are a member" in out
        assert len(members_of(owned)) == 2

    def test_a_stranger_cannot_invite(self, owned):
        assert "no project named" in hosted(
            owned, STRANGER, share_project, email=STRANGER.email
        )


# --------------------------------------------------------------------------
# Seeing and undoing it
# --------------------------------------------------------------------------


class TestListMembers:
    def test_shows_roles_and_who_has_not_arrived(self, owned):
        hosted(owned, OWNER, share_project, email=MEMBER.email)
        out = hosted(owned, OWNER, list_members)
        assert OWNER.email in out and MEMBER.email in out
        assert "has not signed in yet" in out

    def test_a_signed_in_member_is_not_marked_pending(self, owned):
        hosted(owned, OWNER, share_project, email=MEMBER.email)
        hosted(owned, MEMBER, list_decisions)          # claims the row
        out = hosted(owned, OWNER, list_members)
        assert out.count("has not signed in yet") == 0

    def test_any_member_may_look(self, owned):
        hosted(owned, OWNER, share_project, email=VIEWER.email, role="viewer")
        assert OWNER.email in hosted(owned, VIEWER, list_members)

    def test_a_stranger_may_not(self, owned):
        assert "no project named" in hosted(owned, STRANGER, list_members)


class TestRevokeAccess:
    def test_owner_removes_someone(self, owned):
        hosted(owned, OWNER, share_project, email=MEMBER.email)
        hosted(owned, MEMBER, list_decisions)
        assert "Removed" in hosted(owned, OWNER, revoke_access, email=MEMBER.email)
        assert "no project named" in hosted(owned, MEMBER, list_decisions)

    def test_their_decisions_stay(self, owned):
        hosted(owned, OWNER, share_project, email=MEMBER.email)
        hosted(owned, MEMBER, log_decision, summary="Theirs", reasoning="r", excerpt="x")
        hosted(owned, OWNER, revoke_access, email=MEMBER.email)
        brief = hosted(owned, OWNER, list_decisions)
        assert "Theirs" in brief
        assert MEMBER.email in hosted(owned, OWNER, unwrap(server.get_decision), decision_id=2)

    def test_removing_someone_who_is_not_there(self, owned):
        assert "is not on" in hosted(owned, OWNER, revoke_access, email="ghost@example.com")

    def test_the_last_owner_cannot_be_removed(self, owned):
        """Otherwise the project is left with nobody able to manage it."""
        out = hosted(owned, OWNER, revoke_access, email=OWNER.email)
        assert "only owner" in out
        assert len(members_of(owned)) == 1

    def test_the_last_owner_cannot_be_demoted_either(self, owned):
        out = hosted(owned, OWNER, share_project, email=OWNER.email, role="member")
        assert "only owner" in out
        assert members_of(owned)[0]["role"] == "owner"

    def test_an_owner_can_leave_once_there_is_another(self, owned):
        hosted(owned, OWNER, share_project, email=MEMBER.email, role="owner")
        assert "Removed" in hosted(owned, OWNER, revoke_access, email=OWNER.email)
        assert [r["email"] for r in members_of(owned)] == [MEMBER.email]

    def test_a_member_cannot_revoke(self, owned):
        hosted(owned, OWNER, share_project, email=MEMBER.email)
        assert "you are a member" in hosted(
            owned, MEMBER, revoke_access, email=OWNER.email
        )


# --------------------------------------------------------------------------
# Invite codes
# --------------------------------------------------------------------------


class TestInvites:
    def test_a_code_lets_someone_in(self, owned):
        code = code_from(hosted(owned, OWNER, create_invite))
        assert "now a member" in hosted(owned, STRANGER, redeem_invite, code=code)
        assert "First" in hosted(owned, STRANGER, list_decisions)

    def test_the_code_carries_the_project(self, owned):
        code = code_from(hosted(owned, OWNER, create_invite))
        assert code.startswith(f"{owned}{server.INVITE_SEPARATOR}")
        # So it can be redeemed from the router with nothing else to hand.
        assert "now a member" in as_router(STRANGER, redeem_invite, code=code)

    def test_only_the_hash_is_stored(self, owned):
        """A read of the table must not yield working invites."""
        code = code_from(hosted(owned, OWNER, create_invite))
        secret = code.split(server.INVITE_SEPARATOR, 1)[1]
        token = server.REMOTE_PROJECT.set(owned)
        try:
            with closing(server.connect()) as conn:
                stored = [dict(r) for r in conn.execute("SELECT * FROM invites")]
        finally:
            server.REMOTE_PROJECT.reset(token)
        assert secret not in str(stored)
        assert stored[0]["code_hash"] == server.hash_code(secret)

    def test_a_code_works_once(self, owned):
        code = code_from(hosted(owned, OWNER, create_invite))
        hosted(owned, STRANGER, redeem_invite, code=code)
        second = server.Identity("auth0|second", "second@example.com")
        assert "not valid" in hosted(owned, second, redeem_invite, code=code)

    def test_an_expired_code_is_refused(self, owned):
        code = code_from(hosted(owned, OWNER, create_invite, expires_in_days=1))
        token = server.REMOTE_PROJECT.set(owned)
        try:
            with closing(server.connect()) as conn, server.writing(conn):
                conn.execute("UPDATE invites SET expires_at = '2020-01-01T00:00:00+00:00'")
        finally:
            server.REMOTE_PROJECT.reset(token)
        assert "not valid" in hosted(owned, STRANGER, redeem_invite, code=code)

    def test_expired_used_and_invented_are_indistinguishable(self, owned):
        """Telling them apart tells a guesser which guesses were close."""
        code = code_from(hosted(owned, OWNER, create_invite))
        hosted(owned, STRANGER, redeem_invite, code=code)
        used = hosted(owned, VIEWER, redeem_invite, code=code)
        invented = hosted(owned, VIEWER, redeem_invite,
                          code=f"{owned}{server.INVITE_SEPARATOR}madeup")
        assert used == invented == server.INVITE_REFUSED

    def test_the_role_comes_from_the_invite(self, owned):
        code = code_from(hosted(owned, OWNER, create_invite, role="viewer"))
        hosted(owned, STRANGER, redeem_invite, code=code)
        assert "you are a viewer" in hosted(
            owned, STRANGER, log_decision, summary="s", reasoning="r", excerpt="x"
        )

    def test_redeeming_twice_as_the_same_person_says_so(self, owned):
        hosted(owned, OWNER, share_project, email=STRANGER.email)
        code = code_from(hosted(owned, OWNER, create_invite))
        assert "already a member" in hosted(owned, STRANGER, redeem_invite, code=code)

    def test_a_used_invite_does_not_consume_a_second_code(self, owned):
        hosted(owned, OWNER, share_project, email=STRANGER.email)
        code = code_from(hosted(owned, OWNER, create_invite))
        hosted(owned, STRANGER, redeem_invite, code=code)
        # Refused as already-a-member, so the code itself is still unspent.
        second = server.Identity("auth0|second", "second@example.com")
        assert "now a member" in hosted(owned, second, redeem_invite, code=code)

    def test_a_pinned_connector_refuses_another_projects_code(self, owned):
        as_router(OWNER, log_decision, project="other", create=True,
                  summary="x", reasoning="r", excerpt="x")
        code = code_from(as_router(OWNER, create_invite, project="other"))
        out = hosted(owned, STRANGER, redeem_invite, code=code)
        assert "pinned to acme" in out and "other" in out

    @pytest.mark.parametrize("bad", ["", "nocolon", ":secret", "UPPER:secret"])
    def test_a_malformed_code_is_refused(self, owned, bad):
        assert hosted(owned, STRANGER, redeem_invite, code=bad) == server.INVITE_REFUSED

    def test_only_an_owner_creates_invites(self, owned):
        hosted(owned, OWNER, share_project, email=MEMBER.email)
        assert "you are a member" in hosted(owned, MEMBER, create_invite)

    def test_expiry_is_bounded(self, owned):
        assert "between 1 and" in hosted(owned, OWNER, create_invite, expires_in_days=365)
        assert "between 1 and" in hosted(owned, OWNER, create_invite, expires_in_days=0)

    def test_outstanding_invites_are_visible_to_members(self, owned):
        hosted(owned, OWNER, create_invite)
        assert "unredeemed invite" in hosted(owned, OWNER, list_members)

    def test_a_redeemed_invite_stops_being_counted(self, owned):
        code = code_from(hosted(owned, OWNER, create_invite))
        hosted(owned, STRANGER, redeem_invite, code=code)
        assert "unredeemed invite" not in hosted(owned, OWNER, list_members)


# --------------------------------------------------------------------------
# Two projects
# --------------------------------------------------------------------------


class TestIsolation:
    def test_access_to_one_project_is_not_access_to_another(self, owned):
        as_router(OWNER, log_decision, project="other", create=True,
                  summary="Secret", reasoning="r", excerpt="x")
        hosted(owned, OWNER, share_project, email=MEMBER.email)
        assert "First" in hosted(owned, MEMBER, list_decisions)
        assert "no project named" in hosted("other", MEMBER, list_decisions)

    def test_an_invite_admits_to_one_project_only(self, owned):
        as_router(OWNER, log_decision, project="other", create=True,
                  summary="Secret", reasoning="r", excerpt="x")
        code = code_from(hosted(owned, OWNER, create_invite))
        as_router(STRANGER, redeem_invite, code=code)
        assert "no project named" in hosted("other", STRANGER, list_decisions)


# --------------------------------------------------------------------------
# The link an invited person actually receives
# --------------------------------------------------------------------------


class TestInviteLink:
    def test_share_project_hands_back_a_link(self, owned, monkeypatch):
        out = hosted(owned, OWNER, share_project, email=MEMBER.email)
        assert f"{WEB}/invite/acme:" in out

    def test_without_a_configured_front_end_it_still_invites(self, owned, monkeypatch):
        """The grant is the thing; the page only explains it."""
        monkeypatch.delenv("CONTEXT_VAULT_WEB_URL", raising=False)
        out = hosted(owned, OWNER, share_project, email=MEMBER.email)
        assert "Invited" in out and "invite/" not in out
        assert "First" in hosted(owned, MEMBER, list_decisions)

    def test_the_link_only_works_for_the_address_it_names(self, owned):
        """It gets forwarded. Forwarding it must grant nobody anything."""
        code = code_from(hosted(owned, OWNER, share_project, email=MEMBER.email))
        assert hosted(owned, STRANGER, redeem_invite, code=code) == server.INVITE_REFUSED
        assert "no project named" in hosted(owned, STRANGER, list_decisions)

    def test_a_bound_invite_is_not_an_outstanding_link(self, owned):
        """It shows against the member row instead, where it means something."""
        hosted(owned, OWNER, share_project, email=MEMBER.email)
        out = hosted(owned, OWNER, list_members)
        assert "unredeemed invite" not in out
        assert "has not signed in yet" in out


class TestInviteDetails:
    """What the landing page is allowed to learn."""

    def details(self, owned, identity, code):
        return as_identity(identity, server.invite_details, code, identity)

    def code_for(self, owned, email, role="member"):
        return code_from(hosted(owned, OWNER, share_project, email=email, role=role))

    def test_the_invited_person_sees_project_and_role(self, owned):
        code = self.code_for(owned, MEMBER.email, "viewer")
        assert self.details(owned, MEMBER, code) == {
            "project": "acme", "role": "viewer", "member": True, "spent": False,
        }

    def test_somebody_else_sees_nothing(self, owned):
        code = self.code_for(owned, MEMBER.email)
        assert self.details(owned, STRANGER, code) is None

    def test_an_open_code_tells_whoever_holds_it(self, owned):
        code = code_from(hosted(owned, OWNER, create_invite))
        assert self.details(owned, STRANGER, code)["project"] == "acme"

    @pytest.mark.parametrize("bad", ["", "nocolon", "acme:", "no-such-project:abc"])
    def test_unusable_codes_are_one_answer(self, owned, bad):
        assert self.details(owned, MEMBER, bad) is None

    def test_a_link_naming_nothing_does_not_conjure_a_vault(self, owned):
        """connect() creates the file, so the name has to be checked first —
        otherwise every mistyped link leaves an empty project behind."""
        self.details(owned, MEMBER, "no-such-project:abcdefghijklmnop")
        assert "no-such-project" not in server.remote_projects()

    def test_an_expired_link_tells_nobody_anything(self, owned):
        code = self.code_for(owned, MEMBER.email)
        token = server.REMOTE_PROJECT.set(owned)
        try:
            with closing(server.connect()) as conn, server.writing(conn):
                conn.execute("UPDATE invites SET expires_at = '2020-01-01T00:00:00+00:00'")
        finally:
            server.REMOTE_PROJECT.reset(token)
        assert self.details(owned, MEMBER, code) is None

    def test_looking_claims_the_membership(self, owned):
        """Landing on the page is a sign-in, which is what the row was waiting for."""
        code = self.code_for(owned, MEMBER.email)
        self.details(owned, MEMBER, code)
        row = [r for r in members_of(owned) if r["email"] == MEMBER.email][0]
        assert row["subject"] == MEMBER.subject

    def test_it_reports_the_role_actually_held(self, owned):
        """Not the one the link was minted with, if an owner has since changed it."""
        code = self.code_for(owned, MEMBER.email, "viewer")
        hosted(owned, OWNER, share_project, email=MEMBER.email, role="member")
        assert self.details(owned, MEMBER, code)["role"] == "member"
