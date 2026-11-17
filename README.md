# Gmail Filter Bot

A Python CLI tool to sync and manage Gmail filters from a local YAML configuration. Automatically handles Gmail's filter entry limits by splitting large filters across multiple rules.

It exists to help manage the bullshit task that is keeping up with our emails.
Robots can't do it for us alone. So maybe let's make the process less robotic
... or is it more.

## Features

- **Simple 3-command workflow**: `init`, `apply`, `clean`
- **Smart sync**: Auto-detects which direction changes need to go (Gmail → local, local → Gmail, or both)
- **Interactive triage**: Triage senders one by one with `gfb label` — filter, allow, spam, unsubscribe, skip, expand, or archive — in a two-pane UI (see [The triage screen](#7-the-triage-screen)) with a live panel of what you've decided so far
- **Deferred, reversible side effects**: spam reports and unsubscribe requests are queued as you go and fired once, at the end, after you review them
- **Rollup labels**: Apply nested label hierarchies (e.g. `a/b/c` → `a` + `a/b` + `a/b/c`)
- **Part-level optimization**: Only updates changed parts of split filters (saves time on large filters)
- **Auto-split**: Automatically creates "filter-name", "filter-name-2", etc. when entry count exceeds limits
- **Dry-run support**: Preview all changes before applying

## Installation

### From PyPI (Recommended for Users)

```bash
pip install gmail-filter-bot
```

Then run:
```bash
gmail-filter-bot --help
```

### From Source (For Development)

Using [uv](https://github.com/astral-sh/uv) (recommended):

```bash
# Clone the repository
git clone https://github.com/btburton42/gmail-filter-bot.git
cd gmail-filter-bot

# Install uv if you haven't already
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install the package, its dependencies, and the dev group
uv sync

# Run it
uv run gfb --help

# Or activate the venv and use the entry points directly
source .venv/bin/activate
gfb --help
```

`uv sync` builds and installs the project itself, so `gfb` and
`gmail-filter-bot` are on the venv's `bin` from the start — no separate
`uv pip install -e .` step. Development dependencies live in the PEP 735
`[dependency-groups] dev` table, which `uv sync` installs by default; if you
only want the runtime ones, `uv sync --no-dev`.

If `gfb --help` reports `No module named 'textual'`, you are running an older
**global** install that is shadowing the project venv. Either use `uv run gfb`,
or activate `.venv` first, or refresh the global one with
`uv pip install -e . --python "$(which python)"`.


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

### 4. Rollup filters (stacked filters)

A filter can be marked `rollup: true` to indicate it intentionally *stacks* its
action on top of other filters (e.g. a broad catch-all that archives everything
from a domain, alongside a per-newsletter labeling filter):

```yaml
filters:
  newsletters:
    action: label_and_archive
    label: Newsletters
    entries:
      - someone@blog.substack.com
  substack-rollup:
    action: archive
    rollup: true
    entries:
      - '*substack.com'
```

Normally an entry appearing in multiple filters with **conflicting** actions
is a hard `doctor` failure (silent precedence conflict). When one of those
filters is a rollup filter, the overlap is treated as a consequence of rollup
and is *not* flagged. The same applies to same-action duplicates.

### 5. Hand mode (one-handed `label` triage)

The `label` command's triage keys can be kept under one hand:

```bash
gfb config set hand-mode left   # f filter / a allow / s spam / u unsubscribe / k skip / e expand / q quit / z undo (default)
gfb config set hand-mode right  # j filter / a allow / l spam / i unsubscribe / k skip / e expand / p quit / u undo
```

Only the key hints change — the action names stay the same. Inside the app the
filter picker is a fuzzy typeahead modal (type, arrows, enter; an unknown name
creates a filter).

`audit` uses the same keys with a different vocabulary: `allow` becomes
`remove`, `filter` becomes `move`, `skip` becomes `keep`. The former `c` and
`;` skip/keep bindings remain accepted for compatibility.

Reading the message is driven by the arrow keys (no action is taken, nothing is
recorded):

- `↑` / `↓` show less / more of the message body, a screenful at a time. The
  body is fetched once per message and cached.
- `←` / `→` pick which of the sender's messages you are looking at (the list
  marks it with `▸`). Actions then apply to *that* message — its
  `List-Unsubscribe` header, its archive — and `f`'s "archive this message now"
  archives exactly the message on screen.

`e` (expand) shows the body of the selected message and makes no changes.
Moving with `←` / `→` while expanded keeps the body open and loads the newly
selected message; moving back reuses that message's cached body.

### 6. Unpushed changes are surfaced before triage

Triage skips senders that already appear in your local `filters.yaml`. If you
declined `apply` at the end of an earlier session, those senders are in the
config but *not* in Gmail — so `label`/`audit` would stop offering them while
Gmail keeps delivering them unfiltered. Both commands therefore check for
unpushed changes up front:

```
! 2 filter(s) have changes not yet in Gmail:
    newsletters: +2 sender(s)
    shopping: +1 sender(s)

Apply these to Gmail before continuing? [y/N]
```

Answering `n` (or running non-interactively, e.g. in CI where stdin isn't a
TTY) just prints the reminder to run `gfb apply` and continues.

### 7. The triage screen

`gfb label` and `gfb audit` open a full-screen app (alternate screen; your
scrollback is untouched). Above 96 columns the layout is two panes and a status
line, roughly:

```
+---------------------------+------------+
| [3/12] Name <email>       | SESSION    |
|                           | FILTERS 2  |
|  messages / message body  |   work +1  |
|                           |     a@x.co |
|                           | 2 spam     |
+---------------------------+ 1 unsub    |
| f filter / a allow / ...  |            |
+---------------------------+------------+
| NORMAL  left  3/12  filtered 4  queued 3 |
+------------------------------------------+
```

Below 96 columns the right-hand panel is dropped and you get the plain
single-column version; nothing else changes.

The panel is live and it is *interactive*. `tab` switches panes (clicking a row
does the same thing), and in the panel:

| Key | Does |
|-----|------|
| `j`/`k` or arrows | move the cursor |
| `enter` / `e` | expand or collapse a filter branch |
| `x` | drop the row from this session |
| `m` | move the row to another filter |
| `s` | queue the row for spam |
| `a` | allow the row (leave its delivery alone) |
| `u` | unqueue a row that is waiting to be fired |

Rows are undoable with `z`, same as decisions made in the left pane. Until you
exit, *nothing has been sent*: the local config changes immediately (they are
visible and revertible), but the spam reports and unsubscribe requests are only
queued. When the walk ends — or when you press `q` — the app stops at a review
screen listing the queue, where `x` skips an item and `enter` approves the rest.
Skipping and cancelling both mean nothing is sent.

Once the app exits, the summary, the save, and the actual network calls all
happen in your normal terminal, in that order, printed as plain text:

```
==================================================
Triage Summary:
  Filtered:     4
  Allowed:      1
  Spammed:      2
  Unsubscribed: 1
  Skipped:      5
  Queued:       2 spam, 1 unsubscribe (not sent yet)

Sending 2 spam report(s) and 1 unsubscribe(s)...
  ✓ spam: 7 sender(s) reported
  ✓ list@example.com: unsubscribed via POST
```

That ordering is the point of deferring: you see exactly what was sent, and a
`Ctrl-C` before the review screen means nothing went out at all.

`gfb config set theme <name>` picks the colour scheme (any built-in Textual
theme: `textual-dark`, `gruvbox`, `nord`, `catppuccin-mocha`, `dracula`, …).
The zero state the panel shows is `0 queued. suspicious.` — a small joke that
also happens to be the truth.

## Usage

### Commands

| Command | Description | Common Flags |
|---------|-------------|--------------|
| `init` | Import existing Gmail filters from your account into `filters.yaml`. **Run this first** to automatically create your config from Gmail. | `--dry-run`: Preview what would be imported<br>`--rollup`: Enable global rollup in generated config |
| `plan` | Preview what changes would be applied. Shows exactly what would happen during apply without making any changes. | `--push`: Show push plan only<br>`--pull`: Show pull plan only |
| `apply` | **Main command** - syncs changes between local config and Gmail. Auto-detects direction and applies changes immediately. | `--push`: Force push local → Gmail only<br>`--pull`: Force pull Gmail → local only<br>`--no-apply-existing`: Skip labeling existing conversations |
| `clean` | Optimize your configuration by removing duplicates and consolidating similar filters. | `--dry-run`: Preview changes |
| `doctor` | Diagnose configuration, credentials, and (optionally) your Gmail connection. Each diagnosis is prefixed with its type. Exits non-zero on hard failures. | `--no-remote`: Skip Gmail checks<br>`--quiet`: Hide passing checks |
| `doctor resolve [type]` | Auto-fix a diagnosis type in `filters.yaml` (e.g. `wildcard_shadowing`, `within_filter_duplicates`, `cross_filter_duplicates`). Run with no type to list what's fixable. | — |
| `config` | Get, set, or list user settings (`hand-mode`, `filters-path`, `theme`). | `get`/`set`/`ls` |
| `forget` | Forget the remembered `filters.yaml` location. | — |

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

# 5. Interactively triage and label senders
gmail-filter-bot label
gmail-filter-bot label newsletters          # Triage a specific label
gmail-filter-bot label automated --rollup   # Triage with sub-labels rolled up
# 5. Diagnose problems (malformed entries, duplicates, drift)
gmail-filter-bot doctor
```

### Detailed Examples

**Initialize from Gmail:**
```bash
# Import your existing Gmail filters
gmail-filter-bot init

# Enable global rollup during import
gmail-filter-bot init --rollup

# Preview first (recommended)
gmail-filter-bot init --dry-run
```

**Plan changes (preview):**
```bash
# Preview what would happen without making changes
gmail-filter-bot plan

  # Preview specific direction
  gmail-filter-bot plan --push   # Show what would be pushed
  gmail-filter-bot plan --pull   # Show what would be pulled
  ```

**Apply changes:**
```bash
  # Auto-detect direction and sync (applies immediately)
  gmail-filter-bot apply

  # Force specific direction
  gmail-filter-bot apply --push   # Only push local changes
  gmail-filter-bot apply --pull   # Only pull Gmail changes

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

**Diagnose problems:**
```bash
# Run all health checks (local + Gmail)
gmail-filter-bot doctor

# Skip Gmail checks (offline / CI)
gmail-filter-bot doctor --no-remote

# Only show warnings and failures
gmail-filter-bot doctor --quiet
```

`doctor` checks credentials, the config file, malformed entries
(e.g. stray whitespace inside an address), duplicate entries (within and
across filters), wildcard shadowing, near-split filters, and — unless
`--no-remote` is set — your Gmail connection, missing referenced labels,
and orphaned remote filters. It exits non-zero on hard failures, so it's
safe to run in CI or before `apply`.

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
