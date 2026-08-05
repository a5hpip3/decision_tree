"""Attribution and concurrent writes — the two things sharing a project breaks.

Until now a vault had one user, so `author` being a plain tool argument was a
convenience and one writer at a time was a safe assumption. Neither survives a
shared project: a byline the caller types is a byline the caller can forge, and
two people logging at once is the normal case rather than the exception.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from contextlib import closing

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

import http_app
import oauth
import server
from conftest import unwrap
from test_http import Server, run
from test_oauth import ISSUER, make_token

log_decision = unwrap(server.log_decision)
supersede_decision = unwrap(server.supersede_decision)
retire_decision = unwrap(server.retire_decision)
get_decision = unwrap(server.get_decision)


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, key.public_key()


def as_identity(identity, fn, *args, **kw):
    token = server.IDENTITY.set(identity)
    try:
        return fn(*args, **kw)
    finally:
        server.IDENTITY.reset(token)


def local_project(vault):
    """A vault bound by env var, so worker threads see it too.

    Threads do not inherit contextvars, and the concurrency tests need two of
    them writing to the same file.
    """
    root = vault.project("shared")
    vault._mp.setenv("CONTEXT_VAULT_PROJECT", str(root))
    return root


# --------------------------------------------------------------------------
# Reading an identity out of verified claims
# --------------------------------------------------------------------------


class TestIdentityFromClaims:
    def test_subject_alone_is_enough(self):
        """The claim set Auth0 actually sends before a tenant Action is added."""
        identity = server.identity_from_claims({"sub": "auth0|user123"})
        assert identity == server.Identity(subject="auth0|user123", email="")

    def test_namespaced_email_claim_is_read(self):
        identity = server.identity_from_claims(
            {"sub": "auth0|user123", server.EMAIL_CLAIM: "Ashish@Exelsior.co"}
        )
        assert identity.email == "ashish@exelsior.co"

    def test_plain_email_claim_is_read(self):
        identity = server.identity_from_claims(
            {"sub": "auth0|user123", "email": "someone@example.com"}
        )
        assert identity.email == "someone@example.com"

    def test_namespaced_claim_wins_over_plain(self):
        """The tenant-controlled claim is the one the server asked for."""
        identity = server.identity_from_claims(
            {
                "sub": "auth0|user123",
                server.EMAIL_CLAIM: "real@example.com",
                "email": "other@example.com",
            }
        )
        assert identity.email == "real@example.com"

    @pytest.mark.parametrize("claims", [{}, {"sub": ""}, {"sub": "   "}, {"sub": None}])
    def test_no_subject_means_no_identity(self, claims):
        """Better to attribute nothing than to invent a name that looks real."""
        assert server.identity_from_claims(claims) is None

    @pytest.mark.parametrize("value", [None, 42, ["a@b.c"], {"x": 1}, "", "   "])
    def test_unusable_email_claim_is_ignored(self, value):
        identity = server.identity_from_claims({"sub": "s", server.EMAIL_CLAIM: value})
        assert identity.email == ""
        assert identity.subject == "s"


class TestIdentityLabel:
    def test_email_preferred(self):
        assert server.Identity("auth0|u", "a@b.co").label == "a@b.co"

    def test_falls_back_to_subject(self):
        assert server.Identity("auth0|u").label == "auth0|u"

    def test_truncated_to_the_column_limit(self):
        """A server-derived author must never become a validation error."""
        identity = server.Identity("s", "x" * 200 + "@example.com")
        assert len(identity.label) == server.MAX_AUTHOR


# --------------------------------------------------------------------------
# Attribution
# --------------------------------------------------------------------------


class TestAttributedAuthor:
    def test_supplied_author_used_when_nobody_authenticated(self):
        """Local stdio use has no token, so the argument is all there is."""
        assert server.attributed_author("Ash") == "Ash"

    def test_verified_identity_wins(self):
        assert as_identity(
            server.Identity("auth0|u", "real@example.com"),
            server.attributed_author,
            "Someone Else",
        ) == "real@example.com"

    def test_forged_author_is_discarded_not_merged(self):
        """The point: one story about where attribution comes from."""
        recorded = as_identity(
            server.Identity("auth0|u", "real@example.com"),
            server.attributed_author,
            "ceo@example.com",
        )
        assert "ceo@example.com" not in recorded

    def test_empty_supplied_author_still_attributed(self):
        assert as_identity(
            server.Identity("auth0|u", "real@example.com"),
            server.attributed_author,
            "",
        ) == "real@example.com"


class TestAttributionOnWrite:
    def test_log_decision_records_the_identity(self, vault):
        vault.enter(vault.project("repo"))
        identity = server.Identity("auth0|u", "real@example.com")
        as_identity(
            identity, log_decision,
            summary="Use SQLite", reasoning="simple", excerpt="x", author="Impostor",
        )
        assert "real@example.com" in get_decision(1)
        assert "Impostor" not in get_decision(1)

    def test_supersede_records_the_identity(self, vault):
        vault.enter(vault.project("repo"))
        log_decision(summary="First", reasoning="r", excerpt="x")
        as_identity(
            server.Identity("auth0|u", "real@example.com"), supersede_decision,
            decision_id=1, summary="Second", reasoning="r", excerpt="x", author="Impostor",
        )
        assert "real@example.com" in get_decision(2)
        assert "Impostor" not in get_decision(2)

    def test_unauthenticated_write_keeps_the_supplied_author(self, vault):
        """Attribution tightens for shared projects without changing local use."""
        vault.enter(vault.project("repo"))
        log_decision(summary="Use SQLite", reasoning="simple", excerpt="x", author="Ash")
        assert "Ash" in get_decision(1)

    def test_subject_used_when_the_tenant_sends_no_email(self, vault):
        vault.enter(vault.project("repo"))
        as_identity(
            server.Identity("auth0|user123"), log_decision,
            summary="Use SQLite", reasoning="simple", excerpt="x",
        )
        assert "auth0|user123" in get_decision(1)


# --------------------------------------------------------------------------
# Identity over the wire
# --------------------------------------------------------------------------


class TestIdentityOverHttp:
    def _app(self, keypair, **kwargs):
        _, public = keypair
        stub = type(
            "S", (), {"get_signing_key_from_jwt": lambda self, t: type("K", (), {"key": public})()}
        )()
        config = oauth.OAuthConfig(issuer=ISSUER)
        return http_app.build_app(
            oauth_config=config, verifier=oauth.TokenVerifier(config, jwk_client=stub), **kwargs
        )

    def test_decision_is_attributed_to_the_token_holder(self, vault, keypair):
        """End to end: the byline comes off the signature, not the argument."""
        from test_http import call

        async def scenario():
            async with Server(self._app(keypair)) as srv:
                token = make_token(
                    keypair,
                    aud=srv.url("shared"),
                    sub="auth0|teammate",
                    **{server.EMAIL_CLAIM: "teammate@example.com"},
                )
                headers = {"Authorization": f"Bearer {token}"}
                await call(
                    srv.url("shared"), "log_decision",
                    {"summary": "Use WAL", "reasoning": "r", "excerpt": "x",
                     "author": "someone.else@example.com"},
                    headers=headers,
                )
                return await call(
                    srv.url("shared"), "get_decision", {"decision_id": 1}, headers=headers
                )

        body = run(scenario())
        assert "teammate@example.com" in body
        assert "someone.else@example.com" not in body

    def test_static_token_carries_no_identity(self, vault, keypair):
        """A shared secret names a machine, so it must not fabricate a person."""
        from test_http import call

        async def scenario():
            async with Server(self._app(keypair, token="s3cret")) as srv:
                headers = {"Authorization": "Bearer s3cret"}
                await call(
                    srv.url("shared"), "log_decision",
                    {"summary": "Use WAL", "reasoning": "r", "excerpt": "x",
                     "author": "Ash"},
                    headers=headers,
                )
                return await call(
                    srv.url("shared"), "get_decision", {"decision_id": 1}, headers=headers
                )

        assert "Ash" in run(scenario())

    def test_identity_does_not_leak_between_requests(self, vault, keypair):
        """One request's caller must not become the next request's author."""
        from test_http import call

        async def scenario():
            async with Server(self._app(keypair)) as srv:
                first = make_token(
                    keypair, aud=srv.url("shared"), sub="auth0|one",
                    **{server.EMAIL_CLAIM: "one@example.com"},
                )
                second = make_token(
                    keypair, aud=srv.url("shared"), sub="auth0|two",
                    **{server.EMAIL_CLAIM: "two@example.com"},
                )
                for token in (first, second):
                    await call(
                        srv.url("shared"), "log_decision",
                        {"summary": "d", "reasoning": "r", "excerpt": "x"},
                        headers={"Authorization": f"Bearer {token}"},
                    )
                headers = {"Authorization": f"Bearer {second}"}
                return [
                    await call(srv.url("shared"), "get_decision", {"decision_id": i},
                               headers=headers)
                    for i in (1, 2)
                ]

        first_body, second_body = run(scenario())
        assert "one@example.com" in first_body
        assert "two@example.com" not in first_body
        assert "two@example.com" in second_body

    def test_identity_does_not_survive_into_an_anonymous_request(self, vault, keypair):
        """The case a stale contextvar would actually corrupt.

        Both credentials are accepted here. A static-token request carries no
        identity, so if the previous request's identity were still bound it
        would sign this decision with somebody who had nothing to do with it.
        """
        from test_http import call

        async def scenario():
            async with Server(self._app(keypair, token="s3cret")) as srv:
                token = make_token(
                    keypair, aud=srv.url("shared"), sub="auth0|one",
                    **{server.EMAIL_CLAIM: "one@example.com"},
                )
                await call(
                    srv.url("shared"), "log_decision",
                    {"summary": "first", "reasoning": "r", "excerpt": "x"},
                    headers={"Authorization": f"Bearer {token}"},
                )
                headers = {"Authorization": "Bearer s3cret"}
                await call(
                    srv.url("shared"), "log_decision",
                    {"summary": "second", "reasoning": "r", "excerpt": "x",
                     "author": "Ash"},
                    headers=headers,
                )
                return await call(
                    srv.url("shared"), "get_decision", {"decision_id": 2}, headers=headers
                )

        body = run(scenario())
        assert "Ash" in body
        assert "one@example.com" not in body


# --------------------------------------------------------------------------
# Concurrent writes
# --------------------------------------------------------------------------


class TestConcurrency:
    def test_wal_is_enabled(self, vault):
        vault.enter(vault.project("repo"))
        with closing(server.connect()) as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"

    def test_busy_timeout_is_set(self, vault):
        """Without it, a competing writer fails instantly instead of waiting."""
        vault.enter(vault.project("repo"))
        with closing(server.connect()) as conn:
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == server.BUSY_TIMEOUT_MS

    def test_connecting_takes_no_write_lock_once_set_up(self, vault, monkeypatch):
        """connect() runs on reads too, so it must not queue behind a writer.

        Doing the one-time setup unconditionally put every reader in line
        behind whoever was logging a decision — the exact stall WAL is here to
        avoid. Run with the owner variable set, because that is the production
        configuration and the one with a second reason to reach for the lock.
        """
        monkeypatch.setenv("CONTEXT_VAULT_OWNER_EMAIL", "ashish@exelsior.co")
        vault.enter(vault.project("repo"))
        log_decision(summary="First", reasoning="r", excerpt="x")
        with closing(server.connect()) as holder:
            holder.execute("BEGIN IMMEDIATE")
            try:
                with closing(server.connect()) as other:
                    rows = other.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
            finally:
                holder.rollback()
        assert rows == 1

    def test_connect_survives_a_blocked_wal_switch(self, vault):
        """A vault predating WAL, met while somebody else is mid-write.

        Switching journal mode needs a lock nobody else holds and is not
        covered by busy_timeout, so it fails the instant another writer has
        one. Connecting has to survive that — the switch is an optimisation,
        not a precondition.

        The holder drops the vault out of WAL itself, after connecting: doing
        it on a separate connection would leave the holder's own connect() to
        put it straight back, and the switch under test would never be reached.
        """
        vault.enter(vault.project("repo"))
        log_decision(summary="First", reasoning="r", excerpt="x")

        with closing(server.connect()) as holder:
            holder.execute("PRAGMA journal_mode = DELETE")
            holder.execute("BEGIN IMMEDIATE")
            holder.execute(
                "INSERT INTO decisions (summary, reasoning, excerpt, created_at)"
                " VALUES ('held', 'r', 'x', '2026-01-01T00:00:00+00:00')"
            )
            try:
                with closing(server.connect()) as other:
                    rows = other.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
            finally:
                holder.rollback()
        assert rows == 1

    def test_reads_are_not_blocked_by_an_open_write(self, vault):
        """WAL's actual payoff: one person logging does not stall everyone else."""
        vault.enter(vault.project("repo"))
        log_decision(summary="First", reasoning="r", excerpt="x")
        with closing(server.connect()) as writer, closing(server.connect()) as reader:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute(
                "INSERT INTO decisions (summary, reasoning, excerpt, created_at)"
                " VALUES ('held', 'r', 'x', '2026-01-01T00:00:00+00:00')"
            )
            rows = reader.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
            writer.rollback()
        assert rows == 1

    def _interleave(self, monkeypatch, hook: str, call):
        """Force the interleaving these transactions have to survive.

        Racing two threads and hoping they collide tests nothing reliably — the
        work is fast enough that one usually finishes before the other starts,
        so a deferred transaction passes by luck. Instead the first caller is
        held at `hook`, which sits between the read and the write, and the
        second is started while it waits.

        Under BEGIN IMMEDIATE the second caller blocks at BEGIN, so it cannot
        read a value the first is about to invalidate; it is released when the
        first commits and sees the real state. Under a deferred transaction it
        reads straight past and both try to write.

        The first caller is released on a timer rather than after the second
        finishes, because under correct locking the second cannot finish until
        the first has let go.
        """
        reached = threading.Event()
        release = threading.Event()
        armed = threading.Lock()
        first = [True]
        original = getattr(server, hook)

        def paused(*args, **kwargs):
            with armed:
                pause, first[0] = first[0], False
            if pause:
                reached.set()
                release.wait(timeout=10)
            return original(*args, **kwargs)

        monkeypatch.setattr(server, hook, paused)

        results: dict[str, object] = {}

        def worker(key):
            try:
                results[key] = call()
            except Exception as exc:  # noqa: BLE001 — an exception is the failure
                results[key] = exc

        a = threading.Thread(target=worker, args=("a",))
        a.start()
        assert reached.wait(timeout=10), "first caller never reached the hook"

        b = threading.Thread(target=worker, args=("b",))
        b.start()
        b.join(timeout=0.5)          # still blocked on the lock, as it should be
        release.set()

        a.join(timeout=15)
        b.join(timeout=15)
        assert not a.is_alive() and not b.is_alive(), "a writer never finished"
        return [results["a"], results["b"]]

    def test_interleaved_supersede_yields_one_winner_and_a_clean_refusal(
        self, vault, monkeypatch
    ):
        """The 'already superseded' check has to mean something under contention."""
        local_project(vault)
        log_decision(summary="First", reasoning="r", excerpt="x")

        results = self._interleave(
            monkeypatch, "check_context",
            lambda: supersede_decision(
                decision_id=1, summary="Replacement", reasoning="r", excerpt="x"
            ),
        )

        assert not any(isinstance(r, Exception) for r in results), results
        assert len([r for r in results if r.startswith("Logged decision")]) == 1
        assert len([r for r in results if "already superseded" in r]) == 1

        with closing(server.connect()) as conn:
            successors = conn.execute(
                "SELECT COUNT(*) FROM decisions WHERE supersedes = 1"
            ).fetchone()[0]
        assert successors == 1

    def test_interleaved_retire_yields_one_winner_and_a_clean_refusal(
        self, vault, monkeypatch
    ):
        local_project(vault)
        log_decision(summary="First", reasoning="r", excerpt="x")

        # now() is called between the already-retired check and the UPDATE.
        results = self._interleave(
            monkeypatch, "now",
            lambda: retire_decision(decision_id=1, reason="typo"),
        )

        assert not any(isinstance(r, Exception) for r in results), results
        assert len([r for r in results if r.startswith("Retired decision")]) == 1
        assert len([r for r in results if "already retired" in r]) == 1

    def test_concurrent_logs_all_land(self, vault):
        """Plain appends have no invariant to protect and must not be lost."""
        local_project(vault)
        barrier = threading.Barrier(4)
        results: list = [None] * 4

        def worker(i):
            barrier.wait()
            try:
                results[i] = log_decision(summary="d", reasoning="r", excerpt="x")
            except Exception as exc:  # noqa: BLE001
                results[i] = exc

        workers = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for w in workers:
            w.start()
        for w in workers:
            w.join()

        assert not any(isinstance(r, Exception) for r in results), results
        with closing(server.connect()) as conn:
            assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 4


# --------------------------------------------------------------------------
# Bootstrap ownership
# --------------------------------------------------------------------------


def members(conn) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM members ORDER BY id"))


class TestSeedOwner:
    def test_existing_vault_gains_the_members_table(self, vault):
        """Vaults are long-lived files that predate this table."""
        vault.enter(vault.project("repo"))
        with closing(server.connect()) as conn:
            conn.execute("DROP TABLE members")
        with closing(server.connect()) as conn:
            assert conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='members'"
            ).fetchone()

    def test_owner_seeded_from_the_environment(self, vault, monkeypatch):
        monkeypatch.setenv("CONTEXT_VAULT_OWNER_EMAIL", "Ashish@Exelsior.co")
        vault.enter(vault.project("repo"))
        with closing(server.connect()) as conn:
            rows = members(conn)
        assert len(rows) == 1
        assert rows[0]["email"] == "ashish@exelsior.co"
        assert rows[0]["role"] == "owner"
        assert rows[0]["subject"] is None  # filled in when they first sign in

    def test_nothing_seeded_without_the_environment_variable(self, vault, monkeypatch):
        monkeypatch.delenv("CONTEXT_VAULT_OWNER_EMAIL", raising=False)
        vault.enter(vault.project("repo"))
        with closing(server.connect()) as conn:
            assert members(conn) == []

    def test_seeding_is_idempotent_across_connections(self, vault, monkeypatch):
        monkeypatch.setenv("CONTEXT_VAULT_OWNER_EMAIL", "ashish@exelsior.co")
        vault.enter(vault.project("repo"))
        for _ in range(3):
            with closing(server.connect()) as conn:
                rows = members(conn)
        assert len(rows) == 1

    def test_never_overrides_an_existing_membership_list(self, vault, monkeypatch):
        """Seeding is a bootstrap, not a policy that reasserts itself."""
        monkeypatch.delenv("CONTEXT_VAULT_OWNER_EMAIL", raising=False)
        vault.enter(vault.project("repo"))
        with closing(server.connect()) as conn, server.writing(conn):
            conn.execute(
                "INSERT INTO members (subject, email, role, added_at)"
                " VALUES ('auth0|someone', 'someone@example.com', 'owner', '2026-01-01')"
            )
        monkeypatch.setenv("CONTEXT_VAULT_OWNER_EMAIL", "ashish@exelsior.co")
        with closing(server.connect()) as conn:
            rows = members(conn)
        assert [r["email"] for r in rows] == ["someone@example.com"]

    def test_every_hosted_project_gets_its_own_owner_row(self, vault, monkeypatch):
        monkeypatch.setenv("CONTEXT_VAULT_OWNER_EMAIL", "ashish@exelsior.co")
        for name in ("alpha", "beta"):
            token = server.REMOTE_PROJECT.set(name)
            try:
                with closing(server.connect()) as conn:
                    rows = members(conn)
            finally:
                server.REMOTE_PROJECT.reset(token)
            assert [r["email"] for r in rows] == ["ashish@exelsior.co"], name

    def test_roles_are_a_closed_set(self):
        assert server.ROLES == ("owner", "member", "viewer")


class TestAuthDiagnostics:
    """Whether a tenant Action fired is invisible from the server otherwise.

    Auth0 silently drops a custom claim whose namespace is malformed, so a
    missing email and a mistyped namespace produce the same token. Logging the
    claim names distinguishes them without putting values in the log.
    """

    def _app(self, keypair, **kwargs):
        _, public = keypair
        stub = type(
            "S", (), {"get_signing_key_from_jwt": lambda self, t: type("K", (), {"key": public})()}
        )()
        config = oauth.OAuthConfig(issuer=ISSUER)
        return http_app.build_app(
            oauth_config=config, verifier=oauth.TokenVerifier(config, jwk_client=stub), **kwargs
        )

    def test_claim_names_are_logged_without_their_values(self, vault, keypair, caplog):
        from test_http import call

        async def scenario():
            async with Server(self._app(keypair)) as srv:
                token = make_token(
                    keypair, aud=srv.url("shared"), sub="auth0|one",
                    **{server.EMAIL_CLAIM: "one@example.com"},
                )
                await call(
                    srv.url("shared"), "log_decision",
                    {"summary": "d", "reasoning": "r", "excerpt": "x"},
                    headers={"Authorization": f"Bearer {token}"},
                )

        with caplog.at_level("INFO", logger="context-vault.auth"):
            run(scenario())

        accepted = [r.getMessage() for r in caplog.records if "auth: accepted" in r.getMessage()]
        assert accepted, "no acceptance was logged"
        assert server.EMAIL_CLAIM in accepted[0]
        assert "one@example.com" not in accepted[0]
