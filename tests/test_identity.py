"""Who this is, what it is for, and where it is running.

Jarvis has one user on one machine today, and the rows still have to
exist: per-account and per-device permissions cannot be expressed
without something to point at, and a connected account needs somewhere
to hold its scope. Building those later means rebuilding everything
stacked on top of them.

The behaviours that matter are about identity being *stable*. Launching
twice must not produce two users, moving the database must not produce a
new machine, and the secret for a connected account must never be in a
row.
"""

from __future__ import annotations

import pytest

from jarvis.identity import IdentityStore, local_device_id


@pytest.fixture
def store(tmp_path):
    store = IdentityStore(str(tmp_path / "jarvis.db"))
    yield store
    store.close()


@pytest.mark.unit
class TestFirstLaunch:
    def test_it_creates_one_user_one_workspace_one_device(self, store):
        store.ensure_local_identity()

        assert len(store.get_users()) == 1
        assert len(store.get_workspaces()) == 1
        assert len(store.get_devices()) == 1

    def test_the_workspace_is_personal_and_belongs_to_the_user(self, store):
        identity = store.ensure_local_identity()

        assert identity.workspace.kind == "personal"
        assert identity.workspace.user_id == identity.user.id

    def test_the_device_belongs_to_the_user_and_names_this_machine(self, store):
        identity = store.ensure_local_identity()

        assert identity.device.user_id == identity.user.id
        assert identity.device.name
        assert identity.device.platform

    def test_no_account_is_connected_yet(self, store):
        store.ensure_local_identity()

        assert store.get_accounts() == []

    def test_the_schema_arrives_with_the_store(self, tmp_path):
        """Same contract as the knowledge graph: opening it is enough."""
        store = IdentityStore(str(tmp_path / "nested" / "jarvis.db"))
        try:
            assert store.get_users() == []
        finally:
            store.close()


@pytest.mark.unit
class TestLaunchingAgain:
    def test_it_does_not_create_a_second_user(self, store):
        first = store.ensure_local_identity()
        second = store.ensure_local_identity()

        assert second.user.id == first.user.id
        assert len(store.get_users()) == 1

    def test_it_does_not_create_a_second_workspace_or_device(self, store):
        first = store.ensure_local_identity()
        second = store.ensure_local_identity()

        assert second.workspace.id == first.workspace.id
        assert second.device.id == first.device.id
        assert len(store.get_workspaces()) == 1
        assert len(store.get_devices()) == 1

    def test_the_device_is_seen_again(self, store):
        first = store.ensure_local_identity()
        second = store.ensure_local_identity()

        assert second.device.last_seen_at >= first.device.last_seen_at
        assert second.device.created_at == first.device.created_at

    def test_a_separate_process_finds_the_same_identity(self, tmp_path):
        """The daemon and the dashboard are two processes over one file."""
        path = str(tmp_path / "jarvis.db")
        daemon = IdentityStore(path)
        dashboard = IdentityStore(path)
        try:
            created = daemon.ensure_local_identity()

            assert dashboard.get_users()[0].id == created.user.id
            assert dashboard.get_devices()[0].id == created.device.id
        finally:
            daemon.close()
            dashboard.close()


@pytest.mark.unit
class TestTheDeviceIdentifiesTheMachine:
    def test_it_survives_the_database_being_replaced(self, tmp_path):
        """A device is a machine, not a database file.

        Moving or rebuilding the database must not make Jarvis think it
        is running somewhere new, or a per-device permission would be
        silently dropped.
        """
        first = IdentityStore(str(tmp_path / "one.db"))
        second = IdentityStore(str(tmp_path / "two.db"))
        try:
            assert second.ensure_local_identity().device.id == (
                first.ensure_local_identity().device.id
            )
        finally:
            first.close()
            second.close()

    def test_another_machine_is_another_device_under_the_same_user(
        self, tmp_path, monkeypatch,
    ):
        store = IdentityStore(str(tmp_path / "jarvis.db"))
        try:
            here = store.ensure_local_identity()

            monkeypatch.setattr(
                "jarvis.identity.local_device_id", lambda: "a-different-machine",
            )
            there = store.ensure_local_identity()

            assert there.device.id != here.device.id
            assert there.user.id == here.user.id
            assert len(store.get_devices()) == 2
            assert len(store.get_users()) == 1
        finally:
            store.close()

    def test_the_identifier_is_stable_and_opaque(self, tmp_path, monkeypatch):
        from jarvis.utils import paths

        monkeypatch.setenv(paths.DATA_DIR_ENV_VAR, str(tmp_path / "data"))

        first = local_device_id()

        assert first == local_device_id()
        assert len(first) >= 16


@pytest.mark.unit
class TestConnectedAccounts:
    def test_linking_one_records_it_against_the_user(self, store):
        identity = store.ensure_local_identity()

        account = store.link_account(provider="imap", account_label="personal mail")

        assert account.user_id == identity.user.id
        assert [a.id for a in store.get_accounts()] == [account.id]

    def test_the_secret_is_referenced_never_stored(self, store):
        """The row holds a name to look up in the OS keychain.

        A credential in a SQLite row is a credential in every backup of
        that row.
        """
        store.ensure_local_identity()

        account = store.link_account(provider="imap", account_label="personal mail")

        assert account.secret_ref
        assert "imap" in account.secret_ref
        row = store.raw_account_row(account.id)
        assert not any(
            "password" in str(value).lower() or "secret" in str(value).lower()
            for key, value in row.items()
            if key != "secret_ref"
        )

    def test_linking_the_same_account_twice_does_not_duplicate_it(self, store):
        store.ensure_local_identity()

        first = store.link_account(provider="imap", account_label="personal mail")
        again = store.link_account(provider="imap", account_label="personal mail")

        assert again.id == first.id
        assert len(store.get_accounts()) == 1

    def test_two_accounts_with_the_same_provider_are_kept_apart(self, store):
        store.ensure_local_identity()

        work = store.link_account(provider="imap", account_label="work mail")
        home = store.link_account(provider="imap", account_label="personal mail")

        assert work.id != home.id
        assert work.secret_ref != home.secret_ref
        assert len(store.get_accounts()) == 2

    def test_unlinking_removes_the_row(self, store):
        store.ensure_local_identity()
        account = store.link_account(provider="imap", account_label="personal mail")

        assert store.unlink_account(account.id) is True
        assert store.get_accounts() == []

    def test_an_account_can_be_scoped_to_a_workspace(self, store):
        identity = store.ensure_local_identity()

        account = store.link_account(
            provider="imap",
            account_label="work mail",
            workspace_id=identity.workspace.id,
        )

        assert account.workspace_id == identity.workspace.id
