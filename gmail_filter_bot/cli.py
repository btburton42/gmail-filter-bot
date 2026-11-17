"""Command-line interface for Gmail Filter Bot."""

import argparse
import contextlib
import sys
from pathlib import Path

from . import settings
from .config import Config, Credentials
from .filter_manager import FilterManager
from .gmail_client import GmailClient
from .paths import (
    CREDENTIALS_FILENAME,
    TOKEN_FILENAME,
    forget_filters_path,
    remember_filters_path,
    resolve_filters_path,
    sibling_dir,
)
from .tui.app import run_triage
from .tui.controller import TriageController
from .tui.sessions import Item, TriageSession
from .tui.state import TriageState


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Sync and manage Gmail filters from YAML configuration",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  gmail-filter-bot init              # Import existing Gmail filters
  gmail-filter-bot plan              # Preview what would change
  gmail-filter-bot apply             # Sync changes (auto-detects direction)
  gmail-filter-bot label             # Interactively triage Inbox senders
  gmail-filter-bot label newsletters # Triage senders in the newsletters label
  gmail-filter-bot audit             # Review & recategorize existing entries
  gmail-filter-bot audit newsletters/*  # Audit every newsletters sublabel
  gmail-filter-bot doctor            # Diagnose configuration and setup problems
  gmail-filter-bot doctor resolve    # List auto-fixable diagnoses
  gmail-filter-bot config set hand-mode right  # One-handed right-hand keys
  gmail-filter-bot clean             # Remove duplicates and optimize config
        """,
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to filters configuration file (default: auto-resolved filters.yaml)",
    )
    parser.add_argument(
        "--credentials",
        type=Path,
        default=None,
        help="Path to credentials file (default: .env next to filters.yaml)",
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
    init_parser.add_argument(
        "--rollup",
        action="store_true",
        default=False,
        help="Enable global rollup in the generated config",
    )

    # plan command - preview changes without applying
    plan_parser = subparsers.add_parser(
        "plan",
        help="Preview what changes would be applied",
        description="""Preview what changes would be applied without making any changes.

Shows a detailed plan of what would happen during an apply operation,
including which filters would be created, updated, or skipped.""",
    )
    plan_parser.add_argument(
        "--push",
        action="store_true",
        help="Show plan for push local → Gmail only",
    )
    plan_parser.add_argument(
        "--pull",
        action="store_true",
        help="Show plan for pull Gmail → local only",
    )

    # apply command - the main workflow
    apply_parser = subparsers.add_parser(
        "apply",
        help="Apply configuration changes (pull, push, or both)",
        description="""Apply changes between local config and Gmail.

By default, this performs a two-way sync:
  - Pulls new/deleted entries from Gmail
  - Pushes local changes to Gmail

Use --push or --pull to force a specific direction.

WARNING: This command applies changes immediately without confirmation.
Use 'plan' first to preview what would change.""",
    )
    apply_parser.add_argument(
        "--push",
        action="store_true",
        help="Force push local → Gmail (skip Gmail → local pull)",
    )
    apply_parser.add_argument(
        "--pull",
        action="store_true",
        help="Force pull Gmail → local (skip local → Gmail push)",
    )
    apply_parser.add_argument(
        "--no-apply-existing",
        action="store_true",
        help="Skip applying labels to existing conversations",
    )

    # doctor command - diagnose problems
    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Diagnose configuration and setup problems",
        description="""Run health checks on your configuration, credentials, and (optionally) your Gmail connection.

Reports per-check status (pass / warn / fail) and exits non-zero when any
hard failure is found. Use --no-remote to skip checks that contact Gmail.

Each diagnosis is prefixed with its type, e.g.
  ✔ wildcard_shadowing: No wildcard shadowing within filters
Use `doctor resolve <type>` to auto-fix supported types.""",
    )
    doctor_parser.add_argument(
        "--no-remote",
        action="store_true",
        help="Skip checks that contact Gmail (credentials/config/token only)",
    )
    doctor_parser.add_argument(
        "--quiet",
        action="store_true",
        help="Only show warnings and failures (hide passing checks)",
    )
    doctor_subparsers = doctor_parser.add_subparsers(
        dest="doctor_command",
        help="Doctor subcommands",
    )
    resolve_parser = doctor_subparsers.add_parser(
        "resolve",
        help="Auto-fix a specific diagnosis type",
        description=(
            "Auto-fix a specific diagnosis type in filters.yaml.\n\n"
            "Supported types: " + ", ".join(settings.RESOLVABLE_CHECKS) + "\n\n"
            "Run with no type to list the currently resolvable diagnoses."
        ),
    )
    resolve_parser.add_argument(
        "type",
        nargs="?",
        default=None,
        help="Diagnosis type to resolve (e.g. wildcard_shadowing)",
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

    # config command - get/set user-level settings
    config_parser = subparsers.add_parser(
        "config",
        help="Get, set, or list user configuration",
        description=(
            "Read and write user-level settings stored in "
            f"{settings.user_config_path()}.\n\n"
            "Settings:\n"
            "  hand-mode     'left' or 'right' key map for the label command\n"
            "  filters-path  remembered location of filters.yaml\n\n"
            "Examples:\n"
            "  gfb config set hand-mode right\n"
            "  gfb config get hand-mode\n"
            "  gfb config ls\n"
            "  gfb config set filters-path ~/Repos/gmail-filter-bot/filters.yaml"
        ),
    )
    config_subparsers = config_parser.add_subparsers(
        dest="config_command",
        help="config accessor",
    )
    config_get_parser = config_subparsers.add_parser(
        "get",
        help="Print the value of a setting",
    )
    config_get_parser.add_argument(
        "key",
        nargs="?",
        default=None,
        help="Setting key (omit to list all)",
    )
    config_set_parser = config_subparsers.add_parser(
        "set",
        help="Set a setting value",
    )
    config_set_parser.add_argument("key", help="Setting key")
    config_set_parser.add_argument("value", help="Setting value")
    config_subparsers.add_parser(
        "ls",
        help="List all settings with their current values",
    )

    # help command
    help_parser = subparsers.add_parser(
        "help",
        help="Show detailed help information",
        description="Show detailed help and troubleshooting information.",
    )
    help_parser.add_argument(
        "topic",
        nargs="?",
        default=None,
        help="Topic to get help on (commands, auth, config, troubleshooting)",
    )

    # forget command - clear remembered filters.yaml location
    subparsers.add_parser(
        "forget",
        help="Forget the remembered filters.yaml location",
        description=(
            "Clear the remembered filters.yaml path stored in the user config "
            "(~/.config/gmail-filter-bot/config.yaml). Useful if you've moved "
            "or deleted a project."
        ),
    )

    # label command - interactive sender triage
    label_parser = subparsers.add_parser(
        "label",
        help="Interactively triage and label email senders",
        description="""Fetch unique senders from a Gmail folder and interactively
assign them to filters. Present each sender one by one:
  (f)ilter - assign to a filter (typeahead, ranked by last used)
  (a)llow  - skip this sender (remembered, won't appear again)
  (s)pam   - add to spam (delete) filter and report as spam
  (u)nsubscribe - use the sender's List-Unsubscribe header to opt out
  (e)xpand - print the opening lines of the message body (re-prompts)
  (k)skip   - skip this sender for now (will appear again next time)
  (q)uit   - exit and proceed to summary

The exact keys follow your hand-mode (left or right); set it with:
  gfb config set hand-mode left   # f / a / s / u / e / k / q / z
  gfb config set hand-mode right  # j / a / l / i / e / k / p / u

Unsubscribe and spam are deferred, undoable decisions. At the end of the
walk (including quitting early), a review screen shows everything queued;
nothing is sent until you approve that review and the app has exited.
Senders with no List-Unsubscribe header are reported as unsupported — filter
or spam them instead.

After the session, changes are saved to filters.yaml and you'll be
prompted to apply them to Gmail.""",
    )
    label_parser.add_argument(
        "folder",
        nargs="?",
        default="Inbox",
        help="Gmail folder/label to look for messages (default: Inbox)",
    )
    label_parser.add_argument(
        "--max-messages",
        type=int,
        default=200,
        help="Maximum number of recent messages to scan (default: 200)",
    )
    label_parser.add_argument(
        "-r",
        "--rollup",
        action="store_true",
        default=False,
        help="Roll up all sub-labels into one view (e.g. 'a' matches 'a', 'a/b', 'a/b/c')",
    )

    # audit command - review & recategorize existing filter entries
    audit_parser = subparsers.add_parser(
        "audit",
        help="Review and recategorize existing filter entries",
        description="""Review each entry already bucketed into a filter and recategorize it.

Present each entry one at a time:
  (f) move   - recategorize to another filter (typeahead, ranked by last used)
  (a) remove - delete the entry from filters.yaml entirely
  (s) spam   - move the entry to the spam (delete) filter
  (k) keep   - leave the entry in its current filter
  (q) quit   - exit and proceed to summary
  (z) undo   - rewind the last action (finger-slip recovery)

The exact keys follow your hand-mode (left or right); set it with:
  gfb config set hand-mode left   # f / a / s / k / q / z
  gfb config set hand-mode right  # j / a / l / k / p / u

There is no "allow" action: the entries being audited are already bucketed
into a label, so you either keep, move, spam, or remove them.

Arguments (optional, selects which filters to review):
  gfb audit                  review every filter, starting from the first
  gfb audit newsletters      review just the newsletters filter
  gfb audit 'newsletters/*'  review every newsletters sublabel (glob)

After the session, changes are saved to filters.yaml and you'll be prompted
to apply them to Gmail.""",
    )
    audit_parser.add_argument(
        "label",
        nargs="?",
        default=None,
        help=(
            "Filter name or glob to review (default: all filters). "
            "A glob like 'newsletters/*' reviews every matching sublabel."
        ),
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    # Resolve filters.yaml (auto: CWD -> walk up -> remembered -> default).
    config_path = resolve_filters_path(args.config)
    filters_dir = sibling_dir(config_path)
    credentials_path = args.credentials if args.credentials is not None else (
        filters_dir / CREDENTIALS_FILENAME
    )
    token_path = filters_dir / TOKEN_FILENAME

    # ``forget`` only touches the user config; it needs nothing else.
    if args.command == "forget":
        if forget_filters_path():
            print("Forgot remembered filters.yaml location.")
        else:
            print("No remembered filters.yaml location to forget.")
        return 0

    # ``config`` only touches the user config; it needs no filters.yaml.
    if args.command == "config":
        return cmd_config(args)

    try:
        # Handle init command separately - it doesn't need existing config
        if args.command == "init":
            credentials = Credentials.from_env_file(credentials_path)
            client = GmailClient(credentials, token_path=token_path)
            return cmd_init(credentials, client, args, config_path, credentials_path)

        # Handle doctor command separately - it must tolerate a missing/broken
        # config and credentials, so it does its own resilient loading.
        if args.command == "doctor":
            return cmd_doctor(args, config_path, credentials_path, token_path)

        # Load configuration for other commands
        config = Config.load(config_path, credentials_path)

        # Remember where filters.yaml lives so future runs from other
        # directories can still find it.
        if config_path.is_file():
            remember_filters_path(config_path)

        # Initialize Gmail client
        client = GmailClient(config.credentials, token_path=token_path)

        # Initialize filter manager
        manager = FilterManager(config, client, config_path)

        if args.command == "plan":
            return cmd_plan(manager, args)
        elif args.command == "apply":
            return cmd_apply(manager, args)
        elif args.command == "clean":
            return cmd_clean(manager, args)
        elif args.command == "label":
            return cmd_label(manager, args)
        elif args.command == "audit":
            return cmd_audit(manager, args)
        elif args.command == "help":
            return cmd_help(args)

    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        if args.command != "init":
            print(f"\nMake sure you've created the {config_path} file.", file=sys.stderr)
            print("Run: gmail-filter-bot init", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    return 0


def cmd_init(credentials, client, args, config_path: Path, credentials_path: Path):
    """Handle init command - import existing Gmail filters."""

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

    for _key, filter_data in imported_filters.items():
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
        if args.rollup:
            print("\n  rollup: true (global)")
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
        rollup=args.rollup,
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

    # Remember where we just created it.
    remember_filters_path(config_path)

    print(f"\nCreated {config_path} with {len(new_config.filters)} filter(s):")
    for name in new_config.filters:
        print(f"  - {name}")

    print(f"\nTotal entries imported: {sum(len(f.entries) for f in new_config.filters.values())}")

    print(f"\nEdit {config_path} to customize filter names and actions.")
    print("Then run: gmail-filter-bot apply")

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
    custom_labels = [lbl for lbl in add_labels if lbl not in system_labels]

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


def cmd_plan(manager: FilterManager, args):
    """Handle plan command - preview changes without applying."""
    force_push = args.push
    force_pull = args.pull

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
        c
        for c in changes
        if c.has_entry_changes or c.action_changed or c.label_changed or c.rollup_changed
    ]

    # Determine operation mode
    if force_push and force_pull:
        print("Error: Cannot use both --push and --pull")
        return 1

    if force_push:
        mode = "push"
    elif force_pull:
        mode = "sync"
    else:
        # Auto-detect: if there are remote-only changes, suggest pull
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

    print(f"\nMode: {mode}")

    # Show changes
    if not changes:
        print("\nNo changes detected. Everything is in sync!")
        return 0

    print(f"\nDetected {len(changes)} filter(s) with changes:")
    for change in changes:
        print(f"\n  {change.name}:")
        if change.local_only:
            print(f"    + {len(change.local_only)} entries (local only - would be pushed)")
            for entry in sorted(change.local_only)[:5]:
                print(f"        {entry}")
            if len(change.local_only) > 5:
                print(f"        ... and {len(change.local_only) - 5} more")
        if change.remote_only:
            print(f"    - {len(change.remote_only)} entries (remote only - would be pulled)")
            for entry in sorted(change.remote_only)[:5]:
                print(f"        {entry}")
            if len(change.remote_only) > 5:
                print(f"        ... and {len(change.remote_only) - 5} more")
        if change.action_changed:
            print("    ~ action changed")
        if change.label_changed:
            print("    ~ label changed")
        if change.rollup_changed:
            print("    ~ rollup changed")

    # Show push preview for detailed output
    if push_changes:
        print("\n" + "-" * 50)
        print("Push preview (local → Gmail):")
        manager.push(apply_to_existing=False, verbose=False, dry_run=True)

    print("\n" + "=" * 50)
    print("Run 'gmail-filter-bot apply' to apply these changes.")

    return 0


def cmd_apply(manager: FilterManager, args):
    """Handle apply command - smart two-way sync."""
    force_push = args.push
    force_pull = args.pull
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
        c
        for c in changes
        if c.has_entry_changes or c.action_changed or c.label_changed or c.rollup_changed
    ]
    sync_changes = [c for c in changes if c.has_entry_changes]

    # Determine operation mode
    if force_push and force_pull:
        print("Error: Cannot use both --push and --pull")
        return 1

    if force_push:
        mode = "push"
    elif force_pull:
        mode = "sync"
    else:
        # Auto-detect: if there are remote-only changes, suggest pull
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

    print(f"\nMode: {mode}")

    # Show changes
    if not changes:
        print("\nNo changes detected. Everything is in sync!")
        return 0

    print(f"\nApplying {len(changes)} filter(s) with changes...")
    for change in changes:
        print(f"  {change.name}:")
        if change.local_only:
            print(f"    + {len(change.local_only)} entries (local only)")
        if change.remote_only:
            print(f"    - {len(change.remote_only)} entries (remote only)")
        if change.action_changed:
            print("    ~ action changed")
        if change.label_changed:
            print("    ~ label changed")
        if change.rollup_changed:
            print("    ~ rollup changed")

    # Execute operations
    results = {"synced": 0, "pushed": 0, "skipped": 0}

    if mode in ("sync", "both") and sync_changes:
        print("\nPulling Gmail → local...")
        for change in sync_changes:
            # Get remote entries for this filter
            remote_filters = manager.client.list_filters()
            remote_entries = manager.get_remote_filter_entries(change.name, remote_filters)

            # Update local filter with remote entries
            manager.config.filters[change.name].entries = sorted(remote_entries)
            results["synced"] += 1
            print(f"  ✓ Pulled {change.name}")

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
        print(f"  Pulled: {results['synced']} filter(s) from Gmail")
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

    # Step 3: Sort filters alphabetically
    print("\n  Step 3: Sorting filters alphabetically...")
    before_order = list(manager.config.filters.keys())
    manager.config.filters = dict(sorted(manager.config.filters.items()))
    after_order = list(manager.config.filters.keys())
    if before_order == after_order:
        print("    ✓ Already sorted")
    else:
        print("    ✓ Sorted filters alphabetically")

    if dry_run:
        print("\n[DRY RUN] No changes saved.")
    else:
        manager.config.save(manager.config_path)
        print(f"\nConfiguration saved to {manager.config_path}")

    return 0


def cmd_doctor(args, config_path: Path, credentials_path: Path, token_path: Path):
    """Handle doctor command - diagnose configuration and setup problems."""
    # `doctor resolve <type>` auto-fixes a diagnosis; handle it separately.
    if getattr(args, "doctor_command", None) == "resolve":
        return cmd_doctor_resolve(args, config_path, credentials_path, token_path)

    no_remote = args.no_remote
    quiet = args.quiet

    checks: list[dict] = []

    # --- Credentials ---
    creds = None
    if not credentials_path.exists():
        checks.append(
            {
                "name": "credentials_file",
                "level": "fail",
                "message": f"Credentials file not found: {credentials_path}",
                "details": [],
            }
        )
    else:
        try:
            creds = Credentials.from_env_file(credentials_path)
        except Exception as e:
            checks.append(
                {
                    "name": "credentials_file",
                    "level": "fail",
                    "message": f"Could not read credentials file: {e}",
                    "details": [],
                }
            )

    if creds is not None:
        issues = []
        if not creds.client_id:
            issues.append("GOOGLE_CLIENT_ID is empty")
        elif not creds.client_id.endswith(".apps.googleusercontent.com"):
            issues.append(
                "GOOGLE_CLIENT_ID doesn't look like a desktop OAuth client id "
                "(expected to end in .apps.googleusercontent.com)"
            )
        if not creds.client_secret:
            issues.append("GOOGLE_CLIENT_SECRET is empty")
        if issues:
            checks.append(
                {
                    "name": "credentials_values",
                    "level": "fail",
                    "message": f"{len(issues)} credential problem(s)",
                    "details": issues,
                }
            )
        else:
            checks.append(
                {
                    "name": "credentials_values",
                    "level": "pass",
                    "message": "Credentials look well-formed",
                    "details": [],
                }
            )

    # --- Config file ---
    config = None
    if not config_path.exists():
        checks.append(
            {
                "name": "config_file",
                "level": "fail",
                "message": f"Configuration file not found: {config_path}",
                "details": ["Run: gmail-filter-bot init"],
            }
        )
    else:
        try:
            config = Config.load(config_path, credentials_path)
            checks.append(
                {
                    "name": "config_file",
                    "level": "pass",
                    "message": f"Loaded {config_path} ({len(config.filters)} filters)",
                    "details": [],
                }
            )
        except Exception as e:
            checks.append(
                {
                    "name": "config_file",
                    "level": "fail",
                    "message": f"Could not load configuration: {e}",
                    "details": [],
                }
            )
        else:
            # Remember where filters.yaml lives for future runs from other
            # dirs. This is a best-effort side effect: a failure to persist the
            # remembered path (e.g. an unwritable user-config dir) must not
            # fail the config_file health check.
            with contextlib.suppress(Exception):
                remember_filters_path(config_path)

    # --- Local (offline) checks ---
    if config is not None:
        try:
            # diagnose() never touches the network, so a None client is safe.
            manager = FilterManager(config, None, config_path)
            checks.extend(manager.diagnose())
        except Exception as e:
            checks.append(
                {
                    "name": "local_checks",
                    "level": "fail",
                    "message": f"Local checks crashed: {e}",
                    "details": [],
                }
            )

    # --- Token ---
    if token_path.exists():
        checks.append(
            {
                "name": "token",
                "level": "pass",
                "message": f"{token_path} present",
                "details": [],
            }
        )
    else:
        checks.append(
            {
                "name": "token",
                "level": "warn",
                "message": (
                    f"{token_path} not found - you'll be prompted to authenticate "
                    "on the next command"
                ),
                "details": [],
            }
        )

    # --- Remote checks ---
    if no_remote:
        checks.append(
            {
                "name": "remote",
                "level": "pass",
                "message": "Remote checks skipped (--no-remote)",
                "details": [],
            }
        )
    elif creds is None:
        checks.append(
            {
                "name": "remote",
                "level": "warn",
                "message": "Remote checks skipped (no credentials)",
                "details": [],
            }
        )
    elif config is None:
        checks.append(
            {
                "name": "remote",
                "level": "warn",
                "message": "Remote checks skipped (no valid config)",
                "details": [],
            }
        )
    elif not token_path.exists():
        checks.append(
            {
                "name": "remote",
                "level": "warn",
                "message": "Remote checks skipped (no token - run another command to authenticate)",
                "details": [],
            }
        )
    else:
        try:
            client = GmailClient(creds)
            manager = FilterManager(config, client, config_path)
            checks.extend(_doctor_remote_checks(manager, client))
        except Exception as e:
            checks.append(
                {
                    "name": "remote",
                    "level": "fail",
                    "message": f"Remote checks failed: {e}",
                    "details": [],
                }
            )

    # --- Report ---
    symbols = {"pass": "✓", "warn": "⚠", "fail": "✗"}
    counts = {"pass": 0, "warn": 0, "fail": 0}
    for c in checks:
        counts[c["level"]] += 1

    print("Running health checks...\n")
    for c in checks:
        if quiet and c["level"] == "pass":
            continue
        sym = symbols[c["level"]]
        print(f"  {sym} {c['name']}: {c['message']}")
        for detail in c.get("details", []):
            print(f"      - {detail}")

    print(f"\n{'=' * 50}")
    print(
        f"Doctor: {counts['pass']} passed, {counts['warn']} warning(s), "
        f"{counts['fail']} failure(s)"
    )

    return 1 if counts["fail"] > 0 else 0


def cmd_doctor_resolve(args, config_path: Path, credentials_path: Path, token_path: Path):
    """Handle `doctor resolve [type]` - auto-fix a diagnosis type."""
    check_name = args.type

    if not config_path.exists():
        print(f"Error: Configuration file not found: {config_path}", file=sys.stderr)
        print("Run: gmail-filter-bot init", file=sys.stderr)
        return 1

    try:
        config = Config.load(config_path, credentials_path)
    except Exception as e:
        print(f"Error: Could not load configuration: {e}", file=sys.stderr)
        return 1

    manager = FilterManager(config, None, config_path)

    # No type given: list the resolvable diagnoses currently present.
    if check_name is None:
        try:
            checks = manager.diagnose()
        except Exception as e:
            print(f"Error running diagnosis: {e}", file=sys.stderr)
            return 1
        print("Resolvable diagnosis types:")
        print(f"  supported: {', '.join(settings.RESOLVABLE_CHECKS)}\n")
        found = False
        for c in checks:
            if c["name"] in settings.RESOLVABLE_CHECKS and c["level"] != "pass":
                found = True
                print(f"  {c['name']}: {c['message']}")
        if not found:
            print("  (no resolvable diagnoses found)")
        print("\nRun: gfb doctor resolve <type>")
        return 0

    try:
        result = manager.resolve(check_name)
    except Exception as e:
        print(f"Error resolving '{check_name}': {e}", file=sys.stderr)
        return 1

    if result["resolved"]:
        print(f"Resolved {result['fixed']} item(s) for '{check_name}'.")
        for d in result["details"]:
            print(f"  - {d}")
        print(f"\nSaved {config_path}")
        return 0
    else:
        reason = result["reason"] or "nothing to fix"
        print(f"Could not resolve '{check_name}': {reason}")
        return 1


def cmd_config(args):
    """Handle `config get/set/ls` - user-level settings."""
    sub = getattr(args, "config_command", None)
    key = getattr(args, "key", None)
    value = getattr(args, "value", None)

    if sub == "ls" or (sub == "get" and key is None) or sub is None:
        return _config_ls()
    if sub == "get":
        return _config_get(key)
    if sub == "set":
        return _config_set(key, value)
    print(f"Unknown config command: {sub}", file=sys.stderr)
    return 1


def _config_ls():
    print(f"User config: {settings.user_config_path()}\n")
    for key in settings.VISIBLE_SETTINGS:
        val = settings.get(key)
        shown = val if val is not None else "<not set>"
        print(f"{key} = {shown}")
        desc = settings.DESCRIPTIONS.get(key, "")
        if desc:
            print(f"    {desc}")
        print()
    return 0


def _config_get(key: str):
    if key not in settings.VISIBLE_SETTINGS:
        print(
            f"Unknown setting '{key}'. Known: {', '.join(settings.VISIBLE_SETTINGS)}",
            file=sys.stderr,
        )
        return 1
    val = settings.get(key)
    print(val if val is not None else "<not set>")
    return 0


def _config_set(key: str, value: str):
    if key not in settings.SETTABLE_SETTINGS:
        print(
            f"Unknown setting '{key}'. Settable: {', '.join(settings.SETTABLE_SETTINGS)}",
            file=sys.stderr,
        )
        return 1
    try:
        settings.set(key, value)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    new_val = settings.get(key)
    print(f"{key} = {new_val}")
    return 0


def _doctor_remote_checks(manager: FilterManager, client: GmailClient):
    """Run checks that require a live Gmail connection."""
    checks: list[dict] = []

    # Connectivity
    try:
        labels = client.list_labels()
        checks.append(
            {
                "name": "remote_connectivity",
                "level": "pass",
                "message": f"Connected to Gmail ({len(labels)} label(s))",
                "details": [],
            }
        )
    except Exception as e:
        checks.append(
            {
                "name": "remote_connectivity",
                "level": "fail",
                "message": f"Could not connect to Gmail: {e}",
                "details": [],
            }
        )
        return checks

    # Referenced labels exist in Gmail
    referenced = {
        f.label for f in manager.config.filters.values() if f.label
    }
    label_names_lower = {n.lower() for n in labels}
    missing = sorted({name for name in referenced if name.lower() not in label_names_lower})
    if missing:
        checks.append(
            {
                "name": "remote_labels",
                "level": "warn",
                "message": f"{len(missing)} referenced label(s) not in Gmail (created on apply)",
                "details": missing,
            }
        )
    else:
        checks.append(
            {
                "name": "remote_labels",
                "level": "pass",
                "message": "All referenced labels exist in Gmail",
                "details": [],
            }
        )

    # Orphaned remote filters (no matching local filter)
    remote_filters = client.list_filters()
    orphans: list[str] = []
    for rf in remote_filters:
        base = manager._match_filter_to_local(rf)
        if not base:
            action = manager._extract_remote_action(rf)
            label = manager._extract_remote_label(rf)
            nentries = len(client.parse_filter_entries(rf))
            orphans.append(f"action={action}, label={label}, {nentries} entries")
    if orphans:
        checks.append(
            {
                "name": "remote_orphans",
                "level": "warn",
                "message": f"{len(orphans)} remote filter(s) not matched to any local config",
                "details": orphans,
            }
        )
    else:
        checks.append(
            {
                "name": "remote_orphans",
                "level": "pass",
                "message": "All remote filters matched to local config",
                "details": [],
            }
        )

    return checks


# Arrow-key escape sequences: ESC [ X (and ESC O X for some terminals).
_ARROW_KEYS = {"A": "up", "B": "down", "C": "right", "D": "left"}


# --- triage aftermath ------------------------------------------------------
#
# The interactive part happens in the Textual app (``gmail_filter_bot.tui``),
# which runs in the alternate screen and returns a TriageResult. Everything
# below runs *after* that screen is gone, so the output is plain text that can
# be piped, grepped, or pasted into an issue.
#
# That separation is the whole point of deferring the irreversible work: the
# user reviews the queue inside the app, but the reports and unsubscribe
# requests are actually sent here, in the open, with their results printed.


def _print_triage_summary(manager: FilterManager, session, *, audit: bool) -> None:
    """Print the closing tally and the list of changes."""
    state = session.state
    print(f"\n{'=' * 50}")
    print("Audit Summary:" if audit else "Triage Summary:")
    for line in state.summary_lines(audit=audit):
        print(line)
    change_lines = TriageController(manager, state).change_lines()
    if change_lines:
        print()
        for line in change_lines:
            print(line)


def _fire_deferred_queue(manager: FilterManager, session, keep: list[str]) -> None:
    """Send the spam reports and unsubscribe requests the user approved.

    ``keep`` is the list of emails that survived the review screen. Anything the
    user skipped is dropped from the queue *before* firing, so a skipped sender
    is never accidentally included by a later loop.
    """
    state = session.state
    approved = set(keep)
    spam = [item for item in state.spam_queue if item.email in approved]
    unsubs = [item for item in state.unsub_queue if item.email in approved]
    dropped = (len(state.spam_queue) - len(spam)) + (len(state.unsub_queue) - len(unsubs))
    state.spam_queue[:] = spam
    state.unsub_queue[:] = unsubs

    if dropped:
        print(f"\nDropped {dropped} queued item(s) at the review screen.")
    if not spam and not unsubs:
        print("\nNothing was sent.")
        return

    print(
        f"\nSending {len(spam)} spam report(s) "
        f"and {len(unsubs)} unsubscribe(s)..."
    )
    report = TriageController(manager, state).fire_queue()

    if report.spam_queued:
        if report.spam_error:
            print(f"  ! spam: failed ({report.spam_error})")
        else:
            print(f"  \u2713 spam: {report.spam_reported} sender(s) reported")
    for email, status, message in report.unsubs:
        mark = "\u2713" if status in ("one_click", "url") else "!"
        print(f"  {mark} {email}: {message}")


def _finish_triage(manager: FilterManager, session, result, *, audit: bool) -> int:
    """Summarize, save, fire the deferred queue, and offer to push to Gmail."""
    state = session.state
    _print_triage_summary(manager, session, audit=audit)

    if not result.completed and session.items:
        print(
            f"\n(stopped at item {session.position} \u2014 "
            "nothing was sent behind your back)"
        )

    if not state.changed_anything:
        print("\nNo changes made.")
        return 0

    manager.config.save(manager.config_path)
    print(f"\nSaved to {manager.config_path}")

    if result.reviewed and result.keep is not None:
        _fire_deferred_queue(manager, session, result.keep)
    elif state.pending_count:
        print(
            f"\n{state.pending_count} queued item(s) left unsent "
            "(the review was cancelled). Run the command again to queue them."
        )

    return _offer_apply(manager)


def cmd_label(manager: FilterManager, args):
    """Handle label command - interactively triage and label email senders."""

    folder = args.folder
    max_messages = args.max_messages
    rollup = getattr(args, "rollup", False)

    # Warn (and offer to push) when an earlier session left local filters
    # unpushed — otherwise these senders are skipped without ever reaching Gmail.
    _check_pending_changes(manager)

    if rollup:
        all_labels = manager.client.list_labels()
        folder_lower = folder.lower()
        matched = [
            name
            for name in all_labels
            if name.lower() == folder_lower or name.lower().startswith(folder_lower + "/")
        ]
        if not matched:
            print(
                f"Error: No labels matching '{folder}' (with sub-labels) found in Gmail.",
                file=sys.stderr,
            )
            available = list(all_labels.keys())
            if available:
                print(f"\nAvailable labels: {', '.join(sorted(available))}", file=sys.stderr)
            return 1

        print(f"Rolling up {len(matched)} label(s): {', '.join(sorted(matched))}")
        per_label = max(1, max_messages // len(matched))
        senders = []
        seen_emails = set()
        for label_name in sorted(matched):
            label_id = all_labels[label_name]
            print(f"  Fetching from '{label_name}' (up to {per_label} messages)...")
            label_senders = manager.client.list_senders(label_id, max_results=per_label)
            for entry in label_senders:
                email = entry.email
                if email not in seen_emails:
                    seen_emails.add(email)
                    senders.append(entry)
    else:
        print(f"Fetching unique senders from '{folder}' (up to {max_messages} messages)...")

        label_id = manager.client._resolve_label_id(folder)
        if label_id is None:
            print(f"Error: Label '{folder}' not found in Gmail.", file=sys.stderr)
            available = list(manager.client.list_labels().keys())
            if available:
                print(f"\nAvailable labels: {', '.join(sorted(available))}", file=sys.stderr)
            return 1

        senders = manager.client.list_senders(label_id, max_results=max_messages)

    if not senders:
        print("No senders found.")
        return 0

    print(f"Found {len(senders)} unique sender(s).")

    # Filter out senders already covered by existing filter entries or allowed list
    new_senders = [s for s in senders if not _is_sender_filtered(s.email, manager.config)]
    skipped = len(senders) - len(new_senders)
    if skipped:
        print(f"Skipping {skipped} sender(s) already covered by existing filters or allowed list.")

    if not new_senders:
        print("All senders are already categorized. Nothing to do.")
        return 0

    print(f"\n{len(new_senders)} sender(s) to triage.")

    # The interactive loop lives in the Textual app now. Everything above this
    # line is fetching and filtering; everything below is the aftermath, and it
    # runs in a *normal* terminal because the app has already exited.
    state = TriageState(config=manager.config)
    controller = TriageController(manager, state)
    session = TriageSession(
        manager,
        state,
        controller,
        items=[Item.from_sender(sender) for sender in new_senders],
    )
    result = run_triage(session)
    return _finish_triage(manager, session, result, audit=False)


def _push_to_gmail(manager: FilterManager, *, apply_to_existing: bool = True) -> int:
    """Push local filters to Gmail, reporting the result. Returns an exit code.

    Shared by :func:`_offer_apply` (end of a triage session) and
    :func:`_check_pending_changes` (start of one).
    """
    print("\nApplying to Gmail...")
    try:
        push_results = manager.push(
            apply_to_existing=apply_to_existing, verbose=False, dry_run=False
        )
        print(f"\nPushed {push_results['created']} filter(s) to Gmail.")
        if push_results.get("skipped"):
            print(f"Skipped {push_results['skipped']} filter(s) (no changes).")
        if push_results.get("applied_to_existing"):
            print(
                f"Applied labels to {push_results['applied_to_existing']} existing conversation(s)."
            )
    except Exception as e:
        print(f"Error applying: {e}", file=sys.stderr)
        return 1

    return 0


def _pending_local_changes(manager: FilterManager) -> list:
    """Return filters whose local config differs from Gmail (empty on failure)."""
    try:
        changes = manager.detect_changes()
    except Exception:
        return []
    return [
        change
        for change in changes
        if change.local_only
        or change.action_changed
        or change.label_changed
        or change.rollup_changed
    ]


def _check_pending_changes(
    manager: FilterManager, *, interactive: bool | None = None
) -> None:
    """Surface unpushed changes left behind by an earlier session.

    Triage skips senders that already appear in the *local* ``filters.yaml``, so
    a session where ``apply`` was declined leaves Gmail without those filters
    while ``label``/``audit`` quietly stop offering the senders. Rather than
    hide that drift, warn about it up front and offer to push.
    """
    pending = _pending_local_changes(manager)
    if not pending:
        return

    print(
        f"\n! {len(pending)} filter(s) have changes not yet in Gmail:",
        file=sys.stderr,
    )
    for change in sorted(pending, key=lambda c: c.name)[:10]:
        details = []
        if change.local_only:
            details.append(f"+{len(change.local_only)} sender(s)")
        if change.action_changed:
            details.append("action changed")
        if change.label_changed:
            details.append("label changed")
        if change.rollup_changed:
            details.append("rollup changed")
        print(f"    {change.name}: {', '.join(details)}", file=sys.stderr)
    if len(pending) > 10:
        print(f"    ... and {len(pending) - 10} more", file=sys.stderr)

    if interactive is None:
        interactive = sys.stdin.isatty()

    if not interactive:
        print(
            "  Run 'gfb apply' to push them; triage skips senders already in filters.yaml.",
            file=sys.stderr,
        )
        return

    response = input("\nApply these to Gmail before continuing? [y/N] ").strip().lower()
    if response not in ("y", "yes"):
        print(
            "  Not applying. Senders already in filters.yaml won't be offered again "
            "until this is pushed."
        )
        return

    _push_to_gmail(manager, apply_to_existing=True)


def _offer_apply(manager: FilterManager) -> int:
    """Show a push plan preview and offer to apply changes to Gmail.

    Shared by the ``label`` and ``audit`` commands. The caller is responsible
    for having already saved the local config. Returns a process exit code.
    """
    print("\n" + "-" * 50)
    print("Plan: changes that would be pushed to Gmail:")
    try:
        validation = manager.validate()
        if not validation["valid"]:
            print("\nValidation errors:")
            for error in validation["errors"]:
                print(f"  - {error}")
            print("\nFix the errors in filters.yaml before applying.")
            return 1
        push_changes = manager.detect_changes()
        if push_changes:
            for change in push_changes:
                print(f"  {change.name}:")
                if change.local_only:
                    print(f"    + {len(change.local_only)} new sender(s)")
        else:
            print("  No changes detected (all entries already in Gmail).")
    except Exception as e:
        print(f"  Could not detect changes: {e}")

    # Apply prompt
    response = input("\nApply changes to Gmail? [y/N] ").strip().lower()
    if response not in ("y", "yes"):
        print("Not applying. You can run 'gfb apply' later.")
        return 0

    return _push_to_gmail(manager)


# ---------------------------------------------------------------------------
# audit command
# ---------------------------------------------------------------------------


def _resolve_audit_filters(config, pattern: str | None) -> tuple[list[str], str | None]:
    """Decide which filter names to review for ``gfb audit [label]``.

    Returns ``(names, error)``. ``pattern`` may be:
      - ``None`` / empty: every filter that has entries (in config order).
      - A glob (contains ``*`` / ``?`` / ``[``): filter names matching it.
      - A plain name: just that filter.

    Filters with no entries are skipped from the review set (nothing to audit).
    """
    import fnmatch

    names = list(config.filters.keys())

    if not pattern:
        return [n for n in names if config.filters[n].entries], None

    if any(c in pattern for c in "*?["):
        matched = [
            n for n in names
            if fnmatch.fnmatchcase(n, pattern) and config.filters[n].entries
        ]
        return matched, None

    # Exact filter name.
    if pattern not in config.filters:
        return [], f"Filter '{pattern}' not found in {config.filters.keys() and 'filters.yaml' or 'config'}."
    if not config.filters[pattern].entries:
        return [], f"Filter '{pattern}' has no entries to audit."
    return [pattern], None


def cmd_audit(manager: FilterManager, args):
    """Handle ``audit [label]`` - review and recategorize existing filter entries.

    Walks every entry in the selected filter(s) one at a time and lets the
    user keep it, move it to another filter (typeahead), send it to spam, or
    remove it entirely. There is no "allow" action: the entries being audited
    are already bucketed into a label.

    ``label`` selects which filters to review:
      - omitted        every filter with entries, starting from the first
      - ``newsletters/*``  every filter name matching the glob
      - ``newsletters``    just that one filter
    """
    pattern = getattr(args, "label", None)

    # Same drift check as `label`: unpushed changes mean these entries look
    # categorised locally while Gmail is still unaware of them.
    _check_pending_changes(manager)

    review, err = _resolve_audit_filters(manager.config, pattern)
    if err:
        print(f"Error: {err}", file=sys.stderr)
        return 1
    if not review:
        print("No filters with entries to review.")
        return 0

    # Snapshot the entries to review as a flat ordered list so a removed/moved
    # entry doesn't shift iteration indices mid-loop, and undo can rewind to
    # a stable position.
    items: list[tuple[str, str]] = []
    for fn in review:
        for entry in list(manager.config.filters[fn].entries):
            items.append((fn, entry))

    if not items:
        print("No entries to review.")
        return 0

    total = len(items)
    scope = review[0] if len(review) == 1 else f"{len(review)} filter(s)"
    print(f"Auditing {total} entr{'y' if total == 1 else 'ies'} across {scope}.")
    state = TriageState(config=manager.config)
    controller = TriageController(manager, state)
    session = TriageSession(
        manager,
        state,
        controller,
        kind="audit",
        items=[Item(email=entry, filter_name=fn) for fn, entry in items],
    )
    result = run_triage(session)
    return _finish_triage(manager, session, result, audit=True)


def _is_sender_filtered(email: str, config) -> bool:
    """Check if a sender email is already covered by an existing filter entry or allowed list.

    Matches against:
      - Exact email address
      - *@domain.com wildcards
      - *domain.com bare domain wildcards
      - The config's allowed list (emails previously marked as "allow")
    """
    email_lower = email.lower()

    # Check allowed list
    if hasattr(config, "allowed") and config.allowed:
        for allowed_email in config.allowed:
            if allowed_email.lower() == email_lower:
                return True

    domain = email_lower.rsplit("@", 1)[-1] if "@" in email_lower else ""

    for filter_config in config.filters.values():
        for entry in filter_config.entries:
            entry = entry.strip()
            if not entry:
                continue
            entry_lower = entry.lower()

            if entry_lower == email_lower:
                return True

            if entry_lower.startswith("*@"):
                pattern_domain = entry_lower[2:]
                if domain == pattern_domain:
                    return True

            if entry_lower.startswith("*") and not entry_lower.startswith("*@"):
                pattern_domain = entry_lower[1:]
                if domain == pattern_domain or domain.endswith("." + pattern_domain):
                    return True

    return False


def _add_to_spam(config, email: str):
    """Add an email to the spam filter, creating it if necessary."""
    from .config import FilterConfig

    if "spam" not in config.filters:
        config.filters["spam"] = FilterConfig(
            name="spam",
            action="delete",
            entries=[],
        )

    if email not in config.filters["spam"].entries:
        config.filters["spam"].entries.append(email)


def cmd_help(args):
    """Handle help command - show detailed help information."""
    topic = args.topic

    if topic is None or topic == "commands":
        print("""
Gmail Filter Bot - Command Reference
====================================

Available Commands:

  init              Import existing Gmail filters into local config
                    Usage: gmail-filter-bot init [--dry-run] [--rollup]

  plan              Preview what changes would be applied
                    Usage: gmail-filter-bot plan [--push] [--pull]

  apply             Sync changes between local config and Gmail
                    Usage: gmail-filter-bot apply [--push] [--pull]

  label             Interactively triage and label email senders
                    Usage: gmail-filter-bot label [folder] [--max-messages N]
                                      [--rollup]

  audit             Review and recategorize existing filter entries
                    Usage: gmail-filter-bot audit [label|glob]
                    Examples:
                      gmail-filter-bot audit              # all filters
                      gmail-filter-bot audit newsletters  # one filter
                      gmail-filter-bot audit 'newsletters/*'  # glob match

  doctor            Diagnose configuration, credentials, and Gmail connection
                    Usage: gmail-filter-bot doctor [--no-remote] [--quiet]
                    Usage: gmail-filter-bot doctor resolve [type]

  config            Get, set, or list user settings (hand-mode, filters-path)
                    Usage: gmail-filter-bot config ls
                    Usage: gmail-filter-bot config get <key>
                    Usage: gmail-filter-bot config set <key> <value>

  clean             Remove duplicates and optimize filter configuration
                    Usage: gmail-filter-bot clean [--dry-run]

  help              Show this help message
                    Usage: gmail-filter-bot help [topic]

Global Options:

  --config PATH     Path to filters.yaml (default: filters.yaml)
  --credentials PATH  Path to .env file with credentials (default: .env)

Run 'gmail-filter-bot help <topic>' for more details on:
  - commands       This help message
  - auth           Authentication and credentials setup
  - config         Configuration file format
  - troubleshooting Common issues and solutions
""")

    elif topic == "auth":
        print("""
Authentication Setup
====================

Gmail Filter Bot uses OAuth2 to access your Gmail account. You need to:

1. Create a Google Cloud Project:
   - Go to https://console.cloud.google.com/
   - Create a new project

2. Enable the Gmail API:
   - Navigate to "APIs & Services" > "Library"
   - Search for "Gmail API" and enable it

3. Create OAuth Credentials:
   - Go to "APIs & Services" > "Credentials"
   - Click "Create Credentials" > "OAuth client ID"
   - Choose "Desktop app" as application type
   - Download the client credentials

4. Create a .env file with your credentials:

   GMAIL_CLIENT_ID=your-client-id.apps.googleusercontent.com
   GMAIL_CLIENT_SECRET=your-client-secret
   GMAIL_REDIRECT_URI=http://localhost:8080

5. Run the init command:
   gmail-filter-bot init

   This will open a browser for you to authorize the app.
   The token will be saved to token.json for future use.

Token Expiration:
-----------------
If you see "Token has been expired or revoked", the app will
automatically re-authenticate you. Just run the command again.

To force re-authentication, delete token.json:
  rm token.json
""")

    elif topic == "config":
        print("""
Configuration File Format
=========================

The filters.yaml file defines your Gmail filters:

  max_entries_per_filter: 50  # Gmail limit per filter

  filters:
    newsletters:               # Filter name
      action: label_and_archive
      label: Newsletters
      entries:
        - newsletter@example.com
        - updates@company.com

    spam:                      # Filter name
      action: delete
      entries:
        - spam@bad-actor.com

Available Actions:

  label_only          - Apply label, keep in inbox
  label_and_archive   - Apply label and archive (remove from inbox)
  archive             - Archive only (no label)
  delete              - Send to trash
  star                - Star the message
  mark_important      - Mark as important
  mark_not_important  - Mark as not important

Entry Formats:

  - Simple email: user@example.com
  - Domain wildcard: *@example.com
  - Multiple domains: {@domain1.com, @domain2.com}

The app automatically splits filters that exceed the entry limit.

Rollup:
======

  Rollup applies all ancestor labels in addition to the filter's label.
  For example, a filter with label "a/b/c" and rollup enabled will apply
  three labels: "a", "a/b", and "a/b/c".

  Enable globally (applies to all filters):

    rollup: true

    filters:
      automated/general:
        action: label_only
        label: automated/general
        entries: [...]

  Or per-filter (overrides global setting):

    filters:
      automated/general:
        action: label_only
        label: automated/general
        rollup: true
        entries: [...]

      work:
        action: label_and_archive
        label: work
        rollup: false        # Explicitly disable, overrides global
        entries: [...]

  WARNING: Rollup increases the number of labels applied to each
  message. In IMAP clients (Apple Mail, Thunderbird, etc.), each
  label appears as a separate folder. Messages with nested labels
  will show up in multiple folders, which uses more space and may
  make navigation more cluttered. Use rollup selectively if you
  rely on IMAP access.
""")

    elif topic == "troubleshooting":
        print("""
Troubleshooting
===============

Error: "Token has been expired or revoked"
-------------------------------------------
Solution: The app will automatically re-authenticate you.
Just run the command again. If it persists, delete token.json:
  rm token.json

Error: "FileNotFoundError: filters.yaml"
---------------------------------------
Solution: Run init first to create the config file:
  gmail-filter-bot init

Error: "invalid_client" or authentication fails
----------------------------------------------
Solution: Check your .env file has correct credentials:
  - GMAIL_CLIENT_ID should end with .apps.googleusercontent.com
  - GMAIL_CLIENT_SECRET should be complete (not truncated)
  - Make sure the Gmail API is enabled in Google Cloud Console

Filter not working as expected
------------------------------
- Check the action type matches what you want
- Verify email addresses are correct
- Use --dry-run to preview changes before applying
- Check Gmail web interface to see current filters

Too many entries for one filter
-------------------------------
The app automatically splits filters exceeding max_entries_per_filter.
You can adjust this in filters.yaml, but Gmail has hard limits.

Still having issues?
--------------------
Check the GitHub repository or run with --help for more options.
""")

    else:
        print(f"Unknown topic: {topic}")
        print("Run 'gmail-filter-bot help' for available topics.")
        return 1

    return 0
