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
  gmail-filter-bot plan              # Preview what would change
  gmail-filter-bot apply             # Sync changes (auto-detects direction)
  gmail-filter-bot label             # Interactively triage Inbox senders
  gmail-filter-bot label newsletters # Triage senders in the newsletters label
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
        "--sync",
        action="store_true",
        help="Show plan for sync Gmail → local only",
    )

    # apply command - the main workflow
    apply_parser = subparsers.add_parser(
        "apply",
        help="Apply configuration changes (sync, push, or both)",
        description="""Apply changes between local config and Gmail.

By default, this performs a two-way sync:
  - Pulls new/deleted entries from Gmail
  - Pushes local changes to Gmail

Use --push or --sync to force a specific direction.

WARNING: This command applies changes immediately without confirmation.
Use 'plan' first to preview what would change.""",
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

    # label command - interactive sender triage
    label_parser = subparsers.add_parser(
        "label",
        help="Interactively triage and label email senders",
        description="""Fetch unique senders from a Gmail folder and interactively
assign them to filters. Present each sender one by one:
  (f)ilter - assign to a filter (with autocomplete)
  (a)llow  - skip this sender
  (s)pam   - add to spam (delete) filter
  (q)uit   - exit and proceed to summary

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

        if args.command == "plan":
            return cmd_plan(manager, args)
        elif args.command == "apply":
            return cmd_apply(manager, args)
        elif args.command == "clean":
            return cmd_clean(manager, args)
        elif args.command == "label":
            return cmd_label(manager, args)
        elif args.command == "help":
            return cmd_help(args)

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


def cmd_plan(manager: FilterManager, args):
    """Handle plan command - preview changes without applying."""
    force_push = args.push
    force_sync = args.sync

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
            print(f"    ~ action changed")
        if change.label_changed:
            print(f"    ~ label changed")

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
            print(f"    ~ action changed")
        if change.label_changed:
            print(f"    ~ label changed")

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


def cmd_label(manager: FilterManager, args):
    """Handle label command - interactively triage and label email senders."""
    import questionary

    folder = args.folder
    max_messages = args.max_messages

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

    # Filter out senders already covered by existing filter entries
    new_senders = [s for s in senders if not _is_sender_filtered(s[1], manager.config)]
    skipped = len(senders) - len(new_senders)
    if skipped:
        print(f"Skipping {skipped} sender(s) already covered by existing filters.")

    if not new_senders:
        print("All senders are already categorized. Nothing to do.")
        return 0

    print(f"\n{len(new_senders)} sender(s) to triage.")
    print("Commands: (f)ilter, (a)llow, (s)pam, (q)uit\n")

    # Track changes
    changes: dict[str, list[str]] = {}  # filter_name -> list of added emails
    allowed_count = 0
    spammed_count = 0
    filtered_count = 0
    should_quit = False

    for i, (display_name, email) in enumerate(new_senders, 1):
        prompt_text = f"[{i}/{len(new_senders)}] {display_name} <{email}>\n  [f]ilter / [a]llow / [s]pam / [q]uit: "

        while True:
            try:
                choice = input(prompt_text).strip().lower()
            except (KeyboardInterrupt, EOFError):
                print("\n\nExiting triage early.")
                should_quit = True
                break

            if choice in ("f", "filter"):
                filter_name = _prompt_filter_selection(manager.config, questionary)
                if filter_name is None:
                    print("  Cancelled. Try again.")
                    continue
                if filter_name not in manager.config.filters:
                    from .config import FilterConfig

                    manager.config.filters[filter_name] = FilterConfig(
                        name=filter_name,
                        action="label_and_archive",
                        label=filter_name,
                        entries=[],
                    )
                if email not in manager.config.filters[filter_name].entries:
                    manager.config.filters[filter_name].entries.append(email)
                    changes.setdefault(filter_name, []).append(email)
                filtered_count += 1
                print(f"  -> Added to '{filter_name}'")
                break

            elif choice in ("a", "allow"):
                if not hasattr(manager.config, "allowed") or manager.config.allowed is None:
                    manager.config.allowed = []
                if email not in manager.config.allowed:
                    manager.config.allowed.append(email)
                allowed_count += 1
                print("  -> Allowed (skipped)")
                break

            elif choice in ("s", "spam"):
                _add_to_spam(manager.config, email)
                changes.setdefault("spam", []).append(email)
                spammed_count += 1
                print("  -> Marked as spam (delete)")
                print("  Reporting existing messages as spam...", end=" ", flush=True)
                try:
                    reported = manager.client.report_spam([email])
                    if reported:
                        print(f"reported {reported} message(s).")
                    else:
                        print("no existing messages found.")
                except Exception as e:
                    print(f"error: {e}")
                break

            elif choice in ("q", "quit"):
                print("\nExiting triage early.")
                should_quit = True
                break

            else:
                print("  Invalid choice. Use f, a, s, or q.")

        if should_quit:
            break

    # Summary
    print(f"\n{'=' * 50}")
    print("Triage Summary:")
    print(f"  Filtered: {filtered_count} sender(s)")
    print(f"  Allowed:  {allowed_count} sender(s)")
    print(f"  Spammed:  {spammed_count} sender(s)")

    if not changes and allowed_count == 0:
        print("\nNo changes made.")
        return 0

    if changes:
        print("\nFilters updated:")
        for filter_name, emails in sorted(changes.items()):
            print(f"  {filter_name}: +{len(emails)} sender(s)")
            for email in emails:
                print(f"    - {email}")

    # Save updated config (includes allowed list + any filter changes)
    manager.config.save(manager.config_path)
    print(f"\nSaved to {manager.config_path}")

    # Plan preview
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

    print("\nApplying to Gmail...")
    try:
        push_results = manager.push(apply_to_existing=True, verbose=False, dry_run=False)
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


def _prompt_filter_selection(config, questionary_module):
    """Prompt user to select a filter.

    Shows top 5 most popular filters (by entry count) as a selectable list,
    plus an "Other (type name)" option. If "Other" is chosen, falls back to
    autocomplete from all existing filter names.
    """
    filter_names = list(config.filters.keys())

    if not filter_names:
        return None

    # Top 5 most popular by entry count
    by_count = sorted(
        filter_names,
        key=lambda n: len(config.filters[n].entries),
        reverse=True,
    )
    top_5 = by_count[:5]

    # Build selectable choices with entry counts
    choices = []
    for name in top_5:
        choices.append(
            questionary_module.Choice(
                title=f"{name} ({len(config.filters[name].entries)} entries)",
                value=name,
            )
        )

    if len(filter_names) > 5:
        choices.append(
            questionary_module.Choice(
                title="Other (type name)",
                value="__other__",
            )
        )

    try:
        choice = questionary_module.select(
            "  Select filter:",
            choices=choices,
        ).ask()

        if choice is None:
            return None

        if choice == "__other__":
            # Fall back to autocomplete for custom selection
            choice = questionary_module.autocomplete(
                "  Type filter name:",
                choices=filter_names,
            ).ask()

        return choice
    except (KeyboardInterrupt, EOFError):
        return None


def cmd_help(args):
    """Handle help command - show detailed help information."""
    topic = args.topic

    if topic is None or topic == "commands":
        print("""
Gmail Filter Bot - Command Reference
====================================

Available Commands:

  init              Import existing Gmail filters into local config
                    Usage: gmail-filter-bot init [--dry-run]

  plan              Preview what changes would be applied
                    Usage: gmail-filter-bot plan [--push] [--sync]

  apply             Sync changes between local config and Gmail
                    Usage: gmail-filter-bot apply [--push] [--sync]

  label             Interactively triage and label email senders
                    Usage: gmail-filter-bot label [folder] [--max-messages N]

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
