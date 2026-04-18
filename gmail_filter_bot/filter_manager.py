"""Filter management logic for Gmail Filter Bot."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config, FilterConfig
from .gmail_client import GmailClient


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
    unchanged_parts: set[int] = field(default_factory=set)

    @property
    def has_changes(self) -> bool:
        """Check if there are any differences between local and remote."""
        return bool(
            self.local_only or self.remote_only or self.action_changed or self.label_changed
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

            # Also check if action/label changed
            remote_action, remote_label = self._get_remote_action_and_label(name, remote_filters)
            if remote_action and local_filter.action != remote_action:
                change.action_changed = True
            if remote_label and local_filter.label != remote_label:
                change.label_changed = True

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

        # Split local entries into parts
        local_parts = self._split_entries(entries, max_per_filter)

        unchanged_parts: set[int] = set()

        # Group remote filters by part name
        remote_by_part: dict[str, dict] = {}
        for rf in remote_filters:
            base_name = self._match_filter_to_local(rf)
            if base_name == name:
                # Extract part name from the remote filter
                rf_entries = self.client.parse_filter_entries(rf)
                # Try to match to a part by entry overlap
                for i, local_part in enumerate(local_parts, 1):
                    part_name = name if i == 1 else f"{name}-{i}"
                    local_part_set = set(local_part)
                    remote_part_set = set(rf_entries)
                    # If they share entries, this is that part
                    if local_part_set & remote_part_set:
                        remote_by_part[part_name] = rf
                        break

        # Compare each local part to its remote counterpart
        for i, local_part in enumerate(local_parts, 1):
            part_name = name if i == 1 else f"{name}-{i}"
            local_part_set = set(local_part)

            if part_name in remote_by_part:
                rf = remote_by_part[part_name]
                remote_entries = set(self.client.parse_filter_entries(rf))

                # Check if action/label matches
                remote_action = self._extract_remote_action(rf)
                remote_label = self._extract_remote_label(rf)
                action_match = filter_config.action == remote_action
                label_match = filter_config.label == remote_label

                # Check if entries match exactly
                entries_match = local_part_set == remote_entries

                if entries_match and action_match and label_match:
                    unchanged_parts.add(i)
                    if verbose:
                        print(
                            f"    [DEBUG] Part '{part_name}' unchanged ({len(local_part)} entries)"
                        )
                elif verbose:
                    if not entries_match:
                        local_only = local_part_set - remote_entries
                        remote_only = remote_entries - local_part_set
                        print(f"    [DEBUG] Part '{part_name}' has entry changes:")
                        if local_only:
                            print(f"      + {len(local_only)} entries to add")
                        if remote_only:
                            print(f"      - {len(remote_only)} entries to remove")
                    if not action_match:
                        print(
                            f"      Action differs: local='{filter_config.action}', remote='{remote_action}'"
                        )
                    if not label_match:
                        print(
                            f"      Label differs: local='{filter_config.label}', remote='{remote_label}'"
                        )
            elif verbose:
                print(f"    [DEBUG] Part '{part_name}' not found remotely (new)")

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

        # Delete existing filters that match changed filters (but only changed parts for split filters)
        deleted_count = 0
        deleted_filter_ids: set[str] = set()

        for rf in remote_filters:
            base_name = self._extract_base_name(rf)
            if base_name in changed_names:
                change = changes_by_name[base_name]
                filter_config = self.config.filters[base_name]

                # For non-split filters, delete all
                if len(filter_config.entries) <= self.config.max_entries_per_filter:
                    if rf["id"] not in deleted_filter_ids:
                        self.client.delete_filter(rf["id"])
                        deleted_filter_ids.add(rf["id"])
                        results["updated"] += 1
                        deleted_count += 1
                else:
                    # For split filters, check if this specific part is unchanged
                    rf_entries = set(self.client.parse_filter_entries(rf))
                    parts = self._split_entries(
                        filter_config.entries, self.config.max_entries_per_filter
                    )

                    # Find which part this is
                    part_index = None
                    for i, local_part in enumerate(parts, 1):
                        if set(local_part) & rf_entries:
                            part_index = i
                            break

                    # Delete only if this part changed
                    if part_index not in change.unchanged_parts:
                        if rf["id"] not in deleted_filter_ids:
                            self.client.delete_filter(rf["id"])
                            deleted_filter_ids.add(rf["id"])
                            results["updated"] += 1
                            deleted_count += 1

        if deleted_count > 0:
            print(f"  ✓ Deleted {deleted_count} existing filter(s)")

        # Create/replace changed filters
        for name in changed_names:
            filter_config = self.config.filters[name]
            entries = filter_config.entries
            change = changes_by_name[name]

            if len(entries) <= self.config.max_entries_per_filter:
                # Create single filter
                self._create_filter_with_entries(name, filter_config, entries)
                results["created"] += 1
                print(f"  ✓ Created filter: {name} ({len(entries)} entries)")
            else:
                # Split into multiple filters
                parts = self._split_entries(entries, self.config.max_entries_per_filter)
                total_parts = len(parts)
                unchanged = change.unchanged_parts
                created_parts = 0

                for i, part_entries in enumerate(parts, 1):
                    if i in unchanged:
                        continue  # Skip unchanged parts

                    part_name = name if i == 1 else f"{name}-{i}"
                    self._create_filter_with_entries(part_name, filter_config, part_entries)
                    results["created"] += 1
                    created_parts += 1
                    print(f"  ✓ Created filter: {part_name} ({len(part_entries)} entries)")

                if unchanged:
                    print(f"  ✓ Updated {name}: {created_parts}/{total_parts} parts changed")
                else:
                    print(f"  ✓ Split '{name}' into {total_parts} part(s)")

                results["split_filters"].append(name)

            # Apply labels to existing conversations ONLY for NEW entries (local_only)
            # (Action/label changes don't affect existing messages)
            if apply_to_existing and name in entry_changes_names and filter_config.label:
                # Only apply label to the NEW entries being added, not all entries
                new_entries = list(change.local_only)
                if new_entries:
                    print(
                        f"  → Applying label '{filter_config.label}' to {len(new_entries)} new conversation(s)..."
                    )
                    label_id = self.client._get_or_create_label(filter_config.label)
                    archive = filter_config.action in ["label_and_archive", "archive"]
                    modified_count = self.client.apply_label_to_existing(
                        new_entries, label_id, archive=archive
                    )
                    results["applied_to_existing"] += modified_count
                    if modified_count > 0:
                        print(f"    ✓ Applied to {modified_count} conversation(s)")

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
        self.client.create_filter(
            from_addresses=entries,
            action=filter_config.action,
            label=filter_config.label,
        )

    def _extract_base_name(self, filter_obj: dict) -> str:
        """Extract base filter name from remote filter.

        Matches remote filters to local filters using action + label,
        which is more reliable than entry matching when filters are scrambled.
        """
        # First try to match by action + label (more reliable)
        remote_action = self._extract_remote_action(filter_obj)
        remote_label = self._extract_remote_label(filter_obj)

        for name, local_filter in self.config.filters.items():
            if local_filter.action == remote_action and local_filter.label == remote_label:
                return name

        # Fallback to entry matching if action+label doesn't match
        entries = self.client.parse_filter_entries(filter_obj)
        for name, local_filter in self.config.filters.items():
            if any(entry in local_filter.entries for entry in entries):
                return name
        return ""

    def _match_filter_to_local(self, filter_obj: dict) -> str:
        """Match a remote filter to a local filter name.

        Uses action + label matching first, then falls back to entry overlap.
        """
        # First try to match by action + label
        remote_action = self._extract_remote_action(filter_obj)
        remote_label = self._extract_remote_label(filter_obj)

        for name, local_filter in self.config.filters.items():
            if local_filter.action == remote_action and local_filter.label == remote_label:
                return name

        # Fallback to entry matching
        entries = self.client.parse_filter_entries(filter_obj)
        for name, local_filter in self.config.filters.items():
            local_entries = set(local_filter.entries)
            remote_entries = set(entries)
            if local_entries & remote_entries:
                return name

        return ""

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
