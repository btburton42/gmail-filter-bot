"""Tests for the label command and related helpers."""

import re
from unittest.mock import MagicMock, Mock

import pytest

from gmail_filter_bot import cli
from gmail_filter_bot.cli import _add_to_spam, _is_sender_filtered
from gmail_filter_bot.config import Config, Credentials, FilterConfig
from gmail_filter_bot.gmail_client import GmailClient, Sender, SenderMessage


def sender(name, email, *messages):
    """Build a ``Sender`` for tests.

    ``messages`` are ``(subject, message_id)`` pairs, or bare message IDs
    (which become ``Subject <id>``).
    """
    pairs = [m if isinstance(m, tuple) else (f"Subject {m}", m) for m in messages]
    return Sender(name, email, tuple(SenderMessage(s, i) for s, i in pairs))


@pytest.fixture
def mock_credentials():
    return Credentials(
        client_id="test-client-id",
        client_secret="test-client-secret",
    )


@pytest.fixture
def mock_config(mock_credentials):
    return Config(
        max_entries_per_filter=50,
        filters={
            "work": FilterConfig(
                name="work",
                action="label_and_archive",
                label="Work",
                entries=["a@example.com", "b@example.com", "*@company.com"],
            ),
            "newsletters": FilterConfig(
                name="newsletters",
                action="label_only",
                label="Newsletters",
                entries=["news@site1.com", "*substack.com"],
            ),
        },
        credentials=mock_credentials,
    )


class TestConfigAllowed:
    """Tests for Config.allowed field."""

    def test_config_defaults_allowed_to_empty(self, mock_credentials):
        config = Config(
            max_entries_per_filter=50,
            filters={},
            credentials=mock_credentials,
        )
        assert config.allowed == []

    def test_config_loads_allowed(self, mock_credentials, tmp_path):
        import yaml as yaml_module

        config_data = {
            "max_entries_per_filter": 50,
            "allowed": ["allowed@example.com", "skip@me.com"],
            "filters": {},
        }
        config_file = tmp_path / "filters.yaml"
        cred_file = tmp_path / ".env"
        cred_file.write_text("GOOGLE_CLIENT_ID=test\nGOOGLE_CLIENT_SECRET=test\n")

        with open(config_file, "w") as f:
            yaml_module.dump(config_data, f)

        config = Config.load(config_file, cred_file)
        assert config.allowed == ["allowed@example.com", "skip@me.com"]

    def test_config_saves_allowed(self, mock_credentials, tmp_path):
        import yaml as yaml_module

        config = Config(
            max_entries_per_filter=50,
            filters={},
            credentials=mock_credentials,
            allowed=["saved@example.com"],
        )
        config_file = tmp_path / "filters.yaml"
        config.save(config_file)

        with open(config_file) as f:
            data = yaml_module.safe_load(f)
        assert data["allowed"] == ["saved@example.com"]

    def test_config_save_omits_empty_allowed(self, mock_credentials, tmp_path):
        import yaml as yaml_module

        config = Config(
            max_entries_per_filter=50,
            filters={},
            credentials=mock_credentials,
            allowed=[],
        )
        config_file = tmp_path / "filters.yaml"
        config.save(config_file)

        with open(config_file) as f:
            data = yaml_module.safe_load(f)
        assert "allowed" not in data


class TestExtractEmail:
    """Tests for GmailClient._extract_email static method."""

    def test_angle_brackets(self):
        assert GmailClient._extract_email("John Doe <john@example.com>") == "john@example.com"

    def test_bare_email(self):
        assert GmailClient._extract_email("john@example.com") == "john@example.com"

    def test_no_name(self):
        assert GmailClient._extract_email("<john@example.com>") == "john@example.com"

    def test_with_quotes(self):
        assert GmailClient._extract_email('"John Doe" <john@example.com>') == "john@example.com"

    def test_empty(self):
        assert GmailClient._extract_email("") == ""

    def test_complex_name(self):
        assert (
            GmailClient._extract_email("O'Brien, Sam <sam.obrien@example.co.uk>")
            == "sam.obrien@example.co.uk"
        )


class TestIsSenderFiltered:
    """Tests for _is_sender_filtered helper."""

    def test_exact_match(self, mock_config):
        assert _is_sender_filtered("a@example.com", mock_config) is True

    def test_case_insensitive_match(self, mock_config):
        assert _is_sender_filtered("A@EXAMPLE.COM", mock_config) is True

    def test_wildcard_at_domain(self, mock_config):
        assert _is_sender_filtered("anyone@company.com", mock_config) is True

    def test_wildcard_bare_domain(self, mock_config):
        assert _is_sender_filtered("someone@blog.substack.com", mock_config) is True

    def test_wildcard_bare_domain_exact(self, mock_config):
        assert _is_sender_filtered("writer@substack.com", mock_config) is True

    def test_not_filtered(self, mock_config):
        assert _is_sender_filtered("random@unknown.com", mock_config) is False

    def test_allowed_list_match(self, mock_credentials):
        config = Config(
            max_entries_per_filter=50,
            filters={},
            credentials=mock_credentials,
            allowed=["random@unknown.com"],
        )
        assert _is_sender_filtered("random@unknown.com", config) is True

    def test_allowed_list_case_insensitive(self, mock_credentials):
        config = Config(
            max_entries_per_filter=50,
            filters={},
            credentials=mock_credentials,
            allowed=["Random@Unknown.COM"],
        )
        assert _is_sender_filtered("random@unknown.com", config) is True

    def test_empty_entries_ignored(self, mock_credentials):
        config = Config(
            max_entries_per_filter=50,
            filters={
                "empty": FilterConfig(
                    name="empty",
                    action="delete",
                    entries=[""],
                ),
            },
            credentials=mock_credentials,
        )
        assert _is_sender_filtered("random@unknown.com", config) is False


class TestAddToSpam:
    """Tests for _add_to_spam helper."""

    def test_creates_spam_filter(self, mock_credentials):
        config = Config(
            max_entries_per_filter=50,
            filters={},
            credentials=mock_credentials,
        )
        _add_to_spam(config, "spammer@bad.com")
        assert "spam" in config.filters
        assert config.filters["spam"].action == "delete"
        assert "spammer@bad.com" in config.filters["spam"].entries

    def test_appends_to_existing_spam(self, mock_config):
        _add_to_spam(mock_config, "spammer@bad.com")
        assert "spam" in mock_config.filters
        assert "spammer@bad.com" in mock_config.filters["spam"].entries

    def test_no_duplicate(self, mock_config):
        _add_to_spam(mock_config, "spammer@bad.com")
        _add_to_spam(mock_config, "spammer@bad.com")
        assert mock_config.filters["spam"].entries.count("spammer@bad.com") == 1


def _make_mock_client():
    """Create a mock GmailClient with real _extract_email bound."""
    client = MagicMock()
    client._extract_email = GmailClient._extract_email
    return client


class TestListSenders:
    """Tests for GmailClient.list_senders."""

    def test_list_senders_extracts_unique(self):
        client = _make_mock_client()

        client.service.users().messages().list.return_value.execute.return_value = {
            "messages": [{"id": "1"}, {"id": "2"}, {"id": "3"}],
        }

        def mock_get(**kwargs):
            msg_id = kwargs.get("id")
            from_headers = {
                "1": {
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "Alice <alice@example.com>"},
                            {"name": "Subject", "value": "Hello World"},
                        ]
                    }
                },
                "2": {
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "Bob <bob@example.com>"},
                            {"name": "Subject", "value": "Re: Project Update"},
                        ]
                    }
                },
                "3": {
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "Alice <alice@example.com>"},
                            {"name": "Subject", "value": "Another message"},
                        ]
                    }
                },
            }
            result = MagicMock()
            result.execute.return_value = from_headers.get(msg_id, {})
            return result

        client.service.users().messages().get.side_effect = mock_get

        senders = GmailClient.list_senders(client, "INBOX", max_results=10)

        emails = [s[1] for s in senders]
        assert "alice@example.com" in emails
        assert "bob@example.com" in emails
        assert len(emails) == 2
        # Every message is kept per sender so triage can step through them
        by_email = {s.email: s for s in senders}
        alice = by_email["alice@example.com"]
        assert alice.subjects == ["Hello World", "Another message"]
        assert [m.message_id for m in alice.messages] == ["1", "3"]
        assert alice.message_id == "1"  # newest first, for unsubscribe/archive
        bob = by_email["bob@example.com"]
        assert bob.subjects == ["Re: Project Update"]
        assert bob.message_id == "2"

    def test_list_senders_respects_max_results(self):
        client = _make_mock_client()

        client.service.users().messages().list.return_value.execute.return_value = {
            "messages": [{"id": str(i)} for i in range(100)],
        }

        def mock_get(**kwargs):
            msg_id = kwargs.get("id")
            result = MagicMock()
            result.execute.return_value = {
                "payload": {"headers": [{"name": "From", "value": f"user{msg_id}@example.com"}]}
            }
            return result

        client.service.users().messages().get.side_effect = mock_get

        senders = GmailClient.list_senders(client, "INBOX", max_results=5)
        assert len(senders) == 5

    def test_list_senders_handles_missing_from(self):
        client = _make_mock_client()

        client.service.users().messages().list.return_value.execute.return_value = {
            "messages": [{"id": "1"}],
        }

        def mock_get(**kwargs):
            result = MagicMock()
            result.execute.return_value = {
                "payload": {"headers": []},
            }
            return result

        client.service.users().messages().get.side_effect = mock_get

        senders = GmailClient.list_senders(client, "INBOX", max_results=10)
        assert len(senders) == 0

    def test_list_senders_pagination(self):
        client = _make_mock_client()

        # First page returns 2 messages + nextPageToken, second page returns 1
        list_results = [
            {
                "messages": [{"id": "1"}, {"id": "2"}],
                "nextPageToken": "page2",
            },
            {
                "messages": [{"id": "3"}],
            },
        ]
        client.service.users().messages().list.return_value.execute.side_effect = list_results

        def mock_get(**kwargs):
            msg_id = kwargs.get("id")
            result = MagicMock()
            result.execute.return_value = {
                "payload": {"headers": [{"name": "From", "value": f"user{msg_id}@example.com"}]}
            }
            return result

        client.service.users().messages().get.side_effect = mock_get

        senders = GmailClient.list_senders(client, "INBOX", max_results=10)
        assert len(senders) == 3

    def test_list_senders_empty_folder(self):
        client = _make_mock_client()

        client.service.users().messages().list.return_value.execute.return_value = {}

        senders = GmailClient.list_senders(client, "INBOX", max_results=10)
        assert len(senders) == 0


class TestReportSpam:
    """Tests for GmailClient.report_spam."""

    def test_report_spam_finds_and_reports(self):
        client = MagicMock()

        client.service.users().messages().list.return_value.execute.return_value = {
            "messages": [{"id": "1"}, {"id": "2"}],
        }

        result = GmailClient.report_spam(client, ["spammer@bad.com"])

        assert result == 2
        # Verify batchModify was called with SPAM label
        call_body = client.service.users().messages().batchModify.call_args[1]["body"]
        assert call_body["addLabelIds"] == ["SPAM"]
        assert call_body["removeLabelIds"] == ["INBOX"]

    def test_report_spam_no_messages(self):
        client = MagicMock()

        client.service.users().messages().list.return_value.execute.return_value = {}

        result = GmailClient.report_spam(client, ["spammer@bad.com"])
        assert result == 0

    def test_report_spam_multiple_addresses(self):
        client = MagicMock()

        client.service.users().messages().list.return_value.execute.return_value = {
            "messages": [{"id": "1"}],
        }

        result = GmailClient.report_spam(client, ["spammer@bad.com", "another@bad.com"])
        assert result == 2

    def test_report_spam_empty_addresses(self):
        client = MagicMock()

        client.service.users().messages().list.return_value.execute.return_value = {
            "messages": [{"id": "1"}],
        }

        result = GmailClient.report_spam(client, [])
        assert result == 0


class TestArchiveMessage:
    """Tests for GmailClient.archive_message."""

    def test_archive_success(self):
        client = MagicMock()

        result = GmailClient.archive_message(client, "msg123")
        assert result is True
        call_body = client.service.users().messages().batchModify.call_args[1]["body"]
        assert call_body["ids"] == ["msg123"]
        assert call_body["removeLabelIds"] == ["INBOX"]

    def test_archive_http_error(self):
        from googleapiclient.errors import HttpError

        client = MagicMock()
        client.service.users().messages().batchModify.side_effect = HttpError(
            resp=MagicMock(status=500), content=b'{"error": "server error"}'
        )

        result = GmailClient.archive_message(client, "msg123")
        assert result is False


class TestGetRollupLabels:
    """Tests for GmailClient._get_rollup_labels."""

    def test_single_level(self):
        assert GmailClient._get_rollup_labels("simple") == ["simple"]

    def test_two_levels(self):
        assert GmailClient._get_rollup_labels("a/b") == ["a", "a/b"]

    def test_three_levels(self):
        assert GmailClient._get_rollup_labels("a/b/c") == ["a", "a/b", "a/b/c"]

    def test_automated_general(self):
        assert GmailClient._get_rollup_labels("automated/general") == [
            "automated",
            "automated/general",
        ]


class TestCreateFilterRollup:
    """Tests that create_filter with rollup creates one filter per ancestor."""

    def test_rollup_creates_multiple_filters(self):
        client = MagicMock()
        client._get_or_create_label.side_effect = lambda lbl: f"Label_{lbl}"
        client._get_rollup_labels = GmailClient._get_rollup_labels

        GmailClient.create_filter(
            client,
            from_addresses=["a@example.com"],
            action="label_and_archive",
            label="automated/general",
            rollup=True,
        )

        # Should create 2 filters: one for "automated" (label_only) and one for
        # "automated/general" (label_and_archive)
        assert client.service.users().settings().filters().create.call_count == 2

        calls = client.service.users().settings().filters().create.call_args_list
        # First call: ancestor "automated" - label_only (no INBOX removal)
        first_body = calls[0][1]["body"]
        assert first_body["action"]["addLabelIds"] == ["Label_automated"]
        assert "removeLabelIds" not in first_body["action"]

        # Second call: deepest "automated/general" - label_and_archive
        second_body = calls[1][1]["body"]
        assert second_body["action"]["addLabelIds"] == ["Label_automated/general"]
        assert second_body["action"]["removeLabelIds"] == ["INBOX"]

    def test_rollup_three_levels(self):
        client = MagicMock()
        client._get_or_create_label.side_effect = lambda lbl: f"Label_{lbl}"
        client._get_rollup_labels = GmailClient._get_rollup_labels

        GmailClient.create_filter(
            client,
            from_addresses=["a@example.com"],
            action="label_only",
            label="a/b/c",
            rollup=True,
        )

        assert client.service.users().settings().filters().create.call_count == 3

    def test_no_rollup_creates_single_filter(self):
        client = MagicMock()
        client._get_or_create_label.return_value = "Label_1"
        client._get_rollup_labels = GmailClient._get_rollup_labels

        GmailClient.create_filter(
            client,
            from_addresses=["a@example.com"],
            action="label_only",
            label="simple",
            rollup=False,
        )

        assert client.service.users().settings().filters().create.call_count == 1

    """Tests for Config.rollup field."""

    def test_config_defaults_rollup_false(self, mock_credentials):
        config = Config(
            max_entries_per_filter=50,
            filters={},
            credentials=mock_credentials,
        )
        assert config.rollup is False

    def test_config_loads_rollup(self, mock_credentials, tmp_path):
        import yaml as yaml_module

        config_data = {
            "max_entries_per_filter": 50,
            "rollup": True,
            "filters": {},
        }
        config_file = tmp_path / "filters.yaml"
        cred_file = tmp_path / ".env"
        cred_file.write_text("GOOGLE_CLIENT_ID=test\nGOOGLE_CLIENT_SECRET=test\n")

        with open(config_file, "w") as f:
            yaml_module.dump(config_data, f)

        config = Config.load(config_file, cred_file)
        assert config.rollup is True

    def test_config_saves_rollup(self, mock_credentials, tmp_path):
        import yaml as yaml_module

        config = Config(
            max_entries_per_filter=50,
            filters={},
            credentials=mock_credentials,
            rollup=True,
        )
        config_file = tmp_path / "filters.yaml"
        config.save(config_file)

        with open(config_file) as f:
            data = yaml_module.safe_load(f)
        assert data["rollup"] is True

    def test_config_save_omits_false_rollup(self, mock_credentials, tmp_path):
        import yaml as yaml_module

        config = Config(
            max_entries_per_filter=50,
            filters={},
            credentials=mock_credentials,
            rollup=False,
        )
        config_file = tmp_path / "filters.yaml"
        config.save(config_file)

        with open(config_file) as f:
            data = yaml_module.safe_load(f)
        assert "rollup" not in data


class TestResolveLabelId:
    """Tests for GmailClient._resolve_label_id."""

    def test_system_label_inbox(self):
        client = MagicMock()
        client.list_labels = Mock(return_value={})
        assert GmailClient._resolve_label_id(client, "INBOX") == "INBOX"

    def test_system_label_case_insensitive(self):
        client = MagicMock()
        client.list_labels = Mock(return_value={})
        assert GmailClient._resolve_label_id(client, "inbox") == "INBOX"

    def test_custom_label(self):
        client = MagicMock()
        client.list_labels = Mock(return_value={"Newsletters": "Label_123"})
        assert GmailClient._resolve_label_id(client, "Newsletters") == "Label_123"

    def test_custom_label_case_insensitive(self):
        client = MagicMock()
        client.list_labels = Mock(return_value={"Newsletters": "Label_123"})
        assert GmailClient._resolve_label_id(client, "newsletters") == "Label_123"

    def test_nonexistent_label(self):
        client = MagicMock()
        client.list_labels = Mock(return_value={"Work": "Label_456"})
        assert GmailClient._resolve_label_id(client, "Nonexistent") is None


class TestPendingChangesPreflight:
    """Unpushed changes from an earlier session are surfaced, not hidden."""

    def _manager(self, changes):
        from gmail_filter_bot.filter_manager import FilterChange

        mgr = MagicMock()
        mgr.detect_changes.return_value = [
            FilterChange(name=name, local_only=entries) for name, entries in changes
        ]
        mgr.push.return_value = {
            "created": 1,
            "updated": 0,
            "skipped": 0,
            "applied_to_existing": 3,
        }
        return mgr

    def test_no_pending_changes_is_silent(self, capsys):
        import gmail_filter_bot.cli as cli

        mgr = self._manager([])
        cli._check_pending_changes(mgr, interactive=False)
        assert capsys.readouterr().err == ""

    def test_ignores_remote_only_changes(self):
        import gmail_filter_bot.cli as cli
        from gmail_filter_bot.filter_manager import FilterChange

        mgr = MagicMock()
        mgr.detect_changes.return_value = [FilterChange(name="work", remote_only={"b@x.com"})]
        assert cli._pending_local_changes(mgr) == []

    def test_detection_failure_is_not_fatal(self):
        import gmail_filter_bot.cli as cli

        mgr = MagicMock()
        mgr.detect_changes.side_effect = RuntimeError("api down")
        assert cli._pending_local_changes(mgr) == []

    def test_non_interactive_warns_and_keeps_going(self, capsys):
        import gmail_filter_bot.cli as cli

        mgr = self._manager([("work", {"a@x.com", "b@x.com"})])
        cli._check_pending_changes(mgr, interactive=False)

        err = capsys.readouterr().err
        assert "1 filter(s) have changes not yet in Gmail" in err
        assert "work: +2 sender(s)" in err
        assert "gfb apply" in err
        mgr.push.assert_not_called()

    def test_interactive_declined_does_not_push(self, capsys, monkeypatch):
        import gmail_filter_bot.cli as cli

        mgr = self._manager([("work", {"a@x.com"})])
        monkeypatch.setattr("builtins.input", lambda prompt="": "n")
        cli._check_pending_changes(mgr, interactive=True)

        mgr.push.assert_not_called()
        assert "won't be offered again" in capsys.readouterr().out

    def test_interactive_accepted_pushes(self, monkeypatch, capsys):
        import gmail_filter_bot.cli as cli

        mgr = self._manager([("work", {"a@x.com"})])
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")
        cli._check_pending_changes(mgr, interactive=True)

        mgr.push.assert_called_once()
        out = capsys.readouterr().out
        assert "Pushed 1 filter(s)" in out
        assert "3 existing conversation(s)" in out

    def test_warning_lists_at_most_ten_filters(self, capsys):
        import gmail_filter_bot.cli as cli

        mgr = self._manager([(f"f{i}", {"a@x.com"}) for i in range(13)])
        cli._check_pending_changes(mgr, interactive=False)

        err = capsys.readouterr().err
        assert "13 filter(s)" in err
        assert "... and 3 more" in err

    def test_cmd_label_runs_the_preflight(self, monkeypatch, tmp_path):
        import argparse

        import gmail_filter_bot.cli as cli
        from gmail_filter_bot import settings
        from gmail_filter_bot.config import Config, Credentials
        from gmail_filter_bot.filter_manager import FilterManager

        monkeypatch.setattr(settings, "user_config_path", lambda: tmp_path / "c.yaml")
        cfg = Config(
            max_entries_per_filter=50,
            filters={},
            credentials=Credentials(client_id="x", client_secret="y"),
            allowed=[],
        )
        client = MagicMock()
        client._resolve_label_id.return_value = "INBOX"
        client.list_senders.return_value = []
        mgr = FilterManager(cfg, client, tmp_path / "filters.yaml")

        calls = []
        monkeypatch.setattr(cli, "_check_pending_changes", lambda m: calls.append(m))

        cli.cmd_label(mgr, argparse.Namespace(folder="Inbox", max_messages=200, rollup=False))
        assert calls == [mgr]


# --- the interactive session, without the terminal -------------------------
#
# cmd_label's middle is a Textual app. Rather than drive escape sequences, these
# tests replace ``cli.run_triage`` with a function that plays a scripted set of
# decisions against the real session object, then assert on the plain-text
# aftermath that cmd_label prints once the app is gone. The widgets themselves
# are covered in ``test_tui_screens.py``.


def _build_manager(tmp_path, senders, *, filters=None, allowed=None):
    """A FilterManager wired to a MagicMock Gmail client."""
    from gmail_filter_bot.filter_manager import FilterManager

    cfg = Config(
        max_entries_per_filter=50,
        filters=filters or {},
        credentials=Credentials(client_id="x", client_secret="y"),
        allowed=list(allowed or []),
    )
    client = MagicMock()
    client._resolve_label_id.return_value = "INBOX"
    client.list_senders.return_value = senders
    client.report_spam.return_value = 3
    client.archive_message.return_value = True
    client.get_unsubscribe_targets.return_value = None
    client.apply_label_to_existing.return_value = 0
    mgr = FilterManager(cfg, client, tmp_path / "filters.yaml")
    mgr.config.save = lambda path: None
    return mgr, client, cfg


def _run_label(monkeypatch, mgr, actions, *, keep=None, completed=True, reviewed=False):
    """Drive cmd_label with the app replaced by ``actions``."""
    import argparse

    from gmail_filter_bot import settings
    from gmail_filter_bot.tui.app import TriageResult

    calls = {"offered": False}

    def fake_run_triage(session, **_kwargs):
        for action in actions:
            action(session)
        if reviewed:
            return TriageResult(session=session, completed=completed, keep=keep, reviewed=True)
        return TriageResult(
            session=session, completed=completed, keep=keep, reviewed=reviewed
        )

    def fake_offer(_manager):
        calls["offered"] = True
        return 0

    # Keep settings writes next to the manager's config, never in the repo.
    monkeypatch.setattr(
        settings, "user_config_path", lambda: mgr.config_path.parent / "cfg.yaml"
    )
    monkeypatch.setattr(cli, "run_triage", fake_run_triage)
    monkeypatch.setattr(cli, "_check_pending_changes", lambda m: None)
    monkeypatch.setattr(cli, "_offer_apply", fake_offer)
    args = argparse.Namespace(folder="Inbox", max_messages=200, rollup=False)
    rc = cli.cmd_label(mgr, args)
    return rc, calls


def _first(session):
    """Apply an action to the first item, wherever the cursor is."""
    return lambda s: s


class TestFreshFetchSemantics:
    """Each run refetches from Gmail; already-triaged senders are filtered out."""

    def test_triaged_senders_are_not_offered_again(self, monkeypatch, tmp_path, capsys):
        from gmail_filter_bot.config import FilterConfig

        mgr, client, _cfg = _build_manager(
            tmp_path,
            senders=[
                sender("The Epoch Times", "newsletter@mail.theepochtimes.com", ("S", "m1")),
                sender("Friend", "known@friend.com", ("S", "m2")),
                sender("New Sender", "new@x.com", ("S", "m3")),
            ],
            # State after a session where apply was declined: local entry, no filter.
            filters={
                "newsletters": FilterConfig(
                    "newsletters",
                    "label_and_archive",
                    "Newsletters",
                    ["newsletter@mail.theepochtimes.com"],
                )
            },
            allowed=["known@friend.com"],
        )

        seen: list[list[str]] = []

        def capture(session):
            seen.append([item.email for item in session.items])

        _run_label(monkeypatch, mgr, [capture], completed=False)
        out = capsys.readouterr().out

        client.list_senders.assert_called_once()
        assert "Skipping 2 sender(s)" in out
        assert "1 sender(s) to triage" in out
        # Only the unknown sender reaches the session.
        assert seen == [["new@x.com"]]


class TestCmdLabelAftermath:
    """What cmd_label prints and does once the app has exited."""

    def test_filtering_saves_and_offers_to_push(self, monkeypatch, tmp_path, capsys):
        mgr, _client, cfg = _build_manager(
            tmp_path, senders=[sender("Alice", "a@x.com", ("S", "m1"))]
        )
        rc, calls = _run_label(
            monkeypatch, mgr, [lambda s: s.filter_item(s.current, "work")]
        )

        assert rc == 0
        assert calls["offered"] is True
        assert "a@x.com" in cfg.filters["work"].entries
        out = capsys.readouterr().out
        assert "Triage Summary:" in out
        assert re.search(r"Filtered:\s+1", out)
        assert "work: +1 sender(s)" in out
        assert "Saved to" in out

    def test_skipping_everything_saves_nothing(self, monkeypatch, tmp_path, capsys):
        mgr, _client, _cfg = _build_manager(
            tmp_path, senders=[sender("Alice", "a@x.com", ("S", "m1"))]
        )
        rc, calls = _run_label(monkeypatch, mgr, [lambda s: s.skip_item(s.current)])

        assert rc == 0
        assert calls["offered"] is False
        out = capsys.readouterr().out
        assert re.search(r"Skipped:\s+1", out)
        assert "No changes made." in out

    def test_quit_early_reports_where_it_stopped(self, monkeypatch, tmp_path, capsys):
        mgr, _client, _cfg = _build_manager(
            tmp_path,
            senders=[
                sender("Alice", "a@x.com", ("S", "m1")),
                sender("Bob", "b@x.com", ("S", "m2")),
            ],
        )
        rc, _calls = _run_label(
            monkeypatch,
            mgr,
            [lambda s: s.filter_item(s.current, "work")],
            completed=False,
        )
        assert rc == 0
        assert "nothing was sent behind your back" in capsys.readouterr().out


class TestDeferredQueue:
    """Spam and unsubscribe are queued during the session and fired once after."""

    def _two_senders(self, tmp_path):
        return _build_manager(
            tmp_path,
            senders=[
                sender("Spammy", "spam@x.com", ("S", "m1")),
                sender("List", "list@x.com", ("S", "m2")),
            ],
        )

    def test_spam_is_not_reported_until_the_queue_is_approved(
        self, monkeypatch, tmp_path, capsys
    ):
        mgr, client, _cfg = self._two_senders(tmp_path)

        def decide(session):
            session.spam_item(session.current, 0)
            # Nothing has been sent yet: only the local filter changed.
            assert not client.report_spam.called

        _run_label(
            monkeypatch,
            mgr,
            [decide],
            keep=["spam@x.com"],
            reviewed=True,
        )
        client.report_spam.assert_called_once_with(["spam@x.com"])
        out = capsys.readouterr().out
        assert "Sending 1 spam report(s)" in out
        assert "spam: 3 sender(s) reported" in out

    def test_skipped_at_the_review_is_never_sent(self, monkeypatch, tmp_path, capsys):
        mgr, client, _cfg = self._two_senders(tmp_path)
        _run_label(
            monkeypatch,
            mgr,
            [lambda s: s.spam_item(s.current, 0)],
            keep=[],
            reviewed=True,
        )
        assert not client.report_spam.called
        out = capsys.readouterr().out
        assert "Dropped 1 queued item(s)" in out
        assert "Nothing was sent." in out

    def test_cancelling_the_review_sends_nothing(self, monkeypatch, tmp_path, capsys):
        mgr, client, _cfg = self._two_senders(tmp_path)
        _run_label(monkeypatch, mgr, [lambda s: s.spam_item(s.current, 0)])

        assert not client.report_spam.called
        out = capsys.readouterr().out
        assert "left unsent" in out
        assert "not sent yet" in out  # the summary is honest about it

    def test_unsubscribe_fires_and_reports(self, monkeypatch, tmp_path, capsys):
        from gmail_filter_bot.unsubscribe import UnsubscribeResult, UnsubscribeTargets

        mgr, client, _cfg = _build_manager(
            tmp_path, senders=[sender("List", "list@x.com", ("S", "m2"))]
        )
        client.get_unsubscribe_targets.return_value = UnsubscribeTargets(
            urls=("https://lists.example/u/1",), mailtos=(), one_click=True
        )
        fired: list[object] = []

        def fake_unsubscribe(targets):
            fired.append(targets)
            return UnsubscribeResult(status="one_click", message="unsubscribed via POST")

        monkeypatch.setattr(
            "gmail_filter_bot.tui.controller.unsubscribe", fake_unsubscribe
        )
        _run_label(
            monkeypatch,
            mgr,
            [lambda s: s.unsubscribe_item(s.current, 0)],
            keep=["list@x.com"],
            reviewed=True,
        )

        assert len(fired) == 1
        out = capsys.readouterr().out
        assert "list@x.com: unsubscribed via POST" in out

    def test_undo_removes_a_sender_from_the_queue(self, monkeypatch, tmp_path):
        mgr, client, _cfg = _build_manager(
            tmp_path, senders=[sender("Spammy", "spam@x.com", ("S", "m1"))]
        )
        _run_label(
            monkeypatch,
            mgr,
            [lambda s: [s.spam_item(s.current, 0), s.undo()]],
            keep=[],
            reviewed=True,
        )
        # Undo emptied the queue, so there was nothing left to approve or send.
        assert not client.report_spam.called
