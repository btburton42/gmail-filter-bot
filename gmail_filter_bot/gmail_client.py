"""Gmail API client for filter operations."""

import re
import sys
from pathlib import Path
from typing import Any

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials as GoogleCredentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import Credentials

# Gmail API scopes needed for filter management
SCOPES = [
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.modify",
]


class GmailClient:
    """Client for Gmail API operations."""

    def __init__(self, credentials: Credentials, token_path: Path = Path("token.json")):
        """Initialize Gmail client."""
        self.credentials = credentials
        self.token_path = token_path
        self.service = None
        self._authenticate()

    def _authenticate(self):
        """Authenticate with Gmail API."""
        creds = None

        # Load existing token
        if self.token_path.exists():
            creds = GoogleCredentials.from_authorized_user_file(str(self.token_path), SCOPES)

        # If no valid credentials, get them
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except RefreshError:
                    # Token expired or revoked, need to re-authenticate
                    print("Token expired or revoked. Re-authenticating...", file=sys.stderr)
                    creds = None

            if not creds:
                # Create flow from client credentials
                client_config = {
                    "installed": {
                        "client_id": self.credentials.client_id,
                        "client_secret": self.credentials.client_secret,
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token",
                        "redirect_uris": [self.credentials.redirect_uri],
                    }
                }

                flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
                creds = flow.run_local_server(port=8080)

            # Save token for future runs
            with open(self.token_path, "w") as token:
                token.write(creds.to_json())

        self.service = build("gmail", "v1", credentials=creds)

    def list_filters(self) -> list[dict[str, Any]]:
        """List all filters in the Gmail account."""
        try:
            results = self.service.users().settings().filters().list(userId="me").execute()
            return results.get("filter", [])
        except HttpError as e:
            if e.resp.status == 404:
                return []
            raise

    def create_filter(
        self,
        from_addresses: list[str],
        action: str,
        label: str | None = None,
    ) -> dict[str, Any]:
        """Create a new filter."""
        # Filter out None values and ensure all entries are strings
        valid_addresses = [
            str(addr).strip() for addr in from_addresses if addr is not None and str(addr).strip()
        ]
        if not valid_addresses:
            raise ValueError("No valid from addresses provided")

        # Build criteria
        criteria = {
            "from": " OR ".join(valid_addresses),
        }

        # Build action
        action_body = {}

        if action == "delete":
            action_body["removeLabelIds"] = ["INBOX"]
            action_body["addLabelIds"] = ["TRASH"]
        elif action == "archive":
            action_body["removeLabelIds"] = ["INBOX"]
        elif action in ["label_only", "label_and_archive"]:
            if label:
                label_id = self._get_or_create_label(label)
                action_body["addLabelIds"] = [label_id]

            if action == "label_and_archive":
                action_body["removeLabelIds"] = ["INBOX"]
        elif action == "mark_important":
            action_body["addLabelIds"] = ["IMPORTANT"]
        elif action == "mark_not_important":
            action_body["removeLabelIds"] = ["IMPORTANT"]
        elif action == "star":
            action_body["addLabelIds"] = ["STARRED"]

        filter_body = {
            "criteria": criteria,
            "action": action_body,
        }

        result = (
            self.service.users()
            .settings()
            .filters()
            .create(
                userId="me",
                body=filter_body,
            )
            .execute()
        )

        return result

    def delete_filter(self, filter_id: str):
        """Delete a filter by ID."""
        self.service.users().settings().filters().delete(
            userId="me",
            id=filter_id,
        ).execute()

    def _get_or_create_label(self, label_name: str) -> str:
        """Get label ID by name, creating if necessary.

        Handles case-insensitive label name matching (Gmail labels are
        case-insensitive for creation but the list API preserves case).
        Falls back to re-fetching if a 409 conflict occurs (e.g. label
        was created by a concurrent call or existed with different casing).
        """
        label_name_lower = label_name.lower()

        # List existing labels
        results = self.service.users().labels().list(userId="me").execute()
        labels = results.get("labels", [])

        # Check if label exists (case-insensitive)
        for label in labels:
            if label["name"].lower() == label_name_lower:
                return label["id"]

        # Create new label
        label_body = {
            "name": label_name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        }

        try:
            result = (
                self.service.users()
                .labels()
                .create(
                    userId="me",
                    body=label_body,
                )
                .execute()
            )
            return result["id"]
        except HttpError as e:
            if e.resp.status == 409:
                # Label already exists (possibly different casing) — re-fetch
                results = self.service.users().labels().list(userId="me").execute()
                labels = results.get("labels", [])
                for label in labels:
                    if label["name"].lower() == label_name_lower:
                        return label["id"]
            raise

    def parse_filter_entries(self, filter_obj: dict[str, Any]) -> list[str]:
        """Parse entries from a Gmail filter object."""
        criteria = filter_obj.get("criteria", {})
        from_str = criteria.get("from", "")

        if not from_str:
            return []

        # Split by OR operator
        entries = [e.strip() for e in from_str.split(" OR ")]
        return entries

    def get_label_name(self, label_id: str) -> str | None:
        """Get label name from label ID, using cache.

        Returns None if label not found or is a system label.
        """
        system_labels = {
            "INBOX": "INBOX",
            "SPAM": "SPAM",
            "TRASH": "TRASH",
            "UNREAD": "UNREAD",
            "STARRED": "STARRED",
            "IMPORTANT": "IMPORTANT",
            "SENT": "SENT",
            "DRAFT": "DRAFT",
        }

        # Return system label names as-is
        if label_id in system_labels:
            return system_labels[label_id]

        # Fetch all labels and cache them
        if not hasattr(self, "_label_cache"):
            results = self.service.users().labels().list(userId="me").execute()
            self._label_cache = {label["id"]: label["name"] for label in results.get("labels", [])}

        return self._label_cache.get(label_id)

    def clear_label_cache(self):
        """Clear the label cache to force refresh."""
        if hasattr(self, "_label_cache"):
            delattr(self, "_label_cache")

    def apply_label_to_existing(
        self,
        from_addresses: list[str],
        label_id: str,
        archive: bool = False,
    ) -> int:
        """Apply label to existing messages matching the from addresses.

        This mimics the "Also apply filter to X matching conversations" option
        in the Gmail web UI when creating a filter.

        Args:
            from_addresses: List of email addresses to search for
            label_id: Label ID to apply
            archive: Whether to also archive (remove from inbox)

        Returns:
            Number of messages modified
        """
        from googleapiclient.errors import HttpError

        total_modified = 0

        # Filter out None/empty addresses
        valid_addresses = [
            str(addr).strip() for addr in from_addresses if addr is not None and str(addr).strip()
        ]

        # Search for messages matching each from address
        for from_addr in valid_addresses:
            query = f"from:{from_addr}"

            try:
                # Get messages matching the query
                results = (
                    self.service.users()
                    .messages()
                    .list(
                        userId="me",
                        q=query,
                    )
                    .execute()
                )

                messages = results.get("messages", [])

                if not messages:
                    continue

                # Batch modify messages in chunks of 1000
                message_ids = [msg["id"] for msg in messages]

                for i in range(0, len(message_ids), 1000):
                    chunk = message_ids[i : i + 1000]

                    body = {
                        "ids": chunk,
                        "addLabelIds": [label_id],
                    }

                    if archive:
                        body["removeLabelIds"] = ["INBOX"]

                    self.service.users().messages().batchModify(
                        userId="me",
                        body=body,
                    ).execute()

                    total_modified += len(chunk)

            except HttpError as e:
                print(f"Warning: Could not apply label to messages from {from_addr}: {e}")
                continue

        return total_modified

    def report_spam(self, from_addresses: list[str]) -> int:
        """Report existing messages from given addresses as spam.

        Moves matching messages to the Spam folder by adding the SPAM
        label and removing INBOX, which trains Gmail's spam filter.

        Args:
            from_addresses: List of email addresses to search for

        Returns:
            Number of messages reported as spam
        """
        total_reported = 0

        valid_addresses = [
            str(addr).strip() for addr in from_addresses if addr is not None and str(addr).strip()
        ]

        for from_addr in valid_addresses:
            query = f"from:{from_addr}"

            try:
                results = self.service.users().messages().list(userId="me", q=query).execute()
                messages = results.get("messages", [])

                if not messages:
                    continue

                message_ids = [msg["id"] for msg in messages]

                for i in range(0, len(message_ids), 1000):
                    chunk = message_ids[i : i + 1000]

                    body = {
                        "ids": chunk,
                        "addLabelIds": ["SPAM"],
                        "removeLabelIds": ["INBOX"],
                    }

                    self.service.users().messages().batchModify(
                        userId="me",
                        body=body,
                    ).execute()

                    total_reported += len(chunk)

            except HttpError as e:
                print(f"Warning: Could not report spam for {from_addr}: {e}")
                continue

        return total_reported

    def list_labels(self) -> dict[str, str]:
        """List all labels in the Gmail account.

        Returns:
            Dict mapping label name → label ID for all labels
            (system + custom).
        """
        results = self.service.users().labels().list(userId="me").execute()
        labels = results.get("labels", [])
        return {label["name"]: label["id"] for label in labels}

    def _resolve_label_id(self, label_name: str) -> str | None:
        """Resolve a label name to its ID.

        Handles system labels (INBOX, SPAM, TRASH, etc.) directly,
        and custom labels via the labels API.

        Returns None if the label doesn't exist.
        """
        system_labels = {
            "INBOX": "INBOX",
            "SPAM": "SPAM",
            "TRASH": "TRASH",
            "UNREAD": "UNREAD",
            "STARRED": "STARRED",
            "IMPORTANT": "IMPORTANT",
            "SENT": "SENT",
            "DRAFT": "DRAFT",
        }
        upper = label_name.upper()
        if upper in system_labels:
            return system_labels[upper]

        labels = self.list_labels()
        # Case-insensitive lookup
        for name, label_id in labels.items():
            if name.lower() == label_name.lower():
                return label_id
        return None

    @staticmethod
    def _extract_email(from_header: str) -> str:
        """Extract the bare email address from a From header.

        Handles formats like:
          "John Doe <john@example.com>"
          "john@example.com"
          "John Doe john@example.com (comment)"
        """
        match = re.search(r"<(.+?)>", from_header)
        if match:
            return match.group(1).strip()
        # No angle brackets, try to find an email directly
        match = re.search(r"[\w.+-]+@[\w.-]+\.\w+", from_header)
        if match:
            return match.group(0)
        return from_header.strip()

    def list_senders(self, label_id: str, max_results: int = 200) -> list[tuple[str, str, str]]:
        """Fetch unique senders from messages with the given label.

        Args:
            label_id: Gmail label ID to search within
            max_results: Maximum number of recent messages to fetch

        Returns:
            List of (display_name, email_address, subject) tuples, deduplicated
            by email address, preserving order of first appearance.
        """
        sender_map: dict[str, tuple[str, str]] = {}
        fetched = 0
        page_token = None

        while fetched < max_results:
            remaining = max_results - fetched
            list_params: dict[str, Any] = {
                "userId": "me",
                "maxResults": min(remaining, 500),
            }
            if label_id:
                list_params["labelIds"] = [label_id]
            if page_token:
                list_params["pageToken"] = page_token

            results = self.service.users().messages().list(**list_params).execute()
            messages = results.get("messages", [])

            if not messages:
                break

            # Batch-fetch metadata for these messages
            for msg in messages:
                if fetched >= max_results:
                    break
                try:
                    msg_data = (
                        self.service.users()
                        .messages()
                        .get(
                            userId="me",
                            id=msg["id"],
                            format="metadata",
                            metadataHeaders=["From", "Subject"],
                        )
                        .execute()
                    )
                    headers = msg_data.get("payload", {}).get("headers", [])
                    from_header = ""
                    subject = ""
                    for h in headers:
                        name_lower = h.get("name", "").lower()
                        if name_lower == "from":
                            from_header = h.get("value", "")
                        elif name_lower == "subject":
                            subject = h.get("value", "")

                    if from_header:
                        email_addr = self._extract_email(from_header)
                        if email_addr and email_addr not in sender_map:
                            display_name = from_header
                            if f"<{email_addr}>" in from_header:
                                display_name = from_header.split("<")[0].strip().strip('"')
                            if not display_name:
                                display_name = email_addr
                            sender_map[email_addr] = (display_name, subject)
                except HttpError:
                    continue
                fetched += 1

            page_token = results.get("nextPageToken")
            if not page_token:
                break

        return [(name, email, subject) for email, (name, subject) in sender_map.items()]
