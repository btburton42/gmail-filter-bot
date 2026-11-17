"""Tests for the user-level settings module."""

from pathlib import Path

import pytest

from gmail_filter_bot import settings


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """Point settings at a temp config file."""
    cfg = tmp_path / "config.yaml"
    monkeypatch.setattr(settings, "user_config_path", lambda: cfg)
    return cfg


class TestHandMode:
    def test_default_is_left(self, isolated):
        assert settings.get_hand_mode() == "left"

    def test_set_valid(self, isolated):
        settings.set("hand-mode", "right")
        assert settings.get("hand-mode") == "right"
        assert settings.get_hand_mode() == "right"

    def test_set_case_insensitive(self, isolated):
        settings.set("hand-mode", "RIGHT")
        assert settings.get("hand-mode") == "right"

    def test_set_invalid_raises(self, isolated):
        with pytest.raises(ValueError):
            settings.set("hand-mode", "middle")

    def test_set_unknown_key_raises(self, isolated):
        with pytest.raises(ValueError):
            settings.set("nope", "x")


class TestFiltersPath:
    def test_default_none(self, isolated):
        assert settings.get("filters-path") is None

    def test_set_resolves_absolute(self, isolated):
        target = Path("/tmp/some/filters.yaml")
        settings.set("filters-path", str(target))
        assert settings.get("filters-path") == str(target.resolve())

    def test_set_empty_clears(self, isolated):
        settings.set("filters-path", "/tmp/f.yaml")
        settings.set("filters-path", "")
        assert settings.get("filters-path") is None

    def test_legacy_underscore_key_is_read(self, isolated, monkeypatch):
        # A pre-config-command file using the old `filters_path` key.
        isolated.write_text("filters_path: /legacy/filters.yaml\n")
        assert settings.get("filters-path") == "/legacy/filters.yaml"

    def test_set_migrates_off_legacy_key(self, isolated):
        isolated.write_text("filters_path: /legacy/filters.yaml\n")
        settings.set("filters-path", "/new/filters.yaml")
        import yaml

        data = yaml.safe_load(isolated.read_text())
        assert "filters_path" not in data
        assert data["filters-path"] == str(Path("/new/filters.yaml").resolve())

    def test_unset(self, isolated):
        settings.set("filters-path", "/tmp/f.yaml")
        assert settings.unset("filters-path") is True
        assert settings.get("filters-path") is None
        assert settings.unset("filters-path") is False


class TestFilterAccess:
    def test_default_empty(self, isolated):
        assert settings.get_filter_access() == {}

    def test_touch_records_timestamp(self, isolated):
        settings.touch_filter_access("newsletters")
        access = settings.get_filter_access()
        assert "newsletters" in access
        # ISO timestamp parses back to a datetime.
        from datetime import datetime

        datetime.fromisoformat(access["newsletters"])

    def test_touch_updates_existing(self, isolated):
        settings.touch_filter_access("newsletters")
        first = settings.get_filter_access()["newsletters"]
        settings.touch_filter_access("newsletters")
        second = settings.get_filter_access()["newsletters"]
        assert second >= first


class TestAllVisible:
    def test_lists_known_keys(self, isolated):
        result = settings.all_visible()
        assert set(result.keys()) == {"hand-mode", "filters-path", "theme"}
        assert result["hand-mode"] == "left"
        assert result["filters-path"] is None
        assert result["theme"] == "textual-dark"
