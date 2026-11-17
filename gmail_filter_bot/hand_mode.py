"""Key maps for the one-handed triage commands (``label`` and ``audit``).

Each hand-mode maps a triage *action* to a ``(key, word)`` pair:

  - ``key``  is the single character the user types (shown inside the ``[]``).
  - ``word`` is the human label shown next to the key.

Actions are stable identifiers used by the dispatch logic; only the key/word
hints change between modes, so the action *names* never need remapping.

Two command vocabularies share the same hand positions:

  label (``label`` command) -- triage *senders*:
    left:  f filter / a allow / s spam / u unsubscribe / k skip / e expand / q quit / z undo
    right: j filter / a allow / l spam / i unsubscribe / k skip / e expand / p quit / u undo

  ``unsubscribe`` and ``spam`` are *deferred*, not one-way: the decision is
  queued (and shown in the right-hand panel) and the network call happens once,
  after the session, at a review screen. Because nothing has been sent while
  you are still triaging, ``undo`` can genuinely reverse both. The local config
  side of a decision is applied immediately, which is why undo says
  "(config only)" for the actions that also had a deferred half.

  ``expand`` is a no-op on the sender: it shows the body of the selected message
  and makes no changes, for senders you don't recognise by name.

  audit (``audit`` command) -- review *entries* already bucketed into a filter.
  ``allow`` is replaced by ``remove`` (delete the entry) and ``filter`` becomes
  ``move`` (recategorize to another filter); the muscle memory is identical:
    left:  f move / a remove / s spam / k keep / q quit / z undo
    right: j move / a remove / l spam / k keep / p quit / u undo

  audit never contacts Gmail: its ``spam`` moves the entry into the local
  ``spam`` filter (a move, tallied as spammed) rather than queueing a report.

``undo`` rewinds the last action (a finger-slip recovery); it is a meta-action
that pops the history stack and re-presents the previous item.
"""

from __future__ import annotations

from .settings import DEFAULT_HAND_MODE, get_hand_mode

# action -> (key, word) for the `label` command.
KEY_MAPS: dict[str, dict[str, tuple[str, str]]] = {
    "left": {
        "filter": ("f", "filter"),
        "allow": ("a", "allow"),
        "spam": ("s", "spam"),
        "unsubscribe": ("u", "unsubscribe"),
        "skip": ("k", "skip"),
        "expand": ("e", "expand"),
        "quit": ("q", "quit"),
        "undo": ("z", "undo"),
    },
    "right": {
        "filter": ("j", "filter"),
        "allow": ("a", "allow"),
        "spam": ("l", "spam"),
        # 'u' is undo on the right hand, so unsubscribe takes the adjacent 'i'.
        "unsubscribe": ("i", "unsubscribe"),
        "skip": ("k", "skip"),
        "expand": ("e", "expand"),
        "quit": ("p", "quit"),
        "undo": ("u", "undo"),
    },
}

# action -> (key, word) for the `audit` command.
AUDIT_KEY_MAPS: dict[str, dict[str, tuple[str, str]]] = {
    "left": {
        "move": ("f", "move"),
        "remove": ("a", "remove"),
        "spam": ("s", "spam"),
        "keep": ("k", "keep"),
        "quit": ("q", "quit"),
        "undo": ("z", "undo"),
    },
    "right": {
        "move": ("j", "move"),
        "remove": ("a", "remove"),
        "spam": ("l", "spam"),
        "keep": ("k", "keep"),
        "quit": ("p", "quit"),
        "undo": ("u", "undo"),
    },
}

# The order actions appear in the prompt line.
PROMPT_ORDER = ("filter", "allow", "spam", "unsubscribe", "skip", "expand", "quit", "undo")
AUDIT_PROMPT_ORDER = ("move", "remove", "spam", "keep", "quit", "undo")


def get_key_map() -> dict[str, tuple[str, str]]:
    """Return the active label action -> (key, word) map for the hand-mode."""
    mode = get_hand_mode()
    return KEY_MAPS.get(mode, KEY_MAPS[DEFAULT_HAND_MODE])


def get_audit_key_map() -> dict[str, tuple[str, str]]:
    """Return the active audit action -> (key, word) map for the hand-mode."""
    mode = get_hand_mode()
    return AUDIT_KEY_MAPS.get(mode, AUDIT_KEY_MAPS[DEFAULT_HAND_MODE])


def prompt_hint(key_map: dict[str, tuple[str, str]] | None = None) -> str:
    """Build the ``[x] word / [y] word ...`` hint string for the label prompt."""
    km = key_map or get_key_map()
    return " / ".join(f"[{km[a][0]}] {km[a][1]}" for a in PROMPT_ORDER)


def audit_prompt_hint(key_map: dict[str, tuple[str, str]] | None = None) -> str:
    """Build the hint string for the audit prompt."""
    km = key_map or get_audit_key_map()
    return " / ".join(f"[{km[a][0]}] {km[a][1]}" for a in AUDIT_PROMPT_ORDER)


def matches(action: str, choice: str, key_map: dict[str, tuple[str, str]] | None = None) -> bool:
    """True if `choice` matches the action's key or word (case-insensitive)."""
    km = key_map or get_key_map()
    if action not in km:
        return False
    key, word = km[action]
    return choice == key.lower() or choice == word.lower()


def valid_keys(
    key_map: dict[str, tuple[str, str]] | None = None,
    order: tuple[str, ...] = PROMPT_ORDER,
) -> list[str]:
    """Return the list of valid single-character keys for error messages."""
    km = key_map or get_key_map()
    return [km[a][0] for a in order]


def audit_valid_keys(key_map: dict[str, tuple[str, str]] | None = None) -> list[str]:
    """Return the list of valid single-character keys for the audit prompt."""
    return valid_keys(key_map or get_audit_key_map(), AUDIT_PROMPT_ORDER)
