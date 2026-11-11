"""Decides whether a download can be completed, by asking the servers about a sample of its articles.

The servers are used the way NZBGet uses them:

* in the order of their level, and within a level by their number;
* servers that share a group number count as one (NZBGet does not try the
  second when the first says it lacks an article);
* a server whose retention is shorter than the age of the post is left out;
* an optional server that rejects the login is left out (NZBGet skips an
  optional server it cannot connect to).

An article counts as *missing* only when every one of those servers answered
"I do not have it". As long as one of them has not said so, for whatever
reason (it was unreachable, it was slow, time ran out), the article is
*unknown*, and unknown never counts against a download.
"""

import threading
import time

from .nntp import ServerSession
from .nzbfile import spread_order
from .stats import Tally, capacity, judge

# Missing articles are slow to ask about: a server can take a second or more to answer "not here", while "here" comes
# back at once. The first round is small so a post that is clearly gone is settled before the time limit runs out;
# healthy posts simply go on to the next round.
DATA_STAGES = (40, 120, 400, 1200)  # content articles asked about after each round, in total
PAR_STAGES = (20, 40, 120, 300)     # repair articles, asked about only once content turns out to be missing
SMALL = 200                         # a download with this many articles or fewer is checked completely
CONFIRM_PAUSE = 1.5                 # seconds before the articles that looked missing are asked about again
CONFIRM_SAMPLE = 24                 # how many of them are asked about again; spread over the whole list
MIN_ROUND_SECONDS = 4.0


class Probe(object):
    """One article that is being asked about."""

    __slots__ = ("article", "found", "denied")

    def __init__(self, article):
        self.article = article
        self.found = None    # level of the best server that has it; None while nobody has it
        self.denied = set()  # the units that answered "I do not have it"


class Unit(object):
    """One server, or the servers NZBGet treats as one because they share a group number."""

    def __init__(self, index, level):
        self.index = index
        self.level = level
        self.sessions = []

    def ask(self, msgids, groups):
        for session in self.sessions:
            answers = session.ask(msgids, groups)
            if answers or session.reachable:
                return answers
        return {}

    @property
    def retention(self):
        return self.sessions[0].server.retention

    @property
    def dead(self):
        return all(session.reachable is False for session in self.sessions)

    @property
    def reachable(self):
        return any(session.reachable for session in self.sessions)

    @property
    def skipped(self):
        """NZBGet would pass over this unit: every server in it is optional and rejects the login."""
        return all(session.server.optional and session.reachable is False
                   and session.failure is not None and session.failure.kind == "auth"
                   for session in self.sessions)

    def problem(self):
        for session in self.sessions:
            if session.failure is not None:
                return "%s could not be asked: %s" % (session.server.label(), session.failure.describe())
        return "%s could not be asked" % self.sessions[0].server.label()


class Outcome(object):
    """The result of one check."""

    def __init__(self, verdict, reason=""):
        self.verdict = verdict    # pass | fail | suspect | gone | error
        self.reason = reason      # why, for 'error'
        self.cause = "missing"    # for 'fail': 'missing' or 'fill'
        self.checked = 0
        self.population = 0
        self.missing = 0
        self.unknown = 0
        self.lost = 0.0           # estimated share of the content that cannot be downloaded
        self.limit = 0.0          # share that the repair data could replace
        self.fill = 0.0           # share found only on fill servers
        self.fill_limit = 1.0
        self.borderline = False
        self.unreachable = []     # servers that could not be asked, in words
        self.left_out = []        # optional servers that were passed over, in words
        self.servers = []         # one line per server about the work done, for the detailed log
        self.seconds = 0.0
        self.fresh = False        # set by the caller: the post is too recent for a final verdict
        self.found = 0            # articles of the sample that exist on at least one server

    def describe(self):
        sample = "%d of %d articles checked" % (self.checked, self.population)
        if self.verdict == "error":
            return self.reason
        if self.verdict == "gone":
            return "none of the %d articles checked exists on any server, which could also be a fault at the " \
                   "news servers, so it is left to NZBGet" % self.checked
        if self.verdict == "fail" and self.cause == "fill":
            return "%.0f%% of it is only on fill servers, more than the %.0f%% allowed (%s)" % (
                self.fill * 100, self.fill_limit * 100, sample)
        if self.verdict == "fail":
            return "%.1f%% of it is missing on every server and only %.1f%% can be repaired (%s)" % (
                self.lost * 100, self.limit * 100, sample)
        if self.verdict == "suspect":
            return "%.1f%% of it was not found, but %s (%s)" % (
                self.lost * 100, "; ".join(self.unreachable[:3]) or "not every server answered", sample)
        if not self.missing and not self.unknown and self.lost == 0:
            return sample + ", all there"
        text = "%s, %.1f%% missing, up to %.1f%% can be repaired" % (sample, self.lost * 100, self.limit * 100)
        if self.borderline:
            text += "; too close to call, left to NZBGet's own health check"
        return text


def _targets(population, limit, stages):
    """How many articles have been asked about after each round."""
    top = min(population, limit)
    if top <= 0:
        return []
    if population <= SMALL:
        return [population]
    targets = []
    for stage in stages:
        stage = min(stage, top)
        if not targets or stage > targets[-1]:
            targets.append(stage)
    if top > targets[-1]:
        targets.append(top)
    return targets


class Checker(object):
    def __init__(self, nzb, servers, settings, deadline, now, session_factory=None):
        self.nzb = nzb
        self.settings = settings
        self.deadline = deadline
        self.now = now
        self._merge = threading.Lock()
        factory = session_factory or ServerSession
        usable = [server for server in servers
                  if server.active and server.connections > 0 and server.id not in settings.ignore_servers]
        levels = {level: index for index, level in enumerate(sorted({server.level for server in usable}))}
        self.units = []
        groups = {}
        for server in sorted(usable, key=lambda item: (item.level, item.id)):
            # NZBGet treats servers as one only when they share a group number and a level.
            key = (server.level, server.group)
            unit = groups.get(key) if server.group > 0 else None
            if unit is None:
                unit = Unit(len(self.units), levels[server.level])
                self.units.append(unit)
                if server.group > 0:
                    groups[key] = unit
            unit.sessions.append(factory(server, settings.connections, deadline, settings.cert_store))

    def close(self):
        for unit in self.units:
            for session in unit.sessions:
                session.close()

    # ------------------------------------------------------------ asking

    def _eligible(self, unit, probe):
        retention = unit.retention
        posted = probe.article.posted
        return not (retention > 0 and posted > 0 and (self.now - posted) / 86400.0 > retention)

    def _ask_unit(self, unit, probes, again=False):
        wanted = [probe for probe in probes if self._eligible(unit, probe)
                  and (not again or probe.found is None or unit.level < probe.found)]
        if not wanted:
            return
        groups = {probe.article.msgid: probe.article.group for probe in wanted}
        answers = unit.ask([probe.article.msgid for probe in wanted], groups)
        with self._merge:
            for probe in wanted:
                answer = answers.get(probe.article.msgid)
                if answer is True:
                    probe.found = unit.level if probe.found is None else min(probe.found, unit.level)
                elif answer is False:
                    probe.denied.add(unit.index)

    def _ask_all(self, units, probes, again=False):
        workers = [threading.Thread(target=self._ask_unit, args=(unit, probes, again), daemon=True)
                   for unit in units]
        for worker in workers:
            worker.start()
        for worker in workers:
            while worker.is_alive() and not self.deadline.expired():
                worker.join(0.1)

    def locate(self, probes):
        """Find out for each probe which server has the article, if any."""
        if not probes:
            return
        first = next((unit for unit in self.units if not unit.dead), self.units[0])
        self._ask_unit(first, probes)
        with self._merge:
            rest = [probe for probe in probes if probe.found is None]
        others = [unit for unit in self.units if unit is not first]
        if rest and others:
            self._ask_all(others, rest)

    def _reask(self, probes):
        """Ask every server once more about these probes. Returns how many turned up, or None when time ran out."""
        if self.deadline.left() < CONFIRM_PAUSE + 3.0:
            return None
        time.sleep(CONFIRM_PAUSE)
        with self._merge:
            before = [probe.found for probe in probes]
        self._ask_all(self.units, probes, again=True)
        if self.deadline.expired():
            return None
        with self._merge:
            return sum(1 for probe, old in zip(probes, before) if probe.found != old)

    def confirm(self, probes):
        """Ask every server once more about the articles that looked missing or fill-only.

        A server can answer "not here" by mistake for a moment. Each "not here" is slow, so a spread sample of
        CONFIRM_SAMPLE is asked first. Only when one of those turns up after all are the others asked too. Returns how
        many articles turned up, or None when there was no time left to ask.
        """
        if not probes:
            return 0
        if len(probes) <= CONFIRM_SAMPLE:
            return self._reask(probes)
        step = len(probes) / float(CONFIRM_SAMPLE)
        picked = {int(i * step) for i in range(CONFIRM_SAMPLE)}
        sample = [probe for index, probe in enumerate(probes) if index in picked]
        turned_up = self._reask(sample)
        if not turned_up:
            return turned_up
        rest = self._reask([probe for index, probe in enumerate(probes) if index not in picked])
        return None if rest is None else turned_up + rest

    # ------------------------------------------------------------ deciding

    def _count(self, tally, probes):
        """Sort the probes into found, missing for certain, and unknown."""
        missing = unknown = fill = 0
        with self._merge:
            for probe in probes:
                asked = [unit for unit in self.units if self._eligible(unit, probe) and not unit.skipped]
                if probe.found is None:
                    if all(unit.index in probe.denied for unit in asked):
                        missing += 1  # every server NZBGet would try has said no
                    else:
                        unknown += 1
                elif probe.found > 0 and all(unit.index in probe.denied for unit in asked if unit.level == 0):
                    fill += 1         # on a fill server, and every first-level server has said no
        tally.checked = len(probes)
        tally.missing = missing
        tally.unknown = unknown
        tally.fill = fill

    def _doubtful(self, probes):
        with self._merge:
            return [probe for probe in probes if probe.found is None]

    def run(self):
        if not self.units:
            return Outcome("error", "NZBGet has no active news server to ask")
        nzb = self.nzb
        settings = self.settings
        data = Tally(len(nzb.data), nzb.data_gaps)
        par = Tally(len(nzb.par), nzb.par_gaps)
        allowed = nzb.allowed_missing
        data_order = spread_order(len(nzb.data), nzb.seed())
        par_order = spread_order(len(nzb.par), nzb.seed() + 1)
        data_targets = _targets(len(nzb.data), settings.max_articles, DATA_STAGES)
        par_targets = _targets(len(nzb.par), max(PAR_STAGES[-1], settings.max_articles // 4), PAR_STAGES)
        rounds = max(len(data_targets), len(par_targets), 1)
        data_probes, par_probes = [], []
        stage = 0
        while True:
            target = data_targets[min(stage, len(data_targets) - 1)] if data_targets else 0
            batch = [Probe(nzb.data[index]) for index in data_order[len(data_probes):target]]
            self.locate(batch)
            data_probes.extend(batch)
            self._count(data, data_probes)
            if batch and not any(unit.reachable for unit in self.units):
                outcome = Outcome("error", "no news server could be asked")
                outcome.unreachable = [unit.problem() for unit in self.units if unit.dead]
                if outcome.unreachable:
                    outcome.reason += " (%s)" % "; ".join(outcome.unreachable[:3])
                return outcome

            if par_targets and (data.missing or data.unknown or data.gaps):
                target = par_targets[min(stage, len(par_targets) - 1)]
                batch = [Probe(nzb.par[index]) for index in par_order[len(par_probes):target]]
                self.locate(batch)
                par_probes.extend(batch)
                self._count(par, par_probes)

            last = stage >= rounds - 1 or self.deadline.left() < MIN_ROUND_SECONDS
            verdict = judge(data, par, allowed, final=last)
            if verdict == "fail":
                if self.confirm(self._doubtful(data_probes + par_probes)) is None:
                    verdict = "suspect"  # no time to make sure: never fail on a single answer
                else:
                    self._count(data, data_probes)
                    self._count(par, par_probes)
                    verdict = judge(data, par, allowed, final=last)
            if verdict != "more":
                break
            stage += 1

        found = sum(1 for probe in data_probes + par_probes if probe.found is not None)
        if verdict == "fail" and found == 0:
            # Proof of life: when not one article was found anywhere, the servers' "not here" cannot be told
            # apart from a provider that answers nonsense. NZBGet fails a post that is completely gone by itself
            # within seconds, so nothing is lost by leaving this one to it.
            verdict = "gone"
        outcome = Outcome(verdict)
        outcome.found = found
        outcome.limit = capacity(allowed, par.estimate())
        outcome.fill_limit = settings.max_fill
        if verdict == "pass" and settings.max_fill < 1.0 and data.fill_share()[0] > settings.max_fill:
            with self._merge:
                on_fill = [probe for probe in data_probes if probe.found]
            if self.confirm(on_fill) is not None:
                self._count(data, data_probes)
                if data.fill_share()[0] > settings.max_fill:
                    outcome.verdict = "fail"
                    outcome.cause = "fill"
        outcome.checked = data.checked
        outcome.population = data.listed
        outcome.missing = data.missing
        outcome.unknown = data.unknown
        outcome.lost = data.estimate(pessimistic=outcome.verdict == "suspect")
        outcome.fill = float(data.fill) / data.checked if data.checked else 0.0
        outcome.borderline = outcome.verdict == "pass" and outcome.lost > outcome.limit
        outcome.unreachable = [unit.problem() for unit in self.units if unit.dead and not unit.skipped]
        outcome.left_out = [unit.sessions[0].server.label() for unit in self.units if unit.skipped]
        return outcome

    def report(self):
        """One line per server about what it was asked, for the detailed log."""
        lines = []
        for unit in self.units:
            for session in unit.sessions:
                text = "%s: %d questions in %.1f s" % (session.server.label(), session.questions, session.seconds)
                if session.failure is not None:
                    text += ", trouble: " + session.failure.describe()
                lines.append(text)
        return lines


def check(nzb, servers, settings, deadline, now, session_factory=None):
    """Check one parsed nzb against NZBGet's news servers. Never raises for network trouble."""
    started = time.monotonic()
    checker = Checker(nzb, servers, settings, deadline, now, session_factory)
    try:
        outcome = checker.run()
        outcome.servers = checker.report()
    finally:
        checker.close()
    outcome.seconds = time.monotonic() - started
    return outcome
