# Gmail Filter Bot

A Python CLI tool to sync and manage Gmail filters from a local YAML configuration. Automatically handles Gmail's filter entry limits by splitting large filters across multiple rules.

## Features

- **Simple 3-command workflow**: `init`, `apply`, `clean`
- **Smart sync**: Auto-detects which direction changes need to go (Gmail → local, local → Gmail, or both)
- **Part-level optimization**: Only updates changed parts of split filters (saves time on large filters)
- **Auto-split**: Automatically creates "filter-name", "filter-name-2", etc. when entry count exceeds limits
- **Dry-run support**: Preview all changes before applying

## Installation

Using [uv](https://github.com/astral-sh/uv) (recommended):

```bash
# Install uv if you haven't already
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install the package
uv sync

# Or install with development dependencies
uv sync --extra dev

# Install the package with CLI entry points (required after uv sync)
uv pip install -e .

# Or activate the venv and run directly
source .venv/bin/activate
gmail-filter-bot --help
```

**Note:** `uv sync` installs dependencies but doesn't register the CLI entry points. You must run `uv pip install -e .` after `uv sync` to use the `gmail-filter-bot` command.


## Configuration

### 1. Set up credentials

Create a `.env` file (gitignored):

```env
GOOGLE_CLIENT_ID=your-client-id
GOOGLE_CLIENT_SECRET=your-client-secret
GOOGLE_REDIRECT_URI=http://localhost:8080
```

Or use a credentials file:

```bash
# Download from Google Cloud Console
cp client_credentials.json credentials.json
```

### 2. Create your filter configuration

**Option A: Import from Gmail (Recommended)**

If you already have filters in Gmail, run `init` to automatically create `filters.yaml` from your existing filters:

```bash
gmail-filter-bot init
```

This will:
- Fetch all your existing Gmail filters
- Group them by action and label
- Create `filters.yaml` with everything already configured

Preview first with `--dry-run`:
```bash
gmail-filter-bot init --dry-run
```

**Option B: Create manually**

Create `filters.yaml` yourself (this file is gitignored and contains your private filter data):

```yaml
# Maximum entries per filter (Gmail default is ~50)
max_entries_per_filter: 50

# Filter definitions
filters:
  newsletters:
    action: label_and_archive
    label: Newsletters
    entries:
      - newsletter1@example.com
      - newsletter2@example.com
      - updates@company.com

  shopping:
    action: label_only
    label: Shopping
    entries:
      - orders@amazon.com
      - receipts@target.com

  social:
    action: archive
    entries:
      - noreply@facebook.com
      - notifications@twitter.com
```

### 3. Actions

Available actions:
- `label_only`: Apply label only
- `label_and_archive`: Apply label and archive
- `archive`: Archive only
- `delete`: Delete messages
- `mark_important`: Mark as important
- `mark_not_important`: Mark as not important
- `star`: Star messages

## Usage

### Commands

| Command | Description | Common Flags |
|---------|-------------|--------------|
| `init` | Import existing Gmail filters from your account into `filters.yaml`. **Run this first** to automatically create your config from Gmail. | `--dry-run`: Preview what would be imported |
| `plan` | Preview what changes would be applied. Shows exactly what would happen during apply without making any changes. | `--push`: Show push plan only<br>`--sync`: Show sync plan only |
| `apply` | **Main command** - syncs changes between local config and Gmail. Auto-detects direction and applies changes immediately. | `--push`: Force push local → Gmail only<br>`--sync`: Force sync Gmail → local only<br>`--no-apply-existing`: Skip labeling existing conversations |
| `clean` | Optimize your configuration by removing duplicates and consolidating similar filters. | `--dry-run`: Preview changes |

### Quick Start

```bash
# 1. Import existing Gmail filters (one-time setup)
# This automatically creates filters.yaml from your Gmail filters
gmail-filter-bot init

# 2. Preview what would change
gmail-filter-bot plan

# 3. Apply changes (use this daily)
gmail-filter-bot apply

# 4. Optimize configuration (remove duplicates, consolidate)
gmail-filter-bot clean
```

### Detailed Examples

**Initialize from Gmail:**
```bash
# Import your existing Gmail filters
gmail-filter-bot init

# Preview first (recommended)
gmail-filter-bot init --dry-run
```

**Plan changes (preview):**
```bash
# Preview what would happen without making changes
gmail-filter-bot plan

# Preview specific direction
gmail-filter-bot plan --push   # Show what would be pushed
gmail-filter-bot plan --sync   # Show what would be synced
```

**Apply changes:**
```bash
# Auto-detect direction and sync (applies immediately)
gmail-filter-bot apply

# Force specific direction
gmail-filter-bot apply --push   # Only push local changes
gmail-filter-bot apply --sync   # Only pull Gmail changes

# Skip "apply to existing" step (don't label existing conversations)
gmail-filter-bot apply --no-apply-existing
```

**Clean up configuration:**
```bash
# Remove duplicates and consolidate
gmail-filter-bot clean

# Preview first
gmail-filter-bot clean --dry-run
```

### Development Commands

```bash
# Run tests
uv run pytest

# Run linting
uv run ruff check .
uv run ruff format .

# Run type checking
uv run mypy gmail_filter_bot/
```

## How Auto-Split Works

If a filter has more than 50 entries:
- Original filter: `newsletters` (entries 1-50)
- Auto-created: `newsletters-2` (entries 51-100)
- Auto-created: `newsletters-3` (entries 101-150)
- etc.

### Part-Level Optimization

When you update a split filter (e.g., add one email to `newsletters`):
- **Before**: Deletes and recreates ALL parts (e.g., 8 parts = 8 API calls)
- **After**: Only updates the changed parts (e.g., 1 part = 1 API call)

This saves significant time when applying labels to existing conversations for large filters.

## Google API Setup

### Creating a Google Cloud Project

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project or select an existing one
3. Enable the **Gmail API**:
   - Navigate to "APIs & Services" > "Library"
   - Search for "Gmail API"
   - Click "Enable"

### Setting up OAuth Desktop Client

1. Go to "APIs & Services" > "Credentials"
2. Click "Create Credentials" > "OAuth client ID"
3. Configure the OAuth consent screen:
   - Click "Configure Consent Screen"
   - Select "External" (for personal use) or "Internal" (if using Google Workspace)
   - Fill in required fields (app name, user support email, developer contact)
   - Save and continue
4. Create the OAuth client:
   - Application type: **Desktop app**
   - Name: "Gmail Filter Bot"
   - Click "Create"
5. Download the credentials:
   - Click "Download JSON"
   - Save as `credentials.json` or extract the values for your `.env` file

### Configuring Gmail Scopes

When you first run the tool, you'll be asked to authorize these scopes:

- `https://www.googleapis.com/auth/gmail.settings.basic` - Manage filters
- `https://www.googleapis.com/auth/gmail.labels` - Read label names

**For personal use (Testing mode):**
1. In "OAuth consent screen", scroll to "Test users"
2. Click "Add users" and add your Gmail address
3. This allows you to use the app without verification

**Note:** You'll see "Google hasn't verified this app" warning. This is normal for personal use. Click "Continue" to proceed.

### Adding Credentials

Create a `.env` file with your credentials:

```env
GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-secret
GOOGLE_REDIRECT_URI=http://localhost:8080
```

Or use the downloaded JSON file directly - the tool will prompt you to authenticate on first run.

## Git Workflow

```bash
# filters.yaml contains your private data and is gitignored
# Only commit the tool and documentation

# Add your filters
echo "filters.yaml" >> .gitignore  # Already done
vim filters.yaml

# Run the tool
gmail-filter-bot apply
```
