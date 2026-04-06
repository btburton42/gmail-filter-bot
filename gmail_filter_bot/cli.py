"""Command-line interface for Gmail Filter Bot."""

import argparse
import sys
from pathlib import Path

from .config import Config, Credentials
from .filter_manager import FilterManager
from .gmail_client import GmailClient


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Sync and manage Gmail filters from YAML configuration"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("filters.yaml"),
        help="Path to filters configuration file (default: filters.yaml)",
    )
    parser.add_argument(
        "--credentials",
        type=Path,
        default=Path(".env"),
        help="Path to credentials file (default: .env)",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # sync command
    sync_parser = subparsers.add_parser("sync", help="Sync Gmail filters with local config")
    sync_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without applying",
    )

    # push command
    push_parser = subparsers.add_parser("push", help="Push local filters to Gmail")
    push_parser.add_argument(
        "--force",
        action="store_true",
        help="Skip confirmation prompt",
    )
    push_parser.add_argument(
        "--no-apply-existing",
        action="store_true",
        help="Don't apply labels to existing conversations (default: labels are applied to existing messages)",
    )

    # trim command
    subparsers.add_parser("trim", help="Remove duplicates and consolidate entries")

    # validate command
    subparsers.add_parser("validate", help="Validate configuration")

    # list command
    list_parser = subparsers.add_parser("list", help="List all filters with their details")
    list_parser.add_argument(
        "--show-entries",
        action="store_true",
        help="Show all entries for each filter (default: only show count)",
    )

    # init command
    init_parser = subparsers.add_parser(
        "init", help="Import existing Gmail filters into local config"
    )
    init_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be imported without creating filters.yaml",
    )
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing filters.yaml if it exists",
    )
    init_parser.add_argument(
        "--format",
        action="store_true",
        help="Consolidate entries with similar filters after import",
    )

    # format command
    format_parser = subparsers.add_parser("format", help="Consolidate entries with similar filters")
    format_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without modifying filters.yaml",
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    try:
        # Handle init command separately - it doesn't need existing config
        if args.command == "init":
            # Load credentials only (not the filters config)
            credentials = Credentials.from_env_file(args.credentials)
            client = GmailClient(credentials)
            return cmd_init(credentials, client, args)

        # Load configuration for other commands
        config = Config.load(args.config, args.credentials)

        # Initialize Gmail client
        client = GmailClient(config.credentials)

        # Initialize filter manager
        manager = FilterManager(config, client, args.config)

        if args.command == "sync":
            return cmd_sync(manager, args)
        elif args.command == "push":
            return cmd_push(manager, args)
        elif args.command == "trim":
            return cmd_trim(manager, args)
        elif args.command == "validate":
            return cmd_validate(manager, args)
        elif args.command == "format":
            return cmd_format(manager, args)
        elif args.command == "list":
            return cmd_list(manager, args)

    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        if args.command != "init":
            print(f"\nMake sure you've created the {args.config} file.", file=sys.stderr)
            print(f"Run: gmail-filter-bot init", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    return 0


def cmd_sync(manager: FilterManager, args):
    """Handle sync command."""
    print("Syncing Gmail filters...")

    changes = manager.sync(dry_run=args.dry_run)

    if args.dry_run:
        print("\n[DRY RUN] The following changes would be made:")

    if not changes:
        print("\nNo changes needed. Local filters are in sync with Gmail.")
        return 0

    print(f"\nFound {len(changes)} filter(s) with differences:")
    for change in changes:
        print(f"\n  {change.name}:")
        if change.added_to_remote:
            print(f"    + {len(change.added_to_remote)} new entries (from Gmail)")
        if change.removed_from_remote:
            print(f"    - {len(change.removed_from_remote)} removed entries (from Gmail)")

    if not args.dry_run:
        print("\nLocal filters.yaml has been updated with Gmail changes.")

    return 0


def cmd_push(manager: FilterManager, args):
    """Handle push command."""
    print("Pushing local filters to Gmail...")

    # Validate first
    validation = manager.validate()
    if not validation["valid"]:
        print("\nValidation errors:")
        for error in validation["errors"]:
            print(f"  - {error}")
        return 1

    # Check for filters that need splitting
    splits = validation.get("splits", [])
    if splits:
        print("\nThe following filters will be auto-split:")
        for split in splits:
            print(f"  {split['name']}: {split['entries']} entries → {split['parts']} filter(s)")

    if not args.force:
        response = input("\nProceed with push? [y/N]: ")
        if response.lower() != "y":
            print("Aborted.")
            return 0

    results = manager.push(apply_to_existing=not getattr(args, "no_apply_existing", False))

    print(f"\nPushed {results['created']} filter(s), updated {results['updated']} filter(s)")

    if results.get("split_filters"):
        print("\nAuto-split filters:")
        for name in results["split_filters"]:
            print(f"  - {name}")

    if results.get("applied_to_existing"):
        print(f"\nApplied labels to {results['applied_to_existing']} existing conversation(s)")

    return 0


def cmd_trim(manager: FilterManager, args):
    """Handle trim command."""
    print("Trimming duplicates and consolidating entries...")

    results = manager.trim()

    if results["duplicates"] == 0:
        print("\nNo duplicates found.")
    else:
        print(
            f"\nRemoved {results['duplicates']} duplicate entries from {len(results['filters'])} filter(s)"
        )
        for name in results["filters"]:
            print(f"  - {name}")

    return 0


def cmd_validate(manager: FilterManager, args):
    """Handle validate command."""
    print("Validating configuration...")

    validation = manager.validate()

    if validation["valid"]:
        print("\nConfiguration is valid!")
        print(f"  - {validation['total_filters']} filter(s)")
        print(f"  - {validation['total_entries']} total entries")

        if validation.get("splits"):
            print("\nFilters requiring auto-split:")
            for split in validation["splits"]:
                print(f"  - {split['name']}: {split['entries']} entries → {split['parts']} part(s)")

        return 0
    else:
        print("\nValidation errors:")
        for error in validation["errors"]:
            print(f"  - {error}")
        return 1


def cmd_init(credentials, client, args):
    """Handle init command - import existing Gmail filters."""
    from .config import Config, FilterConfig

    config_path = args.config

    # Check if filters.yaml already exists
    if config_path.exists() and not args.dry_run and not args.force:
        print(f"Error: {config_path} already exists.")
        print("Use --force to overwrite or --dry-run to preview.")
        return 1

    print("Importing existing Gmail filters...")

    # Fetch all filters from Gmail
    remote_filters = client.list_filters()

    if not remote_filters:
        print("\nNo filters found in Gmail.")
        return 0

    print(f"\nFound {len(remote_filters)} filter(s) in Gmail.")

    # Group filters by their action type and label
    imported_filters = {}

    for rf in remote_filters:
        # Parse the filter
        entries = client.parse_filter_entries(rf)
        if not entries:
            continue

        # Extract action info
        action_obj = rf.get("action", {})
        add_labels = action_obj.get("addLabelIds", [])
        remove_labels = action_obj.get("removeLabelIds", [])

        # Determine action type
        action, label = _classify_action(add_labels, remove_labels, client)

        # Create a unique key for grouping similar filters
        key = f"{action}:{label or 'none'}"

        if key not in imported_filters:
            imported_filters[key] = {
                "action": action,
                "label": label,
                "entries": [],
            }

        imported_filters[key]["entries"].extend(entries)

    # Generate filter names
    filter_configs = {}
    name_counters = {}

    for key, filter_data in imported_filters.items():
        # Generate a base name from label or action
        if filter_data["label"]:
            base_name = filter_data["label"].lower().replace(" ", "-").replace("_", "-")
        else:
            base_name = filter_data["action"].replace("_", "-")

        # Ensure unique names
        if base_name in filter_configs:
            if base_name not in name_counters:
                name_counters[base_name] = 1
            name_counters[base_name] += 1
            filter_name = f"{base_name}-{name_counters[base_name]}"
        else:
            filter_name = base_name

        filter_configs[filter_name] = filter_data

    if args.dry_run:
        print("\n[DRY RUN] The following filters would be created:")
        for name, filter_data in filter_configs.items():
            print(f"\n  {name}:")
            print(f"    action: {filter_data['action']}")
            if filter_data["label"]:
                print(f"    label: {filter_data['label']}")
            print(f"    entries: {len(filter_data['entries'])} email(s)")
        return 0

    # Create the config
    new_config = Config(
        max_entries_per_filter=50,
        filters={},
        credentials=credentials,
    )

    for name, filter_data in filter_configs.items():
        new_config.filters[name] = FilterConfig(
            name=name,
            action=filter_data["action"],
            label=filter_data["label"],
            entries=list(set(filter_data["entries"])),  # Remove duplicates
        )

    # Save the config
    new_config.save(config_path)

    print(f"\nCreated {config_path} with {len(new_config.filters)} filter(s):")
    for name in new_config.filters:
        print(f"  - {name}")

    print(f"\nTotal entries imported: {sum(len(f.entries) for f in new_config.filters.values())}")

    # Format/consolidate if requested
    if getattr(args, "format", False):
        print("\nConsolidating similar filters...")
        # Create temporary manager to format the new config
        temp_manager = FilterManager(new_config, client, config_path)
        format_results = temp_manager.format_filters(dry_run=False)

        if format_results["consolidated"]:
            print(f"Consolidated {len(format_results['consolidated'])} filter group(s):")
            for group in format_results["consolidated"]:
                print(
                    f"  - {group['name']}: combined {group['source_count']} filters into {group['total_entries']} entries"
                )
        else:
            print("No filters needed consolidation.")

    print(f"\nEdit {config_path} to customize filter names and actions.")
    print(f"Then run: gmail-filter-bot push")

    return 0


def _classify_action(add_labels, remove_labels, client=None):
    """Classify a Gmail filter action from label IDs.

    Args:
        add_labels: List of label IDs being added
        remove_labels: List of label IDs being removed
        client: GmailClient instance for looking up label names (optional)
    """
    action = "label_only"
    label = None

    # Check for delete (moved to trash)
    if "TRASH" in add_labels:
        return "delete", None

    # Check for star
    if "STARRED" in add_labels:
        return "star", None

    # Check for important/not important
    if "IMPORTANT" in add_labels:
        return "mark_important", None
    if "IMPORTANT" in remove_labels:
        return "mark_not_important", None

    # Check for archive (inbox removed)
    inbox_removed = "INBOX" in remove_labels

    # Get custom label (not system labels)
    system_labels = {"INBOX", "STARRED", "IMPORTANT", "TRASH", "SPAM", "UNREAD"}
    custom_labels = [l for l in add_labels if l not in system_labels]

    if custom_labels:
        label_id = custom_labels[0]  # Take first custom label
        # Look up label name if client provided
        if client:
            label = client.get_label_name(label_id)
        if not label:
            label = label_id  # Fall back to ID if lookup fails
        action = "label_and_archive" if inbox_removed else "label_only"
    elif inbox_removed:
        action = "archive"

    return action, label


def cmd_format(manager, args):
    """Handle format command - consolidate entries with similar filters."""
    print("Consolidating entries with similar filters...")

    results = manager.format_filters(dry_run=args.dry_run)

    if args.dry_run:
        print("\n[DRY RUN] The following changes would be made:")

    if not results["consolidated"]:
        print("\nNo similar filters found to consolidate.")
        return 0

    print(f"\nConsolidated {len(results['consolidated'])} filter group(s):")

    for group in results["consolidated"]:
        print(f"\n  {group['name']}:")
        print(f"    Combined {group['source_count']} filter(s) with same action/label")
        print(f"    Total entries: {group['total_entries']}")
        if group.get("removed_filters"):
            print(f"    Removed: {', '.join(group['removed_filters'])}")

    if not args.dry_run:
        print(f"\nfilters.yaml has been updated.")

    return 0


def cmd_list(manager, args):
    """Handle list command - display all filters with details."""
    print("Gmail Filters\n")
    print("=" * 60)

    if not manager.config.filters:
        print("\nNo filters configured.")
        return 0

    # Sort filters by name for consistent display
    sorted_filters = sorted(manager.config.filters.items())

    for name, filter_config in sorted_filters:
        print(f"\n📁 {name}")
        print("-" * 40)

        # Display action in human-readable format
        action_display = filter_config.action.replace("_", " ").title()
        print(f"  Action: {action_display}")

        # Display label if present
        if filter_config.label:
            print(f"  Label: {filter_config.label}")

        # Display entry count
        entry_count = len(filter_config.entries)
        print(f"  Entries: {entry_count}")

        # Show entries if requested
        if args.show_entries and filter_config.entries:
            print("  Emails:")
            for entry in filter_config.entries:
                print(f"    • {entry}")
        elif args.show_entries:
            print("  Emails: (none)")

    print("\n" + "=" * 60)
    total_filters = len(manager.config.filters)
    total_entries = sum(len(f.entries) for f in manager.config.filters.values())
    print(
        f"\nTotal: {total_filters} filter(s), {total_entries} entr{'y' if total_entries == 1 else 'ies'}"
    )

    return 0
