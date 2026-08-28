# autocontinue

Watch [herdr](https://herdr.dev) agents for usage-limit walls, show how long
until the window reopens, and pick the agent back up when it does. For the
case: you queue up five agents, go to lunch, and come back to four of them
parked on *"5-hour limit reached ∙ resets 12pm"* since 11:04.

The reset time is right there in the message the agent printed when it blew up,
so the watcher reads it out of the pane, sits out the window, and prods the
agent when it is over.

> **Tested with Claude Code and the Codex CLI only.** Nothing here refuses
> another agent kind, and some of it reaches for one already: with no
> `AUTOCONTINUE_KINDS` set, every kind herdr detects is watched, and
> `AUTOCONTINUE_CLAUDE_KINDS` bills `omp` to the Claude account by default. None
> of that is exercised. The wall wording, the account windows and the resume
> prompt are all written against those two CLIs, so treat any other kind as
> untested rather than supported.

## What it does

- **Detect** — every claude/codex pane is read each poll. A pane showing a
  limit wall gets a countdown badge (`$wall`) whether or not you armed it.
  This half is pure observability and always on.
- **Resume** — panes you **armed** get `continue` submitted once the window
  reopens. Nothing is ever typed into a pane you did not arm.
- **Back off** — if the wall is still up (the parse was off, or it was the
  weekly limit and not the 5-hour one), it retries on a widening delay and
  gives up after five attempts instead of hammering the pane. A retry never
  lands before the reset the wall already knows about: the delay spaces the
  tries once the window has reopened, it does not replace the reopening.
- **Nudge** — the harness clears its own limit message when the window comes
  back, and an agent that stopped mid-task is still stopped. An armed pane is
  prompted once as its wall goes away, so a wall that ends quietly does not
  leave the agent sitting idle.
- **Follow the account** — a wall is stamped with the reopening it was told
  about when it was first seen, and that answer can turn out to be too late:
  rotation moves the pane onto another account, or the account revises its own
  window. The countdown is brought forward when that happens. Only forward — a
  later answer never makes an armed pane wait longer, and a pane already
  backing off keeps the retry it earned.

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

### Why a daemon, and what the event hook does instead

A wall appears when an agent **stops**, and herdr announces exactly that:

```toml
[[events]]
on = "pane.agent_status_changed"
command = ["python3", "autocontinue.py", "on-status"]
```

The hook does no detecting. It sends the daemon a signal that means "look now".
The daemon owns `walls.json` and `armed.json`, and a second process writing them
would race it for work the daemon is about to do anyway. So the hook stays tiny,
and the sweep behind it drops from every 10 seconds to every 60.

It also ignores an agent that just *started* working, since a pane that is
running cannot be sitting at a wall. On a busy session that is about half the
events. A burst is coalesced too: fifteen panes stopping together is one or two
ticks, not fifteen — `min_tick_s` sets that floor.

Spell the event with dots. herdr's API schema lists these kinds with underscores
(`pane_agent_status_changed`), and the manifest turns that form down with
`unknown event`. Verified on herdr 0.8.2, hence `min_herdr_version`.

**The daemon stays, and not only as a fallback.** herdr has no hook for the
clock, and waking up hours later when a weekly limit resets is half of what this
plugin does. No event announces a time. The daemon is also what re-asserts the
badges, which carry a TTL so a dead daemon's badges expire rather than lying to
you. It is started by the `[[startup]]` hook and by the `arm` action,
single-instance via a pidfile, and talks only to the local herdr socket. It exits
on its own if the herdr server goes away.

Without the hook the plugin still works, just up to a sweep late. The startup
line in the log says which mode it is in: `woken by status events`, or
`sweep only`.

## Install

```sh
herdr plugin install rcosteira79/herdr-autocontinue
```

Or link a local checkout: `herdr plugin link /path/to/herdr-autocontinue`.
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
| `AUTOCONTINUE_POLL_S` | `60` | safety-sweep interval (s); the status hook covers the responsive path |
| `AUTOCONTINUE_MIN_TICK_S` | `2` | shortest gap between ticks, so an event burst is one tick |
| `AUTOCONTINUE_GRACE_S` | `60` | extra wait after the parsed reset time |
| `AUTOCONTINUE_RESTAMP_MIN_GAIN_S` | `60` | how much sooner an account must reopen before a wall follows it |
| `AUTOCONTINUE_MAX_ATTEMPTS` | `5` | attempts before giving up on a wall |
| `AUTOCONTINUE_STOP_WAIT_S` | `5` | how long `stop` waits for the daemon to exit |
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
| `AUTOCONTINUE_ROTATE_STALE_S` | `1800` | past this age, an account's reading counts as no reading |
| `AUTOCONTINUE_ROTATE_GAIN_S` | `300` | how much sooner another account must reopen to be worth a switch |
| `AUTOCONTINUE_ROTATE_REFUSED_S` | `21600` | backstop before a refused profile is tried again |
| `AUTOCONTINUE_NUDGE_GAP_S` | `120` | quiet period after a prompt before a nudge may follow |
| `AUTOCONTINUE_ROTATE_REFRESH_GAP_S` | `300` | least time between fresh reads of the accounts |
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

If [account-switch](https://github.com/rcosteira79/herdr-account-switch) is installed, a spent account can hand
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

It ranks the accounts it may switch to. `account-switch` publishes what each
saved account has left, parked ones included, so rotation takes an account with
room first, then one nobody has read in the last half hour, then the accounts
known to be spent, soonest to reopen first. An unread account goes ahead of a
spent one on purpose: a parked account is usually parked because it was left
alone, so its window has most likely reopened, and one switch is what finds out.

Every named account is ranked, **including the one it is already on**. That
matters most when the account it moved to turns out to be the worse of the two:
your personal session may reopen in an hour while the account you switched to
has a spent weekly and twenty hours to run. Rotation moves back. It only moves
for an account that is better by `AUTOCONTINUE_ROTATE_GAIN_S`, which is what
stops it flapping between two that reopen at much the same time.

Nothing reads those accounts on a timer, so by the time a wall appears the
numbers are usually hours old, and ranking them would just re-pick the saved
order. Rotation therefore takes one fresh reading before it chooses — but only
where the answer can change. With a single candidate there is nothing to rank,
and a reading is taken at most once every `AUTOCONTINUE_ROTATE_REFRESH_GAP_S`
seconds: a walled pane asks on every sweep, and answering each one would earn
the rate limit that makes every later reading useless. The read covers the
walled kind alone, so a stuck claude pane never costs a request against your
ChatGPT accounts.

A reading can still fail, and an `account-switch` too old to publish one sends
the names alone. Rotation therefore switches, prompts, and watches, as it always
did: if that account is spent as well, the wall returns and the next profile on
the list is tried. One pass over the list per dry spell, then it waits for the
soonest reset. The list resets once the account has capacity again.

It does know whether the login still **works**. `account-switch` renews a parked
profile and asks the provider before installing it, and refuses a switch the
provider turns down. So rotation cannot land you on a retired login and sign you
out.

A refused profile is then left alone until that login is **saved afresh**. The
refusal is remembered against the profile's `saved_at`, so logging the account
in again and saving it clears it on the next sweep, with nothing to wait out.
Nothing else expires it, except `AUTOCONTINUE_ROTATE_REFUSED_S` as a backstop
for a refusal that was never about the credential — a network blip should not
strand an account until someone thinks to re-save it. A refused switch also
records the attempt, so the cooldown applies: it used to write nothing down and
ask again every sweep, refusing the same dead login five times in two minutes.

A profile `account-switch` reports a `problem` for — "needs re-login" is the one
that matters — is skipped outright. It cannot be read, so it cannot be switched
to either.

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

## The other herdr plugins

Each installs on its own; they share nothing but an author.

- [**herdr-idle-shell-badge**](https://github.com/rcosteira79/herdr-idle-shell-badge) — Badges idle agents that still have a background shell running, so one that *looks* done but left a process alive isn't mistaken for finished.
- [**herdr-readpending**](https://github.com/rcosteira79/herdr-readpending) — Mark agents you started reading but haven't finished — a numbered badge plus a reorderable overlay queue that clears when you focus the agent.
- [**herdr-account-switch**](https://github.com/rcosteira79/herdr-account-switch) — Hot-swap Claude Code / Codex logins without re-authenticating, with what is left on each account in the picker.
