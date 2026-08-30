#!/usr/bin/env python3
"""Auto-continue herdr agents that stalled on a usage-limit wall.

When Claude Code or Codex burns through a usage window it prints the wall and
stops — "5-hour limit reached ∙ resets 12pm", "You've hit your usage limit, try
again in 4 hours 12 minutes". The reset time is right there in the message, so
a poll loop can read it, sit out the window, and prod the agent when it opens.

Two halves:

  detect   a pane showing a wall gets a countdown badge ($wall) whether or not
           you armed it. Free observability — this is how you notice an agent
           died at 11:04. herdr's `pane.agent_status_changed` hook wakes the
           daemon the moment an agent stops, so the sweep behind it is slow.

  resume   only panes you *armed* are typed into. At the reset time the daemon
           re-reads the pane, and if the wall is still there and the agent is
           still sitting idle, submits AUTOCONTINUE_PROMPT ("continue"). It
           backs off and retries if the wall is still up, and gives up after
           AUTOCONTINUE_MAX_ATTEMPTS rather than hammering a pane forever.

Arming is per agent and sticky: `arm` toggles the focused pane, the badge shows
which state it is in, and nothing is ever typed into a pane you did not arm.

Subcommands:
  start / stop / status   the daemon (also autostarted by the [[startup]] hook)
  on-status               wake the daemon now (herdr's status-change hook)
  arm                     toggle auto-continue on the focused agent (action)
  scan                    one-shot detection report, writes nothing (debugging)
  open-list / ui          the overlay showing every wall and its countdown
  daemon                  the poll loop itself (spawned, detached)
"""
import fcntl
import json
import os
import re
import platform
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timedelta

PLUGIN_ID = os.environ.get("HERDR_PLUGIN_ID", "rcosteira.autocontinue")
HERDR = os.environ.get("HERDR_BIN_PATH", "herdr")
ROOT = os.path.dirname(os.path.abspath(__file__))

# The fallback has to match the directory herdr itself uses, so a run outside
# herdr's action runner reads the same state.
STATE_DIR = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.expanduser(
    os.path.join("~/.local/state/herdr/plugins", PLUGIN_ID)
)
def _plugin_config_dir():
    """Where this plugin's settings live, whoever started the process.

    herdr exports the directory to an action it runs itself. A daemon started
    from a shell — `autocontinue restart` after an edit, say — inherits none of
    that, and falling back to the state dir found no config.toml at all: the
    watcher then ran on defaults with rotation silently switched off, because
    an empty profile list is also how rotation is turned off on purpose. Fall
    back to herdr's own directory instead, the way STATE_DIR does.
    """
    named = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if named:
        return named
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "herdr", "plugins", "config", PLUGIN_ID)


CONFIG_DIR = _plugin_config_dir()
WALLS = os.path.join(STATE_DIR, "walls.json")
ARMED = os.path.join(STATE_DIR, "armed.json")
PIDFILE = os.path.join(STATE_DIR, "daemon.pid")
LOCK = os.path.join(STATE_DIR, "state.lock")
LOGFILE = os.path.join(STATE_DIR, "autocontinue.log")
LOG_MAX_BYTES = 512 * 1024


PREFIX = "AUTOCONTINUE_"


def _load_settings():
    """Settings from the plugin's own config dir.

    Actions inherit the herdr server's environment, so an environment variable
    can only be set by restarting herdr with it exported. A file in the config
    dir is the one place a person can actually change these.
    """
    for name in ("config.toml", "config.json"):
        path = os.path.join(CONFIG_DIR, name)
        if not os.path.exists(path):
            continue
        try:
            if name.endswith(".toml"):
                import tomllib
                with open(path, "rb") as handle:
                    return tomllib.load(handle)
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)
        except Exception:
            continue  # a broken config must not stop the watcher
    return {}


SETTINGS = _load_settings()


def _setting(name):
    """The environment first, then the config file, then nothing.

    The config key is the variable without its prefix, lowercased:
    AUTOCONTINUE_POLL_S is `poll_s`.
    """
    if name in os.environ:
        return os.environ[name]
    key = name[len(PREFIX):].lower() if name.startswith(PREFIX) else name.lower()
    return SETTINGS.get(key)


def _num(name, default, cast=float):
    value = _setting(name)
    try:
        return cast(value)
    except (TypeError, ValueError):
        return default


def _flag(name, default=False):
    value = _setting(name)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("0", "false", "no", "off", "")


def _list(name, default=""):
    """A comma-separated variable, or a real list when it came from TOML."""
    value = _setting(name)
    if value is None:
        value = default
    if isinstance(value, str):
        value = value.split(",")
    return [str(v).strip().lower() for v in value if str(v).strip()]


# The sweep is a safety net: herdr's status-change hook wakes the daemon the
# moment an agent stops, which is when a wall can appear. What the hook cannot
# announce is a time, and half of this plugin is "wake up in three hours".
POLL_S = _num("AUTOCONTINUE_POLL_S", 60.0)
# Never tick more often than this, however many events arrive at once. Nineteen
# panes changing status together is one tick, not nineteen.
MIN_TICK_S = _num("AUTOCONTINUE_MIN_TICK_S", 2.0)
# How long any one herdr command may take before it is abandoned.
HERDR_TIMEOUT_S = _num("AUTOCONTINUE_HERDR_TIMEOUT_S", 30.0)
TAIL_LINES = _num("AUTOCONTINUE_TAIL_LINES", 15, int)
READ_LINES = _num("AUTOCONTINUE_READ_LINES", 60, int)
PROMPT_TEXT = _setting("AUTOCONTINUE_PROMPT") or "continue"
GRACE_S = _num("AUTOCONTINUE_GRACE_S", 60.0)
# How much earlier an account has to reopen before a wall is re-stamped.
RESTAMP_MIN_GAIN_S = _num("AUTOCONTINUE_RESTAMP_MIN_GAIN_S", 60.0)
BLIND_RETRY_S = _num("AUTOCONTINUE_BLIND_RETRY_MIN", 20.0) * 60
MAX_ATTEMPTS = _num("AUTOCONTINUE_MAX_ATTEMPTS", 5, int)
# How long `stop` waits for a signalled daemon to actually exit.
STOP_WAIT_S = _num("AUTOCONTINUE_STOP_WAIT_S", 5.0)
# Polls a pane must stay missing before its arming is dropped.
ABSENT_POLLS = _num("AUTOCONTINUE_ABSENT_POLLS", 3, int)
DRY_RUN = _flag("AUTOCONTINUE_DRY_RUN")
IS_MAC = platform.system() == "Darwin"

# Empty means "every kind herdr detects". Name kinds here to narrow it.
KINDS = _list("AUTOCONTINUE_KINDS")

# Not "limit": senna-lang/herdr-agent-usage already writes a $limit token, and
# two plugins writing one token would fight over it.
TOKEN = "wall"
GLYPH_ARMED = _setting("AUTOCONTINUE_GLYPH_ARMED") or (
    "\N{ANTICLOCKWISE DOWNWARDS AND UPWARDS OPEN CIRCLE ARROWS}"  # 🔄 will resume
)
GLYPH_IDLE = _setting("AUTOCONTINUE_GLYPH_SEEN") or (
    "\N{DOUBLE VERTICAL BAR}"                     # ⏸ seen, not armed
)
GLYPH_GAVEUP = _setting("AUTOCONTINUE_GLYPH_GAVEUP") or (
    "\N{WARNING SIGN}"                            # ⚠ gave up
)
TTL_MS = int(POLL_S * 4 * 1000)

MONTHS = {m: i for i, m in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split(), start=1)}


# ---- plumbing -------------------------------------------------------------

def _abandon(proc):
    """Kill a command's whole process group and stop reading from it.

    Killing the command alone is not enough. Something it started can inherit
    the command's own descriptors and outlive it, so the group goes rather than
    the one process. Any stream we opened is closed whether or not it does.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except OSError:
        pass
    for stream in (proc.stdout, proc.stderr):
        try:
            if stream is not None:
                stream.close()
        except OSError:
            pass
    try:
        proc.wait(timeout=1)
    except Exception:
        pass


def herdr(*args):
    """Run one herdr command, and wait only on the command itself.

    Output goes to a temporary file rather than a pipe, because a pipe is read
    until end of file and not until the child exits. Those are the same moment
    only while nothing else holds the write end. `herdr` exited, something it
    had started still held that end, and the read never returned: the daemon
    sat in one call for two hours — no badges, no walls, no rotation, and
    nothing in the log to say so. A file ends when the command does, so a
    process that outlives it cannot hold the watcher.

    The timeout is the remaining backstop, for a command that never exits at
    all. It runs in its own session so the whole group can be cut loose then,
    not just the process that has already left.
    """
    with tempfile.TemporaryFile(mode="w+") as out, \
            tempfile.TemporaryFile(mode="w+") as err:
        proc = subprocess.Popen(
            [HERDR, *args], stdout=out, stderr=err,
            stdin=subprocess.DEVNULL, text=True, start_new_session=True,
        )
        try:
            proc.wait(timeout=HERDR_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            _abandon(proc)
            log("herdr %s: no answer in %gs, gave up on it"
                % (" ".join(str(a) for a in args[:2]), HERDR_TIMEOUT_S))
            return subprocess.CompletedProcess(args, 1, "", "timed out")
        out.seek(0)
        err.seek(0)
        return subprocess.CompletedProcess(
            args, proc.returncode, out.read(), err.read())


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
    """The agent kind herdr reports, or None when this pane is not watched.

    With no AUTOCONTINUE_KINDS set, every kind herdr detects is watched, not
    just claude and codex. herdr knows some twenty agents and the rules already
    say which kind they apply to, so the kind list is a filter, not a gate.
    Detection is read-only: nothing is ever typed into a pane you did not arm.
    """
    label = " ".join(
        str(info.get(k) or "") for k in ("agent", "name", "display_agent")
    ).lower()
    if KINDS:
        for kind in KINDS:
            if kind in label:
                return kind
        return None
    agent = str(info.get("agent") or "").strip().lower()
    return agent or None


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
    """Epoch for a wall-clock reset time, or None.

    Stays naive when the text names no zone. `datetime.now().astimezone()` pins
    *today's* UTC offset onto the result, so a target on the far side of a
    daylight-saving change lands an hour out — "resets Feb 3 at 9am" read in
    August became 08:00. The conversion happens at the end instead, where the
    local zone's rules are applied to the target's own date.
    """
    tz = _tzinfo(groups.get("tz"))
    now = datetime.now(tz) if tz else datetime.now()
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
    if target.tzinfo is None:
        # Local zone, resolved for the target's date rather than for today.
        target = target.astimezone()
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


def _set_token(pane_id, text):
    herdr(
        "pane", "report-metadata", pane_id,
        "--source", PLUGIN_ID,
        "--token", f"{TOKEN}={text}",
        "--ttl-ms", str(TTL_MS),
    )


def set_badge(pane_id, wall, armed):
    if wall["status"] == "gaveup":
        text = GLYPH_GAVEUP
    elif wall.get("stranded") and pane_id not in armed:
        # The account moved out from under this pane and nothing here will type
        # into one nobody armed. Its own harness was going to restart it against
        # an account that is no longer installed, so a countdown here would be
        # a promise from something that has stopped watching the clock for it.
        text = GLYPH_GAVEUP
    else:
        glyph = GLYPH_ARMED if pane_id in armed else GLYPH_IDLE
        text = glyph + _countdown(wall["resume_at"] - time.time())
    _set_token(pane_id, text)


def refresh_badge(pane_id, wall, armed):
    """The badge has to say "armed" before a wall exists, not only after one.

    Arming is the plugin's main switch and a wall may be days away, so an armed
    pane carries the glyph on its own — the countdown is what a wall adds.
    """
    if wall:
        set_badge(pane_id, wall, armed)
    elif pane_id in armed:
        _set_token(pane_id, GLYPH_ARMED)
    else:
        clear_badge(pane_id)


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


def strand_walls(kind):
    """A switch resets every wall of that kind: new account, new clock.

    The time a wall counts down to belongs to the account that raised it, and
    after a switch nothing is paying that bill any more. A codex pane sat
    counting down to the old account's 9:29 PM while the account it had just
    moved to had room all along. The attempts go the same way: the ones already
    spent were spent against a different account.

    The wall itself stays, because the message is still on screen until the
    agent redraws it. What changes is that the pane is tried again shortly
    rather than hours from now, and that a pane nobody armed says so: it is
    never typed into here, and the harness that would have restarted it is now
    holding another account's credentials.
    """
    now = time.time()

    def mutate(walls):
        for entry in walls.values():
            if entry.get("kind") != kind:
                continue
            entry["stranded"] = True
            entry["reset_at"] = None
            entry["resume_at"] = now + BUSY_RETRY_S
            entry["attempts"] = 0
            entry["last_attempt"] = None
            entry["status"] = "waiting"

    _update_walls(mutate)


def drop_wall(pane_id, why):
    def mutate(walls):
        return walls.pop(pane_id, None)

    if _update_walls(mutate) is not None:
        log(f"{pane_id}: wall cleared ({why})")
    clear_badge(pane_id)


# ---- account usage --------------------------------------------------------
#
# The limit belongs to the account, not to the pane. Every harness signed into
# the same Claude account shares one window, so the account can be asked when
# that window reopens instead of reading it off a screen. This is what makes
# detection work for a harness whose wording nobody has written a rule for.
#
# Unofficial endpoint: it can change or disappear. Everything here fails soft
# and the text rules keep working on their own.

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_BETA = "oauth-2025-04-20"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
USAGE_CACHE = os.path.join(STATE_DIR, "usage.json")
USAGE_TTL_S = _num("AUTOCONTINUE_USAGE_TTL_S", 180.0)
USAGE_MIN_GAP_S = _num("AUTOCONTINUE_USAGE_MIN_GAP_S", 30.0)
USAGE_TIMEOUT_S = _num("AUTOCONTINUE_USAGE_TIMEOUT_S", 5.0)
# How long to leave an account alone after it answers 429.
USAGE_BACKOFF_S = _num("AUTOCONTINUE_USAGE_BACKOFF_S", 900.0)
USE_ACCOUNT = _flag("AUTOCONTINUE_USE_ACCOUNT", default=True)


def _kind_providers():
    """kind -> which account pays for it.

    An exhausted Claude account says nothing about a codex pane, so the two
    are asked separately. A kind absent from this map has no account to ask and
    falls back to the text rules alone.
    """
    out = {}
    for provider, default in (("claude", "claude,omp"), ("codex", "codex")):
        for kind in _list("AUTOCONTINUE_%s_KINDS" % provider.upper(), default):
            out[kind] = provider
    return out


KIND_PROVIDER = _kind_providers()
# Every kind that has an account behind it.
ACCOUNT_KINDS = sorted(KIND_PROVIDER)
# A window at or above this percent counts as spent. Severity strings other
# than "normal" are logged rather than trusted: the only value seen in the
# wild so far is "normal", so a guessed name could block on a mere warning.
ACCOUNT_PERCENT = _num("AUTOCONTINUE_ACCOUNT_PERCENT", 100.0)
ACCOUNT_SEVERITIES = _list("AUTOCONTINUE_ACCOUNT_SEVERITIES")


def _oauth_token():
    """The Claude OAuth token, from the same store the CLI reads."""
    if IS_MAC:
        try:
            out = subprocess.run(
                ["security", "find-generic-password",
                 "-s", "Claude Code-credentials", "-w"],
                capture_output=True, text=True, timeout=15,
            )
            if out.returncode == 0 and out.stdout.strip():
                data = json.loads(out.stdout)
                token = (data.get("claudeAiOauth") or {}).get("accessToken")
                if token:
                    return token
        except Exception:
            pass
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    try:
        with open(os.path.join(base, ".credentials.json"), encoding="utf-8") as fh:
            return (json.load(fh).get("claudeAiOauth") or {}).get("accessToken")
    except Exception:
        return None


def _iso_to_epoch(value):
    """Epoch seconds from either an ISO string or an already-numeric stamp.

    Claude answers with ISO text, codex with a unix number.
    """
    if not value:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _codex_auth():
    """(token, account_id) from the store the codex CLI reads."""
    base = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    try:
        with open(os.path.join(base, "auth.json"), encoding="utf-8") as fh:
            tokens = (json.load(fh).get("tokens") or {})
        return tokens.get("access_token"), tokens.get("account_id")
    except Exception:
        return None, None


def _get_json(url, headers):
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=USAGE_TIMEOUT_S) as response:
        return json.load(response)


def _fetch_usage(provider):
    if provider == "claude":
        token = _oauth_token()
        if not token:
            return None
        return _get_json(USAGE_URL, {
            "Authorization": "Bearer %s" % token,
            "anthropic-beta": USAGE_BETA,
        })
    if provider == "codex":
        token, account_id = _codex_auth()
        if not token:
            return None
        return _get_json(CODEX_USAGE_URL, {
            "Authorization": "Bearer %s" % token,
            "chatgpt-account-id": account_id or "",
            "User-Agent": "codex-cli",
            "Accept": "application/json",
        })
    return None


def _codex_windows(body):
    """Codex says outright whether it is blocked, so nothing is inferred."""
    rate = (body or {}).get("rate_limit") or {}
    reached = rate.get("limit_reached")
    out = []
    for name in ("primary_window", "secondary_window"):
        window = rate.get(name)
        if not isinstance(window, dict):
            continue
        seconds = window.get("limit_window_seconds") or 0
        resets = window.get("reset_at")
        if resets is None and window.get("reset_after_seconds") is not None:
            resets = time.time() + window["reset_after_seconds"]
        out.append({
            "kind": "%gh" % round(seconds / 3600.0, 1) if seconds else name,
            "group": name,
            "percent": window.get("used_percent"),
            "severity": "",
            "resets_at": _iso_to_epoch(resets),
            "blocked": bool(reached) if reached is not None else None,
        })
    return out


def _claude_windows(body):
    windows = []
    for entry in body.get("limits") or []:
        if not isinstance(entry, dict):
            continue
        # "weekly_scoped" says nothing on its own; the scope names the model.
        kind = entry.get("kind")
        model = ((entry.get("scope") or {}).get("model") or {}).get("display_name")
        windows.append({
            "kind": "%s %s" % (str(kind).replace("_scoped", ""), model) if model else kind,
            "group": entry.get("group"),
            "percent": entry.get("percent"),
            "severity": (entry.get("severity") or "").lower(),
            "resets_at": _iso_to_epoch(entry.get("resets_at")),
            "blocked": None,
        })
    if windows:
        return windows
    # Older/other shape: the named windows carry the same two numbers.
    for name in ("five_hour", "seven_day"):
        block = body.get(name)
        if isinstance(block, dict):
            windows.append({
                "kind": name,
                "group": name,
                "percent": block.get("utilization"),
                "severity": "",
                "resets_at": _iso_to_epoch(block.get("resets_at")),
                "blocked": None,
            })
    return windows


def usage_windows(provider="claude", force=False):
    """Windows for one provider's account, newest cached.

    Cached on disk per provider and shared by every pane in the poll, so a
    ten-second loop over seventeen panes still asks each account at most once
    per USAGE_MIN_GAP_S.
    """
    if not USE_ACCOUNT or not provider:
        return []
    now = time.time()
    store = _load(USAGE_CACHE, {})
    # The cache used to hold one account's windows at the top level. Drop that
    # shape rather than leaving its keys sitting beside the per-provider ones.
    if "windows" in store or "fetched_at" in store:
        store = {}
    cached = store.get(provider) or {}
    fetched_at = cached.get("fetched_at") or 0
    if cached.get("windows") is not None and (now - fetched_at) < USAGE_TTL_S and not force:
        return cached["windows"]
    if (now - (cached.get("tried_at") or 0)) < USAGE_MIN_GAP_S and not force:
        return cached.get("windows") or []
    cached["tried_at"] = now
    store[provider] = cached
    _save(USAGE_CACHE, store)
    try:
        body = _fetch_usage(provider)
    except Exception as exc:
        # A 429 means asked too often, so retrying on the usual gap only digs
        # in. Sit the account out and keep serving the last answer.
        if getattr(exc, "code", None) == 429:
            cached["tried_at"] = now + USAGE_BACKOFF_S - USAGE_MIN_GAP_S
            store[provider] = cached
            _save(USAGE_CACHE, store)
            log("usage api (%s): rate limited, resting %ds"
                % (provider, int(USAGE_BACKOFF_S)))
        else:
            log("usage api (%s): %s" % (provider, exc))
        return cached.get("windows") or []
    if not isinstance(body, dict):
        return cached.get("windows") or []
    windows = (_codex_windows(body) if provider == "codex"
               else _claude_windows(body))
    store[provider] = {"fetched_at": now, "tried_at": now, "windows": windows}
    _save(USAGE_CACHE, store)
    return windows


def drop_usage_cache(provider):
    """Forget one provider's windows, so the next read asks the account again.

    Credentials are machine-wide, so replacing them retires every number read
    from the account that is leaving. The whole entry goes, `tried_at` with it:
    keeping that would hold the next read off for USAGE_MIN_GAP_S and serve the
    old account's windows for exactly as long.
    """
    if not provider:
        return
    store = _load(USAGE_CACHE, {})
    if store.pop(provider, None) is None:
        return
    _save(USAGE_CACHE, store)
    log("forgot the cached %s windows: the account behind them changed" % provider)


def _spent(window):
    # Codex reports it outright; nothing else has to be inferred for it.
    if window.get("blocked") is not None:
        return bool(window["blocked"])
    percent = window.get("percent")
    severity = window.get("severity") or ""
    if severity and severity != "normal" and severity in ACCOUNT_SEVERITIES:
        return True
    return isinstance(percent, (int, float)) and percent >= ACCOUNT_PERCENT


def account_block(kind=None):
    """(resets_at, window, percent) for a spent window of that kind's account."""
    provider = KIND_PROVIDER.get(kind) if kind else "claude"
    if not provider:
        return None
    spent = [w for w in usage_windows(provider)
             if _spent(w) and w.get("resets_at")]
    if not spent:
        return None
    soonest = min(spent, key=lambda w: w["resets_at"])
    return soonest["resets_at"], soonest.get("kind"), soonest.get("percent")


def account_unknown(kind=None):
    """True when the account behind a kind could not be read at all.

    An empty answer means two very different things: the account has room, or
    nobody could ask it. A switch drops the cached windows on purpose, and a
    reading that is rate limited right after leaves nothing behind — reading
    that as "the account has room" cleared every account wall at once and left
    the armed panes with nothing to fire at the reset.
    """
    provider = KIND_PROVIDER.get(kind) if kind else "claude"
    if not USE_ACCOUNT or not provider:
        return False
    cached = (_load(USAGE_CACHE, {}) or {}).get(provider) or {}
    return cached.get("windows") is None


def account_has_room(kind):
    """True when the account behind a kind was read, and has room to work."""
    return (kind in ACCOUNT_KINDS and not account_unknown(kind)
            and not account_block(kind))


def account_reset_for(kind):
    """The soonest reset a *spent* window of that account has, for a blank time.

    Only a spent window is a reason to wait. Every window has a reset time, and
    taking the soonest of all of them parked a codex pane until 02:36 — the
    hour its 5h window happened to roll over, while that window sat at 22% and
    nothing was blocked at all. With nothing spent there is no time to wait
    for, and the caller falls back to looking again shortly.
    """
    provider = KIND_PROVIDER.get(kind)
    if not provider:
        return None
    times = [w["resets_at"] for w in usage_windows(provider)
             if _spent(w) and w.get("resets_at")]
    return min(times) if times else None


def new_wall(pane_id, kind, info, hit):
    rule_id, matched, context = hit
    now = time.time()
    reset_at, via = parse_reset(context)
    if not reset_at:
        # Nothing parseable on screen: ask the account when its window reopens.
        # This is what carries a harness whose wording has no rule.
        reset_at, via = account_reset_for(kind), "account"
    if reset_at:
        resume_at, reason = reset_at + GRACE_S, via
    else:
        # No time anywhere: come back periodically and look again.
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


def restamp_wall(pane_id, wall, kind):
    """Move a wall's resume time earlier when its account reopens sooner.

    A wall records its reopening once, from whatever it was told when it was
    first seen. That answer can turn out to be too late: rotation moves the
    pane onto a different account, or the account revises the window itself.
    Nothing used to go back and look, so a wall could sit on an hours-old
    answer while the account paying for it had already reopened.

    Only ever bring a wall forward. A later answer is no reason to make an
    armed pane wait longer, and a pane already in backoff keeps the retry it
    earned — that delay was chosen deliberately, one failed attempt at a time.
    """
    if wall.get("attempts"):
        return wall
    if kind not in ACCOUNT_KINDS:
        return wall
    spent = account_block(kind)
    if not spent:
        return wall
    resets_at = spent[0]
    resume_at = resets_at + GRACE_S
    if resume_at >= (wall.get("resume_at") or 0) - RESTAMP_MIN_GAIN_S:
        return wall

    def mutate(walls):
        entry = walls.get(pane_id)
        if entry is None:
            return None
        entry.update(reset_at=resets_at, resume_at=resume_at,
                     reason="account (revised)")
        return dict(entry)

    updated = _update_walls(mutate)
    if updated is None:
        return wall
    log("%s: the account reopens at %s, %s earlier than this wall was told"
        % (pane_id, datetime.fromtimestamp(resume_at).strftime("%H:%M"),
           _countdown(wall["resume_at"] - resume_at)))
    return updated


def _backoff(attempts):
    return min(300 * (3 ** max(0, attempts - 1)), 3600)


# ---- account rotation -----------------------------------------------------
#
# When the account is spent, a second account with capacity left can take over.
# Switching credentials is machine-wide, so this only ever lands on a profile
# you named, and only when a pane you armed is the one that is stuck.
#
# account-switch publishes what each saved account has left, parked ones
# included, so the candidates are ranked rather than taken in the order they
# were saved: an account with room first, then one nobody has read lately, then
# the accounts known to be spent, soonest to reopen first.
#
# Every named account is ranked, the live one included, and rotation moves to
# whichever reopens first. It used to cross a profile off as it tried it — the
# account it was leaving among them — so it could not go back to an account
# that reopened sooner, and a fleet could sit out the night on the worse of two.
#
# A reading can still be old, and no reading at all is normal. Rotation
# therefore switches and watches: if the new account is spent too, the wall
# comes back and the ranking is asked again.

ROTATE_PROFILES = _list("AUTOCONTINUE_ROTATE_PROFILES")
ROTATE_COOLDOWN_S = _num("AUTOCONTINUE_ROTATE_COOLDOWN_S", 300.0)
# Past this age, an account's reading is treated as no reading at all.
ROTATE_STALE_S = _num("AUTOCONTINUE_ROTATE_STALE_S", 1800.0)
# How much sooner another account must reopen before it is worth a switch.
ROTATE_GAIN_S = _num("AUTOCONTINUE_ROTATE_GAIN_S", 300.0)
# How long the live account may still have to run before an account nobody
# could read is worth one switch to find out.
ROTATE_UNKNOWN_HORIZON_S = _num("AUTOCONTINUE_ROTATE_UNKNOWN_HORIZON_S", 900.0)
# And how long that guess stands, so the same unknown is not tried on a loop.
ROTATE_GUESS_GAP_S = _num("AUTOCONTINUE_ROTATE_GUESS_GAP_S", 3600.0)
# A refusal lasts until that login is saved afresh. This is only the backstop
# for a refusal that was never about the credential at all.
ROTATE_REFUSED_S = _num("AUTOCONTINUE_ROTATE_REFUSED_S", 6 * 3600.0)
# A pane prompted this recently is not prompted again by the nudge.
NUDGE_GAP_S = _num("AUTOCONTINUE_NUDGE_GAP_S", 120.0)
# How long a wall waits over when the agent is busy or waiting on you.
BUSY_RETRY_S = _num("AUTOCONTINUE_BUSY_RETRY_S", 60.0)
# A fresh reading is taken at most this often, however many sweeps want one.
ROTATE_REFRESH_GAP_S = _num("AUTOCONTINUE_ROTATE_REFRESH_GAP_S", 300.0)
ROTATE_STATE = os.path.join(STATE_DIR, "rotate.json")
# Restarting a session is a bigger act than typing into one, so it is its own
# switch rather than something arming quietly started to mean.
RESTART_STRANDED = _flag("AUTOCONTINUE_RESTART_STRANDED", default=False)
RESTART_KINDS = _list("AUTOCONTINUE_RESTART_KINDS", "codex")
# What quits the agent. codex takes /exit.
RESTART_QUIT = _setting("AUTOCONTINUE_RESTART_QUIT") or "/exit"
# How long to wait for the pane to come back to its shell prompt.
RESTART_WAIT_S = _num("AUTOCONTINUE_RESTART_WAIT_S", 30.0)
# And how long one restart stands, so a pane is not cycled.
RESTART_GAP_S = _num("AUTOCONTINUE_RESTART_GAP_S", 3600.0)
# How long a wall must stand, while the account it bills to reads as having
# room, before the session is taken to be using a different account. The gap is
# for the account's own reading to catch up with a pane that has just stopped.
STRANDED_AFTER_S = _num("AUTOCONTINUE_STRANDED_AFTER_S", 120.0)
# And how long after that prompt before its answer is taken as read. The answer
# is the wall still standing, which the next sweep already shows, so this is one
# sweep and not the quiet period a nudge keeps.
STRANDED_CONFIRM_S = _num("AUTOCONTINUE_STRANDED_CONFIRM_S", 60.0)
RESTARTS = os.path.join(STATE_DIR, "restarts.json")
SWITCH_PLUGIN_ID = os.environ.get(
    "AUTOCONTINUE_SWITCH_PLUGIN", "rcosteira.account-switch"
)


def _switcher_script():
    """Path to account-switch's switcher.py, or None when it is not installed."""
    res = herdr("plugin", "list", "--plugin", SWITCH_PLUGIN_ID, "--json")
    if res.returncode != 0:
        return None
    try:
        data = json.loads(res.stdout or "{}")
    except ValueError:
        return None
    plugins = (data.get("result") or {}).get("plugins") or []
    if isinstance(plugins, dict):
        plugins = [plugins]
    for plugin in plugins:
        root = plugin.get("plugin_root")
        if not root:
            continue
        path = os.path.join(root, "switcher.py")
        if os.path.exists(path):
            return path
    return None


def _switch_env():
    """Environment for running account-switch's own script.

    The plugin-scoped variables herdr exports name the plugin it invoked, which
    is this one. Passing them down makes switcher.py treat *autocontinue's*
    state directory as its own, where it finds no profiles and reports none —
    so rotation asked for a list, got nothing back, and quietly did nothing.
    Worse, a switch run that way would write credentials into the wrong
    directory. Name the other plugin instead and let it resolve its own paths.
    """
    env = dict(os.environ)
    env.pop("HERDR_PLUGIN_STATE_DIR", None)
    env.pop("HERDR_PLUGIN_CONFIG_DIR", None)
    env["HERDR_PLUGIN_ID"] = SWITCH_PLUGIN_ID
    return env


def _switch_profiles(script, kind):
    """The saved profiles for a kind, from account-switch's own status.

    One dict per profile, as that plugin publishes it: slug, label, active,
    and — where it has a reading — the account's windows and when they were
    read. An older account-switch sends the names alone, which is why nothing
    here treats a missing reading as an error.
    """
    res = subprocess.run(
        ["python3", script, "list", "--kind", kind, "--json"],
        capture_output=True, text=True, timeout=30, env=_switch_env(),
    )
    if res.returncode != 0:
        return []
    try:
        data = json.loads(res.stdout or "[]")
    except ValueError:
        return []
    return [p for p in data if isinstance(p, dict) and p.get("slug")]


def _rotation_candidates(profiles):
    """The profiles rotation may use, the live one included.

    Crossing a profile off once it had been tried is what stranded the fleet on
    the worse account: the account being left was crossed off too, so rotation
    could never go back to it however soon it reopened.
    """
    return [
        p for p in profiles
        if (p["slug"].lower() in ROTATE_PROFILES
            or (p.get("label") or "").lower() in ROTATE_PROFILES)
        # A profile account-switch cannot read is one it cannot switch to
        # either. It says so as `problem`, and "needs re-login" names exactly
        # the account rotation kept reaching for.
        and not p.get("problem")
    ]


def _is_refused(profile, refused, now):
    """True while this profile's saved login is one a switch already refused.

    Keyed on `saved_at`, so the refusal lasts exactly as long as the credential
    that earned it: log the account in again, save it, and the profile is
    trusted on the next sweep with nothing to wait out. The age check is only a
    backstop for a refusal that was never about the credential — a network blip
    should not strand an account until someone thinks to re-save it.
    """
    mark = refused.get(profile["slug"])
    if not isinstance(mark, dict):
        return False
    if (now - (mark.get("at") or 0)) >= ROTATE_REFUSED_S:
        return False
    saved_at = profile.get("saved_at")
    if saved_at is None or mark.get("saved_at") is None:
        return True                     # nothing to compare; trust the age
    return saved_at == mark["saved_at"]


def _worth_moving(live, best):
    """True when `best` beats the live account by enough to justify a switch.

    A better band always wins: an account with room beats one that is merely
    due sooner. Within a band the gain has to clear ROTATE_GAIN_S, so two
    accounts reopening at much the same time do not make it flap between them.
    """
    if best[0] != live[0]:
        return best[0] < live[0]
    return best[1] <= live[1] - ROTATE_GAIN_S


def _switch_is_due(live, best, now, state):
    """False while the better account is not worth moving to *yet*.

    Two answers wait rather than switch. An account that is spent but reopens
    sooner is worth having when it reopens, not hours before: switching early
    puts every pane on the machine onto it for the rest of its window, and
    takes the restart away from a harness that may still do it itself. And an
    account nobody could read is a guess — worth one switch while the live
    account has hours to run, never while it is about to come back on its own,
    and never twice in the same dry spell.
    """
    band, resets_at = _rotate_rank(best, now)
    if band == 2:
        return now >= resets_at
    if band == 1:
        guessed = (state.get("guessed") or {}).get(best["slug"]) or 0
        if (now - guessed) < ROTATE_GUESS_GAP_S:
            return False
        live_band, live_resets = _rotate_rank(live, now)
        if live_band == 2 and (live_resets - now) <= ROTATE_UNKNOWN_HORIZON_S:
            return False
    return True


def _refresh_profiles(script, kind):
    """What each account of one kind has left, read now rather than recalled.

    The profile list serves whatever was last read, and nothing reads on a
    timer, so by the time a wall appears the readings are usually hours old.
    Ranking them then just re-picks the order the profiles were saved in. This
    asks account-switch to go and look instead.

    It costs a request per saved account of that kind, and a token renewal on a
    parked one, which is why the caller asks only where the answer can change.
    """
    res = subprocess.run(
        ["python3", script, "usage", "--json", "--kind", kind],
        capture_output=True, text=True, timeout=90, env=_switch_env(),
    )
    if res.returncode != 0:
        log("rotate: could not read the accounts: %s"
            % (res.stderr or "").strip()[:200])
        return None
    try:
        rows = json.loads(res.stdout or "[]")
    except ValueError:
        log("rotate: the account reading did not parse")
        return None
    # Keep only the kind that was asked about. The same profile names exist
    # under both harnesses, and an account-switch too old to filter answers
    # with every kind — a ChatGPT row must never decide a claude switch.
    return [r for r in rows
            if isinstance(r, dict) and r.get("slug")
            and r.get("kind", kind) == kind] or None


def _rotate_rank(profile, now):
    """Sort key for a rotation candidate, best account first.

    Three bands. An account read recently with nothing spent has room right
    now, so it goes first. Then an account nobody has read lately: a parked
    account is usually parked because it was left alone, so its window has
    most likely reopened already, and one switch is what finds out. Last come
    the accounts known to be spent, soonest to reopen first.

    Every candidate lands in the same band when there is nothing to rank by,
    and the sort is stable, so an older account-switch leaves the saved order
    exactly as it was.
    """
    windows = profile.get("windows")
    read_at = profile.get("at")
    if not windows or not read_at or (now - read_at) > ROTATE_STALE_S:
        return (1, 0.0)
    spent = [w["resets_at"] for w in windows
             if _spent(w) and w.get("resets_at")]
    if not spent:
        return (0, 0.0)
    return (2, min(spent))


def rotate_account(kind):
    """Switch to whichever named account reopens first. True when it switched.

    Every named profile is ranked, the live one included, so the account just
    left is still a candidate — it is often the one that reopens soonest, and
    excluding it is what stranded a fleet on the worse of two accounts.

    Returns False when rotation is off, unavailable, on cooldown, when the live
    account is already the one that reopens first, or when the best of the rest
    does not beat it by ROTATE_GAIN_S.
    """
    if not ROTATE_PROFILES:
        return False
    now = time.time()
    state = _load(ROTATE_STATE, {})
    if (now - (state.get("last_switch") or 0)) < ROTATE_COOLDOWN_S:
        return False
    script = _switcher_script()
    if not script:
        log("rotate: %s is not installed" % SWITCH_PLUGIN_ID)
        return False
    profiles = _switch_profiles(script, kind)
    if not profiles:
        return False
    refused = state.get("refused") or {}
    named = [p for p in _rotation_candidates(profiles)
             if not _is_refused(p, refused, now)]
    if not named:
        return False
    # One fresh reading, only where it can change the answer. With a single
    # candidate there is nothing to rank. And a reading is taken at most once
    # per gap: a walled pane asks on every sweep, and answering each one would
    # earn the rate limit that makes every later reading useless.
    last_refresh = state.get("last_refresh") or 0
    if len(named) > 1 and (now - last_refresh) >= ROTATE_REFRESH_GAP_S:
        last_refresh = now
        _save(ROTATE_STATE, dict(state, last_refresh=now))
        reread = _refresh_profiles(script, kind)
        fresh = [p for p in _rotation_candidates(reread or [])
                 if not _is_refused(p, refused, now)]
        if fresh:
            # The reading does not say which profile the switch command would
            # call live, so carry that answer over from the list that does.
            active = {p["slug"] for p in profiles if p.get("active")}
            for profile in fresh:
                profile["active"] = profile["slug"] in active
            named = fresh
    live = next((p for p in named if p.get("active")), None)
    best = min(named, key=lambda p: _rotate_rank(p, now))
    if live is not None:
        if best["slug"] == live["slug"]:
            return False        # already on the account that reopens first
        if not _worth_moving(_rotate_rank(live, now), _rotate_rank(best, now)):
            return False
        if not _switch_is_due(live, best, now, state):
            return False
    target = best["slug"]
    res = subprocess.run(
        ["python3", script, "switch", kind, target],
        capture_output=True, text=True, timeout=60, env=_switch_env(),
    )
    if res.returncode != 0:
        # Remember it, or the next sweep asks again and the same dead login is
        # refused every minute. Recording the attempt engages the cooldown too:
        # only a successful switch used to write anything down.
        marks = dict(refused)
        marks[target] = {"saved_at": best.get("saved_at"), "at": now}
        _save(ROTATE_STATE, dict(state, refused=marks, last_attempt=now,
                                 last_refresh=last_refresh))
        log("rotate: %s refused, leaving it be until it is saved again: %s"
            % (target, (res.stderr or "").strip()[:160]))
        return False
    # Before anything else reads the account: the pane that triggered this is
    # about to be re-badged, and the windows on disk belong to the old account.
    drop_usage_cache(KIND_PROVIDER.get(kind))
    strand_walls(kind)
    kept = {k: v for k, v in refused.items() if k != target}
    # A switch onto an account nobody could read is a guess, and it is written
    # down as one: the answer it buys is the reading that follows, and without
    # the mark a reading that never comes would have it guessed again every
    # cooldown.
    guessed = dict(state.get("guessed") or {})
    if _rotate_rank(best, now)[0] == 1:
        guessed[target] = now
    _save(ROTATE_STATE, {"last_switch": now, "last_refresh": last_refresh,
                         "refused": kept, "guessed": guessed})
    log("rotate: %s -> %s (%s)" % (kind, target, (res.stdout or "").strip()[:120]))
    herdr("notification", "show", "Auto-continue switched account",
          "--body", (res.stdout or target).strip()[:200], "--sound", "none")
    return True


def keep_wall(pane_id, wall, armed, info):
    """True when a wall whose evidence went away is kept anyway.

    A wall ends when its evidence is genuinely gone — the message left the
    screen, or the account that raised it now reads as having room. A reading
    that could not be taken is not that answer, and `tick` holds those walls
    before this is reached.

    What is held here is a nudge we owe and cannot deliver. An agent that is
    working is already moving. One blocked on a prompt of its own is stopped
    exactly where the wall left it, and it is never typed into — forgetting the
    wall there is what let a reset arrive with nothing left to fire, and an
    armed pane sat idle for hours with its window open.
    """
    if pane_id not in armed or wall.get("status") != "waiting":
        return False
    return info.get("agent_status") == "blocked"


def defer_wall(pane_id, until):
    """Hold a wall over, without ever pulling its resume time earlier."""
    _update_walls(lambda w: w.get(pane_id, {}).update(
        resume_at=max(w.get(pane_id, {}).get("resume_at") or 0, until)))


def restarted_recently(pane_id, now):
    return (now - (_load(RESTARTS, {}).get(pane_id) or 0)) < RESTART_GAP_S


def _mark_restarted(pane_id, now):
    marks = _load(RESTARTS, {})
    marks[pane_id] = now
    _save(RESTARTS, marks)


def _waited_for_shell(pane_id):
    """True once herdr no longer sees a running agent in the pane.

    `/exit` leaves the terminal open at its shell prompt, in the same pane, so
    the only thing to wait for is codex itself going. herdr says that two ways:
    it drops the pane from the agent list, or it keeps the pane and reports the
    status as unknown. Waiting on the first alone timed out on the second.
    """
    deadline = time.time() + RESTART_WAIT_S
    while time.time() < deadline:
        agents = live_agents()
        if agents is not None:
            info = agents.get(pane_id)
            if info is None or info.get("agent_status") == "unknown":
                return True
        time.sleep(1)
    return False


def restart_session(pane_id, info, kind):
    """Quit the agent in a pane and start it again on its own session.

    A session already running keeps the account it started on, so a switch
    cannot reach it: codex prints the same limit straight back. Starting it
    again is what picks the new account up, and the pane does not move —
    `herdr agent start --pane` runs the agent in the pane it is given, so the
    pane id, and with it your arming, are exactly as they were.

    Every step has to hold and none of them is forced. A session that will not
    quit is left where it is rather than killed.
    """
    session = info.get("agent_session") or {}
    if session.get("kind") != "id" or not session.get("value"):
        log("%s: no session id to resume, so not restarting it" % pane_id)
        return False
    log("%s: restarting it on its own session to pick the new account up"
        % pane_id)
    res = herdr("agent", "prompt", pane_id, RESTART_QUIT)
    if res.returncode != 0:
        log("%s: could not send %r: %s"
            % (pane_id, RESTART_QUIT, (res.stderr or "").strip()[:120]))
        return False
    if not _waited_for_shell(pane_id):
        log("%s: still running after %r, so it is left as it is"
            % (pane_id, RESTART_QUIT))
        return False
    res = herdr("agent", "start", label_of(info), "--kind", kind,
                "--pane", pane_id, "--", "resume", session["value"])
    if res.returncode != 0:
        log("%s: could not start it again: %s"
            % (pane_id, (res.stderr or "").strip()[:160]))
        return False
    log("%s: back up on session %s" % (pane_id, str(session["value"])[:8]))
    herdr("agent", "prompt", pane_id, PROMPT_TEXT)
    log("%s: submitted %r to the resumed session" % (pane_id, PROMPT_TEXT))
    return True


def attempt_resume(pane_id, wall, info):
    """Type into an armed pane whose window should have reopened.

    The caller has confirmed, this tick, that the pane is armed and that its
    wall still stands — either the message is on screen, or the wall is one
    `keep_wall` held when the evidence for it went away.
    """
    now = time.time()
    if wall.get("status") != "waiting":
        return  # already gave up; only an explicit resume from the list revives it
    if info.get("agent_status") in ("working", "blocked"):
        # Busy or waiting on you — never interrupt; look again shortly.
        defer_wall(pane_id, now + BUSY_RETRY_S)
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
            # Never retry before the account says the window reopens. The
            # backoff spaces the tries once it has; left to itself it replaced
            # the reset the wall already knew about, so a three-hour wait was
            # retried on a 5m/15m/45m ladder that spent every attempt hours
            # early and gave up before the window could open.
            nudge = now + _backoff(attempts)
            reset_at = entry.get("reset_at")
            if reset_at:
                nudge = max(nudge, reset_at + GRACE_S)
            entry["resume_at"] = nudge

    _update_walls(mutate)


_absent = {}  # pane_id -> consecutive polls it has been missing from the list


def tick(agents, pending):
    """One poll: badge every walled pane, resume the armed ones that are due."""
    walls = load_walls()
    armed = load_armed()
    now = time.time()

    gone = [p for p in walls if p not in agents]
    for pane_id in gone:
        _update_walls(lambda w, p=pane_id: w.pop(p, None))

    # Disarm only a pane that has stayed missing. One incomplete agent list —
    # herdr restarting, a config reload, a plugin relink — used to be enough to
    # drop arming for good, silently, which is the one piece of state here a
    # person set by hand.
    for pane_id in list(_absent):
        if pane_id in agents:
            del _absent[pane_id]
    if agents:
        for pane_id in armed - set(agents):
            _absent[pane_id] = _absent.get(pane_id, 0) + 1
        dead = {p for p, misses in _absent.items() if misses >= ABSENT_POLLS}
        if dead:
            with _Lock():
                armed = load_armed() - dead
                save_armed(armed)
            for pane_id in dead:
                _absent.pop(pane_id, None)
            log("disarmed %s (pane gone for %d polls)"
                % (", ".join(sorted(dead)), ABSENT_POLLS))

    for pane_id, info in agents.items():
        kind = kind_of(info)
        if kind is None:
            continue
        wall = walls.get(pane_id)
        if info.get("agent_status") == "working":
            if wall:  # it is moving again; whatever we saw is history
                drop_wall(pane_id, "agent working")
            pending.discard(pane_id)
            refresh_badge(pane_id, None, armed)
            continue

        text = pane_text(pane_id)
        if text is None:
            continue  # unreadable this tick; leave the wall as it stands
        hit = find_wall(text, kind)
        if hit is None and kind in ACCOUNT_KINDS:
            # No wording matched, but the account itself is out. Every pane
            # billed to it is stuck whatever its harness prints on screen.
            spent = account_block(kind)
            if spent:
                resets_at, window, percent = spent
                hit = (
                    "account:%s" % window,
                    "account window %s at %s%%" % (window, percent),
                    "resets %s" % datetime.fromtimestamp(resets_at).isoformat(),
                )
        if (hit is None and wall
                and str(wall.get("rule") or "").startswith("account:")
                and account_unknown(kind)):
            # The account could not be asked, which is not the answer "it has
            # room". The wall it raised stands until someone can ask again, or
            # until the reset it already carries arrives.
            hit = (wall["rule"], wall["matched"], "")

        if hit is None:
            if wall and keep_wall(pane_id, wall, armed, info):
                # Waiting on you, so it is never typed into. The nudge it is owed
                # waits with it, rather than the wall being forgotten and the
                # pane left sitting idle with its window already open.
                defer_wall(pane_id, now + BUSY_RETRY_S)
                set_badge(pane_id, load_walls().get(pane_id, wall), armed)
                pending.discard(pane_id)
                continue
            if wall:
                # The harness clears its own message when the window reopens,
                # and an agent that stopped mid-task is still stopped. Prompt an
                # armed one on the way out: dropping the wall quietly is what
                # left armed panes sitting idle for hours after their window
                # came back.
                just_prompted = (
                    now - (wall.get("last_attempt") or 0)) < NUDGE_GAP_S
                if (pane_id in armed and wall.get("status") == "waiting"
                        and not just_prompted
                        and info.get("agent_status") != "working"):
                    log("%s: the wall went away on its own; nudging it" % pane_id)
                    attempt_resume(pane_id, wall, info)
                drop_wall(pane_id, "message gone")
            pending.discard(pane_id)
            refresh_badge(pane_id, None, armed)
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

        wall = restamp_wall(pane_id, wall, kind)

        # A pane that says it is walled while the account it bills to has room.
        # Those cannot both be about the same account, so the session is using
        # credentials this account does not have: codex keeps the account it
        # started on, and a switch never reaches it. Anchored on the account
        # rather than on a mark left by the switch, because the wall carrying
        # that mark is dropped the moment the agent redraws its screen — the
        # limit came back eight minutes later as a brand new wall that
        # remembered nothing, and the restart never fired.
        tried_at = wall.get("last_attempt") or 0
        stranded = (pane_id in armed and account_has_room(kind)
                    and (now - wall["detected_at"]) >= STRANDED_AFTER_S)

        if stranded and tried_at < wall["detected_at"] and now < wall["resume_at"]:
            # Not prompted on this wall yet. There is no window to wait out —
            # the account this pane bills to has room right now.
            log("%s: the account it bills to has room; not waiting out a "
                "window nobody is waiting for" % pane_id)
            _update_walls(lambda w, p=pane_id: w.get(p, {}).update(resume_at=now))
            wall = load_walls().get(pane_id, wall)

        # And the answer that prompt gives. Nothing typed into that pane will
        # help, so act on it once rather than spending five attempts finding out.
        elif (stranded and tried_at >= wall["detected_at"]
                and (now - tried_at) >= STRANDED_CONFIRM_S
                and wall["status"] in ("waiting", "gaveup")):
            # A wall that has already given up still qualifies. Giving up here
            # means "nothing typed into this pane will help", which is the exact
            # case restarting the session answers — so switching the setting on
            # has to reach the pane that taught you to want it.
            if (RESTART_STRANDED and kind in RESTART_KINDS
                    and not restarted_recently(pane_id, now)):
                # Marked before the attempt, not after: a restart that fails
                # halfway must not be tried again on the next sweep.
                _mark_restarted(pane_id, now)
                if restart_session(pane_id, info, kind):
                    drop_wall(pane_id, "restarted on the account it moved to")
                    pending.discard(pane_id)
                    refresh_badge(pane_id, None, armed)
                    continue
            if wall["status"] == "waiting":
                log("%s: the wall is still up though the account it now bills "
                    "to has room. This session is not using that account — "
                    "restart it." % pane_id)
                _update_walls(lambda w, p=pane_id: w.get(p, {}).update(
                    status="gaveup"))
                wall = load_walls().get(pane_id, wall)

        set_badge(pane_id, wall, armed)

        # Rotation: only for a pane you armed, and only while the account it
        # bills to is spent. A switch is machine-wide, so an unarmed pane never
        # triggers one.
        if (pane_id in armed and wall["status"] == "waiting"
                and kind in ACCOUNT_KINDS and account_block(kind)
                and rotate_account(kind)):
            # The account we just moved to may be spent as well. Prompting is
            # how that shows: the wall returns and the ranking is asked again.
            attempt_resume(pane_id, wall, info)
            continue

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


# ---- waking the daemon ----------------------------------------------------
#
# herdr's `pane.agent_status_changed` hook runs a separate short-lived process.
# It does not do the detecting itself: the daemon owns walls.json and armed.json,
# and a second writer would race it for the sake of work the daemon is about to
# do anyway. The hook just says "look now", and the daemon's own sweep is what
# slows down.

WAKE_SIGNAL = signal.SIGUSR1
_wake = threading.Event()


def _install_wake_handler():
    """Let SIGUSR1 cut the sleep short. Its default action would kill us."""
    def handler(_signum, _frame):
        _wake.set()
    try:
        signal.signal(WAKE_SIGNAL, handler)
        return True
    except (ValueError, OSError):
        return False  # not the main thread, or no such signal here


def wake_daemon():
    """Ask a running daemon to tick now. True when one was signalled."""
    pid = _read_pid()
    if not _pid_alive(pid):
        return False
    try:
        os.kill(pid, WAKE_SIGNAL)
        return True
    except OSError:
        return False


def cmd_on_status(argv):
    """herdr's status-change hook. Wakes the daemon; detects nothing itself.

    A wall appears when an agent *stops*, so a pane that just started working
    cannot be at one — and the daemon clears a stale wall on its own next tick.
    Skipping those halves the signals on a busy session.

    Silent and always successful: this runs on every status change in the
    session, and a plugin hook that prints or fails on every event is noise.
    """
    raw = os.environ.get("HERDR_PLUGIN_EVENT_JSON")
    status = None
    if raw:
        try:
            status = ((json.loads(raw) or {}).get("data") or {}).get("agent_status")
        except ValueError:
            status = None
    if status == "working":
        return 0
    wake_daemon()
    return 0


def cmd_daemon(argv):
    with _Lock():
        existing = _read_pid()
        if _pid_alive(existing) and existing != os.getpid():
            return 0
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(PIDFILE, "w") as f:
            f.write(str(os.getpid()))

    woken = _install_wake_handler()
    # Say whether rotation is on. It is off whenever the profile list is empty,
    # which is both how it is turned off and how a daemon that cannot find its
    # config.toml comes up — and that second one used to be invisible.
    log(f"daemon up (sweep {POLL_S:g}s, prompt {PROMPT_TEXT!r}"
        f"{', woken by status events' if woken else ', sweep only'}"
        f", rotation {'-> ' + ','.join(ROTATE_PROFILES) if ROTATE_PROFILES else 'off'}"
        f", restarts {'on for ' + ','.join(RESTART_KINDS) if RESTART_STRANDED else 'off'}"
        f"{', DRY RUN' if DRY_RUN else ''})")
    log("settings read from %s" % (
        os.path.join(CONFIG_DIR, "config.toml") if SETTINGS
        else "nowhere: no config file under %s" % CONFIG_DIR))
    pending = set()
    server_fails = 0
    last_tick = 0.0
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
            last_tick = time.time()
            # Sleep until the sweep is due or an event wakes us, whichever comes
            # first, then hold MIN_TICK_S so a burst of events is one tick.
            _wake.clear()
            if _wake.wait(POLL_S):
                remaining = MIN_TICK_S - (time.time() - last_tick)
                if remaining > 0:
                    time.sleep(remaining)
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
    warn_if_unwired()
    return 0


def _await_exit(pid):
    """Wait for a signalled process to go. True once it has."""
    deadline = time.time() + STOP_WAIT_S
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return not _pid_alive(pid)


def cmd_restart(argv):
    """Stop and start again in one process, so new code is actually loaded.

    Two separate calls cannot do this reliably. herdr's action invocation
    returns while the command is still running, so a `start` invoked behind a
    `stop` can read a pid the old daemon has not released yet, report "already
    running", and decline — and then the old process exits, leaving nothing
    watching. Doing both here keeps the order.
    """
    cmd_stop(argv)
    if ensure_daemon():
        print(f"autocontinue: restarted (log: {LOGFILE})")
    else:
        print("autocontinue: could not restart — something still holds the pid",
              file=sys.stderr)
        return 1
    warn_if_unwired()
    return 0


def cmd_stop(argv):
    pid = _read_pid()
    if not _pid_alive(pid):
        print("autocontinue: not running")
    else:
        try:
            os.kill(pid, 15)
        except OSError as exc:
            print(f"autocontinue: could not stop pid {pid}: {exc}", file=sys.stderr)
            return 1
        # Wait for it to go, rather than reporting a stop that has not happened
        # yet. Anything starting a daemon behind us reads the pid to decide
        # whether one is already up, and a pid that outlives this call is what
        # makes it decline.
        if not _await_exit(pid):
            print("autocontinue: pid %d did not stop within %ds"
                  % (pid, int(STOP_WAIT_S)), file=sys.stderr)
            return 1
        print(f"autocontinue: stopped (pid {pid})")
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
    # Stamp now rather than waiting for the next poll: a keypress that changes
    # nothing on screen for ten seconds reads as a keypress that did nothing.
    refresh_badge(pane_id, load_walls().get(pane_id), load_armed())
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


# ---- sidebar wiring -------------------------------------------------------

# Kinds whose rows_by_agent override needs the token merged in as well. The
# watch list can be empty (meaning "all kinds"), which would merge into none.
OVERRIDE_KINDS = KINDS or ["claude", "codex", "omp"]
SIDEBAR_TABLE = "[ui.sidebar.agents]"
OVERRIDE_TABLE = "[ui.sidebar.agents.rows_by_agent]"
DEFAULT_ROWS = '[["state_icon", "workspace", "tab"], ["agent"]]'
CONFIG_BACKUP_DIR = os.path.join(STATE_DIR, "config-backups")
NAG_MARKER = os.path.join(STATE_DIR, ".sidebar-nagged")
_HEADER_RE = re.compile(r"\[\[?[^\[\]]+\]\]?\s*(#.*)?$")


def config_path():
    sock = os.environ.get("HERDR_SOCKET_PATH")
    if sock:
        guess = os.path.join(os.path.dirname(sock), "config.toml")
        if os.path.exists(guess):
            return guess
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "herdr", "config.toml")


def _uncommented(text):
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def _token_wired(text):
    return '"${}"'.format(TOKEN) in _uncommented(text)


def _match_brackets(text, i):
    while i < len(text) and text[i] != "[":
        if not text[i].isspace():
            return None
        i += 1
    depth = 0
    while i < len(text):
        if text[i] == "[":
            depth += 1
        elif text[i] == "]":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def _find_value(text, header, key):
    """Offsets of the `key = [...]` array inside `header`, or None.

    Bracket depth is tracked so a row line such as `["agent"],` inside a
    multi-line value is never mistaken for the next table header.
    """
    lines = text.splitlines(keepends=True)
    start = None
    for i, line in enumerate(lines):
        if line.strip() == header:
            start = i
            break
    if start is None:
        return None
    offset = sum(len(l) for l in lines[: start + 1])
    depth = 0
    for line in lines[start + 1 :]:
        if depth == 0:
            if _HEADER_RE.match(line.strip()):
                return None
            match = re.match(r"\s*%s\s*=\s*" % re.escape(key), line)
            if match:
                vstart = offset + match.end()
                vend = _match_brackets(text, vstart)
                return None if vend is None else (vstart, vend)
        depth += line.count("[") - line.count("]")
        offset += len(line)
    return None


def _append_token(value):
    depth = 0
    inner_open = None
    for i, ch in enumerate(value):
        if ch == "[":
            depth += 1
            if depth == 2:
                inner_open = i
        elif ch == "]":
            if depth == 2 and inner_open is not None:
                inner = value[inner_open + 1 : i].strip()
                sep = ", " if inner else ""
                return value[:i].rstrip() + '%s"$%s"' % (sep, TOKEN) + value[i:]
            depth -= 1
    return None


def _drop_token(value):
    """Remove every "$TOKEN" entry from a rows array, or None if absent."""
    quoted = '"$%s"' % TOKEN
    if quoted not in value:
        return None
    out = re.sub(r",\s*" + re.escape(quoted), "", value)
    out = re.sub(re.escape(quoted) + r"\s*,\s*", "", out)
    out = out.replace(quoted, "")
    return out


def _valid_toml(text):
    """False only when tomllib is present and rejects the text."""
    try:
        import tomllib
    except ImportError:
        return True  # too old to check; the caller still keeps a backup
    try:
        tomllib.loads(text)
        return True
    except Exception:
        return False


def _stripped_config(text, kinds):
    """(new text, [what changed]) on success, else (None, reason)."""
    if not _token_wired(text):
        return None, "already"
    out, changed = text, []
    for header, key in (
        [(SIDEBAR_TABLE, "rows")] + [(OVERRIDE_TABLE, k) for k in kinds]
    ):
        span = _find_value(out, header, key)
        if not span:
            continue
        dropped = _drop_token(out[span[0] : span[1]])
        if dropped is None:
            continue
        out = out[: span[0]] + dropped + out[span[1] :]
        changed.append("rows" if key == "rows" else "rows_by_agent.%s" % key)
    if not changed:
        return None, "unparsed"
    return out, changed


def _merged_config(text, kinds):
    """(new text, [what changed]) on success, else (None, reason).

    An override in rows_by_agent replaces rows rather than extending it, so a
    kind listed there needs the token merged into its own layout too.
    """
    if _token_wired(text):
        return None, "already"
    out, changed = text, []
    span = _find_value(out, SIDEBAR_TABLE, "rows")
    if span:
        merged = _append_token(out[span[0] : span[1]])
        if merged is None:
            return None, "unparsed"
        out = out[: span[0]] + merged + out[span[1] :]
        changed.append("rows")
    elif SIDEBAR_TABLE in _uncommented(out):
        return None, "unparsed"
    else:
        block = "%s\nrows = %s\n" % (SIDEBAR_TABLE, _append_token(DEFAULT_ROWS))
        out = out.rstrip("\n") + "\n\n" + block
        changed.append("rows (new table)")
    for kind in kinds:
        span = _find_value(out, OVERRIDE_TABLE, kind)
        if not span:
            continue
        merged = _append_token(out[span[0] : span[1]])
        if merged is None:
            continue
        out = out[: span[0]] + merged + out[span[1] :]
        changed.append("rows_by_agent.%s" % kind)
    return out, changed


def sidebar_snippet():
    return "%s\nrows = %s" % (SIDEBAR_TABLE, _append_token(DEFAULT_ROWS))


def _rewrite_config(edit, kinds, done_word):
    """Apply `edit` to config.toml, backing the original up first.

    Returns (status, where, what changed). The edited text is parsed before it
    replaces anything, so a bad edit is refused rather than written.
    """
    path = config_path()
    if not os.path.exists(path):
        return "missing", path, []
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    out, info = edit(text, kinds)
    if out is None:
        return info, path, []
    if not _valid_toml(out):
        return "unparsed", path, []
    os.makedirs(CONFIG_BACKUP_DIR, exist_ok=True)
    backup = os.path.join(CONFIG_BACKUP_DIR, "config.toml.%d" % int(time.time()))
    with open(backup, "w", encoding="utf-8") as handle:
        handle.write(text)
    tmp = "%s.autocontinue-tmp" % path
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(out)
    os.replace(tmp, path)
    herdr("server", "reload-config")
    return done_word, backup, info


def wire_sidebar(kinds):
    """Merge $TOKEN into the sidebar rows, keeping a backup of the original."""
    return _rewrite_config(_merged_config, kinds, "wired")


def unwire_sidebar(kinds):
    """Remove $TOKEN from the sidebar rows, keeping a backup of the original."""
    return _rewrite_config(_stripped_config, kinds, "unwired")


def warn_if_unwired():
    """A token nothing references renders nothing, and herdr reports no error.

    Startup says so once rather than editing config behind your back.
    """
    try:
        if os.path.exists(NAG_MARKER):
            return
        path = config_path()
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as handle:
            if _token_wired(handle.read()):
                return
        herdr(
            "notification", "show", "Auto-continue: badge not visible",
            "--body",
            "No sidebar row names $%s. Run the enable-badge action to add it."
            % TOKEN,
            "--sound", "none",
        )
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(NAG_MARKER, "w", encoding="utf-8") as handle:
            handle.write("")
    except Exception:
        pass


def cmd_enable_badge(argv):
    status, where, info = wire_sidebar(OVERRIDE_KINDS)
    if status == "wired":
        print("wired $%s into %s (%s)" % (TOKEN, config_path(), ", ".join(info)))
        print("backup: %s" % where)
        print("config reloaded")
    elif status == "already":
        print("$%s is already named in the sidebar rows — nothing to do." % TOKEN)
    elif status == "missing":
        print("no herdr config at %s\n\nadd:\n\n%s" % (where, sidebar_snippet()))
        return 1
    else:
        print(
            "could not edit %s safely — add $%s by hand:\n\n%s"
            % (where, TOKEN, sidebar_snippet())
        )
        return 1
    return 0


def cmd_disable_badge(argv):
    """Stop showing the countdown badge: leave the rows, then clear the panes."""
    status, where, info = unwire_sidebar(OVERRIDE_KINDS)
    agents = live_agents() or {}
    for pane_id in agents:
        clear_badge(pane_id)
    if status == "unwired":
        print("removed $%s from %s (%s)" % (TOKEN, config_path(), ", ".join(info)))
        print("backup: %s" % where)
        print("cleared the badge from %d pane(s); config reloaded" % len(agents))
    elif status == "already":
        print("$%s is not named in the sidebar rows — nothing to remove." % TOKEN)
        print("cleared the badge from %d pane(s)" % len(agents))
    elif status == "missing":
        print("no herdr config at %s" % where)
        return 1
    else:
        print(
            "could not edit %s safely — remove \"$%s\" from the rows by hand"
            % (where, TOKEN)
        )
        return 1
    return 0


DISPATCH = {
    "on-status": cmd_on_status,
    "start": cmd_start,
    "enable-badge": cmd_enable_badge,
    "disable-badge": cmd_disable_badge,
    "stop": cmd_stop,
    "restart": cmd_restart,
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
