"""Command-line interface for Gmail Filter Bot."""

import argparse
import sys
from pathlib import Path

from .config import Config, Credentials
from .filter_manager import FilterChange, FilterManager
from .gmail_client import GmailClient


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Sync and manage Gmail filters from YAML configuration",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  gmail-filter-bot init              # Import existing Gmail filters
  gmail-filter-bot apply             # Sync changes (auto-detects direction)
  gmail-filter-bot apply --dry-run   # Preview what would change
  gmail-filter-bot clean             # Remove duplicates and optimize config
        """,
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

    # init command
    init_parser = subparsers.add_parser(
        "init",
        help="Import existing Gmail filters into local config",
        description="Create filters.yaml from your existing Gmail filters.",
    )
    init_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be imported",
    )

    # apply command - the main workflow
    apply_parser = subparsers.add_parser(
        "apply",
        help="Apply configuration changes (sync, push, or both)",
        description="""Apply changes between local config and Gmail.

By default, this performs a two-way sync:
  - Pulls new/deleted entries from Gmail
  - Pushes local changes to Gmail

Use --push or --sync to force a specific direction.""",
    )
    apply_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without applying",
    )
    apply_parser.add_argument(
        "--push",
        action="store_true",
        help="Force push local → Gmail (skip Gmail → local sync)",
    )
    apply_parser.add_argument(
        "--sync",
        action="store_true",
        help="Force sync Gmail → local (skip local → Gmail push)",
    )
    apply_parser.add_argument(
        "--no-apply-existing",
        action="store_true",
        help="Skip applying labels to existing conversations",
    )

    # clean command
    clean_parser = subparsers.add_parser(
        "clean",
        help="Optimize configuration (remove duplicates, consolidate filters)",
        description="Clean up filters.yaml by removing duplicates and consolidating similar filters.",
    )
    clean_parser.add_argument(
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
            credentials = Credentials.from_env_file(args.credentials)
            client = GmailClient(credentials)
            return cmd_init(credentials, client, args)

        # Load configuration for other commands
        config = Config.load(args.config, args.credentials)

        # Initialize Gmail client
        client = GmailClient(config.credentials)

        # Initialize filter manager
        manager = FilterManager(config, client, args.config)

        if args.command == "apply":
            return cmd_apply(manager, args)
        elif args.command == "clean":
            return cmd_clean(manager, args)

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


def cmd_init(credentials, client, args):
    """Handle init command - import existing Gmail filters."""
    config_path = args.config

    # Check if filters.yaml already exists
    if config_path.exists() and not args.dry_run:
        print(f"Error: {config_path} already exists.")
        print("Delete it first or use a different --config path.")
        return 1

    print("Importing existing Gmail filters...")

    # Fetch all filters from Gmail
    remote_filters = client.list_filters()

    if not remote_filters:
        print("\nNo filters found in Gmail.")
        return 0

    print(f"\nFound {len(remote_filters)} filter(s) in Gmail.")

    # Group filters by their action type and label
    from .config import FilterConfig

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

    print(f"\nEdit {config_path} to customize filter names and actions.")
    print(f"Then run: gmail-filter-bot apply")

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


def cmd_apply(manager: FilterManager, args):
    """Handle apply command - smart two-way sync."""
    dry_run = args.dry_run
    force_push = args.push
    force_sync = args.sync
    no_apply_existing = args.no_apply_existing

    # Validate first
    print("Validating configuration...")
    validation = manager.validate()
    if not validation["valid"]:
        print("\nValidation errors:")
        for error in validation["errors"]:
            print(f"  - {error}")
        return 1

    print(f"  ✓ {validation['total_filters']} filter(s), {validation['total_entries']} entries")

    if validation.get("splits"):
        print("\nFilters requiring auto-split:")
        for split in validation["splits"]:
            print(f"  - {split['name']}: {split['entries']} entries → {split['parts']} parts")

    # Detect changes
    print("\nAnalyzing changes...")
    changes = manager.detect_changes()

    # Classify changes
    push_changes = [
        c for c in changes if c.has_entry_changes or c.action_changed or c.label_changed
    ]
    sync_changes = [c for c in changes if c.has_entry_changes]

    # Determine operation mode
    if force_push and force_sync:
        print("Error: Cannot use both --push and --sync")
        return 1

    if force_push:
        mode = "push"
    elif force_sync:
        mode = "sync"
    else:
        # Auto-detect: if there are remote-only changes, suggest sync
        has_remote_changes = any(c.remote_only for c in changes)
        has_local_changes = any(
            c.local_only or c.action_changed or c.label_changed for c in changes
        )

        if has_remote_changes and has_local_changes:
            mode = "both"
        elif has_remote_changes:
            mode = "sync"
        else:
            mode = "push"

    if dry_run:
        print(f"\n[DRY RUN] Mode: {mode}")

    # Show changes
    if not changes:
        print("\nNo changes detected. Everything is in sync!")
        return 0

    print(f"\nDetected {len(changes)} filter(s) with changes:")
    for change in changes:
        print(f"\n  {change.name}:")
        if change.local_only:
            print(f"    + {len(change.local_only)} entries (local only)")
        if change.remote_only:
            print(f"    - {len(change.remote_only)} entries (remote only)")
        if change.action_changed:
            print(f"    ~ action changed")
        if change.label_changed:
            print(f"    ~ label changed")

    if dry_run:
        print("\n[DRY RUN] No changes made.")
        return 0

    # Confirm before applying
    if mode == "both":
        print("\nWarning: Changes detected in both directions!")
        print("  - Some entries exist only in Gmail (will be pulled)")
        print("  - Some entries exist only locally (will be pushed)")

    response = input(f"\nApply {mode} changes? [y/N]: ")
    if response.lower() != "y":
        print("Aborted.")
        return 0

    # Execute operations
    results = {"synced": 0, "pushed": 0, "skipped": 0}

    if mode in ("sync", "both") and sync_changes:
        print("\nSyncing Gmail → local...")
        for change in sync_changes:
            # Get remote entries for this filter
            remote_filters = manager.client.list_filters()
            remote_entries = manager.get_remote_filter_entries(change.name, remote_filters)

            # Update local filter with remote entries
            manager.config.filters[change.name].entries = sorted(remote_entries)
            results["synced"] += 1
            print(f"  ✓ Synced {change.name}")

        # Save updated configuration
        manager.config.save(manager.config_path)

    if mode in ("push", "both") and push_changes:
        print("\nPushing local → Gmail...")
        push_results = manager.push(
            apply_to_existing=not no_apply_existing,
            verbose=False,
            dry_run=False,
        )
        results["pushed"] = push_results["created"]
        results["skipped"] = push_results["skipped"]

    # Summary
    print(f"\n{'=' * 50}")
    print("Summary:")
    if results["synced"]:
        print(f"  Synced: {results['synced']} filter(s) from Gmail")
    if results["pushed"]:
        print(f"  Pushed: {results['pushed']} filter(s) to Gmail")
    if results["skipped"]:
        print(f"  Skipped: {results['skipped']} filter(s) (no changes)")

    return 0


def cmd_clean(manager: FilterManager, args):
    """Handle clean command - trim duplicates and format filters."""
    dry_run = args.dry_run

    print("Cleaning configuration...")

    # Step 1: Trim duplicates
    print("\n  Step 1: Removing duplicates...")
    trim_results = manager.trim()

    if trim_results["duplicates"] == 0:
        print("    ✓ No duplicates found")
    else:
        print(
            f"    ✓ Removed {trim_results['duplicates']} duplicates from {len(trim_results['filters'])} filter(s)"
        )
        for name in trim_results["filters"]:
            print(f"      - {name}")

    # Step 2: Consolidate similar filters
    print("\n  Step 2: Consolidating similar filters...")
    format_results = manager.format_filters(dry_run=dry_run)

    if not format_results["consolidated"]:
        print("    ✓ No filters to consolidate")
    else:
        print(f"    ✓ Consolidated {len(format_results['consolidated'])} filter group(s):")
        for group in format_results["consolidated"]:
            print(f"      - {group['name']}: {group['total_entries']} entries")

    if dry_run:
        print("\n[DRY RUN] No changes saved.")
    else:
        print(f"\nConfiguration saved to {manager.config_path}")

    return 0
