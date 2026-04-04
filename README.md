# Gmail Filter Bot

A Python CLI tool to sync and manage Gmail filters from a local YAML configuration. Automatically handles Gmail's filter entry limits by splitting large filters across multiple rules.

## Features

- **Sync**: Pull current Gmail filters and merge with local configuration
- **Push**: Apply local filters to Gmail (auto-splits if entry limits exceeded)
- **Trim**: Remove duplicates and consolidate entries
- **Validate**: Check configuration before pushing
- **Auto-split**: Automatically creates "filter-name", "filter-name-2", etc. when entry count exceeds limits

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

### 2. Create filter configuration

Create `filters.yaml` (this file is gitignored and contains your private filter data):

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

| Command | Description | Flags |
|---------|-------------|-------|
| `init` | Import existing Gmail filters from your account into `filters.yaml`. Perfect if you already have filters set up in Gmail. | `--dry-run`: Preview without creating file<br>`--force`: Overwrite existing filters.yaml<br>`--format`: Consolidate similar filters after import |
| `sync` | Pull current Gmail filters and merge any changes into your local `filters.yaml`. | `--dry-run`: Show changes without applying |
| `push` | Apply your local `filters.yaml` configuration to Gmail. Auto-splits filters if entry limits are exceeded. **By default, labels are applied to existing conversations.** | `--force`: Skip confirmation prompt<br>`--no-apply-existing`: Don't apply labels to existing conversations |
| `list` | Display all configured filters with their actions, labels, and entry counts. | `--show-entries`: Show all email addresses for each filter |
| `trim` | Remove duplicate entries from all filters. | - |
| `format` | Consolidate filters with the same action and label into single filters. | `--dry-run`: Preview changes without modifying |
| `validate` | Check your `filters.yaml` configuration for errors before pushing. | - |

**Note:** If you have pre-existing filters in Gmail, run `gmail-filter-bot init` to import them before using other commands.

### Quick Examples

```bash
# Import existing Gmail filters (run this first if you have filters)
gmail-filter-bot init

# View your filters
gmail-filter-bot list
gmail-filter-bot list --show-entries  # Show all emails

# Sync any changes from Gmail
gmail-filter-bot sync

# Push your filters to Gmail
gmail-filter-bot push

# Validate before pushing
gmail-filter-bot validate

# Clean up duplicates
gmail-filter-bot trim

# Consolidate similar filters
gmail-filter-bot format
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
python -m gmail_filter_bot push
```
