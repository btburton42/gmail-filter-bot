"""Filter management logic for Gmail Filter Bot."""

from pathlib import Path
from typing import Any

from .config import Config, FilterConfig
from .gmail_client import GmailClient


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
                errors.append(
                    f"{name}: Action '{filter_config.action}' requires a label"
                )

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

    def sync(self, dry_run: bool = False) -> list[dict[str, Any]]:
        """Sync Gmail filters with local configuration."""
        changes = []

        # Get remote filters
        remote_filters = self.client.list_filters()

        # Group remote filters by base name (handling split filters)
        remote_by_base: dict[str, list[dict]] = {}
        for rf in remote_filters:
            # Extract filter name from criteria (we'll store it differently)
            entries = self.client.parse_filter_entries(rf)
            # Try to match with local filters
            base_name = self._match_filter_to_local(rf)
            if base_name:
                if base_name not in remote_by_base:
                    remote_by_base[base_name] = []
                remote_by_base[base_name].append(rf)

        # Compare with local filters
        for name, local_filter in self.config.filters.items():
            local_entries = set(local_filter.entries)

            # Get all entries from remote filters with this base name
            remote_entries = set()
            if name in remote_by_base:
                for rf in remote_by_base[name]:
                    remote_entries.update(self.client.parse_filter_entries(rf))

            # Calculate differences
            added = remote_entries - local_entries
            removed = local_entries - remote_entries

            if added or removed:
                change = {
                    "name": name,
                    "added": list(added),
                    "removed": list(removed),
                }
                changes.append(change)

                if not dry_run:
                    # Update local filter
                    self.config.filters[name].entries = list(remote_entries)

        if not dry_run and changes:
            self.config.save(self.config_path)

        return changes

    def push(self) -> dict[str, Any]:
        """Push local filters to Gmail."""
        results = {
            "created": 0,
            "updated": 0,
            "split_filters": [],
        }

        # Get existing remote filters
        remote_filters = self.client.list_filters()

        # Delete existing filters that match our base names
        # (we'll recreate them properly)
        base_names = set(self.config.filters.keys())
        for rf in remote_filters:
            base_name = self._extract_base_name(rf)
            if base_name in base_names:
                self.client.delete_filter(rf["id"])
                results["updated"] += 1

        # Create new filters
        for name, filter_config in self.config.filters.items():
            entries = filter_config.entries

            if len(entries) <= self.config.max_entries_per_filter:
                # Create single filter
                self._create_filter_with_entries(name, filter_config, entries)
                results["created"] += 1
            else:
                # Split into multiple filters
                parts = self._split_entries(entries, self.config.max_entries_per_filter)
                for i, part_entries in enumerate(parts, 1):
                    part_name = name if i == 1 else f"{name}-{i}"
                    self._create_filter_with_entries(
                        part_name, filter_config, part_entries
                    )
                    results["created"] += 1

                results["split_filters"].append(name)

        return results

    def trim(self) -> dict[str, Any]:
        """Remove duplicates and consolidate entries."""
        results = {
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
    ):
        """Create a single filter with given entries."""
        self.client.create_filter(
            from_addresses=entries,
            action=filter_config.action,
            label=filter_config.label,
        )

    def _extract_base_name(self, filter_obj: dict) -> str:
        """Extract base filter name from remote filter."""
        # This is tricky since Gmail doesn't store our custom names
        # We'll need to match by entries or use a different strategy
        # For now, return a placeholder
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
        
        results = {
            "consolidated": [],
        }
        
        # Find groups with more than one filter
        filters_to_remove = []
        
        for (action, label), filter_list in groups.items():
            if len(filter_list) <= 1:
                continue
            
            # Use the first filter name as the consolidated name
            primary_name = filter_list[0][0]
            primary_filter = filter_list[0][1]
            
            # Collect all unique entries
            all_entries = []
            removed_names = []
            
            for name, filter_config in filter_list:
                all_entries.extend(filter_config.entries)
                if name != primary_name:
                    removed_names.append(name)
                    filters_to_remove.append(name)
            
            # Remove duplicates while preserving order
            seen = set()
            unique_entries = []
            for entry in all_entries:
                if entry not in seen:
                    seen.add(entry)
                    unique_entries.append(entry)
            
            results["consolidated"].append({
                "name": primary_name,
                "action": action,
                "label": label,
                "source_count": len(filter_list),
                "total_entries": len(unique_entries),
                "removed_filters": removed_names,
            })
            
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
