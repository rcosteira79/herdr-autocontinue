#!/usr/bin/env python3
"""Checks for reset-time parsing, the wake mechanism, and account rotation.

Run with `python3 test_autocontinue.py`. Standard library only, same as the
plugin. The state directory is a temporary one, the herdr CLI is replaced, and
no account is ever switched: the rotation checks stop at the command that would
be run.
"""
import datetime as dt
import json
import os
import shutil
import signal
import sys
import tempfile
import time
from zoneinfo import ZoneInfo

STATE = tempfile.mkdtemp(prefix="autocontinue-test-")
os.environ["HERDR_PLUGIN_STATE_DIR"] = STATE
os.environ["HERDR_PLUGIN_CONFIG_DIR"] = STATE
os.environ["TZ"] = "Europe/Lisbon"
time.tzset()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autocontinue as A  # noqa: E402

FAILED = []
LISBON = ZoneInfo("Europe/Lisbon")


class _PinnedClock:
    """The real time module, with time() pinned to whatever `at` last set.

    `parse_reset` reads the calendar from datetime.now() but takes its "not in
    the past" cutoff from time.time(). Pinning only the first left every check
    leaning on the real date: a reset parsed for the pinned day dropped into
    the real past once that day rolled over, the cutoff discarded it, and the
    checks went red on their own.
    """

    def __init__(self, real):
        self._real = real
        self.pinned = None

    def time(self):
        return self._real.time() if self.pinned is None else self.pinned

    def __getattr__(self, name):
        return getattr(self._real, name)


CLOCK = _PinnedClock(time)
A.time = CLOCK


def check(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name
          + (" — " + detail if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


def at(now):
    """Pin what the module sees as 'now' — a naive local datetime."""
    class Pinned(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is None else now.replace(tzinfo=LISBON).astimezone(tz)
    A.datetime = Pinned
    CLOCK.pinned = now.replace(tzinfo=LISBON).timestamp()


# ---- a herdr command that never answers ----------------------------------
#
# One call with no timeout froze the whole watcher. herdr had exited; something
# it started held the pipe, and the read never returned. The daemon sat in that
# one call for two hours: no badges, no walls, no rotation, nothing logged.

print("\na herdr command that never answers is abandoned")
REAL_BIN, REAL_TIMEOUT = A.HERDR, A.HERDR_TIMEOUT_S
A.HERDR, A.HERDR_TIMEOUT_S = "sleep", 1.0
began = time.time()
res = A.herdr("60")
took = time.time() - began
A.HERDR, A.HERDR_TIMEOUT_S = REAL_BIN, REAL_TIMEOUT
check("it comes back at all", res.returncode != 0, str(res.returncode))
check("and close to the timeout, not the command", took < 15, "%.1fs" % took)

print("\na command that exits while something it started holds the output")
# This is what actually froze the watcher. The shell exits at once; the sleep it
# left behind keeps the command's own stdout open. A pipe is read to end of
# file, so the read waited on the survivor. A file ends when the command does.
A.HERDR, A.HERDR_TIMEOUT_S = "sh", 20.0
began = time.time()
res = A.herdr("-c", "echo done; sleep 30 & exit 0")
took = time.time() - began
A.HERDR, A.HERDR_TIMEOUT_S = REAL_BIN, REAL_TIMEOUT
check("it does not wait for the survivor", took < 5, "%.1fs" % took)
check("and the command's own output is there", res.stdout.strip() == "done",
      repr(res.stdout))

print("\nand a command that answers is unchanged")
A.HERDR = "echo"
res = A.herdr("hello")
A.HERDR = REAL_BIN
check("the output is returned", res.stdout.strip() == "hello", repr(res.stdout))
check("with its exit code", res.returncode == 0, str(res.returncode))

# ---- reset-time parsing across a daylight-saving change ------------------
#
# Lisbon is UTC+1 in August and UTC+0 in February. A reset time read in August
# for a February date must use February's offset, not today's.

print("\na reset time on the far side of a daylight-saving change")
at(dt.datetime(2026, 8, 24, 12, 0))
got, rule = A.parse_reset("Your limit resets Feb 3 at 9am")
want = dt.datetime(2027, 2, 3, 9, 0, tzinfo=LISBON).timestamp()
check("a rule matched", rule is not None, str(rule))
check("it lands on 9am Lisbon time, not 8am", got == want,
      "off by %s minutes" % ((got - want) / 60 if got else "n/a"))

print("\nand the other way round: a summer date read in winter")
at(dt.datetime(2027, 1, 10, 12, 0))
got, _ = A.parse_reset("resets Aug 3 at 9am")
want = dt.datetime(2027, 8, 3, 9, 0, tzinfo=LISBON).timestamp()
check("it lands on 9am Lisbon time", got == want,
      "off by %s minutes" % ((got - want) / 60 if got else "n/a"))

print("\nan explicit zone in the message is still honoured")
# The patterns capture a zone only in parentheses, which is how both CLIs print
# it. Bare "9:00 America/New_York" is read as local time by design.
at(dt.datetime(2026, 8, 24, 12, 0))
got, _ = A.parse_reset("resets at 9:00 (America/New_York)")
want = dt.datetime(2026, 8, 24, 9, 0, tzinfo=ZoneInfo("America/New_York")).timestamp()
check("parsed as New York time, not Lisbon time", got == want,
      "off by %s hours" % ((got - want) / 3600 if got else "n/a"))

print("\na bare clock time that already passed is tomorrow")
at(dt.datetime(2026, 8, 24, 15, 0))
got, _ = A.parse_reset("resets at 9am")
check("it is tomorrow, not this morning",
      got == dt.datetime(2026, 8, 25, 9, 0, tzinfo=LISBON).timestamp(), str(got))

print("\na codex wall names its time without ever saying 'reset'")
at(dt.datetime(2026, 8, 30, 18, 5))
codex_line = ("\u25a0 You've hit your usage limit. Upgrade to Pro "
              "(https://chatgpt.com/explore/pro), visit "
              "https://chatgpt.com/codex/settings/usage to purchase more "
              "credits or try again at 9:29 PM.")
seen = A.find_wall(codex_line, "codex")
check("the wall itself is seen", seen and seen[0] == "codex-hit", str(seen))
got, which = A.parse_reset(codex_line)
want = dt.datetime(2026, 8, 30, 21, 29, tzinfo=LISBON).timestamp()
check("and 'try again at 9:29 PM' is the reset", got == want,
      "%s via %s" % (got, which))

print("\nthe checks do not lean on today's real date")
# parse_reset drops anything already in the past. It reads that "now" from
# time.time(), not from the datetime this harness pins, so a check pinned to
# any earlier day used to rot into a failure the moment the day rolled over.
at(dt.datetime(2020, 5, 10, 15, 0))
got, _ = A.parse_reset("resets at 9am")
check("a reset pinned years back still resolves",
      got == dt.datetime(2020, 5, 11, 9, 0, tzinfo=LISBON).timestamp(), str(got))

A.datetime = dt.datetime
CLOCK.pinned = None

# ---- waking the daemon --------------------------------------------------

print("\nthe status hook wakes a running daemon")
A._wake.clear()
check("the handler installs", A._install_wake_handler())
os.kill(os.getpid(), A.WAKE_SIGNAL)
check("the signal sets the wake flag", A._wake.wait(2))

print("\nthe hook only wakes when an agent stopped")
REAL_WAKE = A.wake_daemon
woken = []
A.wake_daemon = lambda: woken.append(1) or True


def hook(status):
    del woken[:]
    os.environ["HERDR_PLUGIN_EVENT_JSON"] = json.dumps(
        {"data": {"pane_id": "w1:pA", "agent_status": status}})
    rc = A.cmd_on_status([])
    return rc, len(woken)


check("an agent that stopped wakes it", hook("idle") == (0, 1), str(hook("idle")))
check("`done` wakes it too", hook("done") == (0, 1), str(hook("done")))
check("`blocked` wakes it too", hook("blocked") == (0, 1), str(hook("blocked")))
check("an agent that started working does not", hook("working") == (0, 0),
      str(hook("working")))

print("\nthe hook never fails, whatever it is handed")
os.environ["HERDR_PLUGIN_EVENT_JSON"] = "{not json"
check("a malformed payload still exits 0", A.cmd_on_status([]) == 0)
os.environ.pop("HERDR_PLUGIN_EVENT_JSON", None)
check("no payload at all still exits 0", A.cmd_on_status([]) == 0)

print("\nwaking a daemon that is not running is not an error")
A.wake_daemon = REAL_WAKE
try:
    os.remove(A.PIDFILE)
except OSError:
    pass
check("with no daemon it reports False", A.wake_daemon() is False)
with open(A.PIDFILE, "w") as f:
    f.write("999999")          # a pid that cannot be alive
check("with a dead pid it reports False", A.wake_daemon() is False)

# ---- account rotation ---------------------------------------------------

print("\nrotation does not hand its own plugin environment to account-switch")
env = A._switch_env()
check("our state dir is not passed on",
      "HERDR_PLUGIN_STATE_DIR" not in env, str(env.get("HERDR_PLUGIN_STATE_DIR")))
check("our config dir is not passed on", "HERDR_PLUGIN_CONFIG_DIR" not in env)
check("the other plugin is named instead",
      env.get("HERDR_PLUGIN_ID") == A.SWITCH_PLUGIN_ID, str(env.get("HERDR_PLUGIN_ID")))
check("the rest of the environment survives", env.get("PATH") == os.environ.get("PATH"))

print("\nrotation is off until profiles are named")
A.ROTATE_PROFILES = []
check("it declines with an empty list", A.rotate_account("claude") is False)

print("\nrotation only lands on a named profile")
A.ROTATE_PROFILES = ["spare"]
A._switcher_script = lambda: "/nonexistent/switcher.py"
A._switch_profiles = lambda script, kind: [
    {"slug": "main", "label": "Main", "active": True},
    {"slug": "other", "label": "Other", "active": False}]
check("a profile nobody named is not chosen", A.rotate_account("claude") is False)

print("\nrotation respects its cooldown")
A.ROTATE_PROFILES = ["other"]
A._save(A.ROTATE_STATE, {"last_switch": time.time(), "tried": []})
check("a switch just made blocks the next one",
      A.rotate_account("claude") is False)

# ---- the usage cache across a switch -------------------------------------
#
# The account behind a pane changes the moment credentials are replaced, so a
# window read before the switch describes an account nobody is billing any
# more. Serving it is what stamped every wall with the previous account's
# reset for the three minutes the cache lives.


class _Ran:
    """What subprocess.run and herdr() hand back, minus the process."""

    def __init__(self, code=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = code, out, err


def a_successful_switch():
    """Stub every outside call rotation makes, and let it report success."""
    A.ROTATE_PROFILES = ["spare"]
    A._switcher_script = lambda: "/nonexistent/switcher.py"
    A._switch_profiles = lambda script, kind: [
        {"slug": "main", "label": "Main", "active": True},
        {"slug": "spare", "label": "Spare", "active": False}]
    A.subprocess.run = lambda *a, **k: _Ran(0, "claude: switched to Spare")
    A.herdr = lambda *a, **k: _Ran(0)
    A._save(A.ROTATE_STATE, {})


print("\na switch forgets the windows it read from the old account")
REAL_RUN, REAL_HERDR = A.subprocess.run, A.herdr
a_successful_switch()
A._save(A.USAGE_CACHE, {
    "claude": {"fetched_at": time.time(), "tried_at": time.time(),
               "windows": [{"kind": "session", "percent": 100}]},
    "codex": {"fetched_at": time.time(), "tried_at": time.time(),
              "windows": [{"kind": "5h", "percent": 12}]},
})
check("the switch reports success", A.rotate_account("claude") is True)
cache = A._load(A.USAGE_CACHE, {})
check("the switched account's windows are dropped", "claude" not in cache,
      str(sorted(cache)))
check("the other provider's windows are left alone", "codex" in cache,
      str(sorted(cache)))
A.subprocess.run, A.herdr = REAL_RUN, REAL_HERDR

# ---- a wall whose account reopens sooner than it was told -----------------
#
# A wall records its resume time once, when it is first seen. Rotation, or a
# window the account revises, can move the real reopening earlier — and for two
# hours on 26 August nothing went back to look.


def a_wall(resume_in, attempts=0):
    """A waiting wall that resumes `resume_in` seconds from now."""
    now = time.time()
    return {
        "pane_id": "w1:pA", "kind": "claude", "label": "Claude Code",
        "rule": "account:session", "matched": "account window session at 100%",
        "detected_at": now, "reset_at": now + resume_in - A.GRACE_S,
        "resume_at": now + resume_in, "reason": "account",
        "attempts": attempts, "last_attempt": None, "status": "waiting",
    }


def account_reopens_in(seconds):
    A.account_block = lambda kind=None: (time.time() + seconds, "session", 100)


REAL_BLOCK = A.account_block

print("\na wall follows its account to an earlier reopening")
wall = a_wall(3 * 3600 + 26 * 60)          # the 3h26 the badge showed
A._save(A.WALLS, {"w1:pA": wall})
account_reopens_in(2 * 3600 + 3 * 60)      # what the live account really said
moved = A.restamp_wall("w1:pA", wall, "claude")
check("the wall moves earlier", moved["resume_at"] < wall["resume_at"] - 3000,
      "moved by %d minutes" % ((wall["resume_at"] - moved["resume_at"]) / 60))
check("the move is written down, not only returned",
      A.load_walls()["w1:pA"]["resume_at"] == moved["resume_at"])

print("\nand is never pushed out to a later one")
wall = a_wall(600)
A._save(A.WALLS, {"w1:pA": wall})
account_reopens_in(4 * 3600)
kept = A.restamp_wall("w1:pA", wall, "claude")
check("a later reset does not delay the wall",
      kept["resume_at"] == wall["resume_at"], str(kept["resume_at"]))

print("\na wall already in backoff keeps the retry it earned")
wall = a_wall(300, attempts=2)
A._save(A.WALLS, {"w1:pA": wall})
account_reopens_in(60)
kept = A.restamp_wall("w1:pA", wall, "claude")
check("the backoff is left alone", kept["resume_at"] == wall["resume_at"],
      str(kept["resume_at"]))

A.account_block = REAL_BLOCK

# ---- which account rotation reaches for ----------------------------------
#
# Rotation used to take the first name it was allowed to use, which is the
# order the profiles were saved in. That says nothing about which account can
# actually do any work.


def spent(resets_in):
    return [{"label": "session", "percent": 100,
             "resets_at": time.time() + resets_in}]


def candidates(*profiles):
    A._switch_profiles = lambda script, kind: [
        {"slug": "live", "label": "Live", "active": True}] + list(profiles)


def rotation_chose():
    """The profile rotation switched to, or None.

    Rotation also reads the accounts before it chooses, so only the switch call
    is recorded here. The gap is pre-spent, which keeps these checks about the
    ranking of the readings already on hand.
    """
    chosen = []

    def run(cmd, **kwargs):
        if "switch" in cmd:
            chosen.append(cmd[-1])
        return _Ran(0, "switched")

    A.subprocess.run = run
    A._save(A.ROTATE_STATE, {"last_refresh": time.time()})
    A.rotate_account("claude")
    return chosen[0] if chosen else None


REAL_RUN, REAL_HERDR = A.subprocess.run, A.herdr

print("\nrotation reaches for the account that comes back soonest")
a_successful_switch()
A.ROTATE_PROFILES = ["late", "soon"]
now = time.time()
candidates(
    {"slug": "late", "label": "Late", "active": False, "at": now,
     "windows": spent(4 * 3600)},
    {"slug": "soon", "label": "Soon", "active": False, "at": now,
     "windows": spent(30 * 60)},
)
check("the sooner reset beats the older profile", rotation_chose() == "soon",
      str(rotation_chose()))

print("\nan account with capacity beats any reset at all")
a_successful_switch()
A.ROTATE_PROFILES = ["soon", "free"]
candidates(
    {"slug": "soon", "label": "Soon", "active": False, "at": now,
     "windows": spent(30 * 60)},
    {"slug": "free", "label": "Free", "active": False, "at": now,
     "windows": [{"label": "session", "percent": 12,
                  "resets_at": now + 3600}]},
)
check("the account with room left is chosen", rotation_chose() == "free",
      str(rotation_chose()))

print("\nand an unread account is tried before one known to be spent")
a_successful_switch()
A.ROTATE_PROFILES = ["soon", "unread"]
candidates(
    {"slug": "soon", "label": "Soon", "active": False, "at": now,
     "windows": spent(30 * 60)},
    {"slug": "unread", "label": "Unread", "active": False,
     "at": now - 4 * 3600, "windows": spent(30 * 60)},
)
check("the stale reading is tried first", rotation_chose() == "unread",
      str(rotation_chose()))

print("\nwith nothing to rank by, the saved order still decides")
a_successful_switch()
A.ROTATE_PROFILES = ["first", "second"]
candidates(
    {"slug": "first", "label": "First", "active": False},
    {"slug": "second", "label": "Second", "active": False},
)
check("an older account-switch is not a failure",
      rotation_chose() == "first", str(rotation_chose()))

A.subprocess.run, A.herdr = REAL_RUN, REAL_HERDR

# ---- the poll itself -----------------------------------------------------
#
# restamp_wall is only worth anything if the poll calls it before it draws the
# badge. That wiring is the whole difference between a badge that follows the
# account and one that counts down to a time nobody is waiting for any more.

print("\nthe poll re-stamps a wall before it badges it")
badged = {}
REAL_RUN, REAL_HERDR = A.subprocess.run, A.herdr


def record_badge(*args, **kwargs):
    if args[:2] == ("pane", "report-metadata"):
        for i, arg in enumerate(args):
            if arg == "--token":
                badged["token"] = args[i + 1]
    return _Ran(0)


A.herdr = record_badge
A.pane_text = lambda pane_id, lines=None: "Usage limit reached"
A.account_block = lambda kind=None: (time.time() + 2 * 3600 + 3 * 60,
                                     "session", 100)
A.ROTATE_PROFILES = []                     # no switching in this check
A._save(A.ARMED, [])                       # unarmed: badge only, never a prompt
A._save(A.WALLS, {"w1:pA": a_wall(3 * 3600 + 26 * 60)})
# `agent` is the kind herdr reports; the human-readable name is `name`.
A.tick({"w1:pA": {"agent_status": "idle", "agent": "claude",
                  "name": "Claude Code"}}, set())
check("the badge was drawn", "token" in badged, str(badged))
check("it counts down to the account, not to the old stamp",
      badged.get("token", "").endswith("2h03"), str(badged.get("token")))
check("the wall on disk moved too",
      A.load_walls()["w1:pA"]["reason"] == "account (revised)",
      str(A.load_walls().get("w1:pA", {}).get("reason")))
A.subprocess.run, A.herdr = REAL_RUN, REAL_HERDR
A.account_block = REAL_BLOCK

# ---- reading the accounts before choosing one ----------------------------
#
# Ranking is only as good as the readings it ranks. Nothing refreshes them on a
# timer, so by the time a wall appears they are usually hours old and every
# candidate lands in the same band. A rotation is rare and a switch is heavy,
# so it can afford one fresh look — but only when there is a choice to make,
# and only once per gap, or a sweep every minute would hammer the endpoint.


def recorder(fresh_rows):
    """Record every subprocess rotation makes; answer a usage read with rows."""
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if "usage" in cmd:
            return _Ran(0, json.dumps(fresh_rows))
        return _Ran(0, "switched to %s" % cmd[-1])

    return calls, run


def usage_reads(calls):
    return [c for c in calls if "usage" in c]


def switches(calls):
    return [c[-1] for c in calls if "switch" in c]


def stale(slug, label, percent, active=False):
    """A cached reading old enough that nothing can be ranked on it."""
    return {"slug": slug, "label": label, "active": active,
            "at": time.time() - 4 * 3600,
            "windows": [{"label": "session", "percent": percent,
                         "resets_at": time.time() + 3600}]}


def fresh(slug, label, percent):
    return {"kind": "claude", "slug": slug, "label": label, "active": False,
            "state": "live", "at": time.time(),
            "windows": [{"label": "session", "percent": percent,
                         "resets_at": time.time() + 3600}]}


REAL_RUN, REAL_HERDR = A.subprocess.run, A.herdr

print("\nwith one candidate there is nothing to choose, so nothing is read")
a_successful_switch()
A.ROTATE_PROFILES = ["only"]
A._switch_profiles = lambda script, kind: [
    stale("live", "Live", 100, active=True), stale("only", "Only", 100)]
calls, A.subprocess.run = recorder([])
check("it switched", A.rotate_account("claude") is True)
check("the accounts were not read again", usage_reads(calls) == [],
      str(usage_reads(calls)))

print("\nwith a choice to make, it takes one fresh reading first")
a_successful_switch()
A.ROTATE_PROFILES = ["spent", "free"]
A._switch_profiles = lambda script, kind: [
    stale("live", "Live", 100, active=True),
    stale("spent", "Spent", 100), stale("free", "Free", 100)]
calls, A.subprocess.run = recorder([fresh("spent", "Spent", 100),
                                    fresh("free", "Free", 10)])
A.rotate_account("claude")
check("exactly one reading was taken", len(usage_reads(calls)) == 1,
      str(usage_reads(calls)))
check("it asked for the walled kind only",
      usage_reads(calls) and usage_reads(calls)[0][-2:] == ["--kind", "claude"],
      str(usage_reads(calls)))
check("the fresh reading decides, not the saved order",
      switches(calls) == ["free"], str(switches(calls)))

print("\nand it does not read again inside the gap")
A._save(A.ROTATE_STATE, {"last_refresh": time.time()})
calls, A.subprocess.run = recorder([fresh("free", "Free", 10)])
A.rotate_account("claude")
check("a second look is skipped", usage_reads(calls) == [],
      str(usage_reads(calls)))

print("\nthe time of that reading outlives the switch it informed")
a_successful_switch()          # this resets the profile stub, so set it again
A.ROTATE_PROFILES = ["spent", "free"]
A._switch_profiles = lambda script, kind: [
    stale("live", "Live", 100, active=True),
    stale("spent", "Spent", 100), stale("free", "Free", 100)]
calls, A.subprocess.run = recorder([fresh("free", "Free", 10)])
A.rotate_account("claude")
check("last_refresh is still on record",
      A._load(A.ROTATE_STATE, {}).get("last_refresh") is not None,
      str(A._load(A.ROTATE_STATE, {})))
check("and so is the switch it recorded",
      A._load(A.ROTATE_STATE, {}).get("last_switch") is not None)

print("\na reading is trusted only for the kind it was asked about")
# The same profile names exist under both harnesses, and an account-switch too
# old to filter answers with every kind. A codex row must never decide which
# claude account is switched to.
a_successful_switch()
A.ROTATE_PROFILES = ["spent", "free"]
A._switch_profiles = lambda script, kind: [
    stale("live", "Live", 100, active=True),
    stale("spent", "Spent", 100), stale("free", "Free", 100)]
mixed = [fresh("spent", "Spent", 100), fresh("free", "Free", 10)]
mixed[1]["kind"] = "codex"          # the same slug, the wrong harness
calls, A.subprocess.run = recorder(mixed)
A.rotate_account("claude")
check("a row from another kind is not used",
      switches(calls) == ["spent"], str(switches(calls)))

A.subprocess.run, A.herdr = REAL_RUN, REAL_HERDR

# ---- stopping, and starting again straight after -------------------------
#
# `stop` signalled the daemon and returned while it was still alive. A `start`
# behind it then read a live pid, reported "already running", and declined —
# and a moment later the old process exited, leaving nothing watching. A
# restart is the obvious way to pick up new code, and it silently killed the
# watcher instead.

print("\nstop waits for the daemon to actually go")
REAL_ALIVE, REAL_KILL = A._pid_alive, os.kill
alive = {"n": 3}          # alive for three looks, then gone


def dying(pid):
    alive["n"] -= 1
    return alive["n"] > 0


A._pid_alive = dying
os.kill = lambda pid, sig: None
with open(A.PIDFILE, "w") as f:
    f.write("4242")
rc = A.cmd_stop([])
check("it reports success once the process is gone", rc == 0, str(rc))
check("it waited rather than returning on the first look", alive["n"] <= 0,
      str(alive["n"]))

print("\nand says so when it does not go")
A._pid_alive = lambda pid: True
A.STOP_WAIT_S = 0.2
rc = A.cmd_stop([])
check("a process that will not die is an error", rc == 1, str(rc))

print("\nrestart stops and starts inside one process")
order = []
A._pid_alive = REAL_ALIVE
A.cmd_stop = lambda argv: order.append("stop") or 0
A.ensure_daemon = lambda: order.append("start") or True
rc = A.cmd_restart([])
check("it stopped before it started", order == ["stop", "start"], str(order))
check("it reports success", rc == 0, str(rc))

print("\na restart still starts when nothing was running")
order = []
A.cmd_stop = lambda argv: order.append("stop") or 0
A.ensure_daemon = lambda: order.append("start") or True
check("a failed stop does not block the start",
      A.cmd_restart([]) == 0 and order == ["stop", "start"], str(order))

os.kill = REAL_KILL
A._pid_alive = REAL_ALIVE

# ---- going back to an account that reopens sooner ------------------------
#
# Rotation crossed each profile off as it tried it, the account it was leaving
# included, so it could never return to one. Personal walled with its session
# an hour and a half from resetting, rotation moved to Mindera, and Mindera's
# weekly was spent for another twenty hours. Every armed pane then waited on
# the worse of the two accounts overnight.
#
# It goes back, but on the clock rather than at once. Two spent accounts do the
# same amount of work — none — so the switch is worth making when the sooner
# window actually reopens, and not an hour and a half before.


def account(slug, label, reopens_in, active=False, read_ago=0):
    """A profile whose binding window reopens `reopens_in` seconds from now.

    `reopens_in` of None means it has room right now.
    """
    now = time.time()
    windows = ([] if reopens_in is None else
               [{"label": "session", "percent": 100,
                 "resets_at": now + reopens_in}])
    if reopens_in is None:
        windows = [{"label": "session", "percent": 12,
                    "resets_at": now + 3600}]
    return {"slug": slug, "label": label, "active": active,
            "at": now - read_ago, "windows": windows}


REAL_RUN, REAL_HERDR = A.subprocess.run, A.herdr

print("\nit does not move to a spent account before that account reopens")
a_successful_switch()
A.ROTATE_PROFILES = ["personal", "mindera"]
A._switch_profiles = lambda script, kind: [
    account("personal", "Personal", 94 * 60),          # resets in 1h34
    account("mindera", "Mindera", 20 * 3600 + 34 * 60, active=True)]
# It was left behind on an earlier switch, and that must not rule it out.
A._save(A.ROTATE_STATE, {"tried": ["personal", "mindera"],
                         "last_refresh": time.time()})
calls, A.subprocess.run = recorder([])
check("it stays where it is for now", A.rotate_account("claude") is False)
check("and runs no switch", switches(calls) == [], str(switches(calls)))

print("\nand it goes back the moment that window reopens")
a_successful_switch()
A.ROTATE_PROFILES = ["personal", "mindera"]
A._switch_profiles = lambda script, kind: [
    account("personal", "Personal", -60),              # its window just came back
    account("mindera", "Mindera", 20 * 3600 + 34 * 60, active=True)]
A._save(A.ROTATE_STATE, {"tried": ["personal", "mindera"],
                         "last_refresh": time.time()})
calls, A.subprocess.run = recorder([])
check("it switched back", A.rotate_account("claude") is True)
check("and to the sooner account", switches(calls) == ["personal"],
      str(switches(calls)))

print("\nan account nobody could read is not guessed at while the live one is due")
a_successful_switch()
A.ROTATE_PROFILES = ["spare", "mindera"]
A._switch_profiles = lambda script, kind: [
    account("spare", "Spare", 30 * 60, read_ago=4 * 3600),   # nothing current
    account("mindera", "Mindera", 5 * 60, active=True)]      # back in five minutes
A._save(A.ROTATE_STATE, {"last_refresh": time.time()})
calls, A.subprocess.run = recorder([])
check("it waits for the account it is already on",
      A.rotate_account("claude") is False)
check("and runs no switch", switches(calls) == [], str(switches(calls)))

print("\nbut that guess is worth one switch when the live account has hours to run")
a_successful_switch()
A.ROTATE_PROFILES = ["spare", "mindera"]
A._switch_profiles = lambda script, kind: [
    account("spare", "Spare", 30 * 60, read_ago=4 * 3600),
    account("mindera", "Mindera", 6 * 3600, active=True)]
A._save(A.ROTATE_STATE, {"last_refresh": time.time()})
calls, A.subprocess.run = recorder([])
check("it switches to find out", A.rotate_account("claude") is True)
check("the unread account is tried", switches(calls) == ["spare"],
      str(switches(calls)))
check("and the guess is written down",
      "spare" in (A._load(A.ROTATE_STATE, {}).get("guessed") or {}),
      str(A._load(A.ROTATE_STATE, {})))

print("\nand the same guess is not made twice in one dry spell")
A._save(A.ROTATE_STATE, dict(A._load(A.ROTATE_STATE, {}), last_switch=0))
calls, A.subprocess.run = recorder([])
check("it is not tried again", A.rotate_account("claude") is False)
check("and nothing switched", switches(calls) == [], str(switches(calls)))

print("\nbut it stays put when it is already on the best one")
a_successful_switch()
A.ROTATE_PROFILES = ["personal", "mindera"]
A._switch_profiles = lambda script, kind: [
    account("personal", "Personal", 94 * 60, active=True),
    account("mindera", "Mindera", 20 * 3600)]
A._save(A.ROTATE_STATE, {"last_refresh": time.time()})
calls, A.subprocess.run = recorder([])
check("no switch is made", A.rotate_account("claude") is False)
check("and nothing was switched to", switches(calls) == [], str(switches(calls)))

print("\na barely sooner account is not worth the move")
a_successful_switch()
A.ROTATE_PROFILES = ["personal", "mindera"]
A._switch_profiles = lambda script, kind: [
    account("personal", "Personal", 60 * 60 - 30),     # thirty seconds sooner
    account("mindera", "Mindera", 60 * 60, active=True)]
A._save(A.ROTATE_STATE, {"last_refresh": time.time()})
calls, A.subprocess.run = recorder([])
check("it holds its ground", A.rotate_account("claude") is False)

print("\nan account with room beats one that is merely sooner")
a_successful_switch()
A.ROTATE_PROFILES = ["personal", "mindera"]
A._switch_profiles = lambda script, kind: [
    account("personal", "Personal", None),             # has room now
    account("mindera", "Mindera", 60, active=True)]
A._save(A.ROTATE_STATE, {"last_refresh": time.time()})
calls, A.subprocess.run = recorder([])
A.rotate_account("claude")
check("the account with room is chosen", switches(calls) == ["personal"],
      str(switches(calls)))

A.subprocess.run, A.herdr = REAL_RUN, REAL_HERDR

# ---- retrying, and the reset the wall already knows about ----------------
#
# A failed attempt replaced the wall's resume time with a backoff, throwing
# away the reset the account had already reported. A codex pane knew its window
# reopened at 17:27, was prompted at 13:39, and then retried on the 5m/15m/45m
# ladder for three hours — spending every attempt it had long before the window
# opened, and giving up before it ever could have worked.


def walled_pane(reset_in, attempts=0, status="waiting"):
    now = time.time()
    return {
        "pane_id": "w1:pA", "kind": "codex", "label": "code-review-tour",
        "rule": "account:5h", "matched": "account window 5h at 100%",
        "detected_at": now, "reset_at": now + reset_in,
        "resume_at": now + reset_in + A.GRACE_S, "reason": "iso",
        "attempts": attempts, "last_attempt": None, "status": status,
    }


REAL_HERDR = A.herdr
prompts = []
A.herdr = lambda *a, **k: (prompts.append(a) or _Ran(0)) if a[:2] == ("agent", "prompt") else _Ran(0)

print("\na retry never lands before the window reopens")
wall = walled_pane(3 * 3600)          # the account says three hours
A._save(A.WALLS, {"w1:pA": wall})
A.attempt_resume("w1:pA", wall, {"agent_status": "idle"})
after = A.load_walls()["w1:pA"]
check("the next try waits for the reset, not the backoff",
      after["resume_at"] >= after["reset_at"],
      "resume %ds before the reset" % (after["reset_at"] - after["resume_at"]))
check("and clears the grace period too",
      after["resume_at"] >= after["reset_at"] + A.GRACE_S)

print("\nonce the window has opened, the backoff is what spaces the tries")
wall = walled_pane(-600)              # reopened ten minutes ago
A._save(A.WALLS, {"w1:pA": wall})
A.attempt_resume("w1:pA", wall, {"agent_status": "idle"})
after = A.load_walls()["w1:pA"]
gap = after["resume_at"] - time.time()
check("it backs off rather than retrying at once",
      280 < gap < 320, "%ds" % gap)

# ---- a wall that goes away on its own ------------------------------------
#
# The harness clears its own message when the window reopens. The wall was then
# dropped and the pane forgotten — so an armed agent that had stopped mid-task
# sat idle indefinitely, having never been prompted. One armed pane sat idle
# for forty minutes that way.

print("\nan armed pane is prompted when its wall disappears")
del prompts[:]
A._save(A.ARMED, ["w1:pA"])
A._save(A.WALLS, {"w1:pA": walled_pane(3600)})
A.pane_text = lambda pane_id, lines=None: "nothing about limits here"
A.account_block = lambda kind=None: None
A.tick({"w1:pA": {"agent_status": "idle", "agent": "codex"}}, set())
check("it was prompted once", len(prompts) == 1, str(prompts))
check("and the wall is gone", "w1:pA" not in A.load_walls(),
      str(A.load_walls()))

print("\nbut only when it actually stopped")
del prompts[:]
A._save(A.WALLS, {"w1:pA": walled_pane(3600)})
A.tick({"w1:pA": {"agent_status": "working", "agent": "codex"}}, set())
check("an agent that carried on is left alone", prompts == [], str(prompts))

print("\nand only when you armed it")
del prompts[:]
A._save(A.ARMED, [])
A._save(A.WALLS, {"w1:pA": walled_pane(3600)})
A.tick({"w1:pA": {"agent_status": "idle", "agent": "codex"}}, set())
check("an unarmed pane is never typed into", prompts == [], str(prompts))

A.herdr = REAL_HERDR
A.account_block = REAL_BLOCK

# ---- not reaching for an account that cannot work ------------------------
#
# A switch the provider refuses recorded nothing, so the cooldown never
# engaged and the same dead login was tried on every sweep — five times in two
# minutes. When it finally went through, the account it landed on had its own
# session spent, and its row had said so: account-switch reports a profile it
# cannot read as needing a re-login.

print("\na refused switch is not retried on the next sweep")
a_successful_switch()
A.ROTATE_PROFILES = ["spare"]
SAVED_AT = 1787751156


def profiles_with(saved_at):
    return lambda script, kind: [
        {"slug": "main", "label": "Main", "active": True},
        {"slug": "spare", "label": "Spare", "active": False,
         "saved_at": saved_at}]


A._switch_profiles = profiles_with(SAVED_AT)
attempts = []


def refusing(cmd, **kwargs):
    if "switch" in cmd:
        attempts.append(cmd[-1])
        return _Ran(1, "", "codex: Spare was refused — nothing changed; "
                           "log that account in again and save it")
    return _Ran(0, "[]")


A.subprocess.run = refusing
check("the switch is reported as failed", A.rotate_account("claude") is False)
check("one attempt was made", attempts == ["spare"], str(attempts))
check("a second sweep does not try it again",
      A.rotate_account("claude") is False and attempts == ["spare"], str(attempts))
refused = A._load(A.ROTATE_STATE, {}).get("refused") or {}
check("the refusal remembers which saved login was refused",
      (refused.get("spare") or {}).get("saved_at") == SAVED_AT, str(refused))

print("\nand is trusted again the moment that login is saved afresh")
# Logging the account in again and saving it moves saved_at. Nothing else has
# to expire: the profile is unusable exactly as long as it is the same dead
# credential, which is what the refusal was about.
A._switch_profiles = profiles_with(SAVED_AT + 1)
del attempts[:]
A.rotate_account("claude")
check("it is tried once more", attempts == ["spare"], str(attempts))

print("\na refusal that was never a dead login still lets go eventually")
A._switch_profiles = profiles_with(SAVED_AT)
state = A._load(A.ROTATE_STATE, {})
state["refused"] = {"spare": {"saved_at": SAVED_AT,
                              "at": time.time() - A.ROTATE_REFUSED_S - 1}}
state.pop("last_switch", None)
state.pop("last_attempt", None)
A._save(A.ROTATE_STATE, state)
del attempts[:]
A.rotate_account("claude")
check("a stale refusal does not strand it forever", attempts == ["spare"],
      str(attempts))

print("\nan account that cannot be read is not chosen")
a_successful_switch()
A.ROTATE_PROFILES = ["broken", "ok"]
A._switch_profiles = lambda script, kind: [
    {"slug": "live", "label": "Live", "active": True},
    {"slug": "broken", "label": "Broken", "active": False,
     "problem": "needs re-login", "at": time.time(), "windows": []},
    {"slug": "ok", "label": "Ok", "active": False, "at": time.time(),
     "windows": [{"label": "session", "percent": 5,
                  "resets_at": time.time() + 3600}]}]
calls, A.subprocess.run = recorder([])
A.rotate_account("claude")
check("the readable account is chosen", switches(calls) == ["ok"],
      str(switches(calls)))

print("\na pane just prompted is not prompted again by the nudge")
# a_successful_switch replaces herdr, so put the prompt recorder back first.
A.herdr = lambda *a, **k: (
    (prompts.append(a) or _Ran(0)) if a[:2] == ("agent", "prompt") else _Ran(0))
del prompts[:]
A._save(A.ARMED, ["w1:pA"])
wall = walled_pane(3600)
wall["last_attempt"] = time.time()          # a switch prompted it a moment ago
A._save(A.WALLS, {"w1:pA": wall})
A.pane_text = lambda pane_id, lines=None: "nothing about limits here"
A.account_block = lambda kind=None: None
A.tick({"w1:pA": {"agent_status": "idle", "agent": "codex"}}, set())
check("no second prompt", prompts == [], str(prompts))

print("\nbut one that was never prompted still gets its nudge")
del prompts[:]
A._save(A.WALLS, {"w1:pA": walled_pane(3600)})
A.tick({"w1:pA": {"agent_status": "idle", "agent": "codex"}}, set())
check("it is nudged once", len(prompts) == 1, str(prompts))

# ---- a wall outlives the evidence that raised it -------------------------
#
# The message on screen is how a wall is found, not what makes it real. It can
# go for reasons that have nothing to do with the window reopening — a reading
# that failed, a switch that emptied the cache, a prompt drawn over it — and
# an armed pane that loses its wall then has nothing left to fire at the reset.

print("\nan armed pane keeps its wall when the message goes while it is blocked")
del prompts[:]
A._save(A.ARMED, ["w1:pA"])
A._save(A.WALLS, {"w1:pA": walled_pane(3600)})
A.pane_text = lambda pane_id, lines=None: "nothing about limits here"
A.account_block = lambda kind=None: None
A.tick({"w1:pA": {"agent_status": "blocked", "agent": "codex"}}, set())
check("it is not typed into while it waits on you", prompts == [], str(prompts))
check("but the wall is still remembered", "w1:pA" in A.load_walls(),
      str(A.load_walls()))

print("\nand a wall that is not due yet is left on its own clock")
A._save(A.WALLS, {"w1:pA": walled_pane(3600)})
was = A.load_walls()["w1:pA"]["resume_at"]
A.tick({"w1:pA": {"agent_status": "blocked", "agent": "codex"}}, set())
check("blocking does not push a reset that is still ahead",
      A.load_walls()["w1:pA"]["resume_at"] == was,
      str(A.load_walls()["w1:pA"]["resume_at"] - was))

print("\nand the nudge lands once that same pane is idle again")
A.tick({"w1:pA": {"agent_status": "idle", "agent": "codex"}}, set())
check("it is prompted on the later tick", len(prompts) == 1, str(prompts))

print("\ndeferring a wall never pulls its resume time earlier")
A._save(A.WALLS, {"w1:pA": walled_pane(3600)})
was = A.load_walls()["w1:pA"]["resume_at"]
A.defer_wall("w1:pA", time.time() + 60)
check("an hour off stays an hour off",
      A.load_walls()["w1:pA"]["resume_at"] == was,
      str(A.load_walls()["w1:pA"]["resume_at"] - was))

print("\na pane nobody armed still forgets its wall when the message goes")
A._save(A.ARMED, [])
A._save(A.WALLS, {"w1:pA": walled_pane(3600)})
A.tick({"w1:pA": {"agent_status": "idle", "agent": "codex"}}, set())
check("watching alone holds nothing", A.load_walls() == {}, str(A.load_walls()))

# ---- an account nobody could read ----------------------------------------
#
# A switch drops the cached windows on purpose, and the reading that follows
# can be rate limited. Nothing is then known about the account — which is not
# the same answer as an account with room.

print("\na wall stands while its account cannot be read")


class _RateLimited(Exception):
    code = 429


REAL_FETCH = A._fetch_usage
A.account_block = REAL_BLOCK
A._fetch_usage = lambda provider: (_ for _ in ()).throw(_RateLimited())
A._save(A.USAGE_CACHE, {})                  # a switch just emptied it
A._save(A.ARMED, ["w1:pA"])
A._save(A.WALLS, {"w1:pA": walled_pane(3600)})
A.pane_text = lambda pane_id, lines=None: "nothing about limits here"
del prompts[:]
A.tick({"w1:pA": {"agent_status": "idle", "agent": "codex"}}, set())
check("the wall is not cleared", "w1:pA" in A.load_walls(), str(A.load_walls()))
check("and the pane is left alone until the reset", prompts == [], str(prompts))

print("\nbut an account that answers with room does clear it")
A._save(A.USAGE_CACHE, {"codex": {
    "fetched_at": time.time(), "tried_at": time.time(),
    "windows": [{"kind": "5h", "percent": 4, "resets_at": time.time() + 3600}]}})
del prompts[:]
A.tick({"w1:pA": {"agent_status": "idle", "agent": "codex"}}, set())
check("the pane is prompted", len(prompts) == 1, str(prompts))

A._fetch_usage = REAL_FETCH
A.account_block = lambda kind=None: None

# ---- the panes a switch moves out from under -----------------------------
#
# A switch is machine-wide. An armed pane is prompted by the daemon, but a pane
# nobody armed is left holding a wall whose account is gone, and its harness was
# going to restart it against credentials that are no longer installed.

print("\na switch restarts the clock on the walls of the kind it moved")
A._save(A.WALLS, {"w1:pA": dict(walled_pane(3600), attempts=5, status="gaveup"),
                  "w1:pB": dict(walled_pane(3600), kind="claude")})
A.strand_walls("codex")
walls = A.load_walls()
moved = walls["w1:pA"]
check("the kind that moved is marked", moved.get("stranded") is True,
      str(moved))
check("the old account's reset is dropped", moved["reset_at"] is None,
      str(moved["reset_at"]))
check("it is tried again shortly, not hours from now",
      moved["resume_at"] - A.time.time() <= A.BUSY_RETRY_S + 1,
      str(moved["resume_at"] - A.time.time()))
check("the attempts spent on the old account are forgiven",
      moved["attempts"] == 0, str(moved["attempts"]))
check("and one that had given up is back in play",
      moved["status"] == "waiting", moved["status"])
check("the other kind is left alone",
      "stranded" not in walls["w1:pB"] and walls["w1:pB"]["attempts"] == 0,
      str(walls["w1:pB"]))

print("\nan unarmed pane a switch stranded says so instead of counting down")
badged.clear()
REAL_RUN, REAL_HERDR = A.subprocess.run, A.herdr
A.herdr = record_badge
A._save(A.ARMED, [])
A.set_badge("w1:pA", A.load_walls()["w1:pA"], set())
check("it shows the give-up glyph",
      badged.get("token", "").endswith(A.GLYPH_GAVEUP),
      str(badged.get("token")))

print("\nbut an armed one still counts down, because we resume it ourselves")
badged.clear()
A.set_badge("w1:pA", A.load_walls()["w1:pA"], {"w1:pA"})
check("the armed glyph and a countdown stand",
      A.GLYPH_ARMED in badged.get("token", "")
      and not badged.get("token", "").endswith(A.GLYPH_GAVEUP),
      str(badged.get("token")))
A.subprocess.run, A.herdr = REAL_RUN, REAL_HERDR

# ---- finding the settings from outside herdr -----------------------------
#
# herdr exports the config directory to an action it runs. A daemon started from
# a shell inherits none of that, and the state dir it fell back to holds no
# config.toml — so the watcher came up on defaults with an empty profile list,
# which is exactly how rotation is switched off on purpose. It ran that way for
# fourteen hours without a word.

print("\nthe config directory is herdr's own when nothing names one")
saved = os.environ.pop("HERDR_PLUGIN_CONFIG_DIR")
try:
    got = A._plugin_config_dir()
finally:
    os.environ["HERDR_PLUGIN_CONFIG_DIR"] = saved
want = os.path.join(os.environ.get("XDG_CONFIG_HOME") or
                    os.path.expanduser("~/.config"),
                    "herdr", "plugins", "config", A.PLUGIN_ID)
check("it falls back to the plugin's own config dir", got == want, got)
check("and the state dir is not it", got != A.STATE_DIR, got)

print("\nbut what herdr names still wins")
check("the exported directory is used", A._plugin_config_dir() == STATE,
      A._plugin_config_dir())

print("\na pane and its account disagreeing is not enough on its own")
# An account's reading lags a pane that has only just stopped, and a claude pane
# written off that way was never resumed at its real reset two hours later. The
# stranded case exists because a switch happened, so a switch has to have.
A._save(A.ROTATE_STATE, {})
A._save(A.USAGE_CACHE, {"codex": {
    "fetched_at": time.time(), "tried_at": time.time(),
    "windows": [{"kind": "5h", "percent": 3, "resets_at": time.time() + 3600}]}})
A._save(A.ARMED, ["w1:pA"])
A._save(A.WALLS, {"w1:pA": dict(walled_pane(3600),
                                detected_at=time.time() - 600)})
A.pane_text = lambda pane_id, lines=None: "You've hit your usage limit"
A.account_block = REAL_BLOCK
del prompts[:]
A.tick({"w1:pA": {"agent_status": "idle", "agent": "codex"}}, set())
check("with no switch behind it, the wall is left to its own clock",
      prompts == [] and A.load_walls()["w1:pA"]["status"] == "waiting",
      str(prompts) + str(A.load_walls()["w1:pA"]["status"]))

print("\nbut a pane walled right after a switch is not left waiting")
# Those cannot both be about the same account. The session is using credentials
# this account does not have — codex keeps the account it started on — so there
# is no window here to wait out.
A._save(A.ROTATE_STATE, {"switched": {"codex": time.time() - 60}})
A._save(A.USAGE_CACHE, {"codex": {
    "fetched_at": time.time(), "tried_at": time.time(),
    "windows": [{"kind": "5h", "percent": 3, "resets_at": time.time() + 3600}]}})
A._save(A.ARMED, ["w1:pA"])
A._save(A.WALLS, {"w1:pA": dict(walled_pane(3600),
                                detected_at=time.time() - 600)})
A.pane_text = lambda pane_id, lines=None: "You've hit your usage limit"
A.account_block = REAL_BLOCK
del prompts[:]
A.tick({"w1:pA": {"agent_status": "idle", "agent": "codex"}}, set())
check("it is prompted now, not in an hour", len(prompts) == 1, str(prompts))
del prompts[:]
A.tick({"w1:pA": {"agent_status": "idle", "agent": "codex"}}, set())
check("the next sweep leaves it alone", prompts == [], str(prompts))

print("\nthe prompt's answer is taken from the next sweep, not two minutes on")
A._save(A.WALLS, {"w1:pA": dict(walled_pane(3600), attempts=1,
                                detected_at=time.time() - 300,
                                last_attempt=time.time() - 61)})
del prompts[:]
A.tick({"w1:pA": {"agent_status": "idle", "agent": "codex"}}, set())
check("one sweep after the prompt is enough to conclude",
      A.load_walls()["w1:pA"].get("stranded_noted") is True,
      str(A.load_walls()["w1:pA"]))

print("\nbut not in the same sweep as the prompt")
A._save(A.WALLS, {"w1:pA": dict(walled_pane(3600), attempts=1,
                                detected_at=time.time() - 300,
                                last_attempt=time.time() - 5)})
A.tick({"w1:pA": {"agent_status": "idle", "agent": "codex"}}, set())
check("a prompt just sent is still given its chance",
      A.load_walls()["w1:pA"].get("stranded_noted") is None,
      str(A.load_walls()["w1:pA"]))

print("\na kind that cannot be restarted keeps its wall and its clock")
# Giving up is only right where restarting replaces it. On a kind this cannot
# restart, giving up removes the one thing left that works — the resume at the
# wall's own reset. A claude pane written off that way carried on by itself a
# minute later, at its real reopening.
A._save(A.ROTATE_STATE, {"switched": {"claude": time.time() - 60}})
A._save(A.USAGE_CACHE, {"claude": {
    "fetched_at": time.time(), "tried_at": time.time(),
    "windows": [{"kind": "session", "percent": 4,
                 "resets_at": time.time() + 3600}]}})
A._save(A.WALLS, {"w1:pA": dict(walled_pane(3600), kind="claude", attempts=1,
                                detected_at=time.time() - 600,
                                last_attempt=time.time() - 300)})
A.pane_text = lambda pane_id, lines=None: "Claude usage limit reached"
A.tick({"w1:pA": {"agent_status": "idle", "agent": "claude"}}, set())
kept = A.load_walls()["w1:pA"]
check("it is not written off", kept["status"] == "waiting", kept["status"])
check("and it is only said once", kept.get("stranded_noted") is True,
      str(kept.get("stranded_noted")))

print("\nand a session that cannot use its account is said so, once")
# Prompted, codex printed the same limit straight back, naming the window of an
# account it had already left. Saying so does not write the pane off: its own
# reset is still the thing that will free it.
A._save(A.ROTATE_STATE, {"switched": {"codex": time.time() - 60}})
A._save(A.USAGE_CACHE, {"codex": {
    "fetched_at": time.time(), "tried_at": time.time(),
    "windows": [{"kind": "5h", "percent": 3, "resets_at": time.time() + 3600}]}})
A._save(A.WALLS, {"w1:pA": dict(walled_pane(3600), attempts=1,
                                detected_at=time.time() - 600,
                                last_attempt=time.time() - 600)})
A.pane_text = lambda pane_id, lines=None: "You've hit your usage limit"
del prompts[:]
A.tick({"w1:pA": {"agent_status": "idle", "agent": "codex"}}, set())
noted = A.load_walls()["w1:pA"]
check("it keeps its wall and its clock", noted["status"] == "waiting",
      str(noted))
check("it is said once", noted.get("stranded_noted") is True, str(noted))
check("and it is not prompted again for it", prompts == [], str(prompts))

# ---- restarting a session that cannot use the new account ----------------
#
# codex keeps the account it started on, so the only way it picks up a switch is
# to be started again. The pane never moves: `herdr agent start --pane` runs the
# agent in the pane it is given, which is what keeps the arming attached.

CODEX_INFO = {"agent_status": "idle", "agent": "codex", "name": "mindera-people",
              "agent_session": {"kind": "id", "value": "01a04a49-c4cb-7dd0"}}


def restart_calls(quits=True):
    """Record what a restart runs; answer the shell wait as `quits` says."""
    calls = []

    def fake(*args, **kwargs):
        calls.append(args)
        return _Ran(0)

    A.herdr = fake
    A.live_agents = lambda: ({} if quits else {"w1:pA": CODEX_INFO})
    return calls


REAL_AGENTS = A.live_agents
A.RESTART_WAIT_S = 2.0

print("\na restart quits the session, starts it again, and prompts it")
calls = restart_calls()
ok = A.restart_session("w1:pA", CODEX_INFO, "codex")
check("it reports success", ok is True)
check("it quits with the configured command",
      calls[0][:4] == ("agent", "prompt", "w1:pA", "/exit"), str(calls[0]))
check("it starts the same pane again, resuming that session",
      calls[1][:2] == ("agent", "start") and "--pane" in calls[1]
      and calls[1][calls[1].index("--pane") + 1] == "w1:pA"
      and "01a04a49-c4cb-7dd0" in calls[1], str(calls[1]))
check("and the resumed session gets one continue",
      calls[2][:4] == ("agent", "prompt", "w1:pA", "continue"), str(calls[2]))

print("\nherdr reporting the pane as unknown is codex being gone too")
calls = restart_calls()
A.live_agents = lambda: {"w1:pA": dict(CODEX_INFO, agent_status="unknown")}
ok = A.restart_session("w1:pA", CODEX_INFO, "codex")
check("it starts the session again", ok is True)
check("without waiting the pane out",
      [c[:2] for c in calls].count(("agent", "start")) == 1, str(calls))

print("\na session that will not quit is left where it is")
calls = restart_calls(quits=False)
began = time.time()
ok = A.restart_session("w1:pA", CODEX_INFO, "codex")
check("it reports failure", ok is False)
check("nothing was started over it",
      not [c for c in calls if c[:2] == ("agent", "start")], str(calls))
check("and it gave up waiting rather than hanging",
      time.time() - began < 10, "%.1fs" % (time.time() - began))

print("\na pane with no session to resume is not touched")
calls = restart_calls()
ok = A.restart_session("w1:pA", {"agent": "codex", "name": "x"}, "codex")
check("it reports failure", ok is False)
check("and nothing was sent to it", calls == [], str(calls))

print("\none restart stands for a while, so a pane is not cycled")
A._save(A.RESTARTS, {"w1:pA": time.time()})
check("a pane just restarted is not restarted again",
      A.restarted_recently("w1:pA", time.time()) is True)
A._save(A.RESTARTS, {"w1:pA": time.time() - A.RESTART_GAP_S - 1})
check("and an old one no longer counts",
      A.restarted_recently("w1:pA", time.time()) is False)

print("\nand the restart is its own switch, not something arming turned on")
tried = []
REAL_RESTART = A.restart_session
A.restart_session = lambda pane_id, info, kind: tried.append(pane_id) or True
A._save(A.USAGE_CACHE, {"codex": {
    "fetched_at": time.time(), "tried_at": time.time(),
    "windows": [{"kind": "5h", "percent": 3, "resets_at": time.time() + 3600}]}})
A.account_block = REAL_BLOCK
A._save(A.ARMED, ["w1:pA"])
A._save(A.RESTARTS, {})
A.pane_text = lambda pane_id, lines=None: "You've hit your usage limit"


A._save(A.ROTATE_STATE, {"switched": {"codex": time.time() - 60}})


def a_stranded_wall(status="waiting"):
    A._save(A.WALLS, {"w1:pA": dict(
        walled_pane(3600), attempts=1, status=status,
        detected_at=time.time() - 600, last_attempt=time.time() - 600)})


A.RESTART_STRANDED = False
a_stranded_wall()
A.tick({"w1:pA": CODEX_INFO}, set())
check("off, it only says what happened", tried == [], str(tried))
check("and the wall keeps its clock",
      A.load_walls()["w1:pA"].get("stranded_noted") is True
      and A.load_walls()["w1:pA"]["status"] == "waiting",
      str(A.load_walls()["w1:pA"]))

A.RESTART_STRANDED = True
a_stranded_wall()
A.tick({"w1:pA": CODEX_INFO}, set())
check("on, the session is restarted", tried == ["w1:pA"], str(tried))
check("and the wall goes with it", "w1:pA" not in A.load_walls(),
      str(A.load_walls()))

print("\nswitching it on reaches the pane that had already given up")
del tried[:]
A._save(A.RESTARTS, {})
a_stranded_wall(status="gaveup")
A.tick({"w1:pA": CODEX_INFO}, set())
check("a wall that gave up is restarted too", tried == ["w1:pA"], str(tried))

print("\na restart that fails is not tried again on the next sweep")
A.restart_session = lambda pane_id, info, kind: False
A._save(A.RESTARTS, {})
a_stranded_wall(status="gaveup")
A.tick({"w1:pA": CODEX_INFO}, set())
check("the attempt is written down even though it failed",
      A.restarted_recently("w1:pA", time.time()) is True,
      str(A._load(A.RESTARTS, {})))

A.restart_session = REAL_RESTART
A.RESTART_STRANDED = False
A.live_agents = REAL_AGENTS

A.subprocess.run, A.herdr = REAL_RUN, REAL_HERDR
A.account_block = REAL_BLOCK

shutil.rmtree(STATE, ignore_errors=True)
print("\n%s — %d of the checks failed"
      % ("FAILED" if FAILED else "PASSED", len(FAILED)))
sys.exit(1 if FAILED else 0)
