"""Configuration management for Gmail Filter Bot."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class FilterConfig:
    """Configuration for a single filter.

    Attributes:
        rollup: When True, this filter is a "rollup" filter that intentionally
            stacks its action on top of other filters. The doctor command
            treats entries shared between a rollup filter and other filters as
            intentional and does *not* flag them as conflicting-action duplicates.
    """

    name: str
    action: str
    label: str | None = None
    entries: list[str] = None
    rollup: bool | None = None

    def __post_init__(self):
        if self.entries is None:
            self.entries = []
        else:
            # Filter out None/empty entries
            self.entries = [
                str(entry).strip()
                for entry in self.entries
                if entry is not None and str(entry).strip()
            ]


@dataclass
class Credentials:
    """Google API credentials."""

    client_id: str
    client_secret: str
    redirect_uri: str = "http://localhost:8080"

    @classmethod
    def from_env_file(cls, path: Path) -> Credentials:
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
    def from_json_file(cls, path: Path) -> Credentials:
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
    """Main configuration.

    Attributes:
        rollup: When True (set at the YAML root), *every* filter is treated as a
            rollup filter. Cross-filter overlaps are then considered intentional
            and the doctor does not flag them as conflicting-action duplicates.
            This is the global form of ``FilterConfig.rollup``.
    """

    max_entries_per_filter: int
    filters: dict[str, FilterConfig]
    credentials: Credentials
    allowed: list[str] = None
    rollup: bool = False

    def __post_init__(self):
        if self.allowed is None:
            self.allowed = []
        else:
            self.allowed = [
                str(entry).strip()
                for entry in self.allowed
                if entry is not None and str(entry).strip()
            ]

    @classmethod
    def load(cls, config_path: Path, credentials_path: Path) -> Config:
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
                rollup=bool(filter_data.get("rollup", False)),
            )

        # Parse allowed list
        allowed = data.get("allowed", [])

        return cls(
            max_entries_per_filter=data.get("max_entries_per_filter", 50),
            filters=filters,
            credentials=credentials,
            allowed=allowed,
            rollup=bool(data.get("rollup", False)),
        )

    def save(self, path: Path):
        """Save configuration to file."""
        data: dict = {
            "max_entries_per_filter": self.max_entries_per_filter,
        }

        if self.rollup:
            data["rollup"] = True

        if self.allowed:
            data["allowed"] = sorted(self.allowed)

        data["filters"] = {}

        for name, filter_config in self.filters.items():
            filter_data = {
                "action": filter_config.action,
                "entries": filter_config.entries,
            }
            if filter_config.label:
                filter_data["label"] = filter_config.label
            if filter_config.rollup is not None:
                filter_data["rollup"] = filter_config.rollup
            # if filter_config.rollup:
            #     filter_data["rollup"] = True
            data["filters"][name] = filter_data

        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
