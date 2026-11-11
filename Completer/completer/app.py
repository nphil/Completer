"""The program NZBGet starts: it works out why it was started, does that one thing, and never more.

NZBGet starts the extension in four ways, told apart by the environment:

* as a scan script, when a new nzb arrives (``NZBNP_FILENAME``). It runs for a
  few milliseconds, asks nobody anything, and may put that one download on hold;
* as a queue script on the event NZB_ADDED (``NZBNA_EVENT``): it checks that one
  download with the nzb file NZBGet names for it, and acts on the verdict;
* as a scheduled task (``NZBSP_TASKID``) every five minutes: the safety net
  that checks, rechecks and releases whatever is still held;
* as a command button on the settings page (``NZBCP_COMMAND``).

Everything Completer changes in NZBGet is the download it was started for, or a
download that carries Completer's own token, and each change is verified
immediately beforehand (see :mod:`completer.rpc`). Whenever anything goes wrong
a held download is let go, never failed.
"""

import binascii
import os
import signal
import sys
import threading
import time
import traceback
import zlib

from . import __version__, checker, log, nzbfile
from . import settings as cfg
from .decide import FAIL, RELEASE, WAIT, decide, is_last
from .locks import SWEEP, Heartbeat, Locks, Unfinished, folder
from .nntp import Deadline
from .rpc import QueueGuard, Refused, Rpc, RpcError, Target, params_of

COMMAND_OK = 93     # what NZBGet reads as "the command worked"
COMMAND_ERROR = 94
GRACE = 12          # seconds after its budget before a run is stopped from outside
SWEEP_SECONDS = 240
COMMAND_SECONDS = 100
SCAN_SECONDS = 30
PENDING_GRACE = 90      # a pending download is left to its queue script this long
PENDING_LIMIT = 1800    # and is let go when no check has happened after this long
SAFETY_SECONDS = 4      # of a run's budget, kept for acting on the verdict
FRESH_EDGE = 30         # a download that waits is checked again this long after its fresh window ended


def _number(text, default=0.0):
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return default


def file_sum(path):
    """Size and checksum of a file, to recognise the nzb that was scanned."""
    size = 0
    crc = 0
    with open(path, "rb") as handle:
        while True:
            block = handle.read(1 << 20)
            if not block:
                break
            size += len(block)
            if size > nzbfile.MAX_BYTES:
                raise nzbfile.NzbError("the nzb file is too large")
            crc = zlib.crc32(block, crc)
    return "%d-%08x" % (size, crc & 0xFFFFFFFF)


class Subject(object):
    """A download as Completer knows it: NZBGet's id and name, and what Completer keeps on it."""

    def __init__(self, nzb_id, name, params, category=""):
        self.nzb_id = nzb_id if type(nzb_id) is int else 0
        self.name = log.clean(name or "", 120)
        self.category = category or ""
        self.token = params.get(cfg.TOKEN, "")
        self.state = params.get(cfg.STATE, "")
        self.checks = int(_number(params.get(cfg.CHECKS)))
        self.since = _number(params.get(cfg.SINCE))
        self.next = _number(params.get(cfg.NEXT))
        self.file = params.get(cfg.FILE, "")
        self.sum = params.get(cfg.SUM, "")
        self.raw_name = name or ""

    @classmethod
    def from_env(cls, env):
        params = {}
        for name in cfg.PARAMETERS:
            value = cfg.parameter(env, name, None)
            if value is not None:
                params[name] = value
        number = int(_number(env.get("NZBNA_NZBID")))
        return cls(number, env.get("NZBNA_NZBNAME", ""), params, env.get("NZBNA_CATEGORY", ""))

    @classmethod
    def from_entry(cls, entry):
        return cls(entry.get("NZBID"), entry.get("NZBName") or entry.get("Name") or "",
                   params_of(entry), entry.get("Category", ""))

    def target(self):
        return Target(self.nzb_id, self.token, self.raw_name)

    @property
    def held_state(self):
        return bool(self.token) and self.state in cfg.HELD


class Watchdog(object):
    """Ends the process when a run takes much longer than its budget, whatever it is stuck on."""

    def __init__(self, seconds, action):
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._wait, args=(seconds, action), daemon=True)

    def _wait(self, seconds, action):
        if not self._done.wait(seconds):
            action()

    def start(self):
        self._thread.start()

    def stop(self):
        self._done.set()


class Run(object):
    def __init__(self, env, clock=time.time, hard_exit=os._exit):
        self.env = env
        self.clock = clock
        self.hard_exit = hard_exit
        self.settings = cfg.Settings(env)
        self.began = time.monotonic()
        self.stopping = False
        self.kind = self._kind()
        directory = folder(self.settings.temp_dir)
        self.locks = Locks(directory)
        self.heartbeat = Heartbeat(directory)
        self.unfinished = Unfinished(directory)
        self._deadlines = []
        self._guard = None
        self._servers = None
        self.budget = self._budget()
        self.limit = self._deadline(self.budget)

    # ------------------------------------------------------------ plumbing

    def _kind(self):
        env = self.env
        if env.get("NZBCP_COMMAND"):
            return "command"
        if "NZBNP_FILENAME" in env:
            return "scan"
        if env.get("NZBNA_EVENT"):
            return "queue"
        if "NZBSP_TASKID" in env:
            return "sweep"
        return ""

    def _budget(self):
        return {"scan": SCAN_SECONDS, "queue": self.settings.time_limit, "sweep": SWEEP_SECONDS,
                "command": COMMAND_SECONDS}.get(self.kind, 10)

    def _deadline(self, seconds):
        deadline = Deadline(seconds)
        self._deadlines.append(deadline)
        return deadline

    def _on_signal(self, _number, _frame):
        self.stopping = True
        for deadline in list(self._deadlines):
            deadline.cut()

    def guard(self):
        if self._guard is None:
            self._guard = QueueGuard(Rpc(cfg.Control(self.env)))
        return self._guard

    def servers(self):
        if self._servers is None:
            self._servers = cfg.load_servers(self.env)
        return self._servers

    def usable_servers(self):
        return [server for server in self.servers()
                if server.active and server.connections > 0 and server.id not in self.settings.ignore_servers]

    def seconds_left(self, reserve=SAFETY_SECONDS):
        return self.limit.left() - reserve

    def say(self, subject, text):
        log.info("Completer: %s: %s" % (subject.name or subject.nzb_id, text))

    def note(self, text):
        if self.settings.verbose:
            log.detail("Completer: " + text)

    def go(self):
        """Do what NZBGet started us for. Returns the exit code; never raises."""
        failed = COMMAND_ERROR if self.kind == "command" else 1
        if not self.kind:
            log.error("Completer is started by NZBGet (as a scan script, a queue script, a scheduled task or "
                      "from the settings page); there is nothing to do when it is started by hand")
            return 2
        dog = Watchdog(self.budget + GRACE, lambda: self._give_up(failed))
        dog.start()
        saved = self._catch_signals()
        try:
            handler = {"scan": self.scan, "queue": self.queue, "sweep": self.sweep,
                       "command": self.command}[self.kind]
            return handler()
        except Exception as error:  # nothing may reach NZBGet: what is held is let go by the scheduled check
            frames = traceback.extract_tb(error.__traceback__)
            where = "%s:%d" % (os.path.basename(frames[-1][0]), frames[-1][1]) if frames else "?"
            log.error("Completer stopped because of an unexpected problem (%s at %s); nothing was changed beyond "
                      "this point" % (type(error).__name__, where))
            return failed
        finally:
            dog.stop()
            self.locks.release_all()
            self._restore_signals(saved)

    def _give_up(self, code):
        log.error("Completer ran out of time and was stopped; a download that is on hold is let go by the "
                  "scheduled check")
        self.hard_exit(code)

    def _catch_signals(self):
        saved = {}
        if threading.current_thread() is not threading.main_thread():
            return saved
        for name in ("SIGTERM", "SIGINT"):
            number = getattr(signal, name, None)
            if number is None:
                continue
            try:
                saved[number] = signal.signal(number, self._on_signal)
            except (ValueError, OSError):
                pass
        return saved

    def _restore_signals(self, saved):
        for number, handler in saved.items():
            try:
                signal.signal(number, handler)
            except (ValueError, OSError, TypeError):
                pass

    # ------------------------------------------------------------ scan

    def scan(self):
        """A new nzb is arriving: put exactly this one on hold, so the queue script can check it first."""
        settings = self.settings
        env = self.env
        if settings.mode != "enforce" or not cfg.is_yes(cfg.parameter(env, "Completer:")):
            return 0
        name = log.clean(env.get("NZBNP_NZBNAME", ""), 120)
        path = env.get("NZBNP_FILENAME", "")
        if not settings.wants(env.get("NZBNP_CATEGORY", "")):
            return 0
        if cfg.parameter(env, cfg.TOKEN):
            return 0  # it has been held before; whatever it carries is dealt with by the queue script
        if env.get("NZBNP_PAUSED", "0").strip() == "1":
            self.note("%s was added paused by whoever added it and is left alone" % name)
            return 0
        if not settings.scheduler_active:
            log.warning("Completer: %s is not put on hold because Completer is not in NZBGet's Extensions list, "
                        "so nothing would release it if anything went wrong" % name)
            return 0
        age = self.heartbeat.age(self.clock())
        if not self.heartbeat.alive(self.clock()):
            log.warning("Completer: %s is not put on hold because the scheduled check has not reported %s; "
                        "downloads are only held while that safety net is known to work" %
                        (name, "yet" if age is None else "for %d minutes" % (age // 60)))
            return 0
        try:
            checksum = file_sum(path)
        except (OSError, nzbfile.NzbError):
            self.note("%s is not put on hold because its nzb file cannot be read" % name)
            return 0
        token = binascii.hexlify(os.urandom(8)).decode("ascii")
        log.nzb_command("NZBPR_%s=%s" % (cfg.TOKEN, token))
        log.nzb_command("NZBPR_%s=%s" % (cfg.STATE, cfg.PENDING))
        log.nzb_command("NZBPR_%s=%d" % (cfg.SINCE, int(self.clock())))
        log.nzb_command("NZBPR_%s=%s" % (cfg.SUM, checksum))
        log.nzb_command("NZBPR_%s=%s.queued" % (cfg.FILE, path))  # NZBGet's usual new name; the queue script knows
        log.nzb_command("PAUSED=1")
        self.note("%s is on hold until it has been checked" % name)
        return 0

    # ------------------------------------------------------------ examining and deciding

    def examine(self, subject, path, seconds):
        """Read the nzb and ask the news servers about it. Returns (nzb, outcome); nzb is None when unreadable."""
        try:
            if not path:
                raise nzbfile.NzbError("NZBGet did not name the nzb file")
            if subject.sum and file_sum(path) != subject.sum:
                raise nzbfile.NzbError("it is not the file that was scanned")
            nzb = nzbfile.parse(path)
            if not nzb.data and not nzb.par:
                raise nzbfile.NzbError("it lists no articles")
        except nzbfile.NzbError as error:
            return None, checker.Outcome("error", "the nzb file cannot be used (%s)" % error)
        except OSError:
            return None, checker.Outcome("error", "the nzb file cannot be read")
        deadline = self._deadline(max(1.0, seconds))
        outcome = checker.check(nzb, self.servers(), self.settings, deadline, self.clock())
        return nzb, outcome

    def settle(self, subject, nzb, outcome, held, enforce, own):
        """Decide what the verdict calls for and do it. ``own``: the download is the one this script was run for."""
        settings = self.settings
        now = self.clock()
        checks = subject.checks + 1
        if nzb is None:
            fresh = False
        elif nzb.newest > 0:
            fresh = now - min(nzb.newest, now) < settings.fresh_seconds
        else:
            fresh = True  # no date in the nzb: waiting is the careful choice, and the hold limit ends it
        anchor = min(nzb.newest, now) if nzb is not None and nzb.newest > 0 else (subject.since or now)
        last = is_last(checks, subject.since, now, settings.fresh_seconds)
        decision = decide(outcome.verdict, held, fresh, last, checks)
        outcome.fresh = fresh
        next_check = 0
        if decision.action == WAIT:
            next_check = int(min(now + decision.delay, anchor + settings.fresh_seconds + FRESH_EDGE))
            next_check = max(next_check, int(now) + 60)
        text = self._phrase(decision, outcome, enforce, checks, next_check - now)
        values = {cfg.CHECKS: checks, cfg.INFO: log.clean(text, 300),
                  cfg.VERDICT: "%s/%s" % (outcome.verdict, decision.action)}
        self.say(subject, text)
        self._report_servers(outcome)
        if not enforce:
            values[cfg.STATE] = cfg.OBSERVED
            self.record(subject, values, own=True)
            return
        if decision.action == RELEASE:
            if not self.release(subject):
                self.record(subject, values, own)  # stays on hold; the scheduled check will try again
                return
            values[cfg.STATE] = decision.state
        elif decision.action == WAIT:
            values[cfg.STATE] = cfg.WAITING
            values[cfg.NEXT] = next_check
            if subject.file:
                values[cfg.FILE] = subject.file  # where the nzb really is, for the next check
        elif decision.action == FAIL:
            values[cfg.STATE] = cfg.FAILED
            if own:
                self.record(subject, values, own)
                log.nzb_command("MARK=BAD")  # NZBGet removes this one download as bad, and Sonarr/Radarr see it
            else:
                self.remove_as_failed(subject, values)
            return
        elif subject.state in cfg.HELD:
            values[cfg.STATE] = cfg.RELEASED  # it was not held after all
        self.record(subject, values, own)

    @staticmethod
    def _phrase(decision, outcome, enforce, checks, wait):
        text = outcome.describe()
        if decision.action == RELEASE and decision.state == cfg.PASSED:
            head = "passed and released" if enforce else "would pass"
        elif decision.action == RELEASE:
            head = "let through without a verdict" if enforce else "would be let through without a verdict"
        elif decision.action == WAIT:
            minutes = max(1, int(wait // 60))
            head = "on hold, check %d done, the next one in about %d minutes" % (checks, minutes) if enforce \
                else "would be held and checked again in about %d minutes" % minutes
        elif decision.action == FAIL:
            head = "failed and removed" if enforce else "would be failed"
        else:
            head = "checked but not on hold"
        return "%s: %s" % (head, text)

    def _report_servers(self, outcome):
        for text in outcome.unreachable:
            log.warning("Completer: " + text)
        for text in outcome.left_out:
            self.note("%s was passed over, like NZBGet does (optional, and it rejects the login)" % text)
        for text in outcome.servers:
            self.note(text)

    # ------------------------------------------------------------ changes to NZBGet

    def record(self, subject, values, own):
        """Write Completer's own parameters on a download: as output when it is the script's own, else by RPC."""
        if own:
            for name in cfg.PARAMETERS:
                if name in values:
                    log.nzb_command("NZBPR_%s=%s" % (name, values[name]))
            return True
        try:
            self.guard().set_parameters(subject.target(), values)
            return True
        except (Refused, RpcError) as error:
            log.warning("Completer: %s: the result could not be written to NZBGet (%s)" % (subject.name, error))
            return False

    def release(self, subject):
        """Let a held download start. False when that did not happen; it then stays on hold for the next try."""
        try:
            confirmed = self.guard().resume(subject.target(), not self.settings.par_check_force)
        except Refused as error:
            log.warning("Completer: %s: not touched, it could not be verified (%s)" % (subject.name, error))
            return False
        except RpcError as error:
            log.warning("Completer: %s: could not be released now (%s); the scheduled check will try again" %
                        (subject.name, error))
            return False
        if not confirmed:
            log.warning("Completer: %s: NZBGet did not confirm that it started; the scheduled check will look "
                        "at it again" % subject.name)
        return confirmed

    def let_go(self, subject, why, own):
        """Release a held download without any verdict, for a reason that is not about its articles."""
        if not self.release(subject):
            return False
        self.record(subject, {cfg.STATE: cfg.RELEASED, cfg.INFO: log.clean(why, 300)}, own)
        self.say(subject, "released: " + why)
        return True

    def remove_as_failed(self, subject, values):
        """Fail a download from outside it: remove it, then mark it bad in the history so that Sonarr sees it."""
        guard = self.guard()
        target = subject.target()
        self.unfinished.add(subject.nzb_id)
        try:
            values = dict(values)
            values[cfg.STATE] = cfg.FAILING
            guard.set_parameters(target, values)
            if not guard.edit(target, "GroupDelete"):
                raise RpcError("NZBGet did not remove it")
        except (Refused, RpcError) as error:
            log.warning("Completer: %s: could not be removed now (%s); the scheduled check will try again" %
                        (subject.name, error))
            return False
        for pause in (1, 1, 2, 2, 3):
            try:
                guard.verify(target, history=True)
                break
            except Refused:
                time.sleep(pause)  # NZBGet moves it to the history within a moment
            except RpcError:
                return False
        else:
            return False  # not in the history yet; the next scheduled check finishes the job
        return self.finish_removal(target)

    def finish_removal(self, target):
        """Second step of a failure: the removed download gets NZBGet's 'bad' mark, which Sonarr and Radarr read."""
        guard = self.guard()
        try:
            if not guard.edit_many(target, [("HistoryMarkBad", "")], history=True)[0]:
                raise RpcError("NZBGet did not mark it")
            guard.set_parameters(target, {cfg.STATE: cfg.FAILED}, history=True)
        except (Refused, RpcError) as error:
            log.warning("Completer: download %d: marking it as failed did not work yet (%s); the scheduled check "
                        "will try again" % (target.nzb_id, error))
            return False
        self.unfinished.remove(target.nzb_id)
        return True

    # ------------------------------------------------------------ queue script

    def queue(self):
        """A new download was added: check it, and act on the verdict."""
        env = self.env
        settings = self.settings
        if env.get("NZBNA_EVENT") != "NZB_ADDED":
            return 0
        subject = Subject.from_env(env)
        if subject.nzb_id <= 0:
            return 0
        queued = env.get("NZBNA_QUEUEDFILE", "")
        if queued:
            subject.file = queued  # NZBGet's real name for the nzb, better than the guess made at scan time
        enforce = settings.mode == "enforce" and settings.wants(subject.category)
        if not enforce:
            if subject.held_state:
                why = "Completer is not set to hold downloads any more" if settings.mode != "enforce" \
                    else "its category is not covered by Completer any more"
                self.let_go(subject, why, own=True)
            if settings.mode == "off" or not settings.wants(subject.category):
                return 0
        elif subject.state in (cfg.WAITING, cfg.FAILING):
            self.note("%s is on hold and rechecked by the scheduled check" % subject.name)
            return 0
        if not self.locks.acquire(subject.nzb_id):
            self.note("%s is being checked by another run" % subject.name)
            return 0
        held = True  # a dry run judges as if the download had been held
        if enforce:
            held = subject.held_state and self._still_held(subject)
        nzb, outcome = self.examine(subject, queued, self.seconds_left())
        if self.stopping:
            log.info("Completer: %s: stopped by NZBGet before a verdict; nothing was changed" % subject.name)
            return 0
        self.settle(subject, nzb, outcome, held, enforce, own=True)
        return 0

    def _still_held(self, subject):
        """Whether NZBGet still shows the download as paused. When NZBGet cannot be asked, it is assumed to be."""
        try:
            entry = self.guard().verify(subject.target())
        except Refused as error:
            log.warning("Completer: %s: is not treated as held (%s)" % (subject.name, error))
            return False
        except RpcError as error:
            log.warning("Completer: %s: NZBGet could not be asked whether it is still on hold (%s)" %
                        (subject.name, error))
            return True
        return entry.get("Status") == "PAUSED"

    # ------------------------------------------------------------ scheduled check

    def sweep(self):
        """Every few minutes: check what is due, release what must not stay on hold, finish what was interrupted."""
        if not self.locks.acquire(SWEEP):
            self.note("another scheduled check is running; this one is not needed")
            return 0
        guard = self.guard()
        try:
            groups = guard.groups()
        except RpcError as error:
            log.warning("Completer: the scheduled check could not read NZBGet's queue (%s); nothing was "
                        "changed" % error)
            return 0
        if not self.heartbeat.beat(self.clock()):
            log.warning("Completer: the scheduled check cannot record that it is alive, so new downloads will "
                        "not be put on hold")
        self.finish_failures()
        mine = []
        for entry in groups:
            params = params_of(entry)
            if entry.get("Kind") == "NZB" and params.get(cfg.TOKEN) and \
                    params.get(cfg.STATE) in (cfg.PENDING, cfg.WAITING, cfg.FAILING):
                mine.append(entry)
        order = {cfg.FAILING: 0, cfg.PENDING: 1, cfg.WAITING: 2}
        mine.sort(key=lambda entry: (order[params_of(entry)[cfg.STATE]], _number(params_of(entry).get(cfg.SINCE))))
        counts = {}
        for entry in mine:
            if self.stopping or self.seconds_left(GRACE) < 0:
                break
            try:
                word = self.sweep_one(entry)
            except Exception as error:  # one download's trouble must not stop the others
                log.error("Completer: %s: the scheduled check failed (%s)" % (entry.get("NZBName", ""),
                                                                             type(error).__name__))
                word = "trouble"
            counts[word] = counts.get(word, 0) + 1
        if counts:
            log.info("Completer: scheduled check done: %s" % ", ".join(
                "%s %d" % (word, number) for word, number in sorted(counts.items())))
        else:
            self.note("scheduled check done: nothing is on hold")
        return 0

    def sweep_one(self, entry):
        """One held download found by the scheduled check. Returns a word for the summary."""
        settings = self.settings
        subject = Subject.from_entry(entry)
        now = self.clock()
        paused = entry.get("Status") == "PAUSED"
        if subject.state == cfg.FAILING:
            self.remove_as_failed(subject, {cfg.INFO: "removal finished by the scheduled check"})
            return "removal finished"
        if settings.mode != "enforce" or not settings.wants(subject.category):
            why = "Completer is not set to hold downloads any more" if settings.mode != "enforce" \
                else "its category is not covered by Completer any more"
            return "released" if self.let_go(subject, why, own=False) else "trouble"
        if not paused:
            self.record(subject, {cfg.STATE: cfg.RELEASED, cfg.INFO: "it was started by someone else"}, own=False)
            return "started elsewhere"
        if subject.state == cfg.PENDING:
            age = now - subject.since if subject.since > 0 else PENDING_LIMIT + 1
            if age < -300:
                age = PENDING_LIMIT + 1  # a time stamp from the future cannot be trusted
            if age < PENDING_GRACE:
                return "pending"
            if age > PENDING_LIMIT:
                return "released" if self.let_go(subject, "it was never checked in time", own=False) else "trouble"
        elif subject.next > now:
            return "waiting"
        path = self.find_file(subject)
        if not path:
            return "released" if self.let_go(subject, "its nzb file is not available", own=False) else "trouble"
        subject.file = path
        if not self.locks.acquire(subject.nzb_id):
            return "busy"
        try:
            seconds = min(settings.time_limit, self.seconds_left(GRACE)) - SAFETY_SECONDS
            if seconds < 5:
                return "later"
            nzb, outcome = self.examine(subject, path, seconds)
            if self.stopping:
                return "stopped"
            self.settle(subject, nzb, outcome, True, True, own=False)
            return "checked"
        finally:
            self.locks.release(subject.nzb_id)

    @staticmethod
    def find_file(subject):
        """The nzb file of a held download, if it is still where NZBGet put it and is the one that was scanned."""
        base = subject.file
        if not base:
            return ""
        names = [base]
        if base.endswith(".queued"):
            stem = base[:-len(".queued")]
            names.extend("%s.%d.queued" % (stem, number) for number in range(2, 10))
        for candidate in names:
            try:
                if os.path.isfile(candidate) and (not subject.sum or file_sum(candidate) == subject.sum):
                    return candidate
            except (OSError, nzbfile.NzbError):
                continue
        return ""

    def finish_failures(self):
        """Look for failures that were started and not finished (the run ended between the two steps)."""
        hints = self.unfinished.ids()
        if not hints and time.gmtime(self.clock()).tm_min >= 5:
            return  # the history is read only when a hint says so, and once an hour to be sure
        guard = self.guard()
        try:
            history = guard.history()
        except RpcError:
            return
        for item in history:
            params = params_of(item)
            if item.get("Kind") != "NZB" or params.get(cfg.STATE) != cfg.FAILING or not params.get(cfg.TOKEN):
                continue
            nzb_id = item.get("NZBID")
            if type(nzb_id) is not int or nzb_id <= 0:
                continue
            if item.get("DeleteStatus") != "MANUAL" or item.get("MarkStatus") == "BAD":
                hints.discard(nzb_id)
                continue
            target = Target(nzb_id, params[cfg.TOKEN], item.get("NZBName") or item.get("Name") or "")
            if self.finish_removal(target):
                hints.discard(nzb_id)
                log.info("Completer: finished failing download %d that was interrupted" % nzb_id)
        for nzb_id in hints:
            if not any(item.get("NZBID") == nzb_id for item in history):
                self.unfinished.remove(nzb_id)  # not in the history: it never got that far

    # ------------------------------------------------------------ commands

    def command(self):
        from . import commands
        name = self.env.get("NZBCP_COMMAND", "")
        handler = {"TestServers": commands.test_servers, "ReleaseAll": commands.release_all,
                   "Report": commands.report}.get(name)
        if handler is None:
            log.error("Completer does not know the command %s" % log.clean(name, 40))
            return COMMAND_ERROR
        return handler(self)


def main(env=None, stream=None, clock=time.time, hard_exit=os._exit):
    """Entry point. ``env``, ``stream``, ``clock`` and ``hard_exit`` exist so the tests can drive it."""
    if env is None:
        env = dict(os.environ)
    if stream is not None:
        log.set_stream(stream)
    try:
        run = Run(env, clock, hard_exit)
        if run.settings.verbose:
            log.detail("Completer %s started (%s)" % (__version__, run.kind or "by hand"))
        return run.go()
    except Exception as error:
        log.error("Completer could not start (%s)" % type(error).__name__)
        return 1


if __name__ == "__main__":
    sys.exit(main())
