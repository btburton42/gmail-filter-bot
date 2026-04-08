"""Tests for filter manager."""

from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

from gmail_filter_bot.config import Config, Credentials, FilterConfig
from gmail_filter_bot.filter_manager import FilterChange, FilterManager


@pytest.fixture
def mock_credentials():
    """Create mock credentials."""
    return Credentials(
        client_id="test-client-id",
        client_secret="test-client-secret",
    )


@pytest.fixture
def mock_config(mock_credentials):
    """Create a mock config with test filters."""
    return Config(
        max_entries_per_filter=50,
        filters={
            "work": FilterConfig(
                name="work",
                action="label_and_archive",
                label="Work",
                entries=["a@example.com", "b@example.com", "c@example.com"],
            ),
            "newsletters": FilterConfig(
                name="newsletters",
                action="label_only",
                label="Newsletters",
                entries=["news@site1.com", "news@site2.com"],
            ),
        },
        credentials=mock_credentials,
    )


@pytest.fixture
def mock_gmail_client():
    """Create a mock Gmail client."""
    client = MagicMock()
    client.parse_filter_entries = Mock(return_value=[])
    client.list_filters = Mock(return_value=[])
    return client


@pytest.fixture
def filter_manager(mock_config, mock_gmail_client):
    """Create a filter manager with mock dependencies."""
    return FilterManager(
        config=mock_config,
        client=mock_gmail_client,
        config_path=Path("test_filters.yaml"),
    )


class TestFilterChange:
    """Tests for FilterChange dataclass."""

    def test_empty_change_has_no_changes(self):
        """Test that a FilterChange with no differences reports no changes."""
        change = FilterChange(name="test", local_only=set(), remote_only=set())
        assert not change.has_changes

    def test_change_with_local_only_has_changes(self):
        """Test that FilterChange with local-only entries reports changes."""
        change = FilterChange(name="test", local_only={"a@example.com"}, remote_only=set())
        assert change.has_changes

    def test_change_with_remote_only_has_changes(self):
        """Test that FilterChange with remote-only entries reports changes."""
        change = FilterChange(name="test", local_only=set(), remote_only={"b@example.com"})
        assert change.has_changes

    def test_change_with_both_has_changes(self):
        """Test that FilterChange with both types of differences reports changes."""
        change = FilterChange(
            name="test",
            local_only={"a@example.com"},
            remote_only={"b@example.com"},
        )
        assert change.has_changes

    def test_change_with_action_changed_has_changes(self):
        """Test that FilterChange with action change reports changes."""
        change = FilterChange(name="test", action_changed=True)
        assert change.has_changes

    def test_change_with_label_changed_has_changes(self):
        """Test that FilterChange with label change reports changes."""
        change = FilterChange(name="test", label_changed=True)
        assert change.has_changes

    def test_added_to_remote_returns_sorted_list(self):
        """Test that added_to_remote returns sorted list of remote-only entries."""
        change = FilterChange(
            name="test",
            local_only=set(),
            remote_only={"z@example.com", "a@example.com", "m@example.com"},
        )
        assert change.added_to_remote == ["a@example.com", "m@example.com", "z@example.com"]

    def test_removed_from_remote_returns_sorted_list(self):
        """Test that removed_from_remote returns sorted list of local-only entries."""
        change = FilterChange(
            name="test",
            local_only={"z@example.com", "a@example.com"},
            remote_only=set(),
        )
        assert change.removed_from_remote == ["a@example.com", "z@example.com"]

    def test_to_dict_serialization(self):
        """Test that to_dict serializes change correctly."""
        change = FilterChange(
            name="test_filter",
            local_only={"local@example.com"},
            remote_only={"remote@example.com"},
        )
        result = change.to_dict()
        assert result["name"] == "test_filter"
        assert result["added"] == ["remote@example.com"]
        assert result["removed"] == ["local@example.com"]


class TestCompareFilterEntries:
    """Tests for compare_filter_entries method."""

    def test_identical_entries_no_changes(self, filter_manager):
        """Test that identical local and remote entries produce no changes."""
        local = {"a@example.com", "b@example.com"}
        remote = {"a@example.com", "b@example.com"}

        change = filter_manager.compare_filter_entries(local, remote)

        assert not change.has_changes
        assert change.local_only == set()
        assert change.remote_only == set()

    def test_local_has_extra_entries(self, filter_manager):
        """Test detecting entries that exist locally but not remotely."""
        local = {"a@example.com", "b@example.com", "c@example.com"}
        remote = {"a@example.com", "b@example.com"}

        change = filter_manager.compare_filter_entries(local, remote)

        assert change.has_changes
        assert change.local_only == {"c@example.com"}
        assert change.remote_only == set()

    def test_remote_has_extra_entries(self, filter_manager):
        """Test detecting entries that exist remotely but not locally."""
        local = {"a@example.com", "b@example.com"}
        remote = {"a@example.com", "b@example.com", "c@example.com"}

        change = filter_manager.compare_filter_entries(local, remote)

        assert change.has_changes
        assert change.local_only == set()
        assert change.remote_only == {"c@example.com"}

    def test_both_have_different_entries(self, filter_manager):
        """Test detecting differences in both directions."""
        local = {"a@example.com", "b@example.com", "local_only@example.com"}
        remote = {"a@example.com", "b@example.com", "remote_only@example.com"}

        change = filter_manager.compare_filter_entries(local, remote)

        assert change.has_changes
        assert change.local_only == {"local_only@example.com"}
        assert change.remote_only == {"remote_only@example.com"}

    def test_empty_local_with_remote_entries(self, filter_manager):
        """Test when local has no entries but remote does."""
        local = set()
        remote = {"a@example.com", "b@example.com"}

        change = filter_manager.compare_filter_entries(local, remote)

        assert change.has_changes
        assert change.local_only == set()
        assert change.remote_only == {"a@example.com", "b@example.com"}

    def test_empty_remote_with_local_entries(self, filter_manager):
        """Test when remote has no entries but local does."""
        local = {"a@example.com", "b@example.com"}
        remote = set()

        change = filter_manager.compare_filter_entries(local, remote)

        assert change.has_changes
        assert change.local_only == {"a@example.com", "b@example.com"}
        assert change.remote_only == set()


class TestDetectChanges:
    """Tests for detect_changes method."""

    def test_no_changes_when_in_sync(self, filter_manager, mock_gmail_client):
        """Test that no changes are detected when local and remote are in sync."""
        # Mock remote filters matching all local filters
        mock_gmail_client.list_filters.return_value = [
            {
                "id": "filter1",
                "criteria": {"from": "a@example.com OR b@example.com OR c@example.com"},
            },
            {
                "id": "filter2",
                "criteria": {"from": "news@site1.com OR news@site2.com"},
            },
        ]
        mock_gmail_client.parse_filter_entries.side_effect = lambda f: {
            "filter1": ["a@example.com", "b@example.com", "c@example.com"],
            "filter2": ["news@site1.com", "news@site2.com"],
        }.get(f["id"], [])

        changes = filter_manager.detect_changes()

        assert changes == []

    def test_detects_new_entries_in_gmail(self, filter_manager, mock_gmail_client):
        """Test detecting new entries added via Gmail UI."""
        # Remote has a new entry for work filter
        mock_gmail_client.list_filters.return_value = [
            {
                "id": "filter1",
                "criteria": {
                    "from": "a@example.com OR b@example.com OR c@example.com OR new@example.com"
                },
            },
            {
                "id": "filter2",
                "criteria": {"from": "news@site1.com OR news@site2.com"},
            },
        ]
        mock_gmail_client.parse_filter_entries.side_effect = lambda f: {
            "filter1": ["a@example.com", "b@example.com", "c@example.com", "new@example.com"],
            "filter2": ["news@site1.com", "news@site2.com"],
        }.get(f["id"], [])

        changes = filter_manager.detect_changes()

        assert len(changes) == 1
        assert changes[0].name == "work"
        assert changes[0].remote_only == {"new@example.com"}
        assert changes[0].local_only == set()

    def test_detects_removed_entries_from_gmail(self, filter_manager, mock_gmail_client):
        """Test detecting entries removed via Gmail UI."""
        # Remote is missing an entry from work filter
        mock_gmail_client.list_filters.return_value = [
            {"id": "filter1", "criteria": {"from": "a@example.com OR b@example.com"}},
            {
                "id": "filter2",
                "criteria": {"from": "news@site1.com OR news@site2.com"},
            },
        ]
        mock_gmail_client.parse_filter_entries.side_effect = lambda f: {
            "filter1": ["a@example.com", "b@example.com"],
            "filter2": ["news@site1.com", "news@site2.com"],
        }.get(f["id"], [])

        changes = filter_manager.detect_changes()

        assert len(changes) == 1
        assert changes[0].name == "work"
        assert changes[0].local_only == {"c@example.com"}
        assert changes[0].remote_only == set()

    def test_detects_changes_in_multiple_filters(self, filter_manager, mock_gmail_client):
        """Test detecting changes across multiple filters."""
        mock_gmail_client.list_filters.return_value = [
            {
                "id": "filter1",
                "criteria": {
                    "from": "a@example.com OR b@example.com OR c@example.com OR new1@example.com"
                },
            },
            {
                "id": "filter2",
                "criteria": {"from": "news@site1.com OR news@site2.com OR new2@example.com"},
            },
        ]
        mock_gmail_client.parse_filter_entries.side_effect = lambda f: {
            "filter1": ["a@example.com", "b@example.com", "c@example.com", "new1@example.com"],
            "filter2": ["news@site1.com", "news@site2.com", "new2@example.com"],
        }.get(f["id"], [])

        changes = filter_manager.detect_changes()

        assert len(changes) == 2
        change_names = {c.name for c in changes}
        assert change_names == {"work", "newsletters"}

    def test_no_changes_for_filters_not_in_remote(self, filter_manager, mock_gmail_client):
        """Test that filters with no remote counterpart show all local entries as different.

        This is expected behavior - if a filter exists locally but not remotely,
        all local entries are considered "local_only" (would be added on push).
        """
        mock_gmail_client.list_filters.return_value = []
        mock_gmail_client.parse_filter_entries.return_value = []

        changes = filter_manager.detect_changes()

        # Both filters exist locally but not remotely - all entries are local_only
        assert len(changes) == 2
        change_names = {c.name for c in changes}
        assert change_names == {"work", "newsletters"}


class TestSync:
    """Tests for sync method."""

    def test_dry_run_does_not_modify_config(self, filter_manager, mock_gmail_client, mock_config):
        """Test that dry_run does not modify the local config."""
        # Setup: remote has a new entry for work filter
        mock_gmail_client.list_filters.return_value = [
            {
                "id": "filter1",
                "criteria": {
                    "from": "a@example.com OR b@example.com OR c@example.com OR new@example.com"
                },
            },
            {
                "id": "filter2",
                "criteria": {"from": "news@site1.com OR news@site2.com"},
            },
        ]
        mock_gmail_client.parse_filter_entries.side_effect = lambda f: {
            "filter1": ["a@example.com", "b@example.com", "c@example.com", "new@example.com"],
            "filter2": ["news@site1.com", "news@site2.com"],
        }.get(f["id"], [])

        original_entries = mock_config.filters["work"].entries.copy()

        changes = filter_manager.sync(dry_run=True)

        # Config should not be modified
        assert mock_config.filters["work"].entries == original_entries
        # Only work filter has changes
        assert len(changes) == 1
        assert changes[0].name == "work"

    def test_sync_updates_local_config(self, filter_manager, mock_gmail_client, mock_config):
        """Test that sync updates local config with remote entries."""
        # Setup: remote has a new entry
        mock_gmail_client.list_filters.return_value = [
            {
                "id": "filter1",
                "criteria": {
                    "from": "a@example.com OR b@example.com OR c@example.com OR new@example.com"
                },
            }
        ]
        mock_gmail_client.parse_filter_entries.side_effect = lambda f: (
            ["a@example.com", "b@example.com", "c@example.com", "new@example.com"]
            if f["id"] == "filter1"
            else []
        )
        mock_config.save = Mock()

        changes = filter_manager.sync(dry_run=False)

        # Config should be updated with remote entries
        assert "new@example.com" in mock_config.filters["work"].entries
        assert set(mock_config.filters["work"].entries) == {
            "a@example.com",
            "b@example.com",
            "c@example.com",
            "new@example.com",
        }
        mock_config.save.assert_called_once()

    def test_sync_removes_entries_not_in_remote(
        self, filter_manager, mock_gmail_client, mock_config
    ):
        """Test that sync removes local entries that no longer exist remotely."""
        # Setup: remote is missing an entry from local
        mock_gmail_client.list_filters.return_value = [
            {"id": "filter1", "criteria": {"from": "a@example.com OR b@example.com"}}
        ]
        mock_gmail_client.parse_filter_entries.side_effect = lambda f: (
            ["a@example.com", "b@example.com"] if f["id"] == "filter1" else []
        )
        mock_config.save = Mock()

        changes = filter_manager.sync(dry_run=False)

        # Config should be updated to match remote (without c@example.com)
        assert "c@example.com" not in mock_config.filters["work"].entries
        assert set(mock_config.filters["work"].entries) == {"a@example.com", "b@example.com"}
        mock_config.save.assert_called_once()


class TestValidate:
    """Tests for validate method."""

    def test_valid_config(self, filter_manager):
        """Test that valid config passes validation."""
        result = filter_manager.validate()

        assert result["valid"] is True
        assert result["errors"] == []
        assert result["total_filters"] == 2

    def test_invalid_action(self, filter_manager, mock_config):
        """Test validation catches invalid action."""
        mock_config.filters["invalid"] = FilterConfig(
            name="invalid",
            action="invalid_action",
            entries=["test@example.com"],
        )

        result = filter_manager.validate()

        assert result["valid"] is False
        assert any("Invalid action" in e for e in result["errors"])

    def test_missing_label_for_label_action(self, filter_manager, mock_config):
        """Test validation catches missing label when action requires it."""
        mock_config.filters["no_label"] = FilterConfig(
            name="no_label",
            action="label_only",
            label=None,
            entries=["test@example.com"],
        )

        result = filter_manager.validate()

        assert result["valid"] is False
        assert any("requires a label" in e for e in result["errors"])

    def test_detects_filters_needing_split(self, filter_manager, mock_config):
        """Test validation detects filters that exceed max entries."""
        mock_config.filters["big_filter"] = FilterConfig(
            name="big_filter",
            action="archive",
            entries=[f"test{i}@example.com" for i in range(100)],  # 100 entries
        )

        result = filter_manager.validate()

        assert len(result["splits"]) == 1
        assert result["splits"][0]["name"] == "big_filter"
        assert result["splits"][0]["parts"] == 2  # Split into 2 parts (100/50)


class TestTrim:
    """Tests for trim method."""

    def test_removes_duplicates(self, filter_manager, mock_config):
        """Test that trim removes duplicate entries."""
        mock_config.filters["dups"] = FilterConfig(
            name="dups",
            action="archive",
            entries=["a@example.com", "b@example.com", "a@example.com", "c@example.com"],
        )
        mock_config.save = Mock()

        result = filter_manager.trim()

        assert result["duplicates"] == 1
        assert "dups" in result["filters"]
        assert mock_config.filters["dups"].entries == [
            "a@example.com",
            "b@example.com",
            "c@example.com",
        ]
        mock_config.save.assert_called_once()

    def test_no_duplicates_no_changes(self, filter_manager):
        """Test that trim does nothing when no duplicates exist."""
        result = filter_manager.trim()

        assert result["duplicates"] == 0
        assert result["filters"] == []


class TestFormatFilters:
    """Tests for format_filters method."""

    def test_consolidates_filters_with_same_action_and_label(self, filter_manager, mock_config):
        """Test that format_filters consolidates similar filters."""
        # Add another filter with same action/label
        mock_config.filters["work2"] = FilterConfig(
            name="work2",
            action="label_and_archive",
            label="Work",
            entries=["d@example.com", "e@example.com"],
        )
        mock_config.save = Mock()

        result = filter_manager.format_filters(dry_run=False)

        assert len(result["consolidated"]) == 1
        assert result["consolidated"][0]["name"] == "work"
        assert result["consolidated"][0]["total_entries"] == 5  # 3 + 2
        assert "work2" not in mock_config.filters
        mock_config.save.assert_called_once()

    def test_dry_run_does_not_modify(self, filter_manager, mock_config):
        """Test that dry_run format_filters does not modify config."""
        mock_config.filters["work2"] = FilterConfig(
            name="work2",
            action="label_and_archive",
            label="Work",
            entries=["d@example.com"],
        )
        original_filters = list(mock_config.filters.keys())

        result = filter_manager.format_filters(dry_run=True)

        assert list(mock_config.filters.keys()) == original_filters
        assert len(result["consolidated"]) == 1
