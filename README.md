# autocontinue

Watch [herdr](https://herdr.dev) agents for usage-limit walls, show how long
until the window reopens, and pick the agent back up when it does. For the
case: you queue up five agents, go to lunch, and come back to four of them
parked on *"5-hour limit reached ∙ resets 12pm"* since 11:04.

The reset time is right there in the message the agent printed when it blew up,
so the watcher reads it out of the pane, sits out the window, and prods the
agent when it is over.

## What it does

- **Detect** — every claude/codex pane is read each poll. A pane showing a
  limit wall gets a countdown badge (`$wall`) whether or not you armed it.
  This half is pure observability and always on.
- **Resume** — panes you **armed** get `continue` submitted once the window
  reopens. Nothing is ever typed into a pane you did not arm.
- **Back off** — if the wall is still up (the parse was off, or it was the
  weekly limit and not the 5-hour one), it retries on a widening delay and
  gives up after five attempts instead of hammering the pane.

Badges: `🔄` armed, standing by · `🔄3h09` armed, will continue · `⏸3h09` seen
but not armed · `⚠` gave up. An armed agent carries the glyph from the moment
you arm it, so arming is visible without waiting for a wall; the countdown is
what a wall adds. No badge means not armed and nothing seen.

### Arming is opt-in, per agent

A process that types into your terminals unprompted should be something you
turn on deliberately, one agent at a time. `arm` toggles the focused agent and
the arming sticks until you toggle it off or the pane closes. Before a resume
fires, the daemon re-checks that the agent is still idle (never mid-turn, never
while it is waiting on you for approval) and that the wall message is still on
screen — if you already continued it by hand, the wall is simply forgotten.

### Why a daemon

herdr plugins fire on user actions only — there is no manifest hook for "an
agent's status changed", and none for the clock. Detecting a wall and waking up
hours later are both reactive, so both live in a small companion process
(`autocontinue.py daemon`): a poll loop, started by the `[[startup]]` hook and
by the `arm` action, single-instance via a pidfile, talking only to the local
herdr socket. It exits on its own if the herdr server goes away. Badges carry a
TTL, so if the daemon dies they expire instead of lying to you.

## Install

```sh
herdr plugin install rcosteira79/herdr-plugins/autocontinue
```

Or link a local checkout: `herdr plugin link /path/to/herdr-plugins/autocontinue`.
Re-run `install`/`link` after a `herdr update` — updates drop plugins.

The watcher starts itself on the next herdr start; `herdr plugin action invoke
start --plugin rcosteira.autocontinue` starts it now.

### Config (`~/.config/herdr/config.toml`)

**1. Show the badge.** `$wall` is a pane token, and a token renders only if a
sidebar row names it. Installing a plugin does not edit your config, so until
some row names `$wall`, the badge is invisible and herdr reports no error. The
`enable-badge` action does that one edit for you:

```sh
herdr plugin action invoke enable-badge --plugin rcosteira.autocontinue
```

It backs `config.toml` up first, merges `$wall` into the rows you already have
(and into any `rows_by_agent` override for claude/codex, since an override
replaces `rows` rather than extending it), then reloads the server. Run it
twice and the second run does nothing. To do it by hand instead:

```toml
[ui.sidebar.agents]
rows = [["state_icon", "workspace", "tab", "$wall"], ["agent"]]
```

The watcher checks this at startup and posts one notification if the token is
unreferenced. It never edits the config on its own — that only happens when you
invoke `enable-badge`. It touches the sidebar row only — it does not arm, disarm
or resume anything. That is `arm` and the list, under `open-list`.

**2. Keybindings** — `herdr server reload-config` after editing these:

`prefix+c` is herdr's own `new_tab`, so bind arm to `prefix+ctrl+c` instead —
a custom binding silently shadows the default rather than reporting a clash.

```toml
[[keys.command]]
key = "prefix+ctrl+c"         # arm/disarm auto-continue on the focused agent
type = "plugin_action"
command = "rcosteira.autocontinue.arm"
description = "arm auto-continue"

[[keys.command]]
key = "prefix+shift+c"        # open the list of walls
type = "plugin_action"
command = "rcosteira.autocontinue.open-list"
description = "auto-continue list"
```

## Overlay list keys

```
j / k or ↓ / ↑   move selection
a                arm / disarm the selected agent
enter            resume that agent now (works unarmed — the keypress is consent)
x                forget the wall on the selected agent
q / esc          close
```

Every claude/codex agent is listed, not only the walled ones, because this is
also where you arm them.

## Config

Settings live in a file in the plugin's own config directory, next to
`patterns.json`:

```sh
$(herdr plugin config-dir rcosteira.autocontinue)/config.toml
```

```toml
# What gets typed into an armed agent once its window reopens.
prompt = "continue"

# How often every pane is read, in seconds.
poll_s = 10

# account-switch profiles rotation may move to. Empty means it never does.
rotate_profiles = []

# Which agent kinds bill to which account, so the right one is asked.
claude_kinds = ["claude", "omp"]
codex_kinds = ["codex"]
```

The key is the variable below without its `AUTOCONTINUE_` prefix, lowercased.
`config.json` works too. A file that fails to parse is ignored rather than
fatal, and the defaults stand. The daemon reads it at startup, so restart the
watcher after a change:

```sh
herdr plugin action invoke stop --plugin rcosteira.autocontinue
herdr plugin action invoke start --plugin rcosteira.autocontinue
```

An environment variable still wins over the file, but is rarely the practical
choice: plugin actions and the daemon inherit the **herdr server's**
environment, so setting one means exporting it before herdr starts.

| var | default | meaning |
|-----|---------|---------|
| `AUTOCONTINUE_PROMPT` | `continue` | what gets submitted when the window reopens |
| `AUTOCONTINUE_POLL_S` | `10` | poll interval (s) |
| `AUTOCONTINUE_GRACE_S` | `60` | extra wait after the parsed reset time |
| `AUTOCONTINUE_MAX_ATTEMPTS` | `5` | attempts before giving up on a wall |
| `AUTOCONTINUE_BLIND_RETRY_MIN` | `20` | retry cadence when the message names no time |
| `AUTOCONTINUE_TAIL_LINES` | `15` | how many tail lines of a pane count as "the wall" |
| `AUTOCONTINUE_READ_LINES` | `60` | rows read from each pane per poll |
| `AUTOCONTINUE_KINDS` | *(empty)* | kinds to watch; empty means every kind herdr detects |
| `AUTOCONTINUE_USE_ACCOUNT` | `1` | ask the account when its window reopens |
| `AUTOCONTINUE_CLAUDE_KINDS` | `claude,omp` | kinds billed to the Claude account |
| `AUTOCONTINUE_CODEX_KINDS` | `codex` | kinds billed to the ChatGPT account |
| `AUTOCONTINUE_ACCOUNT_PERCENT` | `100` | percent at which a window counts as spent |
| `AUTOCONTINUE_ACCOUNT_SEVERITIES` | *(empty)* | extra `severity` values that mean spent |
| `AUTOCONTINUE_USAGE_TTL_S` | `180` | how long an account answer is reused |
| `AUTOCONTINUE_USAGE_MIN_GAP_S` | `30` | minimum gap between account requests |
| `AUTOCONTINUE_ROTATE_PROFILES` | *(empty)* | profiles rotation may switch to; empty disables it |
| `AUTOCONTINUE_ROTATE_COOLDOWN_S` | `300` | minimum gap between account switches |
| `AUTOCONTINUE_GLYPH_ARMED` | `🔄` | badge for an armed agent |
| `AUTOCONTINUE_GLYPH_SEEN` | `⏸` | badge for a wall on an agent you did not arm |
| `AUTOCONTINUE_GLYPH_GAVEUP` | `⚠` | badge after the last attempt failed |
| `AUTOCONTINUE_DRY_RUN` | `0` | detect, badge and log, but never type |
| `HERDR_BIN_PATH` | `herdr` | herdr binary (set by herdr when it invokes an action) |

## How it works

- Walls: `HERDR_PLUGIN_STATE_DIR/walls.json`, armed panes: `armed.json`, both
  mutated under an `flock`. Log: `autocontinue.log` (rotated at 512 KB).
- Detection: `herdr pane read <pane> --source visible` each poll, matched
  against the rules in `patterns.default.json`. Only the **last 15 non-blank
  lines** are considered — a wall is the last thing an agent prints, whereas
  "usage limit reached" further up the scrollback is far more likely to be the
  agent *talking* about rate limits than hitting one. A wall also has to be
  seen on two consecutive polls before it is recorded.
- Account usage: a second, wording-free signal. See below.
- Reset time, in order of preference: the message, then the account, then a
  blind retry every `AUTOCONTINUE_BLIND_RETRY_MIN`. From a message these are
  understood: `resets 12pm`, `resets 3:30pm`, `resets 15:00`,
  `resets Feb 3 at 9am`, `will reset at 3pm (Europe/Lisbon)` (named zones
  resolve through `zoneinfo`), `try again in 4 days 2 hours 46 minutes`, ISO
  timestamps, and unix epochs.
- Resume: `herdr agent prompt <pane> "continue"`, which submits atomically with
  Enter. Retry delays are 5m, 15m, 45m, then hourly.
- Badges: `herdr pane report-metadata --token wall=…` with a TTL of four polls.
  The token is `wall`, not `limit`, because `senna-lang/herdr-agent-usage`
  already writes `$limit`; two plugins writing one token would overwrite each
  other.

## The account as a second signal

A usage limit belongs to the **account**, not to the pane. Every harness signed
into the same account shares one window. So the account can be asked when that
window reopens, instead of reading it off a screen. Both providers answer:

```
GET https://api.anthropic.com/api/oauth/usage        # claude, omp
Authorization: Bearer <token>      # the same credential store the CLI reads
anthropic-beta: oauth-2025-04-20

GET https://chatgpt.com/backend-api/wham/usage       # codex
Authorization: Bearer <token>      # from ~/.codex/auth.json
chatgpt-account-id: <account id>
```

Claude answers with a `limits` list — `kind`, `percent`, `severity`, `resets_at`:

```
session         percent=  0   severity=normal   resets 2026-08-22T23:09:59Z
weekly_all      percent= 61   severity=normal   resets 2026-08-27T13:59:59Z
```

Codex answers with `rate_limit`, and it is the better of the two: alongside
`used_percent` and `reset_at` it states `limit_reached` outright, so for codex
nothing has to be inferred from a percentage at all.

This matters because it is **wording-free**. A harness nobody has written a rule
for still gets detected and resumed, as long as it bills to one of these
accounts. That is what makes the plugin work beyond Claude Code.

It is used two ways:

- **Filling in a blank time.** A wall whose message names no time takes the
  account's reset instead of a blind retry.
- **Detecting a wall at all.** For a kind with an account behind it, a spent
  window is itself the wall, whatever the screen says.

Which account pays for which kind is a map, because they are asked separately —
an exhausted Claude account says nothing about a codex pane:

| var | default | meaning |
|-----|---------|---------|
| `AUTOCONTINUE_CLAUDE_KINDS` | `claude,omp` | kinds billed to the Claude account |
| `AUTOCONTINUE_CODEX_KINDS` | `codex` | kinds billed to the ChatGPT account |

Add your own harness to whichever line pays for it, and it inherits the whole
mechanism with no rule to write. A kind on neither line falls back to the text
rules alone.

A claude window counts as spent at `AUTOCONTINUE_ACCOUNT_PERCENT` (default
`100`). `severity` is read and logged but not trusted, because the only value
seen so far is `normal` — a guessed name could stop the fleet on a mere warning.
Once you have seen the real one, name it in `AUTOCONTINUE_ACCOUNT_SEVERITIES`.
Codex needs none of this: it reports `limit_reached` directly, and that boolean
wins over any percentage.

The endpoint is unofficial and can change or vanish. Everything here fails soft:
no token, no network, or an unrecognised shape all fall back to the text rules,
which keep working on their own. `AUTOCONTINUE_USE_ACCOUNT=0` turns it off. The
answer is cached for `AUTOCONTINUE_USAGE_TTL_S` (180s) and asked for at most
once per `AUTOCONTINUE_USAGE_MIN_GAP_S` (30s), shared across every pane, so a
ten-second loop over seventeen panes is still one request every three minutes.

## Rotating to another account

If [account-switch](../account-switch) is installed, a spent account can hand
over to another one you have saved, instead of everything waiting for the
window to reopen. **This is off until you name the profiles it may use:**

In `config.toml`, name the **account-switch profiles** it is allowed to move
to. These are the names you gave them when you saved them — the ones `s` in the
picker prompted for, and the ones `herdr plugin action invoke status --plugin
rcosteira.account-switch` lists:

```toml
# Profiles rotation may switch to, in the order it tries them. These are
# account-switch profile names, not agent kinds and not email addresses.
# Whatever is live at the time is skipped; so is anything not named here.
rotate_profiles = ["personal-max", "team-overflow"]
```

Only profiles on that list are ever switched to. There is no "any profile"
mode on purpose: a switch is machine-wide, so an unnamed work account could
otherwise start paying for a personal side-project without anyone deciding it.

Rotation fires only when **a pane you armed** is walled and the account it bills
to is spent. An unarmed pane never causes one, which keeps the rule that the
plugin acts only where you opted in.

It cannot check an account before switching to it. Only the live account keeps a
fresh token; a parked snapshot's token has usually expired, so there is nothing
to ask. Rotation therefore switches, prompts, and watches: if that account is
spent as well, the wall returns and the next profile on the list is tried. One
pass over the list per dry spell, then it waits for the soonest reset. The list
resets once the account has capacity again.

`AUTOCONTINUE_ROTATE_COOLDOWN_S` (default 300) is the minimum gap between
switches, so a bad detection cannot flip accounts in a loop.

### It keeps your logins current

Switching away from an account used to leave its saved profile holding whatever
tokens it had when you saved it. The CLI keeps renewing the live ones, so that
copy went stale, and restoring a stale copy can leave an account unable to renew
— which costs a browser login. Rotating would have made that routine, so
`account-switch` now refreshes a profile's snapshot as it parks it.

## Fragility

Detection keys off text the agents print, and that text changes. Two things
make that survivable:

**Check what detection sees**, without writing or sending anything:

```sh
herdr plugin action invoke scan --plugin rcosteira.autocontinue
```

It prints, per agent, whether a wall was found, which rule matched, the line it
matched, and the reset time it parsed.

**Fix the rules without touching code.** Copy `patterns.default.json` to
`$(herdr plugin config-dir rcosteira.autocontinue)/patterns.json` and edit it —
your copy replaces the built-in set wholesale. It has three lists: `limit` (what
a wall looks like), `exclude` (what only looks like one — *approaching* limit
warnings, overflow-to-credits notices, the agent quoting a limit error), and
`reset` (named-group regexes the time parser understands).

Run with `AUTOCONTINUE_DRY_RUN=1` if you want a few days of the daemon logging
what it *would* have submitted before you let it type.

## Requirements

- herdr ≥ 0.8.0 (for the `[[startup]]` autostart)
- Python 3 (stdlib only; uses `curses` for the overlay)
- macOS or Linux
