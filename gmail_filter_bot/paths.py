"""Path resolution with persistent location memory.

This lets the tool be invoked from any directory and still find its
``filters.yaml`` (and sibling ``.env`` / ``token.json`` files).

Resolution priority for the filters file (when ``--config`` is not given):

1. ``filters.yaml`` in the current directory
2. the nearest ``filters.yaml`` walking up the directory tree
3. a *remembered* path stored in the user config file
4. fall back to ``./filters.yaml`` (the historical default)

The resolved location is *remembered* (written to the user config) after a
successful ``init`` or config load, so subsequent runs from unrelated
directories still find it.
"""

from __future__ import annotations

from pathlib import Path

from . import settings

# Re-exported for backwards compatibility with earlier imports.
APP_NAME = settings.APP_NAME
USER_CONFIG_FILENAME = settings.USER_CONFIG_FILENAME
FILTERS_FILENAME = "filters.yaml"
CREDENTIALS_FILENAME = ".env"
TOKEN_FILENAME = "token.json"


def user_config_dir() -> Path:
    """Return the user-level config directory for this app (XDG-aware)."""
    return settings.user_config_dir()


def user_config_path() -> Path:
    """Return the path to the user-level config file."""
    return settings.user_config_path()


def recall_filters_path() -> Path | None:
    """Return the remembered filters path, or ``None`` if none/missing."""
    raw = settings.get(settings.SETTING_FILTERS_PATH)
    if not raw:
        return None
    path = Path(str(raw)).expanduser()
    return path if path.is_file() else None


def remember_filters_path(path: Path) -> None:
    """Persist the given filters path as the remembered location."""
    settings.set(settings.SETTING_FILTERS_PATH, str(path.resolve()))


def forget_filters_path() -> bool:
    """Forget the remembered filters path. Returns ``True`` if it existed."""
    return settings.unset(settings.SETTING_FILTERS_PATH)


def find_upward(filename: str, start: Path | None = None) -> Path | None:
    """Search ``start`` and its parents for ``filename``; return first match."""
    start = (start or Path.cwd()).resolve()
    for directory in (start, *start.parents):
        candidate = directory / filename
        if candidate.is_file():
            return candidate
    return None


def resolve_filters_path(explicit: Path | None = None, start: Path | None = None) -> Path:
    """Resolve the filters.yaml path.

    Args:
        explicit: A user-provided ``--config`` value. If given, it is used as-is
            (no memory lookup, no upward search).
        start: Directory to search from. Defaults to the current directory.
    """
    if explicit is not None:
        return explicit

    cwd = start or Path.cwd()

    # 1. Right here.
    here = cwd / FILTERS_FILENAME
    if here.is_file():
        return here

    # 2. Nearest ancestor.
    upward = find_upward(FILTERS_FILENAME, cwd)
    if upward is not None:
        return upward

    # 3. Remembered.
    remembered = recall_filters_path()
    if remembered is not None:
        return remembered

    # 4. Historical default (may not exist).
    return here


def sibling_dir(filters_path: Path) -> Path:
    """Return the directory sibling files (.env, token.json) live in.

    If the filters file exists, its parent; otherwise the current directory.
    """
    return filters_path.parent if filters_path.is_file() else Path.cwd()
