# ROADMAP

Things worth doing, roughly in the order they'd annoy us if they never happened.
Nothing here is promised; some of it is a joke that turned out to be a good idea.

## Anybody else able to run this against their own inbox

**Status:** blocked on a decision, not on work. Nothing below needs writing until
we know whether the next user is "one friend" or "a stranger on the internet".

### The problem

The OAuth client we ship is a Google Cloud project sitting in **Testing**
publishing status. That has two consequences that are easy to forget until the
tool stops working:

1. Only addresses explicitly added under *OAuth consent screen → Test users* can
   authorize. Anybody else gets `Error 403: access_denied` and a wall of text
   about the app not being verified.
2. Test-mode refresh tokens **expire after 7 days**. `token.json` still parses,
   `creds.expired` is true, `creds.refresh()` raises `RefreshError`, and
   `GmailClient._authenticate` falls through to `run_local_server` — a browser
   tab appears, uninvited, in the middle of a `gfb label` session. Every week.
   That is the actual day-to-day symptom, and it reads like a bug rather than a
   policy.

So the question: **once the app is moved to "In production", what does a second
person need in order to run `gfb` against their own Gmail?**

### Why it isn't a switch

Because of one line in `gmail_filter_bot/gmail_client.py`:

```python
SCOPES = [
    "https://www.googleapis.com/auth/gmail.settings.basic",  # sensitive
    "https://www.googleapis.com/auth/gmail.labels",          # sensitive
    "https://www.googleapis.com/auth/gmail.modify",          # restricted
]
```

`gmail.modify` is a **restricted** scope. Publishing a public app that holds one
requires OAuth verification *and* a CASA security assessment (third-party,
recurring, not free) — for a tool whose entire feature set is "edit your
trivially-unimportant Gmail filters". That is not a sane price. Hence the
options below rather than a plan.

### Options

| # | Route | What it costs | Who it works for |
|---|-------|---------------|------------------|
| A | **Verification + CASA** | Money, an annual third-party assessment, a privacy policy on a real domain, a demo video, a review queue measured in weeks | Anybody on any Gmail account |
| B | **Internal app** (Workspace org) | One dropdown: *User type → Internal*. No verification, no CASA, no 7-day expiry | Only accounts in that Workspace domain — but that may be exactly the set of people we care about |
| C | **Stay in Testing** | Free. The only tax is adding each new user by hand under *Test users* | Up to 100 named test users, each re-authorizing weekly |
| D | **Bring your own client** | Free. All the friction moves to the user: create a Cloud project, enable the Gmail API, configure a consent screen, download the JSON | Anybody willing to follow a five-minute README section |

B is the cheapest if "the second person" has a `@yourcompany.com` address.
D is the honest answer if the user is a stranger: everyone brings their own
`client_id`, so nobody is publishing an app or holding anybody's tokens.
C is fine for "me and two friends" and needs **no code**, only a better error
message.

A is almost certainly not worth doing, and the only way it becomes worth doing is
if this stops being a personal tool.

### What to check before deciding

- Do the people we have in mind have Google Workspace accounts on one domain, or
  plain `@gmail.com`? (Determines B vs D.)
- Google moves these goalposts — the 100-test-user cap, the unverified-app user
  cap, and what exactly CASA covers have all changed before. Re-read
  [Manage App Publishing Status](https://support.google.com/cloud/answer/15549257)
  and [Restricted scopes](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification)
  before committing to a number here.
- Is `gmail.modify` avoidable? It is what lets `apply` label *existing*
  conversations and what archiving needs. Dropping it would demote the app from
  *restricted* to merely *sensitive*, which is a much smaller verification
  problem — but it would cost the apply-to-existing feature and archive. Worth
  pricing properly before assuming it is off the table; it may be the single
  highest-leverage change on this list.

### Code-level work (option D mostly, with C's diagnostics)

- [ ] **Stop hard-coding port 8080.** `Credentials.redirect_uri` defaults to
  `http://localhost:8080` and `run_local_server(port=8080)` is literal. Two
  users on one machine collide, and so does any other dev server on 8080. Prefer
  `run_local_server(port=0)` (ephemeral) and reconcile it with
  `GOOGLE_REDIRECT_URI`, or honor the port from the redirect URI.
- [ ] **Say what actually happened when auth fails.** `RefreshError` currently
  prints "Token expired or revoked. Re-authenticating..." and opens a browser.
  Distinguish `invalid_grant` (testing-mode 7-day expiry, re-auth is the fix)
  from `access_denied` (you are not a test user — re-auth will *not* help, and
  looping the user through the consent screen forever is cruel).
- [ ] **`doctor` should catch both**, offline-friendly: token missing / expired /
  missing refresh_token, and a scopes check against `SCOPES` so an old token
  granted before `gmail.modify` was added gets flagged instead of failing later
  with a confusing 403 from a specific API call.
- [ ] **README's scope list is already stale** — it documents
  `gmail.settings.basic` and `gmail.labels` and says nothing about
  `gmail.modify`, which is the restricted one and the one users must agree to.
- [ ] **A "bring your own client" section** in the README (Cloud project → Gmail
  API → consent screen → desktop client → `.env`), with the exact fields
  `Credentials.from_env_file` reads. This is option D's entire user-facing cost.
- [ ] **Document what lives where**, because it is already per-user and nobody
  knows: `filters.yaml`, `.env`, and `token.json` all resolve to the same
  directory (`sibling_dir(config_path)`), so a second user with their own
  checkout is already isolated. `token.json` is a bearer credential for a Gmail
  account — say so, and say it is in `.gitignore` for a reason.
- [ ] Consider a `gfb auth` (or `doctor --reauth`) that guarantees *only* the
  browser dance and nothing else, for when the token dies mid-workflow.

### Definition of done

A second person, given nothing but the README and their own Gmail account, can
get to a working `gfb label` without asking us a question — by whichever route
we picked. And when their token expires, the tool tells them *why* instead of
silently opening a browser tab.

## Interactively explore and clean current filters

**Working command:** `gfb filters`, using the same full-screen Textual UI as
triage. `audit` is a sequential decision walk; this is a spatial browser for
answering "what is actually in this filter?" and pruning it quickly.

### Interaction contract

- Show filter names and their entry counts. `←` / `→` move to the previous /
  next filter, and `↑` / `↓` move through that filter's sender entries. Keep a
  cursor per filter so comparing neighboring filters does not reset position.
- Typing immediately fuzzy-filters the current filter's **sender email
  strings**; no separate search mode or `/` prefix is required. Reuse
  `textual.fuzzy.Matcher`, as the filter picker already does. Show the query
  and match count, with `backspace` editing and `esc` clearing it. Arrow keys
  remain browser navigation rather than becoming search-input cursor keys.
- `x` removes the selected entry from the active filter, advances to the next
  visible entry, and updates the displayed counts. Support `z` to undo a local
  removal. An empty filter or an empty search result has an explicit empty
  state, not a phantom selection.
- At exit, print what was removed and follow the normal local-config save →
  optional `gfb apply` flow. Browsing itself must not call Gmail; removals are
  local and reversible until saved/apply is accepted. This feature does not
  report spam or fire unsubscribe requests.

### Implementation notes / acceptance coverage

- Keep the filter/cursor/query model independent of Textual so matching,
  removal, undo, and cursor behavior can be tested cheaply. Reuse the existing
  config save/apply boundary rather than adding a second persistence path.
- Drive the real screen with `run_test()`/`Pilot`: horizontal filter changes,
  vertical entry movement, query narrowing and clearing, removal at the first
  and last entry, undo, empty filters, narrow terminals, and mouse scrolling.
- Prove that filter navigation preserves the active query and per-filter
  cursors, and that a filtered-out or removed row can never be deleted by a
  stale cursor.

## Visualize rarely-opened senders over time windows

The panel shows what you decided *this session*. The interesting question is what
you have been ignoring for *months*, and the answer is in a place we don't
currently look: Gmail knows when a message was read. Right now triage sorts
senders roughly by "who is loudest in the inbox", which is not the same as "who
never gets opened".

Sketch: record, per sender, the number of unread messages in the group and the
timestamp of the most recently opened one, then bucket senders into windows
(`last week`, `last month`, `last 6 months`, `never`, `all time`) and show the
buckets as a section in the panel or as an extra `gfb audit --stale` pass.
"Rarely opened" is a much better spam/unsubscribe signal than "lots of mail" —
and it is the one number that makes the deferred queue obvious: forty senders
you have not opened since 2023 is an argument, not a hunch.

Open questions: does Gmail expose per-sender read times cheaply enough
(`messages.get` metadata for every sender is a lot of quota), and do we cache the
result locally or recompute per run.

## Deferred queue persistence

The spam/unsubscribe queue lives in memory. A crash mid-session loses it — which
is safe (nothing was sent) but means the decisions were wasted. Writing the
queue to a scratch file would let the next run offer to resume it. Only worth
doing if the crash actually happens to us.

## `gfb doctor` for the UI

The triage app is the newest and most fragile surface. A `doctor` check that the
installed Textual version matches what `styles.tcss` expects, and that the
terminal is at least wide enough for the panel, would save a confusing first run.
