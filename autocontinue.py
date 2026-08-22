#!/usr/bin/env python3
"""Auto-continue herdr agents that stalled on a usage-limit wall.

When Claude Code or Codex burns through a usage window it prints the wall and
stops — "5-hour limit reached ∙ resets 12pm", "You've hit your usage limit, try
again in 4 hours 12 minutes". The reset time is right there in the message, so
a poll loop can read it, sit out the window, and prod the agent when it opens.

Two halves:

  detect   every claude/codex pane is read each poll; a pane showing a wall
           gets a countdown badge ($limit) whether or not you armed it. Free
           observability — this is how you notice an agent died at 11:04.

  resume   only panes you *armed* are typed into. At the reset time the daemon
           re-reads the pane, and if the wall is still there and the agent is
           still sitting idle, submits AUTOCONTINUE_PROMPT ("continue"). It
           backs off and retries if the wall is still up, and gives up after
           AUTOCONTINUE_MAX_ATTEMPTS rather than hammering a pane forever.

Arming is per agent and sticky: `arm` toggles the focused pane, the badge shows
which state it is in, and nothing is ever typed into a pane you did not arm.

Subcommands:
  start / stop / status   the daemon (also autostarted by the [[startup]] hook)
  arm                     toggle auto-continue on the focused agent (action)
  scan                    one-shot detection report, writes nothing (debugging)
  open-list / ui          the overlay showing every wall and its countdown
  daemon                  the poll loop itself (spawned, detached)
"""
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta

PLUGIN_ID = os.environ.get("HERDR_PLUGIN_ID", "rcosteira.autocontinue")
HERDR = os.environ.get("HERDR_BIN_PATH", "herdr")
ROOT = os.path.dirname(os.path.abspath(__file__))

STATE_DIR = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.expanduser(
    "~/.local/state/herdr/autocontinue"
)
CONFIG_DIR = os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or STATE_DIR
WALLS = os.path.join(STATE_DIR, "walls.json")
ARMED = os.path.join(STATE_DIR, "armed.json")
PIDFILE = os.path.join(STATE_DIR, "daemon.pid")
LOCK = os.path.join(STATE_DIR, "state.lock")
LOGFILE = os.path.join(STATE_DIR, "autocontinue.log")
LOG_MAX_BYTES = 512 * 1024


def _num(name, default, cast=float):
    try:
        return cast(os.environ[name])
    except (KeyError, ValueError):
        return default


def _flag(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


POLL_S = _num("AUTOCONTINUE_POLL_S", 10.0)
TAIL_LINES = _num("AUTOCONTINUE_TAIL_LINES", 15, int)
READ_LINES = _num("AUTOCONTINUE_READ_LINES", 60, int)
PROMPT_TEXT = os.environ.get("AUTOCONTINUE_PROMPT", "continue")
GRACE_S = _num("AUTOCONTINUE_GRACE_S", 60.0)
BLIND_RETRY_S = _num("AUTOCONTINUE_BLIND_RETRY_MIN", 20.0) * 60
MAX_ATTEMPTS = _num("AUTOCONTINUE_MAX_ATTEMPTS", 5, int)
DRY_RUN = _flag("AUTOCONTINUE_DRY_RUN")
KINDS = [
    k.strip().lower()
    for k in os.environ.get("AUTOCONTINUE_KINDS", "claude,codex").split(",")
    if k.strip()
]

TOKEN = "limit"
GLYPH_ARMED = "\N{HOURGLASS WITH FLOWING SAND}"   # ⏳ will be continued
GLYPH_IDLE = "\N{DOUBLE VERTICAL BAR}"            # ⏸ seen, not armed
GLYPH_GAVEUP = "\N{WARNING SIGN}"                 # ⚠ gave up
TTL_MS = int(POLL_S * 4 * 1000)

MONTHS = {m: i for i, m in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split(), start=1)}


# ---- plumbing -------------------------------------------------------------

def herdr(*args):
    return subprocess.run(
        [HERDR, *args], capture_output=True, text=True, check=False
    )


def log(message):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        if os.path.exists(LOGFILE) and os.path.getsize(LOGFILE) > LOG_MAX_BYTES:
            os.replace(LOGFILE, LOGFILE + ".1")
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOGFILE, "a") as f:
            f.write(f"{stamp} {message}\n")
    except OSError:
        pass


class _Lock:
    def __enter__(self):
        os.makedirs(STATE_DIR, exist_ok=True)
        self._f = open(LOCK, "w")
        fcntl.flock(self._f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self._f, fcntl.LOCK_UN)
        self._f.close()


def _load(path, empty):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, type(empty)) else empty
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return empty


def _save(path, data):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load_walls():
    return _load(WALLS, {})


def save_walls(walls):
    _save(WALLS, walls)


def load_armed():
    return set(_load(ARMED, []))


def save_armed(armed):
    _save(ARMED, sorted(armed))


def live_agents():
    """pane_id -> agent info, or None when herdr is unreachable."""
    res = herdr("agent", "list")
    if res.returncode != 0:
        return None
    try:
        agents = json.loads(res.stdout)["result"]["agents"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    return {a["pane_id"]: a for a in agents if a.get("pane_id")}


def kind_of(info):
    label = " ".join(
        str(info.get(k) or "") for k in ("agent", "name", "display_agent")
    ).lower()
    for kind in KINDS:
        if kind in label:
            return kind
    return None


def pane_text(pane_id):
    """Rendered pane text, or None if the pane could not be read.

    None is not "" — a transient read failure must not read as "the wall is
    gone", which would forget a wall an armed agent is still stuck behind.
    `herdr pane read` prints text; a JSON envelope is tolerated in case the
    CLI ever wraps it.
    """
    res = herdr("pane", "read", pane_id, "--source", "visible",
                "--lines", str(READ_LINES))
    if res.returncode != 0:
        return None
    out = res.stdout
    try:
        data = json.loads(out)
    except (ValueError, TypeError):
        return out
    if isinstance(data, dict):
        body = data.get("result", data)
        if isinstance(body, dict):
            for key in ("text", "output", "content", "data"):
                if isinstance(body.get(key), str):
                    return body[key]
    return out


def label_of(info):
    for key in ("name", "display_agent", "terminal_title_stripped", "agent"):
        value = info.get(key)
        if value:
            return str(value).strip()
    return info.get("pane_id", "?")


# ---- detection ------------------------------------------------------------

_PATTERNS = None


def patterns():
    """Built-in rules, or the user's replacement copy if one exists."""
    global _PATTERNS
    if _PATTERNS is not None:
        return _PATTERNS
    override = os.path.join(CONFIG_DIR, "patterns.json")
    path = override if os.path.exists(override) else os.path.join(
        ROOT, "patterns.default.json")
    raw = _load(path, {})
    compiled = {"limit": [], "reset": [], "exclude": []}
    for rule in raw.get("limit", []):
        try:
            compiled["limit"].append(
                (rule.get("id", "?"), rule.get("kind"), re.compile(rule["regex"]))
            )
        except (re.error, KeyError, TypeError):
            log(f"bad limit pattern skipped: {rule!r}")
    for rule in raw.get("reset", []):
        try:
            compiled["reset"].append((rule.get("id", "?"), re.compile(rule["regex"])))
        except (re.error, KeyError, TypeError):
            log(f"bad reset pattern skipped: {rule!r}")
    for rule in raw.get("exclude", []):
        try:
            compiled["exclude"].append(re.compile(rule))
        except re.error:
            log(f"bad exclude pattern skipped: {rule!r}")
    _PATTERNS = compiled
    return compiled


def tail_lines(text, count=None):
    lines = [ln.strip() for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln]
    return lines[-(count or TAIL_LINES):]


def find_wall(text, kind):
    """(rule_id, matched_line, context) for the wall on screen, else None.

    Only the tail of the pane is considered: a wall is the last thing an agent
    prints, whereas the words 'usage limit reached' halfway up the scrollback
    are far more likely to be the agent talking about rate limits than hitting
    one.
    """
    pats = patterns()
    lines = tail_lines(text)
    for i, line in enumerate(lines):
        if any(x.search(line) for x in pats["exclude"]):
            continue
        for rule_id, rule_kind, rx in pats["limit"]:
            if rule_kind and kind and rule_kind != kind:
                continue
            if rx.search(line):
                return rule_id, line, " ".join(lines[i:i + 4])
    return None


# ---- reset-time parsing ---------------------------------------------------

def _tzinfo(name):
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name.strip())
    except Exception:
        return None  # abbreviations (PST) and missing tzdata: fall back to local


def _from_clock(groups):
    tz = _tzinfo(groups.get("tz"))
    now = datetime.now(tz) if tz else datetime.now().astimezone()
    hour = int(groups["hour"])
    minute = int(groups.get("minute") or 0)
    ampm = (groups.get("ampm") or "").replace(".", "").lower()
    if ampm:
        hour = hour % 12 + (12 if ampm.startswith("p") else 0)
    if hour > 23 or minute > 59:
        return None
    try:
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    except ValueError:
        return None
    mon, day = groups.get("mon"), groups.get("day")
    if mon and day:
        month = MONTHS.get(mon[:3].lower())
        try:
            target = target.replace(month=month, day=int(day))
        except (TypeError, ValueError):
            return None
        # A date already well behind us is next year's.
        if (target - now).total_seconds() < -180 * 86400:
            try:
                target = target.replace(year=target.year + 1)
            except ValueError:
                return None
    elif (target - now).total_seconds() < -120:
        target += timedelta(days=1)  # a bare clock time that already passed
    return target.timestamp()


def _from_relative(groups):
    days = int(groups.get("days") or 0)
    hours = int(groups.get("hours") or 0)
    minutes = int(groups.get("minutes") or 0)
    if not (days or hours or minutes):
        return None
    return time.time() + timedelta(
        days=days, hours=hours, minutes=minutes).total_seconds()


def _from_iso(text):
    try:
        parsed = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.timestamp()


def parse_reset(context):
    """(epoch, rule_id) for the reset time in `context`, else (None, None)."""
    for rule_id, rx in patterns()["reset"]:
        match = rx.search(context)
        if not match:
            continue
        groups = {k: v for k, v in match.groupdict().items() if v}
        ts = None
        if groups.get("epoch"):
            ts = float(groups["epoch"])
        elif groups.get("iso"):
            ts = _from_iso(groups["iso"])
        elif {"days", "hours", "minutes"} & set(groups):
            ts = _from_relative(groups)
        elif groups.get("hour"):
            ts = _from_clock(groups)
        if ts and ts > time.time() - 300:
            return ts, rule_id
    return None, None


# ---- badges ---------------------------------------------------------------

def _countdown(seconds):
    if seconds <= 0:
        return "now"
    minutes = int(seconds // 60)
    if minutes < 1:
        return "<1m"
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes:02d}"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours}h"


def set_badge(pane_id, wall, armed):
    if wall["status"] == "gaveup":
        text = GLYPH_GAVEUP
    else:
        glyph = GLYPH_ARMED if pane_id in armed else GLYPH_IDLE
        text = glyph + _countdown(wall["resume_at"] - time.time())
    herdr(
        "pane", "report-metadata", pane_id,
        "--source", PLUGIN_ID,
        "--token", f"{TOKEN}={text}",
        "--ttl-ms", str(TTL_MS),
    )


def clear_badge(pane_id):
    herdr(
        "pane", "report-metadata", pane_id,
        "--source", PLUGIN_ID,
        "--clear-token", TOKEN,
    )


# ---- wall bookkeeping -----------------------------------------------------

def _update_walls(mutate):
    with _Lock():
        walls = load_walls()
        result = mutate(walls)
        save_walls(walls)
        return result


def drop_wall(pane_id, why):
    def mutate(walls):
        return walls.pop(pane_id, None)

    if _update_walls(mutate) is not None:
        log(f"{pane_id}: wall cleared ({why})")
    clear_badge(pane_id)


def new_wall(pane_id, kind, info, hit):
    rule_id, matched, context = hit
    now = time.time()
    reset_at, via = parse_reset(context)
    if reset_at:
        resume_at, reason = reset_at + GRACE_S, via
    else:
        # No time in the message: come back periodically and look again.
        resume_at, reason = now + BLIND_RETRY_S, "blind"
    return {
        "pane_id": pane_id,
        "kind": kind,
        "label": label_of(info),
        "rule": rule_id,
        "matched": matched[:200],
        "detected_at": now,
        "reset_at": reset_at,
        "resume_at": resume_at,
        "reason": reason,
        "attempts": 0,
        "last_attempt": None,
        "status": "waiting",
    }


def _backoff(attempts):
    return min(300 * (3 ** max(0, attempts - 1)), 3600)


def attempt_resume(pane_id, wall, info):
    """Type into an armed pane whose window should have reopened. The caller
    has already confirmed, this tick, that the wall is still on screen."""
    now = time.time()
    if wall.get("status") != "waiting":
        return  # already gave up; only an explicit resume from the list revives it
    if info.get("agent_status") in ("working", "blocked"):
        # Busy or waiting on you — never interrupt; look again shortly.
        _update_walls(lambda w: w.get(pane_id, {}).update(resume_at=now + 60))
        return
    if DRY_RUN:
        log(f"{pane_id}: DRY RUN, would submit {PROMPT_TEXT!r}")
        _update_walls(lambda w: w.get(pane_id, {}).update(resume_at=now + 300))
        return

    res = herdr("agent", "prompt", pane_id, PROMPT_TEXT)
    attempts = wall["attempts"] + 1
    if res.returncode != 0:
        log(f"{pane_id}: prompt failed: {(res.stderr or '').strip()[:200]}")
    else:
        log(f"{pane_id}: submitted {PROMPT_TEXT!r} (attempt {attempts})")

    def mutate(walls):
        entry = walls.get(pane_id)
        if entry is None:
            return
        entry["attempts"] = attempts
        entry["last_attempt"] = now
        if attempts >= MAX_ATTEMPTS:
            entry["status"] = "gaveup"
            log(f"{pane_id}: giving up after {attempts} attempts")
        else:
            entry["resume_at"] = now + _backoff(attempts)

    _update_walls(mutate)


def tick(agents, pending):
    """One poll: badge every walled pane, resume the armed ones that are due."""
    walls = load_walls()
    armed = load_armed()
    now = time.time()

    gone = [p for p in walls if p not in agents]
    for pane_id in gone:
        _update_walls(lambda w, p=pane_id: w.pop(p, None))
    dead = armed - set(agents)
    if dead:
        with _Lock():
            armed = load_armed() - dead
            save_armed(armed)

    for pane_id, info in agents.items():
        kind = kind_of(info)
        if kind is None:
            continue
        wall = walls.get(pane_id)
        if info.get("agent_status") == "working":
            if wall:  # it is moving again; whatever we saw is history
                drop_wall(pane_id, "agent working")
            pending.discard(pane_id)
            continue

        text = pane_text(pane_id)
        if text is None:
            continue  # unreadable this tick; leave the wall as it stands
        hit = find_wall(text, kind)
        if hit is None:
            if wall:
                drop_wall(pane_id, "message gone")
            pending.discard(pane_id)
            continue

        if wall is None:
            # Two consecutive sightings before we believe it — one frame of a
            # half-drawn TUI is not a wall.
            if pane_id not in pending:
                pending.add(pane_id)
                continue
            pending.discard(pane_id)
            wall = new_wall(pane_id, kind, info, hit)
            _update_walls(lambda w, p=pane_id, v=wall: w.__setitem__(p, v))
            when = (
                datetime.fromtimestamp(wall["resume_at"]).strftime("%H:%M")
                if wall["reset_at"] else f"+{int(BLIND_RETRY_S / 60)}m (no time in message)"
            )
            log(f"{pane_id} [{wall['label']}]: {wall['rule']} — {wall['matched']!r} "
                f"-> resume {when}"
                f"{'' if pane_id in armed else ' (not armed, watching only)'}")

        set_badge(pane_id, wall, armed)
        if (pane_id in armed and wall["status"] == "waiting"
                and now >= wall["resume_at"]):
            attempt_resume(pane_id, wall, info)


# ---- daemon process -------------------------------------------------------

def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_pid():
    try:
        return int(open(PIDFILE).read().strip())
    except (FileNotFoundError, ValueError, OSError):
        return None


def ensure_daemon():
    """Start the poll loop unless one is already up. Returns True if spawned."""
    with _Lock():
        if _pid_alive(_read_pid()):
            return False
    script = os.path.abspath(__file__)
    subprocess.Popen(
        [sys.executable, script, "daemon"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        cwd=os.path.dirname(script),
        env=os.environ.copy(),
    )
    return True


def cmd_daemon(argv):
    with _Lock():
        existing = _read_pid()
        if _pid_alive(existing) and existing != os.getpid():
            return 0
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(PIDFILE, "w") as f:
            f.write(str(os.getpid()))

    log(f"daemon up (poll {POLL_S:g}s, prompt {PROMPT_TEXT!r}"
        f"{', DRY RUN' if DRY_RUN else ''})")
    pending = set()
    server_fails = 0
    try:
        while True:
            agents = live_agents()
            if agents is None:
                server_fails += 1
                if server_fails >= 10:
                    log("herdr unreachable, exiting")
                    break
            else:
                server_fails = 0
                try:
                    tick(agents, pending)
                except Exception as exc:  # one bad poll must not kill the loop
                    log(f"tick failed: {exc!r}")
            time.sleep(POLL_S)
    finally:
        with _Lock():
            if _read_pid() == os.getpid():
                try:
                    os.remove(PIDFILE)
                except OSError:
                    pass
    return 0


def cmd_start(argv):
    if ensure_daemon():
        print(f"autocontinue: watching (log: {LOGFILE})")
    else:
        print(f"autocontinue: already running (pid {_read_pid()})")
    return 0


def cmd_stop(argv):
    pid = _read_pid()
    if not _pid_alive(pid):
        print("autocontinue: not running")
    else:
        try:
            os.kill(pid, 15)
            print(f"autocontinue: stopped (pid {pid})")
        except OSError as exc:
            print(f"autocontinue: could not stop pid {pid}: {exc}", file=sys.stderr)
            return 1
    for pane_id in load_walls():
        clear_badge(pane_id)
    return 0


# ---- arming ---------------------------------------------------------------

def _resolve_target():
    for env in ("HERDR_ACTIVE_PANE_ID", "HERDR_PANE_ID"):
        if os.environ.get(env):
            return os.environ[env]
    raw = os.environ.get("HERDR_PLUGIN_CONTEXT_JSON")
    if raw:
        try:
            ctx = json.loads(raw)
        except json.JSONDecodeError:
            ctx = {}
        target = ctx.get("focused_pane_id") or ctx.get("pane_id")
        if target:
            return target
    for pane_id, info in (live_agents() or {}).items():
        if info.get("focused"):
            return pane_id
    return None


def toggle_armed(pane_id):
    with _Lock():
        armed = load_armed()
        if pane_id in armed:
            armed.discard(pane_id)
            state = False
        else:
            armed.add(pane_id)
            state = True
        save_armed(armed)
    wall = load_walls().get(pane_id)
    if wall:
        set_badge(pane_id, wall, load_armed())
    return state


def cmd_arm(argv):
    target = argv[0] if argv else _resolve_target()
    if not target:
        print("autocontinue: no focused agent pane to arm", file=sys.stderr)
        return 1
    state = toggle_armed(target)
    ensure_daemon()
    word = "armed" if state else "disarmed"
    print(f"autocontinue: {target} {word}")
    herdr("notification", "show", f"Auto-continue {word}",
          "--body", target, "--sound", "none")
    return 0


def cmd_status(argv):
    pid = _read_pid()
    print(f"daemon    {'running (pid %d)' % pid if _pid_alive(pid) else 'stopped'}")
    print(f"prompt    {PROMPT_TEXT!r}{'  (DRY RUN)' if DRY_RUN else ''}")
    agents = live_agents() or {}
    armed = load_armed()
    walls = load_walls()
    print(f"armed     {len(armed)} pane(s)")
    for pane_id in sorted(armed):
        info = agents.get(pane_id)
        print(f"  {pane_id:<10} {label_of(info) if info else '(pane gone)'}")
    if not walls:
        print("walls     none")
        return 0
    print("walls")
    for pane_id, wall in sorted(walls.items()):
        mark = "armed" if pane_id in armed else "watching"
        left = _countdown(wall["resume_at"] - time.time())
        print(f"  {pane_id:<10} {wall['label']:<18} {mark:<9} {wall['status']:<8} "
              f"in {left:<7} {wall['matched'][:60]}")
    return 0


def cmd_scan(argv):
    """Read every agent pane and report what detection makes of it. Writes
    nothing — this is how you check a pattern against a live wall."""
    agents = live_agents()
    if agents is None:
        print("herdr not reachable (server down?)", file=sys.stderr)
        return 1
    armed = load_armed()
    for pane_id, info in sorted(agents.items()):
        kind = kind_of(info)
        if kind is None:
            continue
        flag = "ARMED " if pane_id in armed else "watch "
        text = pane_text(pane_id)
        if text is None:
            print(f"?      {flag}{pane_id:<10} {label_of(info):<18} "
                  f"{info.get('agent_status','?'):<8} could not read pane")
            continue
        hit = find_wall(text, kind)
        if not hit:
            print(f"-      {flag}{pane_id:<10} {label_of(info):<18} "
                  f"{info.get('agent_status','?'):<8} no wall")
            continue
        reset_at, via = parse_reset(hit[2])
        when = (datetime.fromtimestamp(reset_at + GRACE_S).strftime("%a %H:%M")
                if reset_at else f"+{int(BLIND_RETRY_S / 60)}m (unparsed)")
        print(f"WALL   {flag}{pane_id:<10} {label_of(info):<18} "
              f"{info.get('agent_status','?'):<8} {hit[0]} -> resume {when} [{via}]")
        print(f"       {hit[1][:100]!r}")
    return 0


# ---- overlay list ---------------------------------------------------------

def cmd_open_list(argv):
    ensure_daemon()
    res = herdr(
        "plugin", "pane", "open",
        "--plugin", PLUGIN_ID,
        "--entrypoint", "list",
        "--placement", "overlay",
    )
    if res.returncode != 0:
        sys.stderr.write(res.stderr or "autocontinue: failed to open list pane\n")
    return res.returncode


def cmd_ui(argv):
    import curses

    ensure_daemon()

    def rows():
        """Every watched agent, walled ones first — you arm from this list, so
        it has to show panes that are fine right now too."""
        agents = live_agents() or {}
        walls = load_walls()
        out = []
        for pane_id, info in agents.items():
            if kind_of(info) is None:
                continue
            out.append((pane_id, info, walls.get(pane_id)))
        out.sort(key=lambda r: (r[2] is None, r[0]))
        return out

    def run(stdscr):
        curses.curs_set(0)
        stdscr.timeout(1000)
        sel = 0
        message = ""
        while True:
            entries = rows()
            armed = load_armed()
            if sel >= len(entries):
                sel = max(0, len(entries) - 1)

            stdscr.erase()
            h, w = stdscr.getmaxyx()
            stdscr.addnstr(0, 0, "AUTO CONTINUE", w - 1, curses.A_BOLD)
            if h > 1:
                stdscr.addnstr(
                    1, 0,
                    "j/k select · a arm/disarm · enter resume now · x forget wall · q quit",
                    w - 1, curses.A_DIM,
                )
            if not entries:
                if h > 3:
                    stdscr.addnstr(3, 0, "(no claude/codex agents)", w - 1, curses.A_DIM)
            for i, (pane_id, info, wall) in enumerate(entries):
                row = i + 3
                if row >= h - 1:
                    break
                mark = "[armed]" if pane_id in armed else "[     ]"
                if wall is None:
                    state = "ok"
                elif wall["status"] == "gaveup":
                    state = f"gave up after {wall['attempts']}"
                else:
                    state = f"resume in {_countdown(wall['resume_at'] - time.time())}"
                    if wall["attempts"]:
                        state += f" (retry {wall['attempts']})"
                line = f"{mark} {label_of(info):<18} {info.get('agent_status',''):<8} {state}"
                if wall:
                    line += f"  · {wall['matched'][:40]}"
                attr = curses.A_REVERSE if i == sel else curses.A_NORMAL
                stdscr.addnstr(row, 0, line.ljust(w - 1), w - 1, attr)
            if message and h > 1:
                stdscr.addnstr(h - 1, 0, message[: w - 1], w - 1, curses.A_DIM)
            stdscr.refresh()

            try:
                ch = stdscr.getch()
            except KeyboardInterrupt:
                return
            if ch == -1:
                continue
            if ch in (ord("q"), 27):
                return
            if not entries:
                continue
            pane_id, info, wall = entries[sel]
            if ch in (ord("j"), curses.KEY_DOWN):
                sel = min(len(entries) - 1, sel + 1)
            elif ch in (ord("k"), curses.KEY_UP):
                sel = max(0, sel - 1)
            elif ch == ord("a"):
                state = toggle_armed(pane_id)
                message = f"{label_of(info)} {'armed' if state else 'disarmed'}"
            elif ch == ord("x") and wall:
                drop_wall(pane_id, "dismissed from the list")
                message = f"forgot the wall on {label_of(info)}"
            elif ch in (curses.KEY_ENTER, 10, 13):
                if wall is None:
                    message = "nothing to resume on that agent"
                else:
                    # Manual resume: your keypress is the consent, so this runs
                    # even on a pane you never armed.
                    _update_walls(
                        lambda w, p=pane_id: w.get(p, {}).update(
                            resume_at=0, status="waiting")
                    )
                    fresh = load_walls().get(pane_id)
                    if fresh:
                        attempt_resume(pane_id, fresh, info)
                        message = f"submitted {PROMPT_TEXT!r} to {label_of(info)}"

    curses.wrapper(run)
    return 0


DISPATCH = {
    "start": cmd_start,
    "stop": cmd_stop,
    "status": cmd_status,
    "arm": cmd_arm,
    "scan": cmd_scan,
    "open-list": cmd_open_list,
    "ui": cmd_ui,
    "daemon": cmd_daemon,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in DISPATCH:
        print(f"usage: autocontinue.py {{{'|'.join(DISPATCH)}}}", file=sys.stderr)
        return 2
    return DISPATCH[sys.argv[1]](sys.argv[2:]) or 0


if __name__ == "__main__":
    sys.exit(main())
