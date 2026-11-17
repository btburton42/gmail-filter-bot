"""Tests for the doctor / diagnose checks."""

from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

from gmail_filter_bot.config import Config, Credentials, FilterConfig
from gmail_filter_bot.filter_manager import FilterManager


@pytest.fixture
def mock_credentials():
    return Credentials(
        client_id="test-client-id.apps.googleusercontent.com",
        client_secret="test-client-secret",
    )


@pytest.fixture
def mock_gmail_client():
    client = MagicMock()
    client.parse_filter_entries = Mock(return_value=[])
    client.list_filters = Mock(return_value=[])
    client.list_labels = Mock(return_value={})
    return client


def make_manager(config, mock_gmail_client):
    return FilterManager(
        config=config,
        client=mock_gmail_client,
        config_path=Path("test_filters.yaml"),
    )


def levels_for(checks):
    return {c["name"]: c["level"] for c in checks}


def details_for(checks, name):
    for c in checks:
        if c["name"] == name:
            return c["details"]
    return None


class TestDiagnoseBasics:
    def test_clean_config_all_pass(self, mock_credentials, mock_gmail_client):
        config = Config(
            max_entries_per_filter=50,
            filters={
                "work": FilterConfig(
                    name="work",
                    action="label_and_archive",
                    label="Work",
                    entries=["a@example.com", "b@example.com"],
                ),
            },
            credentials=mock_credentials,
        )
        manager = make_manager(config, mock_gmail_client)
        checks = manager.diagnose()
        assert all(c["level"] == "pass" for c in checks), checks

    def test_invalid_action_fails(self, mock_credentials, mock_gmail_client):
        config = Config(
            max_entries_per_filter=50,
            filters={
                "bad": FilterConfig(
                    name="bad",
                    action="not_a_real_action",
                    entries=["a@example.com"],
                ),
            },
            credentials=mock_credentials,
        )
        manager = make_manager(config, mock_gmail_client)
        checks = manager.diagnose()
        assert levels_for(checks)["config_validation"] == "fail"

    def test_missing_label_fails(self, mock_credentials, mock_gmail_client):
        config = Config(
            max_entries_per_filter=50,
            filters={
                "bad": FilterConfig(
                    name="bad",
                    action="label_and_archive",
                    label=None,
                    entries=["a@example.com"],
                ),
            },
            credentials=mock_credentials,
        )
        manager = make_manager(config, mock_gmail_client)
        checks = manager.diagnose()
        assert levels_for(checks)["config_validation"] == "fail"

    def test_empty_filter_warns(self, mock_credentials, mock_gmail_client):
        config = Config(
            max_entries_per_filter=50,
            filters={
                "ghost": FilterConfig(name="ghost", action="archive", entries=[]),
            },
            credentials=mock_credentials,
        )
        manager = make_manager(config, mock_gmail_client)
        checks = manager.diagnose()
        assert levels_for(checks)["empty_filters"] == "warn"


class TestMalformedEntries:
    def test_whitespace_in_domain_is_malformed(self, mock_credentials, mock_gmail_client):
        """The exact bug from the user's filter list: a space inside the domain."""
        config = Config(
            max_entries_per_filter=50,
            filters={
                "newsletters": FilterConfig(
                    name="newsletters",
                    action="archive",
                    entries=["*@progressivereform .network", "ok@example.com"],
                ),
            },
            credentials=mock_credentials,
        )
        manager = make_manager(config, mock_gmail_client)
        checks = manager.diagnose()
        assert levels_for(checks)["malformed_entries"] == "fail"
        assert any("progressivereform" in d for d in details_for(checks, "malformed_entries"))

    def test_valid_wildcards_not_flagged(self, mock_credentials, mock_gmail_client):
        config = Config(
            max_entries_per_filter=50,
            filters={
                "f": FilterConfig(
                    name="f",
                    action="archive",
                    entries=[
                        "user@example.com",
                        "*@example.com",
                        "*example.com",
                        "404-media@ghost.io",
                        "BikeMN_E_News@mail.vresp.com",
                    ],
                ),
            },
            credentials=mock_credentials,
        )
        manager = make_manager(config, mock_gmail_client)
        checks = manager.diagnose()
        assert levels_for(checks)["malformed_entries"] == "pass"


class TestNearSplit:
    def test_near_limit_warns(self, mock_credentials, mock_gmail_client):
        # 50 max, 45 entries -> 45 > 40 (80%) and <= 50 -> near split
        entries = [f"u{i}@example.com" for i in range(45)]
        config = Config(
            max_entries_per_filter=50,
            filters={
                "big": FilterConfig(name="big", action="archive", entries=entries),
            },
            credentials=mock_credentials,
        )
        manager = make_manager(config, mock_gmail_client)
        checks = manager.diagnose()
        assert levels_for(checks)["near_split"] == "warn"

    def test_well_under_limit_passes(self, mock_credentials, mock_gmail_client):
        entries = [f"u{i}@example.com" for i in range(10)]
        config = Config(
            max_entries_per_filter=50,
            filters={
                "small": FilterConfig(name="small", action="archive", entries=entries),
            },
            credentials=mock_credentials,
        )
        manager = make_manager(config, mock_gmail_client)
        checks = manager.diagnose()
        assert levels_for(checks)["near_split"] == "pass"


class TestWithinFilterDuplicates:
    def test_exact_dupe_within_filter_warns(self, mock_credentials, mock_gmail_client):
        config = Config(
            max_entries_per_filter=50,
            filters={
                "f": FilterConfig(
                    name="f",
                    action="archive",
                    entries=["a@example.com", "a@example.com"],
                ),
            },
            credentials=mock_credentials,
        )
        manager = make_manager(config, mock_gmail_client)
        checks = manager.diagnose()
        assert levels_for(checks)["within_filter_duplicates"] == "warn"


class TestWildcardShadowing:
    def test_bare_wildcard_shadows_subdomain(self, mock_credentials, mock_gmail_client):
        config = Config(
            max_entries_per_filter=50,
            filters={
                "f": FilterConfig(
                    name="f",
                    action="archive",
                    entries=[
                        "*substack.com",
                        "someone@blog.substack.com",  # covered by *substack.com
                    ],
                ),
            },
            credentials=mock_credentials,
        )
        manager = make_manager(config, mock_gmail_client)
        checks = manager.diagnose()
        assert levels_for(checks)["wildcard_shadowing"] == "warn"
        assert any("blog.substack.com" in d for d in details_for(checks, "wildcard_shadowing"))

    def test_at_wildcard_shadows_concrete_email(self, mock_credentials, mock_gmail_client):
        config = Config(
            max_entries_per_filter=50,
            filters={
                "f": FilterConfig(
                    name="f",
                    action="archive",
                    entries=[
                        "*@example.com",
                        "user@example.com",  # covered by *@example.com
                    ],
                ),
            },
            credentials=mock_credentials,
        )
        manager = make_manager(config, mock_gmail_client)
        checks = manager.diagnose()
        assert levels_for(checks)["wildcard_shadowing"] == "warn"

    def test_no_shadowing_passes(self, mock_credentials, mock_gmail_client):
        config = Config(
            max_entries_per_filter=50,
            filters={
                "f": FilterConfig(
                    name="f",
                    action="archive",
                    entries=["*substack.com", "user@example.com"],
                ),
            },
            credentials=mock_credentials,
        )
        manager = make_manager(config, mock_gmail_client)
        checks = manager.diagnose()
        assert levels_for(checks)["wildcard_shadowing"] == "pass"


class TestCrossFilterDuplicates:
    def test_same_action_warns(self, mock_credentials, mock_gmail_client):
        config = Config(
            max_entries_per_filter=50,
            filters={
                "a": FilterConfig(name="a", action="archive", entries=["dup@example.com"]),
                "b": FilterConfig(name="b", action="archive", entries=["dup@example.com"]),
            },
            credentials=mock_credentials,
        )
        manager = make_manager(config, mock_gmail_client)
        checks = manager.diagnose()
        assert levels_for(checks)["cross_filter_duplicates"] == "warn"

    def test_different_actions_fails(self, mock_credentials, mock_gmail_client):
        config = Config(
            max_entries_per_filter=50,
            filters={
                "a": FilterConfig(name="a", action="archive", entries=["dup@example.com"]),
                "b": FilterConfig(name="b", action="delete", entries=["dup@example.com"]),
            },
            credentials=mock_credentials,
        )
        manager = make_manager(config, mock_gmail_client)
        checks = manager.diagnose()
        assert levels_for(checks)["cross_filter_duplicates"] == "fail"

    def test_no_cross_dupes_passes(self, mock_credentials, mock_gmail_client):
        config = Config(
            max_entries_per_filter=50,
            filters={
                "a": FilterConfig(name="a", action="archive", entries=["a@example.com"]),
                "b": FilterConfig(name="b", action="delete", entries=["b@example.com"]),
            },
            credentials=mock_credentials,
        )
        manager = make_manager(config, mock_gmail_client)
        checks = manager.diagnose()
        assert levels_for(checks)["cross_filter_duplicates"] == "pass"


class TestRollupStacking:
    """Conflicting/duplicated actions are a consequence of rollup, not an error."""

    def test_conflicting_actions_with_rollup_passes(self, mock_credentials, mock_gmail_client):
        config = Config(
            max_entries_per_filter=50,
            filters={
                "newsletters": FilterConfig(
                    name="newsletters",
                    action="label_and_archive",
                    label="Newsletters",
                    entries=["dup@example.com"],
                ),
                "catchall": FilterConfig(
                    name="catchall",
                    action="archive",
                    entries=["dup@example.com"],
                    rollup=True,
                ),
            },
            credentials=mock_credentials,
        )
        manager = make_manager(config, mock_gmail_client)
        checks = manager.diagnose()
        # Would be 'fail' without rollup; rollup makes the overlap intentional.
        assert levels_for(checks)["cross_filter_duplicates"] == "pass"

    def test_same_action_dupes_with_rollup_passes(self, mock_credentials, mock_gmail_client):
        config = Config(
            max_entries_per_filter=50,
            filters={
                "a": FilterConfig(name="a", action="archive", entries=["dup@example.com"]),
                "b": FilterConfig(
                    name="b", action="archive", entries=["dup@example.com"], rollup=True
                ),
            },
            credentials=mock_credentials,
        )
        manager = make_manager(config, mock_gmail_client)
        checks = manager.diagnose()
        assert levels_for(checks)["cross_filter_duplicates"] == "pass"

    def test_conflicting_without_rollup_still_fails(self, mock_credentials, mock_gmail_client):
        config = Config(
            max_entries_per_filter=50,
            filters={
                "a": FilterConfig(
                    name="a", action="archive", entries=["dup@example.com"]
                ),
                "b": FilterConfig(
                    name="b", action="delete", entries=["dup@example.com"], rollup=True
                ),
                "c": FilterConfig(
                    name="c", action="star", entries=["other@example.com"]
                ),
            },
            credentials=mock_credentials,
        )
        # 'c' has a unique entry and no rollup, so the only overlap is the rollup
        # pair (a/b) which is suppressed. Overall should pass.
        manager = make_manager(config, mock_gmail_client)
        checks = manager.diagnose()
        assert levels_for(checks)["cross_filter_duplicates"] == "pass"


class TestResolveWildcardShadowing:
    def test_resolve_removes_shadowed_entries(self, mock_credentials, mock_gmail_client, tmp_path):
        config = Config(
            max_entries_per_filter=50,
            filters={
                "f": FilterConfig(
                    name="f",
                    action="archive",
                    entries=["*substack.com", "someone@blog.substack.com"],
                ),
            },
            credentials=mock_credentials,
        )
        manager = FilterManager(config, mock_gmail_client, config_path=tmp_path / "f.yaml")
        result = manager.resolve("wildcard_shadowing")
        assert result["resolved"]
        assert result["fixed"] == 1
        assert "someone@blog.substack.com" not in config.filters["f"].entries
        assert "*substack.com" in config.filters["f"].entries
        # The config file was saved.
        assert (tmp_path / "f.yaml").is_file()

    def test_resolve_unknown_check(self, mock_credentials, mock_gmail_client, tmp_path):
        config = Config(
            max_entries_per_filter=50,
            filters={"f": FilterConfig(name="f", action="archive", entries=[])},
            credentials=mock_credentials,
        )
        manager = FilterManager(config, mock_gmail_client, config_path=tmp_path / "f.yaml")
        result = manager.resolve("nonexistent")
        assert not result["resolved"]

    def test_resolve_passing_check_is_noop(self, mock_credentials, mock_gmail_client, tmp_path):
        config = Config(
            max_entries_per_filter=50,
            filters={"f": FilterConfig(name="f", action="archive", entries=["a@example.com"])},
            credentials=mock_credentials,
        )
        manager = FilterManager(config, mock_gmail_client, config_path=tmp_path / "f.yaml")
        result = manager.resolve("wildcard_shadowing")
        assert not result["resolved"]
        assert "already passing" in result["reason"]


class TestResolveWithinFilterDuplicates:
    def test_resolve_dedupes(self, mock_credentials, mock_gmail_client, tmp_path):
        config = Config(
            max_entries_per_filter=50,
            filters={
                "f": FilterConfig(
                    name="f", action="archive", entries=["a@example.com", "a@example.com", "b@example.com"]
                ),
            },
            credentials=mock_credentials,
        )
        manager = FilterManager(config, mock_gmail_client, config_path=tmp_path / "f.yaml")
        result = manager.resolve("within_filter_duplicates")
        assert result["resolved"]
        assert result["fixed"] == 1
        assert config.filters["f"].entries == ["a@example.com", "b@example.com"]


class TestResolveCrossFilterDuplicates:
    def test_resolve_same_action_keeps_first(self, mock_credentials, mock_gmail_client, tmp_path):
        config = Config(
            max_entries_per_filter=50,
            filters={
                "a": FilterConfig(name="a", action="archive", entries=["dup@example.com"]),
                "b": FilterConfig(name="b", action="archive", entries=["dup@example.com"]),
            },
            credentials=mock_credentials,
        )
        manager = FilterManager(config, mock_gmail_client, config_path=tmp_path / "f.yaml")
        result = manager.resolve("cross_filter_duplicates")
        assert result["resolved"]
        assert result["fixed"] == 1
        assert config.filters["a"].entries == ["dup@example.com"]
        assert config.filters["b"].entries == []

    def test_resolve_conflicting_actions_refuses(self, mock_credentials, mock_gmail_client, tmp_path):
        config = Config(
            max_entries_per_filter=50,
            filters={
                "a": FilterConfig(name="a", action="archive", entries=["dup@example.com"]),
                "b": FilterConfig(name="b", action="delete", entries=["dup@example.com"]),
            },
            credentials=mock_credentials,
        )
        manager = FilterManager(config, mock_gmail_client, config_path=tmp_path / "f.yaml")
        result = manager.resolve("cross_filter_duplicates")
        assert not result["resolved"]
        assert "manual" in result["reason"]
