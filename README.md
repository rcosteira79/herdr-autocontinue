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
  limit wall gets a countdown badge (`$limit`) whether or not you armed it.
  This half is pure observability and always on.
- **Resume** — panes you **armed** get `continue` submitted once the window
  reopens. Nothing is ever typed into a pane you did not arm.
- **Back off** — if the wall is still up (the parse was off, or it was the
  weekly limit and not the 5-hour one), it retries on a widening delay and
  gives up after five attempts instead of hammering the pane.

Badges: `⏸3h09` seen but not armed · `⏳3h09` armed, will continue · `⚠` gave up.

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

Two edits. `herdr server reload-config` after any change.

**1. Show the badge** — `$limit` is a pane token, and tokens only render if a
sidebar row references them:

```toml
[ui.sidebar.agents]
rows = [["state_icon", "workspace", "tab", "$limit"], ["agent"]]
```

**2. Keybindings**:

```toml
[[keys.command]]
key = "prefix+c"              # arm/disarm auto-continue on the focused agent
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

## Config (env)

| var | default | meaning |
|-----|---------|---------|
| `AUTOCONTINUE_PROMPT` | `continue` | what gets submitted when the window reopens |
| `AUTOCONTINUE_POLL_S` | `10` | poll interval (s) |
| `AUTOCONTINUE_GRACE_S` | `60` | extra wait after the parsed reset time |
| `AUTOCONTINUE_MAX_ATTEMPTS` | `5` | attempts before giving up on a wall |
| `AUTOCONTINUE_BLIND_RETRY_MIN` | `20` | retry cadence when the message names no time |
| `AUTOCONTINUE_TAIL_LINES` | `15` | how many tail lines of a pane count as "the wall" |
| `AUTOCONTINUE_READ_LINES` | `60` | rows read from each pane per poll |
| `AUTOCONTINUE_KINDS` | `claude,codex` | agent kinds to watch |
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
- Reset time: parsed out of the matched line and the three under it. Understood
  today: `resets 12pm`, `resets 3:30pm`, `resets 15:00`, `resets Feb 3 at 9am`,
  `will reset at 3pm (Europe/Lisbon)` (named zones resolve through `zoneinfo`),
  `try again in 4 days 2 hours 46 minutes`, ISO timestamps, and unix epochs. A
  wall whose message names no time (`Limits reset every 5h and every week`) is
  re-checked every `AUTOCONTINUE_BLIND_RETRY_MIN` instead.
- Resume: `herdr agent prompt <pane> "continue"`, which submits atomically with
  Enter. Retry delays are 5m, 15m, 45m, then hourly.
- Badges: `herdr pane report-metadata --token limit=…` with a TTL of four polls.

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
