"""Gmail API client for filter operations."""

from pathlib import Path
from typing import Any

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
                creds.refresh(Request())
            else:
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
        # Build criteria
        criteria = {
            "from": " OR ".join(from_addresses),
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
        """Get label ID by name, creating if necessary."""
        # List existing labels
        results = self.service.users().labels().list(userId="me").execute()
        labels = results.get("labels", [])

        # Check if label exists
        for label in labels:
            if label["name"] == label_name:
                return label["id"]

        # Create new label
        label_body = {
            "name": label_name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        }

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
