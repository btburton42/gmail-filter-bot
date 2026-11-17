"""Tests for the `audit` command and its helpers."""

import argparse
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import gmail_filter_bot.cli as cli
from gmail_filter_bot import settings
from gmail_filter_bot.config import Config, Credentials, FilterConfig
from gmail_filter_bot.filter_manager import FilterManager
from gmail_filter_bot.triage import TriageHistory


@pytest.fixture
def mock_credentials():
    return Credentials(client_id="test-id", client_secret="test-secret")


@pytest.fixture
def mock_config(mock_credentials):
    return Config(
        max_entries_per_filter=50,
        filters={
            "newsletters": FilterConfig(
                name="newsletters",
                action="label_only",
                label="Newsletters",
                entries=["a@x.com", "b@x.com", "bad@x.com"],
            ),
            "newsletters/tech": FilterConfig(
                name="newsletters/tech",
                action="label_and_archive",
                label="newsletters/tech",
                entries=["c@x.com"],
            ),
            "work": FilterConfig(
                name="work",
                action="label_and_archive",
                label="Work",
                entries=["e@x.com"],
            ),
            "empty": FilterConfig(
                name="empty", action="label_only", label="Empty", entries=[],
            ),
        },
        credentials=mock_credentials,
    )


@pytest.fixture
def patched_settings(monkeypatch, tmp_path):
    """Point settings at a temp file so defaults/isolation apply."""
    monkeypatch.setattr(settings, "user_config_path", lambda: tmp_path / "cfg.yaml")


@pytest.fixture
def make_manager(mock_config, tmp_path):
    """Factory: build a FilterManager with stubs for audit."""

    def _make():
        client = MagicMock()
        mgr = FilterManager(mock_config, client, tmp_path / "filters.yaml")
        return mgr, mock_config

    return _make


def _run_audit(mgr, config, keys, dest=None, label=None, monkeypatch=None):
    """Drive cmd_audit with a scripted single-key reader and optional move dest.

    Stubs `_read_single_key`, `_prompt_audit_filter_selection`, `config.save`,
    and `_offer_apply` so the run is fully offline.
    """
    it = iter(keys)

    def fake_read(prompt):
        return next(it)

    def fake_dest(c, cur, q):
        return dest

    mgr.config.save = lambda p: None
    monkeypatch.setattr(cli, "_read_single_key", fake_read)
    monkeypatch.setattr(cli, "_prompt_audit_filter_selection", fake_dest)
    monkeypatch.setattr(cli, "_offer_apply", lambda m: 0)
    args = argparse.Namespace(label=label)
    return cli.cmd_audit(mgr, args)


class TestResolveAuditFilters:
    """Tests for _resolve_audit_filters (no-arg / glob / exact / edge cases)."""

    def test_no_arg_reviews_all_nonempty_filters(self, mock_config):
        names, err = cli._resolve_audit_filters(mock_config, None)
        assert err is None
        assert names == ["newsletters", "newsletters/tech", "work"]
        assert "empty" not in names

    def test_glob_matches_sublabels(self, mock_config):
        names, err = cli._resolve_audit_filters(mock_config, "newsletters/*")
        assert err is None
        # The bare "newsletters" filter does not match "newsletters/*".
        assert names == ["newsletters/tech"]

    def test_exact_filter_name(self, mock_config):
        names, err = cli._resolve_audit_filters(mock_config, "newsletters")
        assert err is None
        assert names == ["newsletters"]

    def test_missing_filter_errors(self, mock_config):
        names, err = cli._resolve_audit_filters(mock_config, "nope")
        assert names == []
        assert "not found" in err

    def test_empty_filter_errors(self, mock_config):
        names, err = cli._resolve_audit_filters(mock_config, "empty")
        assert names == []
        assert "no entries" in err


class TestTriageHistory:
    """Tests for the shared undo stack."""

    def test_push_undo_pop(self):
        h = TriageHistory()
        assert h.empty
        assert h.undo() is None
        h.push(0, {"action": "keep"})
        h.push(1, {"action": "move"})
        assert not h.empty
        assert len(h) == 2
        idx, rec = h.undo()
        assert idx == 1 and rec["action"] == "move"
        idx, rec = h.undo()
        assert idx == 0 and rec["action"] == "keep"
        assert h.empty


class TestAuditSessionActions:
    """Audit decisions, driven through the session rather than the old prompt.

    These cover the behaviour the old ``_apply_audit_action`` /
    ``_apply_audit_undo`` pair provided, minus the console printing: the local
    config mutation and the undo record. The interactive part is covered in
    ``test_tui_screens.py``.
    """

    ENTRIES = ["a@x.com", "b@x.com", "bad@x.com"]

    def _session(self, mock_config):
        from gmail_filter_bot.tui.controller import TriageController
        from gmail_filter_bot.tui.sessions import AUDIT, Item, TriageSession
        from gmail_filter_bot.tui.state import TriageState

        client = MagicMock()
        manager = FilterManager(mock_config, client, Path("filters.yaml"))
        state = TriageState(config=mock_config)
        controller = TriageController(manager, state)
        items = [Item(email=e, filter_name="newsletters") for e in self.ENTRIES]
        return TriageSession(manager, state, controller, kind=AUDIT, items=items), client

    def test_keep_leaves_entries_unchanged(self, mock_config):
        session, _client = self._session(mock_config)
        result = session.keep_item(session.current)
        assert result.message == "kept"
        assert mock_config.filters["newsletters"].entries == self.ENTRIES
        assert session.state.counters["kept"] == 1

    def test_move_recategorizes_and_undo_restores(self, mock_config):
        session, _client = self._session(mock_config)
        session.pos = 1
        session.move_item(session.current, "work")
        assert "b@x.com" not in mock_config.filters["newsletters"].entries
        assert "b@x.com" in mock_config.filters["work"].entries
        assert session.state.counters["moved"] == 1

        result = session.undo()
        assert result.index == 1
        assert "b@x.com" in mock_config.filters["newsletters"].entries
        assert "b@x.com" not in mock_config.filters["work"].entries
        assert session.state.counters["moved"] == 0

    def test_move_to_the_same_filter_is_a_keep(self, mock_config):
        session, _client = self._session(mock_config)
        session.move_item(session.current, "newsletters")
        assert session.state.counters["kept"] == 1
        assert session.state.counters["moved"] == 0
        assert mock_config.filters["newsletters"].entries == self.ENTRIES

    def test_spam_moves_locally_without_touching_gmail(self, mock_config):
        """Audit has never reported spam to Gmail; deferral must not add that."""
        session, client = self._session(mock_config)
        session.spam_item(session.current, 0)
        assert "a@x.com" not in mock_config.filters["newsletters"].entries
        assert "a@x.com" in mock_config.filters["spam"].entries
        assert session.state.counters["spammed"] == 1
        # Nothing was queued and no network call was made.
        assert session.state.spam_queue == []
        assert not client.report_spam.called

    def test_undo_spam_returns_the_entry_and_drops_the_filter(self, mock_config):
        session, _client = self._session(mock_config)
        session.spam_item(session.current, 0)
        session.undo()
        assert "a@x.com" in mock_config.filters["newsletters"].entries
        assert "spam" not in mock_config.filters
        assert session.state.counters["spammed"] == 0

    def test_remove_drops_the_entry_and_undo_restores_it(self, mock_config):
        session, _client = self._session(mock_config)
        session.pos = 2
        session.remove_item(session.current)
        assert mock_config.filters["newsletters"].entries == ["a@x.com", "b@x.com"]
        assert session.state.counters["removed"] == 1
        session.undo()
        assert mock_config.filters["newsletters"].entries == self.ENTRIES
        assert session.state.counters["removed"] == 0

    def test_undo_without_history_is_a_no_op(self, mock_config):
        session, _client = self._session(mock_config)
        result = session.undo()
        assert "nothing to undo" in result.message
        assert result.advance is False


class TestCmdAuditAftermath:
    """cmd_audit's post-session half: summary, save, and the push offer."""

    def _drive(self, mgr, monkeypatch, actions):
        """Run cmd_audit with the UI replaced by a scripted session mutator."""
        from gmail_filter_bot.tui.app import TriageResult

        calls: dict = {}

        def fake_run_triage(session, **_kwargs):
            for action in actions:
                action(session)
            return TriageResult(session=session, completed=True, keep=None, reviewed=False)

        saved = {"called": False}

        def fake_save(path):
            saved["called"] = True

        mgr.config.save = fake_save
        monkeypatch.setattr(cli, "run_triage", fake_run_triage)
        def fake_offer(_manager):
            calls["offered"] = True
            return 0

        monkeypatch.setattr(cli, "_offer_apply", fake_offer)
        rc = cli.cmd_audit(mgr, argparse.Namespace(label="newsletters"))
        return rc, saved["called"], calls

    def test_keeping_everything_saves_nothing(
        self, make_manager, patched_settings, monkeypatch
    ):
        mgr, _ = make_manager()
        rc, saved, calls = self._drive(
            mgr, monkeypatch, [lambda s: s.keep_item(s.current) for _ in range(3)]
        )
        assert rc == 0
        assert saved is False
        assert "offered" not in calls

    def test_a_move_saves_and_offers_to_push(
        self, make_manager, mock_config, patched_settings, monkeypatch
    ):
        mgr, _ = make_manager()
        rc, saved, calls = self._drive(
            mgr, monkeypatch, [lambda s: s.move_item(s.current, "work")]
        )
        assert rc == 0
        assert saved is True
        assert calls.get("offered") is True
        assert "a@x.com" in mock_config.filters["work"].entries
        assert "a@x.com" not in mock_config.filters["newsletters"].entries

    def test_queued_work_is_never_fired_by_audit(
        self, make_manager, mock_config, patched_settings, monkeypatch
    ):
        """Audit queues no spam reports, so its aftermath has nothing to send."""
        mgr, _ = make_manager()
        self._drive(mgr, monkeypatch, [lambda s: s.spam_item(s.current, 0)])
        assert not mgr.client.report_spam.called
        assert "a@x.com" in mock_config.filters["spam"].entries


def settings_get_audit_km():
    from gmail_filter_bot import hand_mode

    return hand_mode.get_audit_key_map()
