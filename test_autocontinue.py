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
    ("main", "Main", True), ("other", "Other", False)]
check("a profile nobody named is not chosen", A.rotate_account("claude") is False)

print("\nrotation respects its cooldown")
A.ROTATE_PROFILES = ["other"]
A._save(A.ROTATE_STATE, {"last_switch": time.time(), "tried": []})
check("a switch just made blocks the next one",
      A.rotate_account("claude") is False)

shutil.rmtree(STATE, ignore_errors=True)
print("\n%s — %d of the checks failed"
      % ("FAILED" if FAILED else "PASSED", len(FAILED)))
sys.exit(1 if FAILED else 0)
