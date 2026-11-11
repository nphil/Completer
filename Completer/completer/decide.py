"""What to do with a verdict: one plain table, no side effects, so every line can be tested alone.

``held`` means Completer is holding the download right now (it carries Completer's
token, its state says pending or waiting, and NZBGet reports it paused).
``fresh`` means the post is so recent that articles may simply not have reached
every server yet. ``last`` means no more waiting is allowed for this download.

====================  =====  ==================================================
verdict               held   action
====================  =====  ==================================================
pass                  yes    release, state passed
pass                  no     nothing
fail (or gone)        yes    wait and check again, while fresh and not last
fail (or gone)        no     nothing, while fresh and not last
fail                  any    fail it, otherwise
gone                  yes    release, otherwise (NZBGet fails it by itself)
suspect               yes    wait 5 and then 15 minutes, up to 3 checks
suspect or error      yes    release, state released (fail open)
anything else         no     nothing
====================  =====  ==================================================

Nothing here can fail a download that is not certain to be unrepairable:
``suspect``, ``gone`` and ``error`` never lead to a failure.
"""

from . import settings as cfg

MAX_CHECKS = 8                       # no download is ever checked more often than this
FRESH_DELAYS = (600, 1200, 2400, 3600)  # seconds before the next check of a fresh post that does not pass
SUSPECT_DELAYS = (300, 900)          # seconds before the next check when some server could not be asked
SUSPECT_CHECKS = 3                   # a download is let through after this many doubtful checks
HOLD_EXTRA = 3600                    # a hold ends at the latest this long after the fresh window

RELEASE = "release"
WAIT = "wait"
FAIL = "fail"
NOTHING = "nothing"


class Decision(object):
    def __init__(self, action, state="", delay=0):
        self.action = action
        self.state = state  # the state to record, "" to leave it as it is
        self.delay = delay  # seconds until the next check, for WAIT


def _delay(table, checks):
    return table[min(max(checks, 1), len(table)) - 1]


def is_last(checks, since, now, fresh_seconds):
    """No more waiting: too many checks so far, or held for longer than the fresh window plus an hour."""
    if checks >= MAX_CHECKS:
        return True
    return since > 0 and now - since > fresh_seconds + HOLD_EXTRA


def decide(verdict, held, fresh, last, checks):
    """The action for a verdict. ``checks`` counts the check that has just been done."""
    if verdict == "pass":
        return Decision(RELEASE, cfg.PASSED) if held else Decision(NOTHING)
    if verdict in ("fail", "gone"):
        if fresh and not last:
            return Decision(WAIT, cfg.WAITING, _delay(FRESH_DELAYS, checks)) if held else Decision(NOTHING)
        if verdict == "fail":
            return Decision(FAIL, cfg.FAILED)
        return Decision(RELEASE, cfg.RELEASED) if held else Decision(NOTHING)
    if verdict == "suspect" and held and checks < SUSPECT_CHECKS and not last:
        return Decision(WAIT, cfg.WAITING, _delay(SUSPECT_DELAYS, checks))
    return Decision(RELEASE, cfg.RELEASED) if held else Decision(NOTHING)
