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
    """

    name: str
    local_only: set[str] = field(default_factory=set)
    remote_only: set[str] = field(default_factory=set)

    @property
    def has_changes(self) -> bool:
        """Check if there are any differences between local and remote."""
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

    def detect_changes(self) -> list[FilterChange]:
        """Detect differences between local filters and Gmail.

        Compares local filter configurations with remote Gmail filters
        to identify what would change during a sync operation.

        Returns:
            List of FilterChange objects for filters with differences
        """
        changes: list[FilterChange] = []

        # Get remote filters once
        remote_filters = self.client.list_filters()

        # Compare each local filter with remote
        for name, local_filter in self.config.filters.items():
            local_entries = set(local_filter.entries)
            remote_entries = self.get_remote_filter_entries(name, remote_filters)

            change = self.compare_filter_entries(local_entries, remote_entries)
            change.name = name

            if change.has_changes:
                changes.append(change)

        return changes

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

    def push(self, apply_to_existing: bool = False) -> dict[str, Any]:
        """Push local filters to Gmail."""
        results: dict[str, Any] = {
            "created": 0,
            "updated": 0,
            "split_filters": [],
            "applied_to_existing": 0,
        }

        # Get existing remote filters
        remote_filters = self.client.list_filters()

        # Delete existing filters that match our base names
        # (we'll recreate them properly)
        base_names = set(self.config.filters.keys())
        deleted_count = 0
        for rf in remote_filters:
            base_name = self._extract_base_name(rf)
            if base_name in base_names:
                self.client.delete_filter(rf["id"])
                results["updated"] += 1
                deleted_count += 1

        if deleted_count > 0:
            print(f"  ✓ Deleted {deleted_count} existing filter(s)")

        # Create new filters
        for name, filter_config in self.config.filters.items():
            entries = filter_config.entries

            if len(entries) <= self.config.max_entries_per_filter:
                # Create single filter
                self._create_filter_with_entries(name, filter_config, entries)
                results["created"] += 1
                print(f"  ✓ Created filter: {name} ({len(entries)} entries)")
            else:
                # Split into multiple filters
                parts = self._split_entries(entries, self.config.max_entries_per_filter)
                for i, part_entries in enumerate(parts, 1):
                    part_name = name if i == 1 else f"{name}-{i}"
                    self._create_filter_with_entries(part_name, filter_config, part_entries)
                    results["created"] += 1
                    print(f"  ✓ Created filter: {part_name} ({len(part_entries)} entries)")

                results["split_filters"].append(name)
                print(f"  ✓ Split '{name}' into {len(parts)} part(s)")

            # Apply labels to existing conversations if requested
            if apply_to_existing and filter_config.label:
                print(f"  → Applying label '{filter_config.label}' to existing conversations...")
                label_id = self.client._get_or_create_label(filter_config.label)
                archive = filter_config.action in ["label_and_archive", "archive"]
                modified_count = self.client.apply_label_to_existing(
                    entries, label_id, archive=archive
                )
                results["applied_to_existing"] += modified_count
                if modified_count > 0:
                    print(f"    ✓ Applied to {modified_count} conversation(s)")

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
        """Extract base filter name from remote filter."""
        entries = self.client.parse_filter_entries(filter_obj)
        # Try to find matching local filter by entries
        for name, local_filter in self.config.filters.items():
            if any(entry in local_filter.entries for entry in entries):
                return name
        return ""

    def _match_filter_to_local(self, filter_obj: dict) -> str:
        """Match a remote filter to a local filter name."""
        entries = self.client.parse_filter_entries(filter_obj)

        for name, local_filter in self.config.filters.items():
            local_entries = set(local_filter.entries)
            remote_entries = set(entries)

            # If they share any entries, consider it a match
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
