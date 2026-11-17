"""Filter management logic for Gmail Filter Bot."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config, FilterConfig
from .gmail_client import GmailClient

# Entry format patterns used by doctor checks.
# A valid entry is one of:
#   - a concrete email:        user@example.com
#   - a *@domain wildcard:     *@example.com
#   - a bare-domain wildcard:  *example.com
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_WILDCARD_AT_DOMAIN_RE = re.compile(r"^\*@[^@\s]+\.[^@\s]+$")
_BARE_DOMAIN_WILDCARD_RE = re.compile(r"^\*[^@\s]+\.[^@\s]+$")


def _classify_entry(entry: str) -> str:
    """Classify a filter entry string.

    Returns one of: "email", "at_wildcard", "bare_wildcard", "malformed".
    """
    if _BARE_DOMAIN_WILDCARD_RE.match(entry):
        return "bare_wildcard"
    if _WILDCARD_AT_DOMAIN_RE.match(entry):
        return "at_wildcard"
    if _EMAIL_RE.match(entry):
        return "email"
    return "malformed"


def _entry_domain(entry: str) -> str | None:
    """Return the domain portion of an entry, or None if it has none.

    For "user@example.com" -> "example.com".
    For "*@example.com"   -> "example.com".
    For "*example.com"     -> "example.com".
    """
    if "@" in entry:
        return entry.rsplit("@", 1)[-1]
    if entry.startswith("*"):
        return entry[1:]
    return None


@dataclass
class FilterChange:
    """Represents changes detected between local and remote filter entries.

    Attributes:
        name: The name of the filter
        local_only: Entries present locally but not remotely (would be added to Gmail on push)
        remote_only: Entries present remotely but not locally (would be added to local on sync)
        action_changed: Whether the action has changed between local and remote
        label_changed: Whether the label has changed between local and remote
        unchanged_parts: Set of part indices that don't need updating (for split filters)
    """

    name: str
    local_only: set[str] = field(default_factory=set)
    remote_only: set[str] = field(default_factory=set)
    action_changed: bool = False
    label_changed: bool = False
    rollup_changed: bool = False
    unchanged_parts: set[int] = field(default_factory=set)

    @property
    def has_changes(self) -> bool:
        """Check if there are any differences between local and remote."""
        return bool(
            self.local_only
            or self.remote_only
            or self.action_changed
            or self.label_changed
            or self.rollup_changed
        )

    @property
    def has_entry_changes(self) -> bool:
        """Check if there are entry-level changes (not just action/label)."""
        return bool(self.local_only or self.remote_only)

    @property
    def added_to_remote(self) -> list[str]:
        """Entries that exist in Gmail but not locally (would be added on sync)."""
        return sorted(self.remote_only)

    @property
    def removed_from_remote(self) -> list[str]:
        """Entries that exist locally but were removed from Gmail (would be removed on sync)."""
        return sorted(self.local_only)

    def to_dict(self) -> dict[str, Any]:
        """Convert change to dictionary for serialization."""
        return {
            "name": self.name,
            "added": self.added_to_remote,
            "removed": self.removed_from_remote,
        }


class FilterManager:
    """Manages filter synchronization and operations."""

    def __init__(
        self,
        config: Config,
        client: GmailClient,
        config_path: Path = Path("filters.yaml"),
    ):
        """Initialize filter manager."""
        self.config = config
        self.client = client
        self.config_path = config_path

    def validate(self) -> dict[str, Any]:
        """Validate filter configuration."""
        errors = []
        splits = []
        total_entries = 0

        for name, filter_config in self.config.filters.items():
            # Check action validity
            valid_actions = [
                "label_only",
                "label_and_archive",
                "archive",
                "delete",
                "mark_important",
                "mark_not_important",
                "star",
            ]
            if filter_config.action not in valid_actions:
                errors.append(f"{name}: Invalid action '{filter_config.action}'")

            # Check label requirement
            if (
                filter_config.action in ["label_only", "label_and_archive"]
                and not filter_config.label
            ):
                errors.append(f"{name}: Action '{filter_config.action}' requires a label")

            # Check entry count
            entry_count = len(filter_config.entries)
            total_entries += entry_count

            if entry_count > self.config.max_entries_per_filter:
                parts = (
                    entry_count + self.config.max_entries_per_filter - 1
                ) // self.config.max_entries_per_filter
                splits.append(
                    {
                        "name": name,
                        "entries": entry_count,
                        "limit": self.config.max_entries_per_filter,
                        "parts": parts,
                    }
                )

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "splits": splits,
            "total_filters": len(self.config.filters),
            "total_entries": total_entries,
        }

    def diagnose(self) -> list[dict[str, Any]]:
        """Run local (offline) health checks on the configuration.

        Returns a list of check result dicts, each with:
            {"name": str, "level": "pass"|"warn"|"fail",
             "message": str, "details": list[str]}

        These checks never touch the network and are safe to unit test
        with a mocked (or None) Gmail client.
        """
        checks: list[dict[str, Any]] = []

        # --- 1. Structural validation (actions + labels) ---
        validation = self.validate()
        if validation["valid"]:
            checks.append(
                {
                    "name": "config_validation",
                    "level": "pass",
                    "message": (
                        f"Configuration valid ({validation['total_filters']} filters, "
                        f"{validation['total_entries']} entries)"
                    ),
                    "details": [],
                }
            )
        else:
            checks.append(
                {
                    "name": "config_validation",
                    "level": "fail",
                    "message": f"{len(validation['errors'])} validation error(s)",
                    "details": validation["errors"],
                }
            )

        # --- 2. Empty filters ---
        empty = [n for n, f in self.config.filters.items() if not f.entries]
        if empty:
            checks.append(
                {
                    "name": "empty_filters",
                    "level": "warn",
                    "message": f"{len(empty)} filter(s) have no entries",
                    "details": empty,
                }
            )
        else:
            checks.append(
                {
                    "name": "empty_filters",
                    "level": "pass",
                    "message": "No empty filters",
                    "details": [],
                }
            )

        # --- 3. Malformed entries (e.g. embedded whitespace) ---
        malformed: list[str] = []
        for name, f in self.config.filters.items():
            for e in f.entries:
                if _classify_entry(e) == "malformed":
                    malformed.append(f"{name}: {e}")
        if malformed:
            checks.append(
                {
                    "name": "malformed_entries",
                    "level": "fail",
                    "message": f"{len(malformed)} malformed entry(s) found",
                    "details": malformed,
                }
            )
        else:
            checks.append(
                {
                    "name": "malformed_entries",
                    "level": "pass",
                    "message": "All entries well-formed",
                    "details": [],
                }
            )

        # --- 4. Near-split filters (>80% of the per-filter limit) ---
        threshold = int(self.config.max_entries_per_filter * 0.8)
        near = [
            f"{n}: {len(f.entries)}/{self.config.max_entries_per_filter} entries"
            for n, f in self.config.filters.items()
            if threshold < len(f.entries) <= self.config.max_entries_per_filter
        ]
        if near:
            checks.append(
                {
                    "name": "near_split",
                    "level": "warn",
                    "message": f"{len(near)} filter(s) close to the split limit",
                    "details": near,
                }
            )
        else:
            checks.append(
                {
                    "name": "near_split",
                    "level": "pass",
                    "message": "No filters near the split limit",
                    "details": [],
                }
            )

        # --- 5. Exact duplicates within a single filter ---
        within_dupes: list[str] = []
        for name, f in self.config.filters.items():
            seen: set[str] = set()
            for e in f.entries:
                if e in seen:
                    within_dupes.append(f"{name}: {e}")
                seen.add(e)
        if within_dupes:
            # de-duplicate the report lines while keeping them readable
            within_dupes = sorted(set(within_dupes))
            checks.append(
                {
                    "name": "within_filter_duplicates",
                    "level": "warn",
                    "message": f"{len(within_dupes)} duplicate entry(s) within a single filter",
                    "details": within_dupes,
                }
            )
        else:
            checks.append(
                {
                    "name": "within_filter_duplicates",
                    "level": "pass",
                    "message": "No within-filter duplicates",
                    "details": [],
                }
            )

        # --- 6. Wildcard shadowing within a filter ---
        # A specific address or *@domain is redundant when a broader wildcard
        # in the same filter already covers it.
        shadowing: list[str] = []
        for name, f in self.config.filters.items():
            bare_domains = [
                e[1:] for e in f.entries if _classify_entry(e) == "bare_wildcard"
            ]
            at_domains = [
                e[2:] for e in f.entries if _classify_entry(e) == "at_wildcard"
            ]
            for e in f.entries:
                cls = _classify_entry(e)
                if cls not in ("email", "at_wildcard"):
                    continue
                dom = _entry_domain(e)
                if dom is None:
                    continue
                covered_by = None
                # bare-domain wildcard *B covers dom == B or *.B (subdomain)
                for b in bare_domains:
                    if dom == b or dom.endswith("." + b):
                        covered_by = f"*{b}"
                        break
                # *@B covers a concrete email user@B
                if covered_by is None and cls == "email":
                    for b in at_domains:
                        if dom == b:
                            covered_by = f"*@{b}"
                            break
                if covered_by is not None:
                    shadowing.append(f"{name}: {e} (covered by {covered_by})")
        if shadowing:
            checks.append(
                {
                    "name": "wildcard_shadowing",
                    "level": "warn",
                    "message": f"{len(shadowing)} entry(s) shadowed by a wildcard in the same filter",
                    "details": shadowing,
                }
            )
        else:
            checks.append(
                {
                    "name": "wildcard_shadowing",
                    "level": "pass",
                    "message": "No wildcard shadowing within filters",
                    "details": [],
                }
            )

        # --- 7. Exact duplicates across filters ---
        # Warn for plain redundancy; escalate to a hard failure when the
        # competing filters use different actions (a silent precedence
        # conflict resolved by Gmail's filter order). Entries that appear in a
        # rollup filter are excluded: stacking onto a rollup filter is
        # intentional, so any resulting overlap (same- or different-action) is
        # just a consequence of rollup, not a conflict.
        cross_dupes, cross_conflicts = self._cross_filter_findings()
        if cross_conflicts:
            checks.append(
                {
                    "name": "cross_filter_duplicates",
                    "level": "fail",
                    "message": (
                        f"{len(cross_conflicts)} entry(s) in multiple filters with "
                        f"CONFLICTING actions"
                    ),
                    "details": sorted(cross_conflicts),
                }
            )
        elif cross_dupes:
            checks.append(
                {
                    "name": "cross_filter_duplicates",
                    "level": "warn",
                    "message": (
                        f"{len(cross_dupes)} entry(s) duplicated across filters "
                        f"(same action)"
                    ),
                    "details": sorted(cross_dupes),
                }
            )
        else:
            checks.append(
                {
                    "name": "cross_filter_duplicates",
                    "level": "pass",
                    "message": "No entries duplicated across filters",
                    "details": [],
                }
            )

        return checks

    def _is_rollup_filter(self, name: str) -> bool:
        """True if `name` is a rollup filter.

        A filter is a rollup when the *global* ``Config.rollup`` flag is set
        (every filter rolls up) or when the filter itself has ``rollup: True``.
        """
        if getattr(self.config, "rollup", False):
            return True
        f = self.config.filters.get(name)
        return bool(f is not None and getattr(f, "rollup", False))

    def _rollup_entries(self) -> set[str]:
        """Return the set of entries that belong to at least one rollup filter.

        When the global ``Config.rollup`` flag is set, every filter rolls up, so
        this returns the union of all entries.
        """
        rollup_entries: set[str] = set()
        for name, f in self.config.filters.items():
            if self._is_rollup_filter(name):
                rollup_entries.update(f.entries)
        return rollup_entries

    def _cross_filter_findings(self) -> tuple[list[str], list[str]]:
        """Compute cross-filter duplicate findings, excluding rollup entries.

        Returns ``(cross_dupes, cross_conflicts)`` where each is a list of
        human-readable detail lines. Entries that appear in any rollup filter
        are skipped entirely (see ``diagnose`` for the rationale).
        """
        rollup_entries = self._rollup_entries()

        entry_locations: dict[str, list[tuple[str, str]]] = {}
        for name, f in self.config.filters.items():
            for e in f.entries:
                if e in rollup_entries:
                    continue
                entry_locations.setdefault(e, []).append((name, f.action))

        cross_dupes: list[str] = []
        cross_conflicts: list[str] = []
        for e, locs in entry_locations.items():
            names = [n for n, _ in locs]
            if len(set(names)) <= 1:
                continue
            actions = sorted({a for _, a in locs})
            line = f"{e} in {', '.join(f'{n}({a})' for n, a in locs)}"
            if len(actions) > 1:
                cross_conflicts.append(line)
            else:
                cross_dupes.append(line)
        return cross_dupes, cross_conflicts

    # Diagnosis types that `doctor resolve` knows how to auto-fix.
    RESOLVABLE_CHECKS = (
        "wildcard_shadowing",
        "within_filter_duplicates",
        "cross_filter_duplicates",
    )

    def resolve(self, check_name: str) -> dict[str, Any]:
        """Attempt to auto-fix one diagnosis type in-place.

        Re-runs ``diagnose`` to confirm the check is currently failing, then
        applies the matching resolver and saves the config. Returns a result
        dict::

            {"resolved": bool, "reason": str, "fixed": int, "details": list[str]}
        """
        checks = self.diagnose()
        target = next((c for c in checks if c["name"] == check_name), None)
        if target is None:
            return {"resolved": False, "reason": f"No check named '{check_name}'",
                    "fixed": 0, "details": []}
        if target["level"] == "pass":
            return {"resolved": False, "reason": f"'{check_name}' is already passing",
                    "fixed": 0, "details": []}
        if check_name not in self.RESOLVABLE_CHECKS:
            return {
                "resolved": False,
                "reason": (
                    f"No auto-resolver for '{check_name}'. Resolvable types: "
                    f"{', '.join(self.RESOLVABLE_CHECKS)}"
                ),
                "fixed": 0,
                "details": [],
            }

        if check_name == "wildcard_shadowing":
            fixed, details = self._resolve_wildcard_shadowing()
        elif check_name == "within_filter_duplicates":
            fixed, details = self._resolve_within_filter_duplicates()
        else:  # cross_filter_duplicates
            fixed, details = self._resolve_cross_filter_duplicates()
            if fixed < 0:
                # Negative signals an unresolvable (conflicting) case.
                return {
                    "resolved": False,
                    "reason": (
                        "conflicting actions across filters require manual "
                        "resolution (mark a rollup filter with `rollup: true` "
                        "if the overlap is intentional)"
                    ),
                    "fixed": 0,
                    "details": [],
                }

        if fixed > 0:
            self.config.save(self.config_path)
        return {
            "resolved": fixed > 0,
            "reason": "" if fixed > 0 else "nothing to fix",
            "fixed": fixed,
            "details": details,
        }

    def _resolve_wildcard_shadowing(self) -> tuple[int, list[str]]:
        """Remove entries shadowed by a wildcard within the same filter."""
        removed = 0
        details: list[str] = []
        for name, f in self.config.filters.items():
            bare_domains = [
                e[1:] for e in f.entries if _classify_entry(e) == "bare_wildcard"
            ]
            at_domains = [
                e[2:] for e in f.entries if _classify_entry(e) == "at_wildcard"
            ]
            to_remove: list[str] = []
            for e in f.entries:
                cls = _classify_entry(e)
                if cls not in ("email", "at_wildcard"):
                    continue
                dom = _entry_domain(e)
                if dom is None:
                    continue
                covered_by = None
                for b in bare_domains:
                    if dom == b or dom.endswith("." + b):
                        covered_by = f"*{b}"
                        break
                if covered_by is None and cls == "email":
                    for b in at_domains:
                        if dom == b:
                            covered_by = f"*@{b}"
                            break
                if covered_by is not None:
                    to_remove.append(e)
            for e in to_remove:
                if e in f.entries:
                    f.entries.remove(e)
                    removed += 1
                    details.append(f"{name}: removed {e}")
        return removed, details

    def _resolve_within_filter_duplicates(self) -> tuple[int, list[str]]:
        """Deduplicate entries within each filter, preserving first order."""
        removed = 0
        details: list[str] = []
        for name, f in self.config.filters.items():
            seen: set[str] = set()
            unique: list[str] = []
            for e in f.entries:
                if e in seen:
                    removed += 1
                    details.append(f"{name}: removed duplicate {e}")
                    continue
                seen.add(e)
                unique.append(e)
            f.entries = unique
        return removed, details

    def _resolve_cross_filter_duplicates(self) -> tuple[int, list[str]]:
        """Remove same-action duplicates across filters, keeping the first.

        Returns a negative ``fixed`` count when the overlap involves
        conflicting actions (which cannot be auto-resolved).
        """
        cross_dupes, cross_conflicts = self._cross_filter_findings()
        if cross_conflicts:
            return -1, []
        if not cross_dupes:
            return 0, []

        rollup_entries = self._rollup_entries()
        # entry -> ordered list of filter names containing it (excl. rollup)
        entry_names: dict[str, list[str]] = {}
        for name, f in self.config.filters.items():
            for e in f.entries:
                if e in rollup_entries:
                    continue
                entry_names.setdefault(e, []).append(name)

        removed = 0
        details: list[str] = []
        for e, names in entry_names.items():
            if len(set(names)) <= 1:
                continue
            keep = names[0]  # first filter in config order
            for n in names[1:]:
                if e in self.config.filters[n].entries:
                    self.config.filters[n].entries.remove(e)
                    removed += 1
                    details.append(f"{n}: removed {e} (kept in {keep})")
        return removed, details

    def compare_filter_entries(
        self, local_entries: set[str], remote_entries: set[str]
    ) -> FilterChange:
        """Compare local and remote filter entries.

        Args:
            local_entries: Set of entries from local configuration
            remote_entries: Set of entries from Gmail

        Returns:
            FilterChange object describing the differences
        """
        return FilterChange(
            name="",  # Name will be set by caller
            local_only=local_entries - remote_entries,
            remote_only=remote_entries - local_entries,
        )

    def get_remote_filter_entries(self, name: str, remote_filters: list[dict]) -> set[str]:
        """Extract all entries from remote filters matching a base name.

        Args:
            name: Base filter name to match
            remote_filters: List of remote filter objects from Gmail API

        Returns:
            Set of all entries from matching remote filters
        """
        remote_entries: set[str] = set()

        for rf in remote_filters:
            base_name = self._match_filter_to_local(rf)
            if base_name == name:
                remote_entries.update(self.client.parse_filter_entries(rf))

        return remote_entries

    def detect_changes(self, verbose: bool = False) -> list[FilterChange]:
        """Detect differences between local filters and Gmail.

        Compares local filter configurations with remote Gmail filters
        to identify what would change during a sync operation.

        Returns:
            List of FilterChange objects for filters with differences
        """
        changes: list[FilterChange] = []

        # Get remote filters once
        remote_filters = self.client.list_filters()

        if verbose:
            print(f"\n  [DEBUG] Found {len(remote_filters)} remote filter(s) in Gmail")

        # Compare each local filter with remote
        for name, local_filter in self.config.filters.items():
            local_entries = set(local_filter.entries)
            remote_entries = self.get_remote_filter_entries(name, remote_filters)

            change = self.compare_filter_entries(local_entries, remote_entries)
            change.name = name

            # Also check if action/label/rollup changed
            remote_action, remote_label = self._get_remote_action_and_label(name, remote_filters)
            if remote_action and local_filter.action != remote_action:
                change.action_changed = True
            if remote_label and local_filter.label != remote_label:
                change.label_changed = True
            remote_rollup = self._has_remote_rollup_filters(name, remote_filters)
            if remote_rollup is not None:
                local_rollup = (
                    local_filter.rollup if local_filter.rollup is not None else self.config.rollup
                )
                if local_rollup != remote_rollup:
                    change.rollup_changed = True

            if change.has_changes:
                # For split filters, check which specific parts are unchanged
                entry_count = len(local_filter.entries)
                if entry_count > self.config.max_entries_per_filter:
                    change.unchanged_parts = self.detect_split_filter_changes(
                        name, remote_filters, verbose
                    )

                if verbose:
                    print(f"  [DEBUG] Filter '{name}' has changes:")
                    print(
                        f"    - Local entries: {len(local_entries)}, Remote entries: {len(remote_entries)}"
                    )
                    if change.local_only:
                        print(
                            f"    - Local only (would add): {list(change.local_only)[:3]}{'...' if len(change.local_only) > 3 else ''}"
                        )
                    if change.remote_only:
                        print(
                            f"    - Remote only (would remove): {list(change.remote_only)[:3]}{'...' if len(change.remote_only) > 3 else ''}"
                        )
                    if change.action_changed:
                        print(
                            f"    - Action changed: local='{local_filter.action}', remote='{remote_action}'"
                        )
                    if change.label_changed:
                        print(
                            f"    - Label changed: local='{local_filter.label}', remote='{remote_label}'"
                        )
                    if change.rollup_changed:
                        local_rollup = (
                            local_filter.rollup
                            if local_filter.rollup is not None
                            else self.config.rollup
                        )
                        print(f"    - Rollup changed: local={local_rollup}, remote={remote_rollup}")
                    if change.unchanged_parts:
                        total_parts = (
                            entry_count + self.config.max_entries_per_filter - 1
                        ) // self.config.max_entries_per_filter
                        print(f"    - Unchanged parts: {change.unchanged_parts} of {total_parts}")
                changes.append(change)
            elif verbose:
                print(f"  [DEBUG] Filter '{name}' - no changes ({len(local_entries)} entries)")

        return changes

    def _get_remote_action_and_label(
        self, name: str, remote_filters: list[dict]
    ) -> tuple[str | None, str | None]:
        """Extract action and label from remote filters matching a base name.

        Args:
            name: Base filter name to match
            remote_filters: List of remote filter objects from Gmail API

        Returns:
            Tuple of (action, label) or (None, None) if no matching filters found
        """
        for rf in remote_filters:
            base_name = self._match_filter_to_local(rf)
            if base_name == name:
                action = self._extract_remote_action(rf)
                label = self._extract_remote_label(rf)
                return action, label
        return None, None

    def _has_remote_rollup_filters(self, name: str, remote_filters: list[dict]) -> bool | None:
        """Check if ancestor rollup filters exist remotely for a local filter.

        Returns True if all expected ancestor filters exist, False if any
        are missing, or None if rollup is not applicable (no nested label or
        rollup not enabled locally).
        """
        filter_config = self.config.filters[name]
        if not filter_config.label:
            return None

        effective_rollup = (
            filter_config.rollup if filter_config.rollup is not None else self.config.rollup
        )
        if not effective_rollup:
            return False

        rollup_labels = GmailClient._get_rollup_labels(filter_config.label)
        if len(rollup_labels) <= 1:
            return None  # No ancestors, rollup is a no-op

        ancestor_labels = rollup_labels[:-1]  # All except the deepest
        local_entries = set(filter_config.entries)

        for ancestor_label in ancestor_labels:
            found = False
            for rf in remote_filters:
                rf_label = self._extract_remote_label(rf)
                if rf_label and rf_label.lower() == ancestor_label.lower():
                    rf_entries = set(self.client.parse_filter_entries(rf))
                    if rf_entries & local_entries:
                        found = True
                        break
            if not found:
                return False

        return True

    def _extract_remote_action(self, filter_obj: dict) -> str:
        """Extract action type from a remote filter object."""
        action_obj = filter_obj.get("action", {})
        add_labels = set(action_obj.get("addLabelIds", []))
        remove_labels = set(action_obj.get("removeLabelIds", []))

        # Check for delete (moved to trash)
        if "TRASH" in add_labels:
            return "delete"

        # Check for star
        if "STARRED" in add_labels:
            return "star"

        # Check for important/not important
        if "IMPORTANT" in add_labels:
            return "mark_important"
        if "IMPORTANT" in remove_labels:
            return "mark_not_important"

        # Check for archive (inbox removed)
        inbox_removed = "INBOX" in remove_labels

        # Check for custom labels
        system_labels = {"INBOX", "STARRED", "IMPORTANT", "TRASH", "SPAM", "UNREAD"}
        custom_labels = add_labels - system_labels

        if custom_labels:
            if inbox_removed:
                return "label_and_archive"
            else:
                return "label_only"
        elif inbox_removed:
            return "archive"

        return "label_only"  # Default

    def _extract_remote_label(self, filter_obj: dict) -> str | None:
        """Extract label name from a remote filter object."""
        action_obj = filter_obj.get("action", {})
        add_labels = set(action_obj.get("addLabelIds", []))

        # Find first non-system label
        system_labels = {"INBOX", "STARRED", "IMPORTANT", "TRASH", "SPAM", "UNREAD"}
        custom_labels = add_labels - system_labels

        if custom_labels:
            label_id = custom_labels.pop()
            return self.client.get_label_name(label_id)

        return None

    def detect_split_filter_changes(
        self, name: str, remote_filters: list[dict], verbose: bool = False
    ) -> set[int]:
        """Detect which parts of a split filter have changes.

        For split filters (e.g., newsletters-1, newsletters-2), compares each
        remote part to its corresponding local part to identify unchanged
        parts that can be skipped during push.

        Args:
            name: Base filter name
            remote_filters: List of remote filter objects from Gmail API
            verbose: Whether to print debug info

        Returns:
            Set of part indices (1-based) that are unchanged and can be skipped
        """
        filter_config = self.config.filters[name]
        entries = filter_config.entries
        max_per_filter = self.config.max_entries_per_filter

        # Split local entries into ordered parts.
        local_parts = self._split_entries(entries, max_per_filter)
        local_sets = [frozenset(p) for p in local_parts]

        # Collect remote parts attributed to this filter, with their entry sets.
        remote_parts: list[tuple[dict, frozenset[str]]] = []
        for rf in remote_filters:
            if self._attribute_remote_part(rf) == name:
                remote_parts.append((rf, frozenset(self.client.parse_filter_entries(rf))))

        unchanged_parts: set[int] = set()
        consumed = [False] * len(remote_parts)

        # A local part is unchanged when some unconsumed remote part has the
        # same (action, label) AND an identical entry set. Matching is
        # order-agnostic: a part matches any remote part with the same set, so
        # Gmail reordering parts no longer causes spurious churn.
        for i, local_set in enumerate(local_sets, 1):
            part_name = name if i == 1 else f"{name}-{i}"
            for j, (rf, remote_set) in enumerate(remote_parts):
                if consumed[j]:
                    continue
                remote_action = self._extract_remote_action(rf)
                remote_label = self._extract_remote_label(rf)
                if (
                    filter_config.action == remote_action
                    and filter_config.label == remote_label
                    and local_set == remote_set
                ):
                    unchanged_parts.add(i)
                    consumed[j] = True
                    if verbose:
                        print(
                            f"    [DEBUG] Part '{part_name}' unchanged "
                            f"({len(local_parts[i - 1])} entries)"
                        )
                    break
            else:
                if verbose:
                    # Diagnose why this part changed.
                    matched = next(
                        (j for j in range(len(remote_parts)) if not consumed[j]),
                        None,
                    )
                    if matched is None:
                        print(f"    [DEBUG] Part '{part_name}' not found remotely (new)")
                    else:
                        rf, remote_set = remote_parts[matched]
                        local_only = local_set - remote_set
                        remote_only = remote_set - local_set
                        print(f"    [DEBUG] Part '{part_name}' has entry changes:")
                        if local_only:
                            print(f"      + {len(local_only)} entries to add")
                        if remote_only:
                            print(f"      - {len(remote_only)} entries to remove")
                        remote_action = self._extract_remote_action(rf)
                        remote_label = self._extract_remote_label(rf)
                        if filter_config.action != remote_action:
                            print(
                                f"      Action differs: local='{filter_config.action}',"
                                f" remote='{remote_action}'"
                            )
                        if filter_config.label != remote_label:
                            print(
                                f"      Label differs: local='{filter_config.label}',"
                                f" remote='{remote_label}'"
                            )

        return unchanged_parts

    def sync(self, dry_run: bool = False) -> list[FilterChange]:
        """Sync Gmail filters with local configuration.

        Updates local filter entries to match what's in Gmail,
        effectively pulling any changes made in the Gmail UI.

        Args:
            dry_run: If True, only detect changes without applying them

        Returns:
            List of FilterChange objects describing what changed
        """
        changes = self.detect_changes()

        if not dry_run and changes:
            # Update local filters to match remote
            for change in changes:
                # Get all remote entries for this filter
                remote_filters = self.client.list_filters()
                remote_entries = self.get_remote_filter_entries(change.name, remote_filters)

                # Update local filter with remote entries
                self.config.filters[change.name].entries = sorted(remote_entries)

            # Save updated configuration
            self.config.save(self.config_path)

        return changes

    def push(
        self, apply_to_existing: bool = False, verbose: bool = False, dry_run: bool = False
    ) -> dict[str, Any]:
        """Push local filters to Gmail."""
        results: dict[str, Any] = {
            "created": 0,
            "updated": 0,
            "skipped": 0,
            "split_filters": [],
            "applied_to_existing": 0,
        }

        # Get existing remote filters
        remote_filters = self.client.list_filters()

        # Detect which filters have changes
        changed_filters = self.detect_changes(verbose=verbose)
        changed_names = {c.name for c in changed_filters}

        # Build a set of filters that have ENTRY changes (not just action/label)
        # These are the only ones that need "apply to existing"
        entry_changes_names = {c.name for c in changed_filters if c.local_only or c.remote_only}

        # Identify filters that need no changes
        unchanged_names = set(self.config.filters.keys()) - changed_names

        # Build lookup for change details
        changes_by_name = {c.name: c for c in changed_filters}

        if dry_run:
            # Just report what would happen
            for name in changed_names:
                filter_config = self.config.filters[name]
                entries = filter_config.entries
                entry_count = len(entries)
                change = changes_by_name[name]

                if len(entries) <= self.config.max_entries_per_filter:
                    print(f"  [DRY RUN] Would update: {name} ({entry_count} entries)")
                else:
                    parts = self._split_entries(entries, self.config.max_entries_per_filter)
                    total_parts = len(parts)
                    unchanged = change.unchanged_parts
                    changed_part_count = total_parts - len(unchanged)

                    if unchanged:
                        print(
                            f"  [DRY RUN] Would update: {name} ({entry_count} entries) "
                            f"→ {changed_part_count}/{total_parts} parts changed"
                        )
                        for i in range(1, total_parts + 1):
                            part_name = name if i == 1 else f"{name}-{i}"
                            if i in unchanged:
                                print(f"    [DRY RUN] Would skip: {part_name} - no changes")
                            else:
                                part_entries = parts[i - 1]
                                print(
                                    f"    [DRY RUN] Would update: {part_name} ({len(part_entries)} entries)"
                                )
                    else:
                        print(
                            f"  [DRY RUN] Would update: {name} ({entry_count} entries) "
                            f"→ {total_parts} filters"
                        )

                if name in entry_changes_names and apply_to_existing and filter_config.label:
                    # Only apply label to NEW entries (local_only), not all entries
                    new_entries = list(change.local_only)
                    if new_entries:
                        print(
                            f"    [DRY RUN] Would apply label '{filter_config.label}' to {len(new_entries)} new conversation(s)"
                        )

            for name in unchanged_names:
                entry_count = len(self.config.filters[name].entries)
                print(f"  [DRY RUN] Would skip: {name} ({entry_count} entries) - no changes")

            results["created"] = len(changed_names)
            results["updated"] = len(changed_names)
            results["skipped"] = len(unchanged_names)
            return results

        # --- Per-part upsert (create-before-delete) ---
        #
        # For every changed filter we reconcile remote parts to local parts by
        # exact (action, label, entry-set). New parts are created BEFORE stale
        # parts are deleted, so a crash mid-push never leaves a filter missing:
        # the old parts keep working until the new ones exist. Matching is
        # order-agnostic (a local part matches any remote part with the same
        # set), so Gmail reordering parts no longer causes churn, and parts are
        # attributed to the right filter by content — fixing the
        # ``delete``/``spam`` collision that both map to (delete, None).
        created_count = 0
        deleted_count = 0
        deleted_filter_ids: set[str] = set()

        # Iterate changed filters in config order for deterministic output.
        changed_order = [n for n in self.config.filters if n in changed_names]

        for name in changed_order:
            filter_config = self.config.filters[name]
            entries = filter_config.entries
            change = changes_by_name[name]

            local_parts = self._split_entries(entries, self.config.max_entries_per_filter)
            local_sets = [frozenset(p) for p in local_parts]

            # Remote parts attributed to this filter (content-aware).
            remote_parts: list[tuple[dict, frozenset[str]]] = []
            for rf in remote_filters:
                if self._attribute_remote_part(rf) == name:
                    remote_parts.append(
                        (rf, frozenset(self.client.parse_filter_entries(rf)))
                    )

            # Match local parts to remote parts by exact (action, label, set).
            consumed = [False] * len(remote_parts)
            parts_to_create: list[tuple[int, list[str]]] = []  # (1-based index, entries)
            for i, local_set in enumerate(local_sets, 1):
                matched = False
                for j, (rf, remote_set) in enumerate(remote_parts):
                    if consumed[j]:
                        continue
                    remote_action = self._extract_remote_action(rf)
                    remote_label = self._extract_remote_label(rf)
                    if (
                        filter_config.action == remote_action
                        and filter_config.label == remote_label
                        and local_set == remote_set
                    ):
                        consumed[j] = True
                        matched = True
                        break
                if not matched:
                    parts_to_create.append((i, local_parts[i - 1]))

            # Orphan remote parts not matched by any local part.
            orphans = [
                remote_parts[j][0] for j in range(len(remote_parts)) if not consumed[j]
            ]

            total_parts = len(local_parts)
            is_split = total_parts > 1

            # Phase 1: create new/changed parts FIRST (crash-safe).
            created_parts = 0
            for i, part_entries in parts_to_create:
                part_name = name if i == 1 else f"{name}-{i}"
                self._create_filter_with_entries(part_name, filter_config, part_entries)
                created_count += 1
                created_parts += 1
                print(f"  ✓ Created filter: {part_name} ({len(part_entries)} entries)")

            # Phase 2: delete orphan remote parts LAST.
            deleted_this_filter = 0
            for rf in orphans:
                if rf["id"] in deleted_filter_ids:
                    continue
                self.client.delete_filter(rf["id"])
                deleted_filter_ids.add(rf["id"])
                deleted_count += 1
                deleted_this_filter += 1

            if deleted_this_filter > 0:
                print(f"  ✓ Deleted {deleted_this_filter} stale part(s) of {name}")

            if is_split:
                unchanged = change.unchanged_parts
                if unchanged and created_parts < total_parts:
                    print(
                        f"  ✓ Updated {name}: {created_parts}/{total_parts} parts changed"
                    )
                elif created_parts == total_parts and deleted_this_filter == 0:
                    print(f"  ✓ Split '{name}' into {total_parts} part(s)")
                elif created_parts:
                    print(
                        f"  ✓ Updated {name}: {created_parts}/{total_parts} parts created"
                    )
                results["split_filters"].append(name)
            elif created_parts and deleted_this_filter:
                # Single-part filter replaced.
                pass

            # Apply labels to existing conversations ONLY for NEW entries
            # (local_only). Action/label-only changes don't affect existing
            # messages.
            if (
                apply_to_existing
                and name in entry_changes_names
                and filter_config.label
            ):
                new_entries = list(change.local_only)
                if new_entries:
                    effective_rollup = (
                        filter_config.rollup
                        if filter_config.rollup is not None
                        else self.config.rollup
                    )
                    if effective_rollup:
                        rollup_labels = self.client._get_rollup_labels(filter_config.label)
                        label_ids = [self.client._get_or_create_label(lbl) for lbl in rollup_labels]
                        label_desc = " + ".join(rollup_labels)
                    else:
                        label_ids = [self.client._get_or_create_label(filter_config.label)]
                        label_desc = filter_config.label
                    print(
                        f"  → Applying label filter_config.label'{filter_config.label}' label_desc'{label_desc}' to {len(new_entries)} new conversation(s)..."
                    )
                    archive = filter_config.action in ["label_and_archive", "archive"]
                    modified_count = self.client.apply_label_to_existing(
                        new_entries, label_ids, archive=archive
                    )
                    results["applied_to_existing"] += modified_count
                    if modified_count > 0:
                        print(f"    ✓ Applied to {modified_count} conversation(s)")

        results["created"] = created_count
        results["updated"] = deleted_count

        # Report unchanged filters
        for name in unchanged_names:
            results["skipped"] += 1
            entry_count = len(self.config.filters[name].entries)
            print(f"  ⏭ Skipped filter: {name} ({entry_count} entries) - no changes")

        return results

    def trim(self) -> dict[str, Any]:
        """Remove duplicates and consolidate entries."""
        results: dict[str, Any] = {
            "duplicates": 0,
            "filters": [],
        }

        for name, filter_config in self.config.filters.items():
            original_count = len(filter_config.entries)

            # Remove duplicates while preserving order
            seen = set()
            unique_entries = []
            for entry in filter_config.entries:
                if entry not in seen:
                    seen.add(entry)
                    unique_entries.append(entry)

            duplicates_removed = original_count - len(unique_entries)

            if duplicates_removed > 0:
                self.config.filters[name].entries = unique_entries
                results["duplicates"] += duplicates_removed
                results["filters"].append(name)

        if results["duplicates"] > 0:
            self.config.save(self.config_path)

        return results

    def _split_entries(self, entries: list[str], chunk_size: int) -> list[list[str]]:
        """Split entries into chunks of specified size."""
        return [entries[i : i + chunk_size] for i in range(0, len(entries), chunk_size)]

    def _create_filter_with_entries(
        self,
        name: str,
        filter_config: FilterConfig,
        entries: list[str],
    ) -> None:
        """Create a single filter with given entries."""
        effective_rollup = (
            filter_config.rollup if filter_config.rollup is not None else self.config.rollup
        )
        self.client.create_filter(
            from_addresses=entries,
            action=filter_config.action,
            label=filter_config.label,
            rollup=effective_rollup,
        )

    def _attribute_remote_part(self, filter_obj: dict) -> str:
        """Return the local filter name a remote filter part belongs to.

        Matching is *content-aware*. Among local filters sharing the remote
        part's ``(action, label)``, the one whose entry set is a superset of the
        remote part's entries wins. This disambiguates filters that collide on
        ``(action, label)`` alone — for example ``delete`` and ``spam`` both
        map to ``(delete, None)`` — by their actual entries, so each remote
        part is attributed to the filter it really belongs to instead of the
        first one that happens to share an action+label.

        When no ``(action, label)`` candidate contains the remote entries, a
        single candidate is still attributed (so drifted/stale parts are
        recognised as orphans and pruned), and otherwise matching falls back to
        entry overlap across all filters.
        """
        remote_action = self._extract_remote_action(filter_obj)
        remote_label = self._extract_remote_label(filter_obj)
        remote_entries = set(self.client.parse_filter_entries(filter_obj))

        candidates = [
            name
            for name, f in self.config.filters.items()
            if f.action == remote_action and f.label == remote_label
        ]
        # Prefer the candidate that contains every remote entry (exact content).
        for name in candidates:
            if remote_entries <= set(self.config.filters[name].entries):
                return name
        # Single (action, label) candidate: attribute even when the remote part
        # has drifted, so the part is recognised as an orphan and pruned.
        if len(candidates) == 1:
            return candidates[0]
        # Fall back to entry overlap across all filters.
        for name, f in self.config.filters.items():
            if remote_entries & set(f.entries):
                return name
        return ""

    def _extract_base_name(self, filter_obj: dict) -> str:
        """Extract base filter name from a remote filter (content-aware)."""
        return self._attribute_remote_part(filter_obj)

    def _match_filter_to_local(self, filter_obj: dict) -> str:
        """Match a remote filter to a local filter name (content-aware)."""
        return self._attribute_remote_part(filter_obj)

    def format_filters(self, dry_run: bool = False) -> dict[str, Any]:
        """Consolidate filters with the same action and label.

        This merges filters that have identical actions and labels into a single
        filter with all entries combined.
        """
        from collections import defaultdict

        # Group filters by (action, label) tuple
        groups: dict[tuple[str, str | None], list[tuple[str, FilterConfig]]] = defaultdict(list)

        for name, filter_config in self.config.filters.items():
            key = (filter_config.action, filter_config.label)
            groups[key].append((name, filter_config))

        results: dict[str, Any] = {
            "consolidated": [],
        }

        # Find groups with more than one filter
        filters_to_remove: list[str] = []

        for (action, label), filter_list in groups.items():
            if len(filter_list) <= 1:
                continue

            # Use the first filter name as the consolidated name
            primary_name = filter_list[0][0]

            # Collect all unique entries
            all_entries: list[str] = []
            removed_names: list[str] = []

            for name, filter_config in filter_list:
                all_entries.extend(filter_config.entries)
                if name != primary_name:
                    removed_names.append(name)
                    filters_to_remove.append(name)

            # Remove duplicates while preserving order
            seen: set[str] = set()
            unique_entries: list[str] = []
            for entry in all_entries:
                if entry not in seen:
                    seen.add(entry)
                    unique_entries.append(entry)

            results["consolidated"].append(
                {
                    "name": primary_name,
                    "action": action,
                    "label": label,
                    "source_count": len(filter_list),
                    "total_entries": len(unique_entries),
                    "removed_filters": removed_names,
                }
            )

            if not dry_run:
                # Update the primary filter with all entries
                self.config.filters[primary_name].entries = unique_entries

        if not dry_run:
            # Remove the consolidated filters
            for name in filters_to_remove:
                del self.config.filters[name]

            # Save if changes were made
            if filters_to_remove:
                self.config.save(self.config_path)

        return results
