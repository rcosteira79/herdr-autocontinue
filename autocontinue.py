#!/usr/bin/env python3
"""Auto-continue herdr agents that stalled on a usage-limit wall.

When Claude Code or Codex burns through a usage window it prints the wall and
stops — "5-hour limit reached ∙ resets 12pm", "You've hit your usage limit, try
again in 4 hours 12 minutes". The reset time is right there in the message, so
a poll loop can read it, sit out the window, and prod the agent when it opens.

Two halves:

  detect   every claude/codex pane is read each poll; a pane showing a wall
           gets a countdown badge ($wall) whether or not you armed it. Free
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
import platform
import subprocess
import sys
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
# Polls a pane must stay missing before its arming is dropped.
ABSENT_POLLS = _num("AUTOCONTINUE_ABSENT_POLLS", 3, int)
DRY_RUN = _flag("AUTOCONTINUE_DRY_RUN")
IS_MAC = platform.system() == "Darwin"

# Empty means "every kind herdr detects". Name kinds here to narrow it.
KINDS = [
    k.strip().lower()
    for k in os.environ.get("AUTOCONTINUE_KINDS", "").split(",")
    if k.strip()
]

# Not "limit": senna-lang/herdr-agent-usage already writes a $limit token, and
# two plugins writing one token would fight over it.
TOKEN = "wall"
GLYPH_ARMED = os.environ.get("AUTOCONTINUE_GLYPH_ARMED") or (
    "\N{ANTICLOCKWISE DOWNWARDS AND UPWARDS OPEN CIRCLE ARROWS}"  # 🔄 will resume
)
GLYPH_IDLE = os.environ.get("AUTOCONTINUE_GLYPH_SEEN") or (
    "\N{DOUBLE VERTICAL BAR}"                     # ⏸ seen, not armed
)
GLYPH_GAVEUP = os.environ.get("AUTOCONTINUE_GLYPH_GAVEUP") or (
    "\N{WARNING SIGN}"                            # ⚠ gave up
)
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
        raw = os.environ.get(
            "AUTOCONTINUE_%s_KINDS" % provider.upper(), default)
        for kind in raw.split(","):
            if kind.strip():
                out[kind.strip().lower()] = provider
    return out


KIND_PROVIDER = _kind_providers()
# Every kind that has an account behind it.
ACCOUNT_KINDS = sorted(KIND_PROVIDER)
# A window at or above this percent counts as spent. Severity strings other
# than "normal" are logged rather than trusted: the only value seen in the
# wild so far is "normal", so a guessed name could block on a mere warning.
ACCOUNT_PERCENT = _num("AUTOCONTINUE_ACCOUNT_PERCENT", 100.0)
ACCOUNT_SEVERITIES = [
    s.strip().lower()
    for s in os.environ.get("AUTOCONTINUE_ACCOUNT_SEVERITIES", "").split(",")
    if s.strip()
]


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


def account_reset_for(kind):
    """The soonest reset that kind's account knows about, for a blank time."""
    provider = KIND_PROVIDER.get(kind)
    if not provider:
        return None
    times = [w["resets_at"] for w in usage_windows(provider) if w.get("resets_at")]
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


def _backoff(attempts):
    return min(300 * (3 ** max(0, attempts - 1)), 3600)


# ---- account rotation -----------------------------------------------------
#
# When the account is spent, a second account with capacity left can take over.
# Switching credentials is machine-wide, so this only ever lands on a profile
# you named, and only when a pane you armed is the one that is stuck.
#
# There is no way to ask a parked account whether it has capacity: only the live
# account keeps a fresh token, so a saved snapshot cannot be queried. Rotation
# therefore switches and watches. If the new account is spent too, the wall
# comes back and the next profile is tried, up to one pass over the list.

ROTATE_PROFILES = [
    p.strip().lower()
    for p in os.environ.get("AUTOCONTINUE_ROTATE_PROFILES", "").split(",")
    if p.strip()
]
ROTATE_COOLDOWN_S = _num("AUTOCONTINUE_ROTATE_COOLDOWN_S", 300.0)
ROTATE_STATE = os.path.join(STATE_DIR, "rotate.json")
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


def _switch_profiles(script, kind):
    """[(slug, label, is_live)] for a kind, from account-switch's own status."""
    res = subprocess.run(
        ["python3", script, "list", "--kind", kind, "--json"],
        capture_output=True, text=True, timeout=30,
    )
    if res.returncode != 0:
        return []
    try:
        data = json.loads(res.stdout or "[]")
    except ValueError:
        return []
    return [
        (p.get("slug"), p.get("label"), bool(p.get("active")))
        for p in data if p.get("slug")
    ]


def rotate_account(kind):
    """Switch to the next allowed profile with capacity. True when it switched.

    Returns False when rotation is off, unavailable, on cooldown, or every
    named profile has already been tried since the account ran out.
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
    live = next((slug for slug, _, active in profiles if active), None)
    tried = set(state.get("tried") or [])
    if live:
        tried.add(live)
    allowed = [
        slug for slug, label, _ in profiles
        if slug not in tried
        and (slug.lower() in ROTATE_PROFILES or (label or "").lower() in ROTATE_PROFILES)
    ]
    if not allowed:
        return False
    target = allowed[0]
    res = subprocess.run(
        ["python3", script, "switch", kind, target],
        capture_output=True, text=True, timeout=60,
    )
    if res.returncode != 0:
        log("rotate: switch to %s failed: %s"
            % (target, (res.stderr or "").strip()[:200]))
        return False
    tried.add(target)
    _save(ROTATE_STATE, {"last_switch": now, "tried": sorted(tried)})
    log("rotate: %s -> %s (%s)" % (kind, target, (res.stdout or "").strip()[:120]))
    herdr("notification", "show", "Auto-continue switched account",
          "--body", (res.stdout or target).strip()[:200], "--sound", "none")
    return True


def clear_rotation_state():
    """Called once the account has capacity again, so the next dry spell starts
    from a full list rather than one already crossed off."""
    if _load(ROTATE_STATE, {}).get("tried"):
        _save(ROTATE_STATE, {})
        log("rotate: account has capacity again, profile list reset")


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

    # Rotation only ever moves claude accounts, so it is that account's
    # recovery that resets the list.
    if ROTATE_PROFILES and not account_block("claude"):
        clear_rotation_state()

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
        if hit is None:
            if wall:
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

        set_badge(pane_id, wall, armed)

        # Rotation: only for a pane you armed, and only while the account it
        # bills to is spent. A switch is machine-wide, so an unarmed pane never
        # triggers one.
        if (pane_id in armed and wall["status"] == "waiting"
                and kind in ACCOUNT_KINDS and account_block(kind)
                and rotate_account(kind)):
            # The account we just moved to may be spent as well. Prompting is
            # how that shows: the wall returns and the next profile is tried.
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
    warn_if_unwired()
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
    "start": cmd_start,
    "enable-badge": cmd_enable_badge,
    "disable-badge": cmd_disable_badge,
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
