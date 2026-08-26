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
    """The profile rotation switched to, or None."""
    chosen = []
    A.subprocess.run = lambda cmd, **k: (
        chosen.append(cmd[-1]) or _Ran(0, "switched"))
    A._save(A.ROTATE_STATE, {})
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

shutil.rmtree(STATE, ignore_errors=True)
print("\n%s — %d of the checks failed"
      % ("FAILED" if FAILED else "PASSED", len(FAILED)))
sys.exit(1 if FAILED else 0)
