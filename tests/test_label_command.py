"""Tests for the label command and related helpers."""

from unittest.mock import MagicMock, Mock

import pytest

from gmail_filter_bot.cli import _add_to_spam, _is_sender_filtered
from gmail_filter_bot.config import Config, Credentials, FilterConfig
from gmail_filter_bot.gmail_client import GmailClient


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
        # Subject from first occurrence is kept
        subjects = {s[1]: s[2] for s in senders}
        assert subjects["alice@example.com"] == "Hello World"
        assert subjects["bob@example.com"] == "Re: Project Update"

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
