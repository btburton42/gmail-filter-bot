"""Configuration management for Gmail Filter Bot."""

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class FilterConfig:
    """Configuration for a single filter."""

    name: str
    action: str
    label: str | None = None
    entries: list[str] = None

    def __post_init__(self):
        if self.entries is None:
            self.entries = []


@dataclass
class Credentials:
    """Google API credentials."""

    client_id: str
    client_secret: str
    redirect_uri: str = "http://localhost:8080"

    @classmethod
    def from_env_file(cls, path: Path) -> "Credentials":
        """Load credentials from .env file."""
        if not path.exists():
            raise FileNotFoundError(f"Credentials file not found: {path}")

        env_vars = {}
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    env_vars[key.strip()] = value.strip().strip('"').strip("'")

        return cls(
            client_id=env_vars.get("GOOGLE_CLIENT_ID", ""),
            client_secret=env_vars.get("GOOGLE_CLIENT_SECRET", ""),
            redirect_uri=env_vars.get("GOOGLE_REDIRECT_URI", "http://localhost:8080"),
        )

    @classmethod
    def from_json_file(cls, path: Path) -> "Credentials":
        """Load credentials from JSON file."""
        import json

        if not path.exists():
            raise FileNotFoundError(f"Credentials file not found: {path}")

        with open(path) as f:
            data = json.load(f)

        # Handle different JSON formats
        if "installed" in data:
            client = data["installed"]
        elif "web" in data:
            client = data["web"]
        else:
            client = data

        return cls(
            client_id=client.get("client_id", ""),
            client_secret=client.get("client_secret", ""),
            redirect_uri=client.get("redirect_uris", ["http://localhost:8080"])[0],
        )


@dataclass
class Config:
    """Main configuration."""

    max_entries_per_filter: int
    filters: dict[str, FilterConfig]
    credentials: Credentials

    @classmethod
    def load(cls, config_path: Path, credentials_path: Path) -> "Config":
        """Load configuration from files."""
        # Load filter config
        if not config_path.exists():
            raise FileNotFoundError(
                f"Configuration file not found: {config_path}\n"
                "Create a filters.yaml file with your filter definitions."
            )

        with open(config_path) as f:
            data = yaml.safe_load(f)

        # Load credentials
        if credentials_path.suffix == ".json":
            credentials = Credentials.from_json_file(credentials_path)
        else:
            credentials = Credentials.from_env_file(credentials_path)

        # Parse filters
        filters = {}
        filters_data = data.get("filters", {})
        for name, filter_data in filters_data.items():
            filters[name] = FilterConfig(
                name=name,
                action=filter_data.get("action", "label_only"),
                label=filter_data.get("label"),
                entries=filter_data.get("entries", []),
            )

        return cls(
            max_entries_per_filter=data.get("max_entries_per_filter", 50),
            filters=filters,
            credentials=credentials,
        )

    def save(self, path: Path):
        """Save configuration to file."""
        data = {
            "max_entries_per_filter": self.max_entries_per_filter,
            "filters": {},
        }

        for name, filter_config in self.filters.items():
            filter_data = {
                "action": filter_config.action,
                "entries": filter_config.entries,
            }
            if filter_config.label:
                filter_data["label"] = filter_config.label
            data["filters"][name] = filter_data

        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
