"""Tests for path resolution and location memory."""

from pathlib import Path

import pytest

from gmail_filter_bot import paths, settings


@pytest.fixture
def isolated_user_config(tmp_path, monkeypatch):
    """Point the user config dir at a temp dir so tests don't touch the real one.

    ``settings`` owns the user-config file, so it must be patched; ``paths``
    delegators are patched too for completeness.
    """
    config_dir = tmp_path / "userconfig"
    config_file = config_dir / settings.USER_CONFIG_FILENAME
    monkeypatch.setattr(settings, "user_config_dir", lambda: config_dir)
    monkeypatch.setattr(settings, "user_config_path", lambda: config_file)
    monkeypatch.setattr(paths, "user_config_dir", lambda: config_dir)
    monkeypatch.setattr(paths, "user_config_path", lambda: config_file)
    return config_dir


@pytest.fixture
def isolated_cwd(tmp_path, monkeypatch):
    """Run path resolution from a temp working directory."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.setattr(Path, "cwd", staticmethod(lambda: cwd))
    return cwd


class TestResolveFiltersPath:
    def test_explicit_is_used_verbatim(self, isolated_cwd, isolated_user_config):
        explicit = Path("/somewhere/else/filters.yaml")
        assert paths.resolve_filters_path(explicit) == explicit

    def test_cwd_filters_yaml_wins(self, isolated_cwd, isolated_user_config):
        (isolated_cwd / "filters.yaml").write_text("filters: {}")
        # Even if a parent has one, CWD wins.
        (isolated_cwd.parent / "filters.yaml").write_text("filters: {}")
        result = paths.resolve_filters_path()
        assert result == isolated_cwd / "filters.yaml"

    def test_walks_upward(self, isolated_cwd, isolated_user_config):
        nested = isolated_cwd / "a" / "b"
        nested.mkdir(parents=True)
        root_filters = isolated_cwd / "filters.yaml"
        root_filters.write_text("filters: {}")
        result = paths.resolve_filters_path(start=nested)
        assert result == root_filters

    def test_falls_back_to_remembered(self, isolated_cwd, isolated_user_config):
        # No filters.yaml in CWD or any ancestor.
        remembered = isolated_cwd / "remembered" / "filters.yaml"
        remembered.parent.mkdir(parents=True)
        remembered.write_text("filters: {}")
        paths.remember_filters_path(remembered)
        assert paths.resolve_filters_path() == remembered

    def test_remembered_path_must_exist(self, isolated_cwd, isolated_user_config):
        paths.remember_filters_path(Path("/nonexistent/filters.yaml"))
        # No CWD file, no ancestor file, remembered gone -> default fallback.
        result = paths.resolve_filters_path()
        assert result == isolated_cwd / "filters.yaml"

    def test_default_fallback_when_nothing_found(self, isolated_cwd, isolated_user_config):
        result = paths.resolve_filters_path()
        assert result == isolated_cwd / "filters.yaml"

    def test_explicit_overrides_remembered(self, isolated_cwd, isolated_user_config):
        remembered = isolated_cwd / "remembered" / "filters.yaml"
        remembered.parent.mkdir(parents=True)
        remembered.write_text("filters: {}")
        paths.remember_filters_path(remembered)
        explicit = Path("/explicit/filters.yaml")
        assert paths.resolve_filters_path(explicit) == explicit


class TestRememberForget:
    def test_remember_and_recall_roundtrip(self, isolated_user_config, tmp_path):
        target = tmp_path / "filters.yaml"
        target.write_text("filters: {}")
        paths.remember_filters_path(target)
        recalled = paths.recall_filters_path()
        assert recalled is not None
        assert recalled == target.resolve()

    def test_recall_none_when_unset(self, isolated_user_config):
        assert paths.recall_filters_path() is None

    def test_recall_none_when_missing_on_disk(self, isolated_user_config, tmp_path):
        paths.remember_filters_path(tmp_path / "filters.yaml")  # does not exist
        assert paths.recall_filters_path() is None

    def test_forget_clears(self, isolated_user_config, tmp_path):
        target = tmp_path / "filters.yaml"
        target.write_text("filters: {}")
        paths.remember_filters_path(target)
        assert paths.forget_filters_path() is True
        assert paths.recall_filters_path() is None
        # Forgetting again is a no-op.
        assert paths.forget_filters_path() is False

    def test_remember_overwrites_previous(self, isolated_user_config, tmp_path):
        a = tmp_path / "a" / "filters.yaml"
        a.parent.mkdir(parents=True)
        a.write_text("filters: {}")
        b = tmp_path / "b" / "filters.yaml"
        b.parent.mkdir(parents=True)
        b.write_text("filters: {}")
        paths.remember_filters_path(a)
        paths.remember_filters_path(b)
        assert paths.recall_filters_path() == b.resolve()


class TestSiblingDir:
    def test_uses_filters_parent_when_exists(self, tmp_path):
        f = tmp_path / "proj" / "filters.yaml"
        f.parent.mkdir(parents=True)
        f.write_text("filters: {}")
        assert paths.sibling_dir(f) == f.parent

    def test_uses_cwd_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "cwd", staticmethod(lambda: tmp_path))
        assert paths.sibling_dir(tmp_path / "filters.yaml") == tmp_path


class TestFindUpward:
    def test_finds_in_parent(self, tmp_path):
        target = tmp_path / "filters.yaml"
        target.write_text("filters: {}")
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        assert paths.find_upward("filters.yaml", nested) == target

    def test_returns_none_when_absent(self, tmp_path):
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        assert paths.find_upward("filters.yaml", nested) is None
