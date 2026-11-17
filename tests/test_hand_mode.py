"""Tests for the hand-mode key maps."""

from gmail_filter_bot import hand_mode, settings


def _isolate_user_config(monkeypatch, tmp_path):
    """Point settings at a nonexistent file so defaults apply."""
    monkeypatch.setattr(settings, "user_config_path", lambda: tmp_path / "config.yaml")


def test_left_key_map_uses_k_for_skip(monkeypatch, tmp_path):
    _isolate_user_config(monkeypatch, tmp_path)
    km = hand_mode.get_key_map()
    assert km["filter"] == ("f", "filter")
    assert km["allow"] == ("a", "allow")
    assert km["spam"] == ("s", "spam")
    # Unsubscribe is the one-way opt-out on the 'u' key.
    assert km["unsubscribe"] == ("u", "unsubscribe")
    # Expand fetches the message body to identify an unknown sender.
    assert km["expand"] == ("e", "expand")
    # k consistently means skip in both hand modes.
    assert km["skip"] == ("k", "skip")
    assert km["quit"] == ("q", "quit")
    # Undo (rewind) lives on the 'z' key in left-hand mode.
    assert km["undo"] == ("z", "undo")


def test_right_key_map_uses_jkl(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "get", lambda key, default=None: "right")
    km = hand_mode.get_key_map()
    assert km["filter"] == ("j", "filter")
    assert km["allow"] == ("a", "allow")
    assert km["spam"] == ("l", "spam")
    # 'u' is undo on the right hand, so unsubscribe moves to 'i'.
    assert km["unsubscribe"] == ("i", "unsubscribe")
    assert km["expand"] == ("e", "expand")
    assert km["skip"] == ("k", "skip")
    assert km["quit"] == ("p", "quit")
    # Right-hand undo is the vim-conventional 'u' key.
    assert km["undo"] == ("u", "undo")


def test_prompt_hint_orders_actions(monkeypatch, tmp_path):
    _isolate_user_config(monkeypatch, tmp_path)
    hint = hand_mode.prompt_hint()
    assert "[f] filter" in hint
    assert "[k] skip" in hint
    assert "[q] quit" in hint
    assert "[z] undo" in hint
    assert "[u] unsubscribe" in hint
    assert "[e] expand" in hint
    assert hint.index("filter") < hint.index("allow") < hint.index("spam")
    assert hint.index("spam") < hint.index("unsubscribe")
    assert hint.index("spam") < hint.index("skip") < hint.index("quit")
    # undo is shown last, after quit.
    assert hint.index("quit") < hint.index("undo")


def test_matches_accepts_key_or_word(monkeypatch, tmp_path):
    _isolate_user_config(monkeypatch, tmp_path)
    km = hand_mode.get_key_map()
    assert hand_mode.matches("filter", "f", km)
    assert hand_mode.matches("filter", "filter", km)
    assert hand_mode.matches("skip", "k", km)
    assert hand_mode.matches("skip", "skip", km)
    assert not hand_mode.matches("filter", "x", km)
    # k is shared; right-mode allow moves onto the same key as left mode.
    assert hand_mode.matches("skip", "k", hand_mode.KEY_MAPS["right"])
    assert hand_mode.matches("allow", "a", hand_mode.KEY_MAPS["right"])


def test_valid_keys(monkeypatch, tmp_path):
    _isolate_user_config(monkeypatch, tmp_path)
    assert hand_mode.valid_keys() == ["f", "a", "s", "u", "k", "e", "q", "z"]


class TestAuditKeyMaps:
    """Tests for the audit command's one-handed key maps."""

    def test_left_audit_key_map(self, monkeypatch, tmp_path):
        _isolate_user_config(monkeypatch, tmp_path)
        km = hand_mode.get_audit_key_map()
        assert km["move"] == ("f", "move")
        assert km["remove"] == ("a", "remove")
        assert km["spam"] == ("s", "spam")
        assert km["keep"] == ("k", "keep")
        assert km["quit"] == ("q", "quit")
        assert km["undo"] == ("z", "undo")

    def test_right_audit_key_map(self, monkeypatch, tmp_path):
        monkeypatch.setattr(settings, "get", lambda key, default=None: "right")
        km = hand_mode.get_audit_key_map()
        assert km["move"] == ("j", "move")
        assert km["remove"] == ("a", "remove")
        assert km["spam"] == ("l", "spam")
        assert km["keep"] == ("k", "keep")
        assert km["quit"] == ("p", "quit")
        assert km["undo"] == ("u", "undo")

    def test_audit_prompt_hint(self, monkeypatch, tmp_path):
        _isolate_user_config(monkeypatch, tmp_path)
        hint = hand_mode.audit_prompt_hint()
        assert "[f] move" in hint
        assert "[a] remove" in hint
        assert "[k] keep" in hint
        assert "[z] undo" in hint
        assert hint.index("move") < hint.index("remove") < hint.index("spam")
        assert hint.index("spam") < hint.index("keep") < hint.index("quit")

    def test_audit_valid_keys(self, monkeypatch, tmp_path):
        _isolate_user_config(monkeypatch, tmp_path)
        assert hand_mode.audit_valid_keys() == ["f", "a", "s", "k", "q", "z"]

    def test_audit_matches_uses_audit_map(self, monkeypatch, tmp_path):
        _isolate_user_config(monkeypatch, tmp_path)
        km = hand_mode.get_audit_key_map()
        assert hand_mode.matches("move", "f", km)
        assert hand_mode.matches("remove", "a", km)
        assert hand_mode.matches("keep", "k", km)
        assert not hand_mode.matches("allow", "a", km)  # no allow in audit
