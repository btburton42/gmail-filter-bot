"""User-level settings stored in ``~/.config/gmail-filter-bot/config.yaml``.

This is the single owner of the user-config file. It stores:

  - ``hand-mode``      -- "left" or "right" (key map for the ``label`` command)
  - ``filters-path``   -- remembered location of ``filters.yaml``
  - ``filter-access``  -- internal map of filter-name -> last-accessed ISO timestamp,
                          used to rank the label command's typeahead by recency

The CLI exposes ``hand-mode`` and ``filters-path`` via the ``config`` command.
``filter-access`` is bookkeeping managed by the app and is not directly settable.

For backwards compatibility, the legacy ``filters_path`` (underscore) key is
still read if present, but new writes always use ``filters-path``.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import yaml

APP_NAME = "gmail-filter-bot"
USER_CONFIG_FILENAME = "config.yaml"

# CLI / storage keys (hyphenated).
SETTING_HAND_MODE = "hand-mode"
SETTING_FILTERS_PATH = "filters-path"
SETTING_FILTER_ACCESS = "filter-access"
SETTING_THEME = "theme"

# Legacy (pre-config-command) storage key for the filters path.
_LEGACY_FILTERS_PATH = "filters_path"

HAND_MODES = ("left", "right")
DEFAULT_HAND_MODE = "left"

DEFAULT_THEME = "textual-dark"

# Settings the user can see and set via `gfb config`.
VISIBLE_SETTINGS = (SETTING_HAND_MODE, SETTING_FILTERS_PATH, SETTING_THEME)
SETTABLE_SETTINGS = (SETTING_HAND_MODE, SETTING_FILTERS_PATH, SETTING_THEME)

DESCRIPTIONS = {
    SETTING_HAND_MODE: (
        "Key map for the `label` command. 'left' keeps all triage keys on the "
        "left hand (f/a/s/k/q); 'right' maps them to j/a/l/k/p."
    ),
    SETTING_FILTERS_PATH: (
        "Remembered absolute path to filters.yaml, used when the file isn't "
        "found in the current directory or its parents."
    ),
    SETTING_THEME: (
        "Textual theme for the interactive triage UI. Any built-in Textual "
        "theme works, e.g. textual-dark, nord, gruvbox, tokyo-night, monokai, "
        "catppuccin-mocha, dracula, flexoki."
    ),
}

# Diagnosis types that `doctor resolve` can auto-fix (kept here so the CLI
# can advertise them without importing filter_manager).
RESOLVABLE_CHECKS = (
    "wildcard_shadowing",
    "within_filter_duplicates",
    "cross_filter_duplicates",
)


def user_config_dir() -> Path:
    """Return the user-level config directory for this app (XDG-aware)."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / APP_NAME


def user_config_path() -> Path:
    """Return the path to the user-level config file."""
    return user_config_dir() / USER_CONFIG_FILENAME


def load() -> dict:
    """Load the raw user config dict (empty dict if missing/corrupt)."""
    path = user_config_path()
    if not path.is_file():
        return {}
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def save(data: dict) -> None:
    """Persist the user config dict, creating the directory if needed."""
    path = user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def _get_raw(key: str, default=None):
    return load().get(key, default)


def get(key: str, default=None):
    """Read a setting, with legacy fallback for filters-path."""
    if key == SETTING_FILTERS_PATH:
        raw = load()
        return raw.get(SETTING_FILTERS_PATH) or raw.get(_LEGACY_FILTERS_PATH) or default
    return _get_raw(key, default)


def unset(key: str) -> bool:
    """Remove a key from the user config. Returns True if it was present."""
    data = load()
    existed = key in data
    if existed:
        del data[key]
        save(data)
    # Also clean legacy filters_path when unsetting filters-path.
    if key == SETTING_FILTERS_PATH and _LEGACY_FILTERS_PATH in data:
        del data[_LEGACY_FILTERS_PATH]
        save(data)
        existed = True
    return existed


def set(key: str, value) -> None:
    """Validate and persist a setting.

    Raises ValueError for unknown or invalid values.
    """
    if key not in SETTABLE_SETTINGS:
        raise ValueError(
            f"Unknown setting '{key}'. Settable settings: {', '.join(SETTABLE_SETTINGS)}."
        )

    data = load()

    if key == SETTING_HAND_MODE:
        value = str(value).lower()
        if value not in HAND_MODES:
            raise ValueError(
                f"Invalid hand-mode '{value}'. Choose one of: {', '.join(HAND_MODES)}."
            )
        data[SETTING_HAND_MODE] = value
    elif key == SETTING_FILTERS_PATH:
        if value in (None, "", []):
            # Empty value clears the setting.
            data.pop(SETTING_FILTERS_PATH, None)
            data.pop(_LEGACY_FILTERS_PATH, None)
        else:
            resolved = Path(str(value)).expanduser().resolve()
            data[SETTING_FILTERS_PATH] = str(resolved)
            data.pop(_LEGACY_FILTERS_PATH, None)  # migrate off legacy key
    else:  # pragma: no cover - guarded by the SETTABLE_SETTINGS check above
        data[key] = value

    save(data)


def all_visible() -> dict:
    """Return a dict of visible settings -> current value (defaults applied)."""
    return {
        SETTING_HAND_MODE: get_hand_mode(),
        SETTING_FILTERS_PATH: get(SETTING_FILTERS_PATH),
        SETTING_THEME: get_theme_name(),
    }


def get_hand_mode() -> str:
    """Return the configured hand-mode, defaulting to left."""
    mode = get(SETTING_HAND_MODE, DEFAULT_HAND_MODE)
    return mode if mode in HAND_MODES else DEFAULT_HAND_MODE


def get_theme_name() -> str:
    """Return the configured Textual theme, defaulting to textual-dark."""
    name = get(SETTING_THEME, DEFAULT_THEME)
    return name if isinstance(name, str) and name else DEFAULT_THEME


def get_filter_access() -> dict[str, str]:
    """Return the filter-name -> ISO timestamp map (empty if absent)."""
    raw = get(SETTING_FILTER_ACCESS, {})
    return raw if isinstance(raw, dict) else {}


def touch_filter_access(name: str) -> None:
    """Record that filter `name` was just accessed (used for typeahead ranking)."""
    data = load()
    access = data.get(SETTING_FILTER_ACCESS, {})
    if not isinstance(access, dict):
        access = {}
    access[name] = datetime.now(timezone.utc).isoformat()
    data[SETTING_FILTER_ACCESS] = access
    save(data)
