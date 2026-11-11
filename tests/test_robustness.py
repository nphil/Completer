"""Robustness tests: what Completer does when NZBGet, the network, a process or an input misbehaves.

The promises proved here, in short:

* a fault anywhere (NZBGet unreachable, answering nonsense, dropping the line, a news server that lies or
  stalls, a run that is killed or hangs) never fails a download that is not truly bad, never leaves a script
  with a traceback, and is repaired by a later scheduled check;
* the two-step failure (remove the download, then mark it bad in the history) always ends on the one
  download it began with, also when it is interrupted halfway;
* no password ever reaches a script's output;
* the commands never edit more than they promise;
* odd starts and odd nzb files end in "let it through", never in a crash or a failure.

Everything runs against the fake NZBGet (``nzbgetsim``) and fake news servers. Tests that need real processes
(``inprocess=False``: signals, locks, ``__pycache__``) use the real clock and say so.
"""

import gzip
import contextlib
import io
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fakenntp  # noqa: E402
import nzbgetsim as S  # noqa: E402
import nzbtools  # noqa: E402
from completer import app, checker, log  # noqa: E402
from completer import commands as commands_module  # noqa: E402
from completer import settings as cfg  # noqa: E402

HINT = "completer-unfinished"
KINDS = ("http500", "error", "garbage", "badstatus", "drop", "slow")
METHODS = ("listgroups", "editqueue", "history", "status")
HELD = ("pending", "waiting", "failing")


def spelled(values):
    """NZBGet gives every variable twice: as written and in upper case with punctuation turned into '_'."""
    out = dict(values)
    out.update((S.normalise(name), value) for name, value in values.items())
    return out


def unset(names):
    return list(names) + [S.normalise(name) for name in names]


class _NoNap(object):
    """Stands in for the ``time`` module of a Completer module: everything real, except that sleeping is recorded."""

    def __init__(self):
        self.pauses = []

    def __getattr__(self, name):
        return getattr(time, name)

    def sleep(self, seconds):
        self.pauses.append(seconds)


def wait_until(test, condition, seconds=20.0, what="condition"):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return
        time.sleep(0.02)
    test.fail("%s was not reached in time" % what)


class Base(S.SimTestCase):
    """A fake NZBGet with one fake news server, small posts, and no real sleeping."""

    def setUp(self):
        self.napped = _NoNap()           # what completer.app would have slept (the history retry loop)
        self.checker_naps = _NoNap()     # what completer.checker would have slept (the confirmation pause)
        for module, fake in ((app, self.napped), (checker, self.checker_naps)):
            patcher = mock.patch.object(module, "time", fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tags = 0
        self.news = None
        self.posts = {}

    # ------------------------------------------------------------ building

    def build(self, mode="Enforce", options=None, nzbget=None, inprocess=True, boot=True, login=None, server=None,
              **switches):
        """The usual world; ``boot`` runs the first scheduled check (the heartbeat) when the mode is Enforce.

        ``login`` is a (user, password) pair for the news server (and for NZBGet's server entry); ``switches``
        are behaviour switches of the news server; ``server`` more fields of NZBGet's entry for it.
        """
        fields = dict(server or {})
        if login is not None:
            switches["username"], switches["password"] = login
            fields["username"], fields["password"] = login
        self.news = self.make_news(**switches)
        merged = {"Mode": mode}
        merged.update(options or {})
        sim = self.make_sim(clock=S.SimClock() if inprocess else time.time, options=merged, nzbget=nzbget,
                            inprocess=inprocess)
        sim.add_server(self.news, **fields)
        self.sim = sim
        if boot and mode.strip().lower() == "enforce":
            self.assertEqual(sim.boot().code, 0)
        return sim

    def make_post(self, good=True, tag="p", **kwargs):
        """A small post (24 articles). A bad one has all its content articles missing and its repair files present."""
        self.tags += 1
        tag = "%s%d" % (tag, self.tags)
        kwargs.setdefault("data_files", 1)
        kwargs.setdefault("segments", 20)
        kwargs.setdefault("par_files", 1)
        kwargs.setdefault("par_segments", 4)
        post = self.post(tag, **kwargs)
        self.news.have.update(post.all_ids if good else post.par_ids)
        return post

    def add(self, good=True, name=None, run=False, tag="p", post_kw=None, **kwargs):
        """Add a small post to the fake NZBGet (its queue scripts run only with ``run=True``, else ``sim.pump()``)."""
        post = self.make_post(good, tag, **(post_kw or {}))
        nzb_id = self.sim.add(post, name or "Release %s" % post.tag, run=run, **kwargs)
        self.posts[nzb_id] = post
        return nzb_id

    # ------------------------------------------------------------ looking

    def clear_faults(self):
        with self.sim.lock:
            del self.sim.faults[:]

    def commands_for(self, nzb_id, since=0):
        return [command for command, _text, ids in self.sim.edits[since:] if ids == [nzb_id]]

    def unfinished(self, nzb_id):
        """Whether Completer still has something to do for this download (held, failing, or on its way)."""
        where = self.sim.where(nzb_id)
        if where == "queue":
            return self.sim.param(nzb_id, "Completer_State") in HELD
        if where == "history":
            return self.sim.param(nzb_id, "Completer_State") == "failing"
        return True  # deleted, and not in the history yet

    def settle(self, nzb_id, rounds=8, step=400):
        """Faults have stopped: scheduled checks follow until Completer is done with the download."""
        self.clear_faults()
        for _ in range(rounds):
            if not self.unfinished(nzb_id):
                return
            self.sim.advance(step)
            self.assert_calm([self.sim.sweep()])
        self.assertFalse(self.unfinished(nzb_id), "the scheduled checks did not finish download %d" % nzb_id)

    def at_half_past(self):
        """Move the simulated clock to minute 30 of a later hour and let a scheduled check report that it is alive."""
        now = int(self.sim.now)
        self.sim.clock.now = float((now // 3600 + 1) * 3600 + 1800)
        self.assert_calm([self.sim.sweep()])

    def goto_minute(self, minute):
        """Move the simulated clock forward to the next time it is ``minute`` past the hour (seconds 0)."""
        now = int(self.sim.now)
        target = now - now % 3600 + minute * 60
        if target <= now:
            target += 3600
        self.sim.clock.now = float(target)

    def assert_calm(self, runs, codes=(0,)):
        """No crash: an expected exit code, no traceback, no 'unexpected problem' (the catch-all of the program)."""
        for run in runs:
            self.assertIn(run.code, codes, run.out)
            self.assertNotIn("Traceback", run.out + run.err)
            self.assertNotIn("unexpected problem", run.out)
            self.assertNotIn("could not start", run.out)
            self.assertEqual(run.err, "")

    def hint_exists(self):
        return os.path.exists(os.path.join(self.sim.temp_dir, HINT))

    def warned(self, run):
        return any(kind == "warning" for kind, _text in run.messages)

    def assert_no_secret(self, extra=()):
        self.assertTrue(self.sim.runs)
        for run in self.sim.runs:
            for secret in set(self.sim.secrets) | set(extra):
                self.assertNotIn(secret, run.out)
                self.assertNotIn(secret, run.err)


# ======================================================================== NZBGet unreachable


class RpcDownTests(Base):
    def test_rpc_down_during_the_queue_script_keeps_a_good_post_on_hold_until_the_sweep_releases_it(self):
        """Nothing can be released while NZBGet cannot be asked: the download stays held, the script does not crash."""
        sim = self.build()
        nzb_id = self.add(True)
        self.assertEqual(sim.status(nzb_id), "PAUSED")
        sim.rpc_down()
        (run,) = sim.pump()
        self.assert_calm([run])  # exit code 0
        self.assertTrue(self.warned(run))
        self.assertEqual(sim.status(nzb_id), "PAUSED")
        self.assertEqual(sim.param(nzb_id, "Completer_State"), "pending")
        self.assertEqual(sim.edits, [])
        sim.rpc_up()
        sim.advance(100)
        self.assert_calm([sim.sweep()])
        self.assertEqual(sim.param(nzb_id, "Completer_State"), "passed")
        self.assertNotEqual(sim.status(nzb_id), "PAUSED")
        self.assertEqual(sim.where(nzb_id), "queue")

    def test_rpc_down_a_bad_post_is_still_failed_through_the_script_output(self):
        """The queue script marks its own download bad with [NZB] MARK=BAD: that needs no RPC at all."""
        sim = self.build()
        nzb_id = self.add(False)
        calls = len(sim.calls)
        sim.rpc_down()
        (run,) = sim.pump()
        self.assert_calm([run])
        self.assertIn("MARK=BAD", run.commands)
        self.assertEqual(sim.where(nzb_id), "history")
        self.assertEqual(sim.entry(nzb_id).mark_status, "BAD")
        self.assertEqual(sim.param(nzb_id, "Completer_State"), "failed")
        self.assertEqual(sim.sonarr_view(nzb_id), "failed")
        self.assertEqual(sim.calls, sim.calls[:calls])  # nothing more reached NZBGet, and nothing needed to

    def test_rpc_down_a_fresh_bad_post_waits_and_is_checked_again_when_nzbget_is_back(self):
        """A fresh post that looks bad waits (state written through [NZB]); the sweep rechecks it later."""
        sim = self.build()
        post = self.make_post(False, age_hours=0.5)
        nzb_id = sim.add(post, "Fresh release", run=False)
        sim.rpc_down()
        (run,) = sim.pump()
        self.assert_calm([run])
        self.assertEqual(sim.param(nzb_id, "Completer_State"), "waiting")
        due = int(sim.param(nzb_id, "Completer_Next"))
        self.assertGreater(due, sim.now)
        self.assertEqual(sim.status(nzb_id), "PAUSED")
        self.assertEqual(sim.edits, [])
        sim.rpc_up()
        self.news.have.update(post.all_ids)  # the post has finished spreading over the servers
        sim.advance(due - sim.now + 1)
        self.assert_calm([sim.sweep()])
        self.assertEqual(sim.param(nzb_id, "Completer_State"), "passed")
        self.assertEqual(sim.where(nzb_id), "queue")
        self.assertNotEqual(sim.status(nzb_id), "PAUSED")

    def test_rpc_down_the_sweep_changes_nothing_and_exits_zero(self):
        sim = self.build()
        nzb_id = self.add(True)
        sim.advance(100)
        before = sim.snapshot()
        sim.rpc_down()
        run = sim.sweep()
        self.assert_calm([run])
        self.assertTrue(self.warned(run))
        self.assertEqual(sim.snapshot(), before)
        sim.rpc_up()
        self.assert_calm([sim.sweep()])
        self.assertEqual(sim.param(nzb_id, "Completer_State"), "passed")

    def test_with_nzbget_unreachable_for_long_the_safety_net_lapses_and_new_downloads_are_not_held(self):
        """The heartbeat is written only after the sweep talked to NZBGet: without it nothing new is put on hold."""
        sim = self.build()
        sim.rpc_down()
        self.assert_calm([sim.sweep()])
        sim.advance(1000)  # longer than the life of a heartbeat (900 s)
        nzb_id = self.add(True)
        scan = sim.runs_of("scan")[-1]
        self.assertEqual(scan.code, 0)
        self.assertTrue(self.warned(scan))
        self.assertIsNone(sim.param(nzb_id, "Completer_Token"))
        self.assertNotEqual(sim.status(nzb_id), "PAUSED")
        sim.rpc_up()
        self.assert_calm(sim.pump())  # its queue script finds nothing held and nothing to change
        self.assertEqual(sim.edits, [])
        self.assert_calm([sim.sweep()])  # NZBGet is back: the safety net works again
        held = self.add(True)
        self.assertEqual(sim.status(held), "PAUSED")
        self.assertEqual(sim.param(held, "Completer_State"), "pending")
        self.assert_calm(sim.pump())
        self.assertEqual(sim.param(held, "Completer_State"), "passed")

    def test_a_wrong_control_password_changes_nothing_and_never_fails_a_download(self):
        """NZBGet answers 401: like an unreachable NZBGet, the download stays held (or passes through the sweep later)."""
        sim = self.build()
        nzb_id = self.add(False)
        sim.secrets.add("other-secret")
        (run,) = [sim.run_queue_script(nzb_id, env_update={"NZBOP_ControlPassword": "other-secret"})]
        self.assert_calm([run])
        # a bad post is failed by its own output, which needs no password
        self.assertEqual(sim.sonarr_view(nzb_id), "failed")
        good = self.add(True)
        run = sim.run_queue_script(good, env_update={"NZBOP_ControlPassword": "other-secret"})
        self.assert_calm([run])
        self.assertTrue(self.warned(run))
        self.assertEqual(sim.status(good), "PAUSED")
        self.assertEqual(sim.param(good, "Completer_State"), "pending")
        self.assertEqual([call for call in sim.calls if call.method == "editqueue"], [])
        self.assert_no_secret(["other-secret"])


# ======================================================================== the fault matrix

EXPECTED_HITS = {
    ("queue", True): {"listgroups", "editqueue"},
    ("queue", False): {"listgroups"},
    ("sweep", True): {"listgroups", "editqueue"},
    ("sweep", False): {"listgroups", "editqueue", "history"},
}


class FaultMatrixTests(Base):
    """Every kind of NZBGet misbehaviour on every method it is asked, through both ways a download is checked.

    ``times=2`` faults in a row, either at the first call (skip 0) or after one good call (skip 1), then NZBGet
    behaves again and the next scheduled checks finish the job. Exit codes: the queue script and the sweep return 0
    whatever happens (a 1 would be the catch-all of the program, which is also asserted never to have fired).
    """

    def run_matrix(self, path, good):
        sim = self.build()
        hits = {}
        for kind in KINDS:
            for method in METHODS:
                for skip in (0, 1):
                    with self.subTest(path=path, good=good, kind=kind, method=method, skip=skip):
                        self.at_half_past()
                        nzb_id = self.add(good)
                        sim.advance(100)
                        sim.fault(kind, method=method, times=2, skip=skip, delay=0.02)
                        fault = sim.faults[-1]
                        mark = len(sim.edits)
                        if path == "queue":
                            runs = [sim.run_queue_script(nzb_id)]
                        else:
                            runs = [sim.sweep()]
                        if skip == 0:
                            hits[(kind, method)] = 2 - fault["times"]
                        self.assert_calm(runs)
                        self.settle(nzb_id)
                        edited = [number for _c, _t, ids in sim.edits[mark:] for number in ids]
                        self.assertEqual(set(edited) - {nzb_id}, set())
                        if good:
                            self.assertEqual(sim.where(nzb_id), "queue")
                            self.assertNotEqual(sim.status(nzb_id), "PAUSED")
                            self.assertIn(sim.param(nzb_id, "Completer_State"), ("passed", "released"))
                            self.assertNotIn("GroupDelete", self.commands_for(nzb_id, mark))
                            self.assertNotIn("HistoryMarkBad", self.commands_for(nzb_id, mark))
                            self.assertIsNone(sim.sonarr_view(nzb_id))
                        else:
                            self.assertEqual(sim.where(nzb_id), "history")
                            self.assertEqual(sim.entry(nzb_id).mark_status, "BAD")
                            self.assertEqual(sim.param(nzb_id, "Completer_State"), "failed")
                            self.assertEqual(sim.sonarr_view(nzb_id), "failed")
                            self.assertFalse(self.hint_exists())
        for kind in KINDS:
            got = {method for method in METHODS if hits.get((kind, method), 0) > 0}
            self.assertEqual(got, EXPECTED_HITS[(path, good)], kind)  # the faults really bit, and only where used

    def test_every_nzbget_fault_leaves_a_good_post_released_on_the_queue_script_path(self):
        self.run_matrix("queue", True)

    def test_every_nzbget_fault_still_fails_a_bad_post_on_the_queue_script_path(self):
        self.run_matrix("queue", False)

    def test_every_nzbget_fault_leaves_a_good_post_released_on_the_sweep_path(self):
        self.run_matrix("sweep", True)

    def test_every_nzbget_fault_ends_a_bad_post_failed_and_marked_on_the_sweep_path(self):
        self.run_matrix("sweep", False)

    def test_a_good_post_is_never_failed_by_a_long_outage_of_any_method(self):
        """Faults that last for many calls (not two): the download stays held and is released when they end."""
        sim = self.build()
        for method in METHODS:
            with self.subTest(method=method):
                self.at_half_past()
                nzb_id = self.add(True)
                sim.advance(100)
                sim.fault("http500", method=method, times=50)
                runs = [sim.run_queue_script(nzb_id), sim.sweep(), sim.sweep()]
                self.assert_calm(runs)
                self.assertNotIn("GroupDelete", self.commands_for(nzb_id))
                self.assertEqual(sim.where(nzb_id), "queue")
                self.settle(nzb_id)
                self.assertIn(sim.param(nzb_id, "Completer_State"), ("passed", "released"))
                self.assertNotEqual(sim.status(nzb_id), "PAUSED")

    def test_a_download_resumed_but_not_confirmed_ends_as_released_not_failed(self):
        """GroupResume works, the pause of the extra repair files does not: it is running, and Completer says so."""
        sim = self.build()
        nzb_id = self.add(True)
        sim.fault("http500", method="editqueue", times=1, skip=1)
        (run,) = sim.pump()
        self.assert_calm([run])
        self.assertEqual(self.commands_for(nzb_id)[:2], ["GroupResume", "GroupPauseExtraPars"])
        self.assertNotEqual(sim.status(nzb_id), "PAUSED")  # NZBGet has started it
        self.assertEqual(sim.param(nzb_id, "Completer_State"), "pending")  # but the script could not say so
        sim.advance(100)
        self.assert_calm([sim.sweep()])
        self.assertEqual(sim.param(nzb_id, "Completer_State"), "released")  # the sweep sees: started elsewhere
        self.assertEqual(sim.where(nzb_id), "queue")
        self.assertNotIn("GroupDelete", self.commands_for(nzb_id))

    def test_a_slow_nzbget_is_only_slow(self):
        """Answers that take a while (below the client timeout) change nothing about the outcome."""
        sim = self.build()
        nzb_id = self.add(True)
        sim.fault("slow", times=None, delay=0.3)
        self.assert_calm(sim.pump())
        self.assertEqual(sim.param(nzb_id, "Completer_State"), "passed")
        self.assertEqual(sim.where(nzb_id), "queue")
        self.clear_faults()


# ======================================================================== the two-step failure


class TwoStepTests(Base):
    """A failure found by the scheduled check is two steps in NZBGet: remove the download, then mark it bad.

    When the run ends between the steps, the download is gone from the queue but Sonarr/Radarr still ignore it
    (a user-deleted download is not a failure to them). A hint file tells the next scheduled check where to
    look. Every step names the one id the failure began with, and nothing else is ever touched.
    """

    def setUp(self):
        Base.setUp(self)
        self.sim = None

    def start(self):
        """A bad held download waiting for the sweep, plus two downloads that must stay out of it."""
        sim = self.build()
        self.victim = self.add(False, "The bad one")
        sim.advance(100)
        # paused by hand, never held by Completer: no token, so nothing of Completer may ever touch it
        self.bystander = self.add(True, "Paused by hand", queue_scripts=False, paused=True)
        self.shot = sim.snapshot([self.bystander])
        self.deleted_by_user = self.add(True, "Deleted by the user", queue_scripts=False)
        sim.user_delete(self.deleted_by_user)
        self.user_shot = sim.snapshot([self.deleted_by_user])
        return sim

    def assert_only_the_victim(self, sim):
        self.assertEqual(sim.edited_ids(), {self.victim})
        self.assertEqual(sim.snapshot([self.bystander]), self.shot)
        self.assertEqual(sim.snapshot([self.deleted_by_user]), self.user_shot)

    def assert_finished(self, sim):
        entry = sim.entry(self.victim)
        self.assertEqual(sim.where(self.victim), "history")
        self.assertEqual(entry.mark_status, "BAD")
        self.assertEqual(entry.get("Completer_State"), "failed")
        self.assertEqual(sim.sonarr_view(self.victim), "failed")
        self.assertFalse(self.hint_exists())

    def test_when_the_history_cannot_be_read_the_failure_is_left_half_done_and_marked_for_later(self):
        sim = self.start()
        sim.fault("http500", method="history", times=10)
        run = sim.sweep()
        self.assert_calm([run])
        entry = sim.entry(self.victim)
        self.assertEqual(sim.where(self.victim), "history")
        self.assertEqual(entry.delete_status, "MANUAL")
        self.assertEqual(entry.mark_status, "NONE")
        self.assertEqual(entry.get("Completer_State"), "failing")
        self.assertIsNone(sim.sonarr_view(self.victim))  # Sonarr ignores it as it stands
        self.assertTrue(self.hint_exists())
        self.assertEqual(self.commands_for(self.victim), ["GroupSetParameter"] * 4 + ["GroupDelete"])
        self.assertEqual(sim.snapshot([self.bystander]), self.shot)

    def test_the_next_scheduled_check_finishes_it_even_when_it_is_not_the_hour_for_reading_the_history(self):
        sim = self.start()
        sim.fault("http500", method="history", times=10)
        self.assert_calm([sim.sweep()])
        self.clear_faults()
        self.goto_minute(20)  # the history is read only because of the hint
        before = len([call for call in sim.calls if call.method == "history"])
        run = sim.sweep()
        self.assert_calm([run])
        self.assertGreater(len([call for call in sim.calls if call.method == "history"]), before)
        self.assert_finished(sim)
        self.assertEqual(self.commands_for(self.victim)[-2:], ["HistoryMarkBad", "HistorySetParameter"])
        self.assert_only_the_victim(sim)

    def test_without_a_hint_the_history_is_read_only_in_the_first_minutes_of_the_hour(self):
        """The hint is what makes the quick recovery possible; the hourly look is the safety net behind it."""
        sim = self.start()
        sim.fault("http500", method="history", times=10)
        self.assert_calm([sim.sweep()])
        self.clear_faults()
        os.remove(os.path.join(sim.temp_dir, HINT))  # (as after a restart of the machine that lost the folder)
        self.goto_minute(20)
        before = len([call for call in sim.calls if call.method == "history"])
        self.assert_calm([sim.sweep()])
        self.assertEqual(len([call for call in sim.calls if call.method == "history"]), before)
        self.assertEqual(sim.entry(self.victim).mark_status, "NONE")
        self.goto_minute(2)
        self.assert_calm([sim.sweep()])
        self.assert_finished(sim)
        self.assert_only_the_victim(sim)

    def test_when_the_removal_itself_fails_the_download_stays_and_the_next_check_removes_it(self):
        sim = self.start()
        sim.fault("http500", method="editqueue", times=1, skip=4)  # the 5th edit is GroupDelete
        self.assert_calm([sim.sweep()])
        self.assertEqual(sim.where(self.victim), "queue")
        self.assertEqual(sim.param(self.victim, "Completer_State"), "failing")
        self.assertEqual(sim.status(self.victim), "PAUSED")
        self.assertEqual(self.commands_for(self.victim), ["GroupSetParameter"] * 4 + ["GroupDelete"])  # the last one failed
        self.assert_calm([sim.sweep()])
        self.assert_finished(sim)
        self.assert_only_the_victim(sim)

    def test_when_marking_it_bad_fails_the_next_check_marks_it(self):
        sim = self.start()
        sim.fault("http500", method="editqueue", times=1, skip=5)  # the 6th edit is HistoryMarkBad
        self.assert_calm([sim.sweep()])
        entry = sim.entry(self.victim)
        self.assertEqual(entry.mark_status, "NONE")
        self.assertEqual(entry.get("Completer_State"), "failing")
        self.assertTrue(self.hint_exists())
        self.assertIsNone(sim.sonarr_view(self.victim))
        sim.advance(300)
        self.assert_calm([sim.sweep()])
        self.assert_finished(sim)
        self.assert_only_the_victim(sim)

    def test_when_only_the_last_note_is_lost_the_download_is_marked_bad_and_the_next_check_completes_the_note(self):
        """HistoryMarkBad worked (Sonarr already sees the failure); only 'Completer_State=failed' did not get written."""
        sim = self.start()
        sim.fault("http500", method="editqueue", times=1, skip=6)  # the 7th edit is HistorySetParameter
        self.assert_calm([sim.sweep()])
        self.assertEqual(sim.entry(self.victim).mark_status, "BAD")
        self.assertEqual(sim.sonarr_view(self.victim), "failed")  # the part that matters is done
        sim.advance(300)
        self.assert_calm([sim.sweep()])
        self.assert_finished(sim)  # includes: the hint file is gone
        self.assertEqual(self.commands_for(self.victim).count("HistoryMarkBad"), 1)  # it is not marked a second time
        self.assertFalse(self.hint_exists())
        self.assertEqual(self.commands_for(self.victim).count("HistorySetParameter"), 2)  # the lost one, and the retry
        self.assert_only_the_victim(sim)
        before = len(sim.edits)
        sim.advance(300)
        self.assert_calm([sim.sweep()])
        self.assertEqual(len(sim.edits), before)  # and nothing is left to do on later checks

    def test_when_nzbget_is_slow_to_move_it_to_the_history_the_run_waits_a_moment_and_finishes(self):
        sim = self.start()
        sim.history_delay = 2
        self.assert_calm([sim.sweep()])
        self.assertEqual(self.napped.pauses, [1, 1])  # no real waiting: the pauses were only recorded
        self.assert_finished(sim)
        self.assert_only_the_victim(sim)

    def test_when_nzbget_is_very_slow_the_run_gives_up_and_a_later_check_finishes_the_removal(self):
        sim = self.start()
        sim.history_delay = 20
        self.assert_calm([sim.sweep()])
        self.assertEqual(self.napped.pauses, [1, 1, 2, 2, 3])
        self.assertEqual(sim.where(self.victim), None)  # removed, and not in the history yet
        self.assertTrue(self.hint_exists())
        sim.finish_deletions()  # NZBGet has moved it by the time of the next scheduled check
        self.assertEqual(sim.entry(self.victim).mark_status, "NONE")
        sim.advance(300)
        self.assert_calm([sim.sweep()])
        self.assert_finished(sim)
        self.assert_only_the_victim(sim)

    def test_a_connection_that_drops_between_the_steps_is_repaired_by_the_next_check(self):
        sim = self.start()
        sim.fault("drop", method="history", times=1)
        self.assert_calm([sim.sweep()])
        self.assertEqual(sim.entry(self.victim).mark_status, "NONE")
        self.assertTrue(self.hint_exists())
        sim.advance(300)
        self.assert_calm([sim.sweep()])
        self.assert_finished(sim)
        self.assert_only_the_victim(sim)

    def test_a_process_ended_between_the_steps_is_repaired_by_the_next_check(self):
        """The run dies (here: an interruption that no handler of the program catches) right after GroupDelete."""
        sim = self.start()
        with mock.patch.object(app.Run, "finish_removal", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                sim.sweep()
        self.assertEqual(sim.where(self.victim), "history")
        self.assertEqual(sim.entry(self.victim).mark_status, "NONE")
        self.assertTrue(self.hint_exists())
        self.assertIsNone(sim.sonarr_view(self.victim))
        sim.advance(300)
        self.assert_calm([sim.sweep()])  # the locks of the dead run are gone with it: this run is not "busy"
        self.assert_finished(sim)
        self.assert_only_the_victim(sim)

    def test_a_hint_for_a_download_that_is_not_ours_changes_nothing(self):
        """The hint only says where to look: the entry itself must carry Completer's token and the state failing."""
        sim = self.start()
        with open(os.path.join(sim.temp_dir, HINT), "w") as handle:
            handle.write("%d\n%d\n%d\n99999\n" % (self.deleted_by_user, self.bystander, self.victim))
        sim.advance(60)
        before = sim.snapshot()
        self.assert_calm([sim.sweep()])
        for nzb_id in (self.deleted_by_user, self.bystander):
            self.assertEqual(sim.snapshot([nzb_id]), {nzb_id: before[nzb_id]})
        self.assertEqual(sim.edited_ids() - {self.victim}, set())
        self.assertEqual(sim.where(self.victim), "history")  # the victim, held and bad, was failed by its own state
        self.assertEqual(sim.entry(self.victim).mark_status, "BAD")
        self.assertFalse(self.hint_exists())  # every hint that is finished, absent or not ours is dropped
        self.assertEqual(self.commands_for(self.victim).count("HistoryMarkBad"), 1)

    def test_a_good_post_found_while_the_removal_is_unfinished_is_not_touched(self):
        """A download that failed once and is somehow good now is still only handled by its own state."""
        sim = self.start()
        sim.fault("http500", method="history", times=10)
        self.assert_calm([sim.sweep()])
        self.clear_faults()
        self.news.have.update(self.posts[self.victim].all_ids)  # (the post was complete after all)
        sim.advance(300)
        self.assert_calm([sim.sweep()])
        self.assert_finished(sim)  # a failure that was begun is finished: it is never turned around halfway
        self.assert_only_the_victim(sim)


# ======================================================================== real processes: signals, locks, time limits


class Proc(object):
    """One real run of ``main.py`` that the test can signal; its result is recorded like any other run."""

    def __init__(self, test, kind, env):
        self.test = test
        self.kind = kind
        self.env = env
        self.result = None
        self.process = subprocess.Popen([sys.executable, S.MAIN], env=dict(env), cwd=test.sim.root,
                                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        test.addCleanup(self.close)

    def signal(self, number):
        self.process.send_signal(number)

    @property
    def running(self):
        return self.process.poll() is None

    def finish(self, timeout=60, record=True):
        if self.result is None:
            out, err = self.process.communicate(timeout=timeout)
            self.result = S.RunResult(self.kind, self.process.returncode, out.decode("utf-8", "replace"),
                                      err.decode("utf-8", "replace"), self.env)
            if record:
                self.test.sim._inspect(self.result)
        return self.result

    def close(self):
        if self.result is None:
            self.process.kill()
            self.process.communicate()
            self.result = S.RunResult(self.kind, self.process.returncode, "", "", self.env)


class RealBase(Base):
    """The real clock and real processes. Long checks are made with a news server that answers slowly or never."""

    def queue_proc(self, nzb_id):
        return Proc(self, "queue", self.sim.queue_env(self.sim.entry(nzb_id)))

    def sweep_proc(self):
        env = self.sim.base_env()
        env["NZBSP_TASKID"] = "1"
        return Proc(self, "sweep", env)

    def age(self, nzb_id, seconds=500):
        """Pretend the download was held ``seconds`` ago (a real process cannot be given a simulated clock)."""
        self.sim.entry(nzb_id).set("Completer_Since", str(int(time.time()) - seconds))

    def queue_bg(self, nzb_id):
        """A queue script run that the fake NZBGet carries out (and applies the [NZB] commands of), in a thread."""
        return self.sim.background(self.sim.run_queue_script, nzb_id)

    @staticmethod
    def alive(background):
        return background._thread.is_alive()

    def released_once(self, nzb_id):
        commands = self.commands_for(nzb_id)
        self.assertEqual(commands.count("GroupResume"), 1)
        self.assertEqual(commands.count("GroupPauseExtraPars"), 1)
        self.assertNotIn("GroupDelete", commands)

    def await_questions(self, count=1):
        wait_until(self, lambda: self.news.stats_total >= count, what="the news server being asked")


class KillAndTimeTests(RealBase):
    def test_a_queue_script_stopped_by_nzbget_changes_nothing_and_the_sweep_settles_the_download(self):
        sim = self.build(inprocess=False, stall="stat")
        nzb_id = self.add(True)
        shot = sim.snapshot()
        edits = len(sim.edits)
        proc = self.queue_proc(nzb_id)
        self.await_questions()
        began = time.monotonic()
        proc.signal(signal.SIGTERM)
        run = proc.finish(30)
        self.assertLess(time.monotonic() - began, 10)  # promptly: it does not sit out its time limit
        self.assertEqual(run.code, 0)
        self.assertTrue(run.says("nothing was changed"))
        self.assertEqual(run.commands, [])
        self.assertEqual(run.err, "")
        self.assertEqual(sim.snapshot(), shot)
        self.assertEqual(len(sim.edits), edits)
        self.assertEqual(sim.status(nzb_id), "PAUSED")
        self.assertEqual(sim.param(nzb_id, "Completer_State"), "pending")
        self.news.stall = False
        self.age(nzb_id)
        self.assert_calm([sim.sweep()])
        self.assertEqual(sim.param(nzb_id, "Completer_State"), "passed")
        self.assertNotEqual(sim.status(nzb_id), "PAUSED")
        self.released_once(nzb_id)

    def test_a_sweep_stopped_by_nzbget_leaves_every_download_untouched_and_frees_its_locks(self):
        sim = self.build(inprocess=False, stall="stat")
        ids = [self.add(True, "Release number %d" % number) for number in range(3)]
        for nzb_id, seconds in zip(ids, (900, 700, 500)):  # the oldest is looked at first
            self.age(nzb_id, seconds)
        shot = sim.snapshot()
        proc = self.sweep_proc()
        self.await_questions()
        proc.signal(signal.SIGTERM)
        run = proc.finish(30)
        self.assertEqual(run.code, 0)
        self.assertEqual(run.err, "")
        self.assertEqual(sim.snapshot(), shot)
        self.assertEqual(sim.edits, [])
        self.assertLessEqual(set(self.news.stat_log), set(self.posts[ids[0]].all_ids))  # the others were not reached
        self.news.stall = False
        again = sim.sweep()  # no lock was left behind
        self.assert_calm([again])
        self.assertFalse(again.says("another scheduled check"))
        for nzb_id in ids:
            self.assertEqual(sim.param(nzb_id, "Completer_State"), "passed")
            self.released_once(nzb_id)

    def test_a_sweep_that_is_killed_outright_leaves_no_lock_behind(self):
        """The locks belong to the process: when it dies, however it dies, the kernel lets them go."""
        sim = self.build(inprocess=False, stall="stat", options={"Verbose": "yes"})
        nzb_id = self.add(True)
        self.age(nzb_id)
        proc = self.sweep_proc()
        self.await_questions()
        proc.process.kill()
        proc.finish(30, record=False)
        self.assertEqual(proc.result.returncode if hasattr(proc.result, "returncode") else proc.result.code,
                         -signal.SIGKILL)
        self.news.stall = False
        run = sim.sweep()
        self.assert_calm([run])
        self.assertFalse(run.says("another scheduled check"))
        self.assertEqual(sim.param(nzb_id, "Completer_State"), "passed")

    def hung_run(self, env, patches):
        """Run the program in this process with a watchdog that fires after about 0.1 s, while the run is stuck.

        ``patches(gone)`` returns the patches that make the run hang until ``gone`` is set; the injected
        ``hard_exit`` records the exit code and sets it (the real one ends the process on the spot).
        """
        gone = threading.Event()
        exits = []

        def hard_exit(code):
            exits.append(code)
            gone.set()

        stream = io.StringIO()
        began = time.monotonic()
        try:
            with contextlib.ExitStack() as stack:
                for patch in patches(gone):
                    stack.enter_context(patch)
                app.main(env, stream, self.sim.clock, hard_exit)
        finally:
            log.set_stream(None)
            log.forget_secrets()
        gone.set()
        self.assertLess(time.monotonic() - began, 8)
        return exits, stream.getvalue()

    def test_a_run_that_hangs_is_ended_from_outside_with_the_failure_code_of_its_kind(self):
        """The watchdog (budget + GRACE) must end a run that is stuck wherever it is stuck."""

        def stuck(gone):
            def hang(*_args, **_kwargs):
                gone.wait(30)
                return None
            return hang

        sim = self.build(options={"TimeLimit": "10"})
        nzb_id = self.add(True)
        scan_env = sim.base_env()
        scan_env.update({"NZBNP_FILENAME": self.posts[nzb_id].write(os.path.join(sim.nzb_dir, "probe.nzb")),
                         "NZBNP_NZBNAME": "Probe", "NZBNP_CATEGORY": "", "NZBNP_PAUSED": "0"})
        S.export(scan_env, "NZBPR", "Completer:", "yes")
        sweep_env = sim.base_env()
        sweep_env["NZBSP_TASKID"] = "1"
        command_env = sim.base_env()
        command_env["NZBCP_COMMAND"] = "TestServers"
        cases = (
            ("queue", sim.queue_env(sim.entry(nzb_id)), 1,
             lambda gone: [mock.patch.object(app, "GRACE", -9.9),
                           mock.patch.object(app.Run, "examine", lambda *_a, **_k: (stuck(gone)(),
                                                                                   (None, checker.Outcome("error", "x")))[1])]),
            ("sweep", sweep_env, 1,
             lambda gone: [mock.patch.object(app, "GRACE", -0.9), mock.patch.object(app, "SWEEP_SECONDS", 1),
                           mock.patch.object(app.Run, "finish_failures", stuck(gone))]),
            ("scan", scan_env, 1,
             lambda gone: [mock.patch.object(app, "GRACE", -0.9), mock.patch.object(app, "SCAN_SECONDS", 1),
                           mock.patch.object(app, "file_sum", lambda *_a: (stuck(gone)(), "x")[1])]),
            ("command", command_env, 94,
             lambda gone: [mock.patch.object(app, "GRACE", -0.9), mock.patch.object(app, "COMMAND_SECONDS", 1),
                           mock.patch.object(commands_module, "test_servers",
                                             lambda *_a: (stuck(gone)(), 93)[1])]),
        )
        for kind, env, code, patches in cases:
            with self.subTest(kind=kind):
                exits, out = self.hung_run(env, patches)
                self.assertEqual(exits, [code])
                self.assertIn("ran out of time", out)


class ConcurrencyTests(RealBase):
    """Two real runs at once, over slow news servers so that they truly overlap."""

    def count_asked(self, post):
        return len([item for item in self.news.stat_log if item in set(post.all_ids)])

    def test_two_scheduled_checks_at_once_do_the_work_once(self):
        sim = self.build(inprocess=False, slow=0.25, options={"Verbose": "yes", "Connections": "1"})
        nzb_id = self.add(True)
        self.age(nzb_id)
        first = self.sweep_proc()
        self.await_questions()
        second = sim.sweep()  # runs to its end while the first is still asking
        self.assertTrue(first.running)
        self.assert_calm([second])
        self.assertTrue(second.says("another scheduled check is running"))
        self.assertEqual(sim.edits, [])
        self.assert_calm([first.finish()])
        self.assertEqual(sim.param(nzb_id, "Completer_State"), "passed")
        self.released_once(nzb_id)
        self.assertEqual(len(self.news.stat_log), len(set(self.news.stat_log)))  # no article was asked twice

    def test_a_queue_script_and_a_sweep_on_the_same_download_check_it_only_once(self):
        sim = self.build(inprocess=False, slow=0.4, options={"Connections": "1"})
        nzb_id = self.add(True)
        self.age(nzb_id)
        queue = self.queue_bg(nzb_id)
        self.await_questions()
        sweep = sim.sweep()
        self.assertTrue(self.alive(queue))  # the sweep met a download that is being checked, and left it alone
        self.assert_calm([sweep])
        self.assertTrue(sweep.says("busy 1"))
        self.assertEqual(sim.edits, [])
        self.assert_calm([queue.wait()])
        self.assertEqual(sim.param(nzb_id, "Completer_State"), "passed")
        self.released_once(nzb_id)  # one resume, not two
        self.assertEqual(len(self.news.stat_log), len(set(self.news.stat_log)))

    def test_two_queue_scripts_for_two_downloads_both_finish_within_the_connection_limit(self):
        sim = self.build(inprocess=False, slow=0.1, options={"Connections": "2"})
        first = self.add(True, "First release", post_kw={"segments": 40})
        second = self.add(True, "Second release", post_kw={"segments": 40})
        runs = [self.queue_bg(first), self.queue_bg(second)]
        self.assert_calm([run.wait() for run in runs])
        for nzb_id in (first, second):
            self.assertEqual(sim.param(nzb_id, "Completer_State"), "passed")
            self.released_once(nzb_id)
        self.assertGreaterEqual(self.news.max_concurrent, 1)
        self.assertLessEqual(self.news.max_concurrent, 2 * 2)  # Connections per run, two runs
        self.assertEqual(self.news.rejected_total, 0)

    def test_a_queue_script_for_one_download_and_a_sweep_for_another_do_not_disturb_each_other(self):
        sim = self.build(inprocess=False, slow=0.1, options={"Connections": "2"})
        old = self.add(True, "The older release", post_kw={"segments": 30})
        self.age(old)
        new = self.add(True, "The newer release", post_kw={"segments": 30})
        queue = self.queue_bg(new)
        sweep = self.sweep_proc()
        self.assert_calm([queue.wait(), sweep.finish()])
        self.released_once(old)
        self.released_once(new)
        self.assertEqual(sim.edited_ids(), {old, new})
        for nzb_id in (old, new):
            self.assertEqual(sim.param(nzb_id, "Completer_State"), "passed")
        self.assertEqual(len(self.news.stat_log), len(set(self.news.stat_log)))
        self.assertLessEqual(self.news.max_concurrent, 2 * 2)


# ======================================================================== the command buttons


class CommandTests(Base):
    """TestServers and Report never change anything; ReleaseAll only resumes what Completer itself holds."""

    def run_command(self, name):
        run = self.sim.command(name)
        self.assert_calm([run], codes=(93, 94))
        return run

    def history_entry(self, name, verdict="", state="", delete="NONE", mark="NONE", size_mb=0, downloaded_mb=0,
                      extra=None):
        """A finished download in the history, the way NZBGet keeps it."""
        sim = self.sim
        with sim.lock:
            nzb_id = 5000 + len(sim.history)
            entry = S.Entry(nzb_id, name, "")
            if verdict:
                entry.set("Completer_Verdict", verdict)
            if state:
                entry.set("Completer_State", state)
            for key, value in (extra or {}).items():
                entry.set(key, value)
            entry.files = [{"id": nzb_id * 10, "name": "data.rar", "subject": "data.rar", "size": size_mb * 1048576,
                            "posted": 0, "par": False, "vol": False, "paused": False}]
            entry.delete_status = delete
            entry.mark_status = mark
            entry.downloaded_mb = downloaded_mb
            entry.history_time = int(sim.now)
            sim.history.append(entry)
        return nzb_id

    # ------------------------------------------------------------ TestServers

    def test_testservers_on_a_healthy_world_says_so_and_changes_nothing(self):
        sim = self.build()
        run = self.run_command("TestServers")
        self.assertEqual(run.code, 93)
        self.assertTrue(run.says("2 of 2 connections logged in"))
        self.assertTrue(run.says("is not downloading right now"))
        self.assertEqual(sim.edits, [])
        self.assertFalse(self.warned(run))

    def test_testservers_says_when_nzbget_is_busy_downloading(self):
        sim = self.build()
        nzb_id = self.add(True)
        sim.entry(nzb_id).active = 3
        sim.download_rate = 5 * 1048576
        run = self.run_command("TestServers")
        self.assertEqual(run.code, 93)
        self.assertTrue(run.says("is downloading right now (3 connections busy, 5.0 MB/s)"))
        self.assertEqual(sim.edits, [])

    def test_testservers_still_works_when_nzbget_cannot_say_how_busy_it_is(self):
        sim = self.build()
        sim.fault("http500", method="status", times=5)
        sim.fault("http500", method="listgroups", times=5)
        run = self.run_command("TestServers")
        self.assertEqual(run.code, 93)
        self.assertTrue(run.says("could not be asked how busy it is"))
        self.assertEqual(sim.edits, [])

    def test_testservers_reports_every_kind_of_broken_news_server_with_the_failure_code(self):
        cases = (("login refused", {"reject_login": True}, "could not log in"),
                 ("hangs up", {"hangup": True}, "could not log in"),
                 ("not nntp", {"garbage_greeting": True}, "could not log in"),
                 ("bad greeting", {"greeting": "502 go away"}, "could not log in"))
        for label, switches, text in cases:
            with self.subTest(label):
                sim = self.build(**switches)
                run = self.run_command("TestServers")
                self.assertEqual(run.code, 94, run.out)
                self.assertTrue(run.says(text), run.out)
                self.assertTrue(self.warned(run))
                self.assertEqual(sim.edits, [])

    def test_testservers_tells_the_user_to_lower_the_connections_when_the_server_has_none_to_spare(self):
        for style in ("greeting", "greeting-text", "login"):
            with self.subTest(style):
                sim = self.build(max_connections=1, limit_style=style)
                run = self.run_command("TestServers")
                self.assertEqual(run.code, 94, run.out)
                self.assertTrue(run.says("only 1 of 2 connections logged in"), run.out)
                self.assertTrue(run.says("Lower this server's Connections in NZBGet by 1"), run.out)
                self.assertEqual(sim.edits, [])

    def test_testservers_says_so_when_a_server_is_stopped_and_calls_an_optional_one_optional(self):
        sim = self.build()
        second = self.make_news(reject_login=True)
        sim.add_server(second, optional=True, name="spare")
        run = self.run_command("TestServers")
        self.assertEqual(run.code, 94)
        self.assertTrue(run.says("(optional: NZBGet passes over it as well)"))
        self.assertTrue(run.says("2 of 2 connections logged in"))  # the healthy server is still reported fine
        third = self.make_news()
        sim.add_server(third)
        third.stop()  # nothing listens on its port any more
        run = self.run_command("TestServers")
        self.assertEqual(run.code, 94)
        self.assertTrue(run.says("server 3"))
        self.assertEqual(sim.edits, [])

    def test_testservers_without_any_usable_server_fails_clearly(self):
        sim = self.build(options={"IgnoreServers": "1"})
        run = self.run_command("TestServers")
        self.assertEqual(run.code, 94)
        self.assertTrue(run.says("no active news server to test"))
        self.assertEqual(self.news.connections_total, 0)

    def test_testservers_never_prints_a_password_even_when_the_server_echoes_it(self):
        sim = self.build(echo_password=True, reject_login=True, login=("alice-account", "pass-word-9876"))
        run = self.run_command("TestServers")
        self.assertEqual(run.code, 94)
        self.assert_no_secret(["pass-word-9876"])
        self.assertNotIn("alice-account", run.out)

    # ------------------------------------------------------------ ReleaseAll

    def hold_everything(self):
        """Held downloads of every state, plus bystanders. Returns (held ids, bystander ids)."""
        sim = self.sim
        pending = [self.add(True, "Pending release %d" % number) for number in range(2)]
        waiting = self.add(True, "Waiting release")
        failing = self.add(True, "Failing release")
        for nzb_id in pending + [waiting, failing]:
            self.assertEqual(sim.status(nzb_id), "PAUSED")
            self.assertEqual(sim.param(nzb_id, "Completer_State"), "pending")
        sim.entry(waiting).set("Completer_State", "waiting")
        sim.entry(failing).set("Completer_State", "failing")
        users = self.add(True, "Paused by the user", queue_scripts="no", paused=True)
        no_token = self.add(True, "Paused with no token", queue_scripts="no")
        sim.user_pause(no_token)
        passed = self.add(True, "Passed but paused by the user")
        sim.entry(passed).set("Completer_State", "passed")
        sim.user_resume(passed)
        sim.user_pause(passed)
        forged = self.add(True, "Looks held but is not ours", queue_scripts="no")
        sim.entry(forged).set("Completer_State", "pending")  # a state without Completer's token
        sim.user_pause(forged)
        return pending + [waiting, failing], [users, no_token, passed, forged]

    def test_releaseall_resumes_every_held_download_and_nothing_else(self):
        sim = self.build()
        held, others = self.hold_everything()
        before = sim.snapshot(others)
        edits = len(sim.edits)
        run = self.run_command("ReleaseAll")
        self.assertEqual(run.code, 93)
        self.assertTrue(run.says("ReleaseAll: 4 downloads were held by Completer, 4 released"))
        for nzb_id in held:
            self.assertNotEqual(sim.status(nzb_id), "PAUSED")
            self.assertEqual(sim.param(nzb_id, "Completer_State"), "released")
        self.assertEqual(sim.snapshot(others), before)
        self.assertEqual({nzb_id for _c, _t, ids in sim.edits[edits:] for nzb_id in ids}, set(held))
        self.assertNotIn("GroupDelete", [command for command, _t, _i in sim.edits])
        self.assertEqual(sim.where(held[0]), "queue")

    def test_releaseall_a_second_time_has_nothing_left_to_do(self):
        sim = self.build()
        held, _others = self.hold_everything()
        self.assertEqual(self.run_command("ReleaseAll").code, 93)
        edits = len(sim.edits)
        shot = sim.snapshot()
        run = self.run_command("ReleaseAll")
        self.assertEqual(run.code, 93)
        self.assertTrue(run.says("0 downloads were held by Completer, 0 released"))
        self.assertEqual(len(sim.edits), edits)
        self.assertEqual(sim.snapshot(), shot)

    def test_releaseall_works_whatever_the_mode_is(self):
        for mode in ("DryRun", "Off", "Enforce"):
            with self.subTest(mode):
                sim = self.build()
                held, _others = self.hold_everything()
                sim.set_option("Mode", mode)
                run = self.run_command("ReleaseAll")
                self.assertEqual(run.code, 93)
                for nzb_id in held:
                    self.assertNotEqual(sim.status(nzb_id), "PAUSED")

    def test_releaseall_with_nzbget_down_fails_without_a_traceback_and_changes_nothing(self):
        sim = self.build()
        held, _others = self.hold_everything()
        shot = sim.snapshot()
        sim.rpc_down()
        run = self.run_command("ReleaseAll")
        self.assertEqual(run.code, 94)
        self.assertTrue(any(kind == "error" for kind, _text in run.messages))
        sim.rpc_up()
        self.assertEqual(sim.snapshot(), shot)

    def test_releaseall_leaves_a_download_alone_that_the_user_deleted_while_it_ran(self):
        sim = self.build()
        held, others = self.hold_everything()
        original = app.Run.release
        victim = held[1]
        gone = []

        def release(run, subject):
            if subject.nzb_id == victim and not gone:
                gone.append(True)
                sim.user_delete(victim)  # NZBGet's user clicked 'delete' just before this one's turn
            return original(run, subject)

        with mock.patch.object(app.Run, "release", release):
            run = self.run_command("ReleaseAll")
        self.assertTrue(gone)
        self.assertNotIn(victim, {nzb_id for command, _t, ids in sim.edits if command == "GroupResume"
                                  for nzb_id in ids})
        for nzb_id in held:
            if nzb_id != victim:
                self.assertNotEqual(sim.status(nzb_id), "PAUSED")
        self.assertEqual(sim.where(victim), "history")  # still where the user's deletion put it
        self.assertTrue(run.says("4 downloads were held by Completer, 3 released"))
        self.assertTrue(self.warned(run))  # said, not hidden

    # ------------------------------------------------------------ Report

    def test_report_counts_misses_false_alarms_and_savings_and_changes_nothing(self):
        sim = self.build()
        self.history_entry("Passed, then failed on health", "pass/release", "passed", delete="HEALTH", size_mb=900)
        self.history_entry("Flagged, but fine", "fail/fail", "failed", delete="NONE", size_mb=800)
        self.history_entry("Flagged, and failed", "gone/fail", "released", delete="HEALTH", size_mb=700,
                           downloaded_mb=2048)
        self.history_entry("Removed by Completer", "fail/fail", "failed", delete="MANUAL", mark="BAD", size_mb=3072)
        self.history_entry("Only watched", "pass/release", "observed", size_mb=600)
        self.history_entry("No verdict at all", "", "", size_mb=500)
        for _ in range(3):
            self.history_entry("Passed and fine", "pass/release", "passed", size_mb=400)
        run = self.run_command("Report")
        self.assertEqual(run.code, 93)
        self.assertTrue(run.says("8 downloads in the history carry a verdict (1 from a dry run, 7 while holding)"))
        self.assertTrue(run.says("(a miss): 1"))
        self.assertTrue(run.says("(a false alarm, or a post that was still spreading): 1"))
        self.assertTrue(run.says("and failed too (right): 1, 2.0 GB"))
        self.assertTrue(run.says("removed by Completer before anything was downloaded: 1 downloads of 3.0 GB"))
        self.assertTrue(run.says("0 waiting for their first check, 0 waiting for a recheck"))
        self.assertEqual(sim.edits, [])

    def test_report_shows_what_is_on_hold_and_for_how_long(self):
        sim = self.build()
        first = self.add(True, "Held for a while")
        sim.entry(first).set("Completer_Since", str(int(sim.now) - 1500))  # (the safety net would lapse in a wait)
        second = self.add(True, "Held more shortly")
        waiting = self.add(True, "Waiting for the recheck")
        sim.entry(waiting).set("Completer_State", "waiting")
        run = self.run_command("Report")
        self.assertEqual(run.code, 93)
        self.assertTrue(run.says("on hold now: 2 waiting for their first check, 1 waiting for a recheck, the oldest "
                                 "for 25 minutes"))
        self.assertEqual(sim.edits, [])

    def test_report_with_an_empty_history_and_nothing_held_is_short_and_quiet(self):
        sim = self.build()
        run = self.run_command("Report")
        self.assertEqual(run.code, 93)
        self.assertTrue(run.says("0 downloads in the history carry a verdict"))
        self.assertTrue(run.says("on hold now: 0 waiting for their first check, 0 waiting for a recheck"))
        self.assertFalse(run.says("oldest"))

    def test_report_survives_history_entries_with_nonsense_in_them(self):
        sim = self.build()
        self.history_entry("Nonsense sizes", "fail/fail", "failed", delete="MANUAL",
                           extra={"Completer_Since": "not a number"})
        with sim.lock:
            sim.history[-1].files[0]["size"] = 0
        self.history_entry("Odd verdict", "banana", "failed", delete="HEALTH")
        self.history_entry("A verdict that is only a slash", "/", "failed", delete="HEALTH")
        run = self.run_command("Report")
        self.assertEqual(run.code, 93)
        self.assertEqual(sim.edits, [])

    def test_report_with_nzbget_down_fails_without_a_traceback(self):
        sim = self.build()
        sim.rpc_down()
        run = self.run_command("Report")
        self.assertEqual(run.code, 94)
        self.assertTrue(any(kind == "error" for kind, _text in run.messages))

    # ------------------------------------------------------------ nonsense

    def test_an_unknown_command_fails_and_the_name_is_printed_safely(self):
        sim = self.build()
        for name in ("Nonsense", "testservers", "Report\x1b[31m" + "x" * 200, "A\nB"):
            with self.subTest(name=name):
                env = sim.base_env()
                env["NZBCP_COMMAND"] = name
                run = sim._execute("command", env)
                self.assert_calm([run], codes=(94,))
                self.assertTrue(run.says("does not know the command"))
                self.assertNotIn("\x1b", run.out)
        self.assertEqual(sim.edits, [])
        self.assertEqual(self.news.connections_total, 0)


# ======================================================================== secrets


class SecretTests(Base):
    """No password, user name or control password ever reaches a log line, whatever the servers say."""

    LOGIN = ("alice-account", "pass-word-9876")

    def everything_once(self, sim):
        """A queue script, scheduled checks and all three buttons."""
        runs = [sim.sweep()]
        good = self.add(True, "Secret check good")
        runs.extend(sim.pump())
        bad = self.add(False, "Secret check bad")
        runs.extend(sim.pump())
        sim.advance(600)
        runs.append(sim.sweep())
        for name in ("TestServers", "ReleaseAll", "Report"):
            runs.append(sim.command(name))
        self.assert_calm(runs, codes=(0, 93, 94))
        return runs

    def test_a_server_that_echoes_the_password_in_its_errors_leaks_nothing(self):
        for label, switches in (("login refused", {"reject_login": True}), ("articles refused", {})):
            with self.subTest(label):
                sim = self.build(echo_password=True, login=self.LOGIN, options={"Verbose": "yes"}, **switches)
                self.everything_once(sim)
                self.assertIn(self.LOGIN[1], self.news.passwords_seen or [self.LOGIN[1]])
                self.assert_no_secret(self.LOGIN)

    def test_a_wrong_control_password_leaks_nothing_in_any_kind_of_run(self):
        sim = self.build(options={"Verbose": "yes"})
        held = self.add(True, "Held while the password is wrong")
        sim.secrets.add("other-secret")
        sim.set_nzbget("ControlPassword", "other-secret")
        runs = [sim.sweep()]
        runs.extend(sim.command(name) for name in ("TestServers", "ReleaseAll", "Report"))
        runs.append(sim.run_queue_script(held))
        self.assert_calm(runs, codes=(0, 93, 94))
        self.assertEqual(sim.edits, [])  # nothing could be changed, and nothing was
        self.assert_no_secret(["other-secret"])

    def test_credentials_with_odd_characters_work_and_still_leak_nothing(self):
        user, password = "ünï user", "p@ss:w/rd %20 é&=+"
        for name, value in (("CONTROL_USER", user), ("CONTROL_PASSWORD", password)):
            patcher = mock.patch.object(S, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        sim = self.build(login=("news user", "n3ws:p@ss/w0rd"))
        good = self.add(True, "Odd credentials", run=True)
        self.assertEqual(sim.param(good, "Completer_State"), "passed")
        self.assertNotEqual(sim.status(good), "PAUSED")
        self.assertEqual(sim.command("TestServers").code, 93)
        self.assertEqual(sim.command("ReleaseAll").code, 93)
        self.assert_no_secret([password, "n3ws:p@ss/w0rd"])


# ======================================================================== odd starts and odd settings


class OddStartTests(Base):
    def test_started_by_hand_it_explains_itself_in_one_line_and_changes_nothing(self):
        stream = io.StringIO()
        self.addCleanup(log.set_stream, None)
        code = app.main({"PATH": "/usr/bin"}, stream, time.time, lambda _code: self.fail("hard exit"))
        self.assertEqual(code, 2)
        lines = stream.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("[ERROR] "))

    def test_a_mode_that_is_not_spelled_right_is_a_dry_run_and_never_acts(self):
        for mode in ("banana", "", "enforc", "yes"):
            with self.subTest(mode=mode):
                sim = self.build(mode=mode)
                nzb_id = self.add(False, run=True)
                self.assertEqual(sim.status(nzb_id), "QUEUED")
                self.assertEqual(sim.where(nzb_id), "queue")
                self.assertEqual(sim.param(nzb_id, "Completer_State"), "observed")
                self.assertEqual(sim.edits, [])
                self.assertTrue(any("would be failed" in text for _kind, text in sim.log))

    def test_an_option_that_is_missing_altogether_means_dry_run(self):
        sim = self.build(boot=False)
        nzb_id = self.add(False)
        run = sim.run_queue_script(nzb_id, env_remove=unset(["NZBPO_Mode"]))
        self.assert_calm([run])
        self.assertEqual(sim.where(nzb_id), "queue")
        self.assertEqual(sim.edits, [])
        self.assertEqual(sim.param(nzb_id, "Completer_State"), "observed")

    def test_mode_is_read_without_regard_to_case_and_blanks(self):
        sim = self.build(mode="  ENFORCE ")
        nzb_id = self.add(False, run=True)
        self.assertEqual(sim.where(nzb_id), "history")
        self.assertEqual(sim.sonarr_view(nzb_id), "failed")
        sim.set_option("Mode", " off ")
        other = self.add(False, run=True)
        self.assertEqual(sim.status(other), "QUEUED")
        self.assertNotIn(other, sim.edited_ids())
        self.assertIsNone(sim.param(other, "Completer_State"))  # off: not even a note

    def test_settings_outside_their_range_are_pulled_back_into_it(self):
        sim = self.build(boot=False)
        table = (("Connections", ("500", "0", "-3", "abc", "nan", "inf", "3.9", ""), "connections",
                  (8, 1, 1, 2, 2, 2, 3, 2)),
                ("FreshHours", ("-5", "1000", "abc", "0.5"), "fresh_seconds", (0, 72 * 3600, 7200, 1800)),
                 ("MaxArticles", ("5", "1000000000", "abc", "-1"), "max_articles", (100, 20000, 1200, 100)),
                 ("TimeLimit", ("0", "99999", "x", "12"), "time_limit", (10, 300, 45, 12)),
                 ("MaxFillPercent", ("250", "-10", "abc", "50"), "max_fill", (1.0, 0.0, 1.0, 0.5)))
        for option, values, attribute, wanted in table:
            for value, expected in zip(values, wanted):
                with self.subTest(option=option, value=value):
                    sim.set_option(option, value)
                    self.assertEqual(getattr(cfg.Settings(sim.base_env()), attribute), expected)
                    sim.set_option(option, "")

    def test_extreme_settings_all_at_once_still_check_properly(self):
        extremes = {"Connections": "500", "FreshHours": "-5", "MaxArticles": "5", "TimeLimit": "0",
                    "MaxFillPercent": "250"}
        sim = self.build(options=extremes)
        good = self.add(True, "Extreme good", post_kw={"segments": 60}, run=True)
        self.assertEqual(sim.param(good, "Completer_State"), "passed")
        self.assertNotEqual(sim.status(good), "PAUSED")
        bad = self.add(False, "Extreme bad", run=True)
        self.assertEqual(sim.sonarr_view(bad), "failed")
        self.assertLessEqual(self.news.max_concurrent, 8)
        self.assertEqual(sim.edited_ids() - {good, bad}, set())

    def test_nzbget_that_cannot_be_reached_the_way_it_is_configured_leaves_a_held_download_held(self):
        cases = (("no credentials", {}, ("NZBOP_ControlUsername", "NZBOP_ControlPassword")),
                 ("a host that does not exist", {"NZBOP_ControlIP": "999.999.999.999"}, ()),
                 ("only ipv6", {"NZBOP_ControlIP": "::"}, ()),
                 ("port zero", {"NZBOP_ControlPort": "0"}, ()),
                 ("port beyond the range", {"NZBOP_ControlPort": "70000"}, ()))
        for label, update, remove in cases:
            with self.subTest(label):
                sim = self.build()
                nzb_id = self.add(True)
                run = sim.run_queue_script(nzb_id, env_update=spelled(update), env_remove=unset(remove))
                self.assert_calm([run])
                self.assertEqual(sim.status(nzb_id), "PAUSED")
                self.assertEqual(sim.param(nzb_id, "Completer_State"), "pending")
                self.assertEqual(sim.edits, [])
                self.assertTrue(self.warned(run), run.out)
                sim.advance(200)  # (a held download is left to its own queue script for the first 90 seconds)
                self.assert_calm([sim.sweep()])  # the next scheduled check, with the right settings, lets it go
                self.assertNotEqual(sim.status(nzb_id), "PAUSED")

    def test_a_control_ip_of_all_interfaces_means_this_machine(self):
        sim = self.build()
        nzb_id = self.add(True)
        run = sim.run_queue_script(nzb_id, env_update=spelled({"NZBOP_ControlIP": "0.0.0.0"}))
        self.assert_calm([run])
        self.assertEqual(sim.param(nzb_id, "Completer_State"), "passed")
        self.assertNotEqual(sim.status(nzb_id), "PAUSED")

    # ------------------------------------------------------------ the temporary folder

    def unusable_temp(self, sim):
        path = os.path.join(sim.root, "a-file-not-a-folder")
        with open(path, "w") as handle:
            handle.write("x")
        for option in ("TempDir",):
            sim.set_nzbget(option, path)
        return path

    def test_without_a_usable_temp_folder_nothing_is_held_and_nothing_goes_wrong(self):
        sim = self.build(boot=False)
        self.unusable_temp(sim)
        sweep = sim.sweep()
        self.assert_calm([sweep])
        self.assertTrue(sweep.says("cannot record that it is alive"))
        good = self.add(True, "No folder good", run=True)
        bad = self.add(False, "No folder bad", run=True)
        self.assertEqual(sim.where(good), "queue")
        self.assertNotEqual(sim.status(good), "PAUSED")  # never held: there is no safety net to release it
        self.assertEqual(sim.where(bad), "history")  # a truly bad post is still failed, by its own script output
        self.assertEqual(sim.sonarr_view(bad), "failed")
        self.assertEqual(sim.edits, [])  # which needs no call to NZBGet at all
        self.assertTrue(any("not put on hold" in text for _kind, text in sim.log))
        self.assertEqual([command for run in sim.runs for command in run.commands].count("MARK=BAD"), 1)

    def test_a_download_that_is_already_held_is_still_released_when_the_temp_folder_goes_bad(self):
        sim = self.build()
        good = self.add(True, "Held before the folder went bad")
        bad = self.add(False, "Bad before the folder went bad", post_kw={"age_hours": 48.0})
        self.unusable_temp(sim)
        sim.advance(200)
        self.assert_calm([sim.sweep()])
        self.assertNotEqual(sim.status(good), "PAUSED")
        self.assertEqual(sim.param(good, "Completer_State"), "passed")
        self.assertEqual(sim.sonarr_view(bad), "failed")
        self.assertEqual(sim.edited_ids() - {good, bad}, set())

    def test_a_failure_still_ends_properly_without_a_temp_folder_for_its_hint(self):
        sim = self.build()
        bad = self.add(False, "Bad, no folder")
        self.unusable_temp(sim)
        sim.advance(200)
        self.assert_calm([sim.sweep()])
        self.assertEqual(sim.where(bad), "history")
        self.assertEqual(sim.sonarr_view(bad), "failed")
        self.assertEqual(sim.param(bad, "Completer_State"), "failed")


# ======================================================================== odd nzb files


HAND_HEAD = '<?xml version="1.0" encoding="UTF-8"?>\n<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">\n'


def hand_nzb(ids, subject='[x] - &quot;data.rar&quot; yEnc (1/%d)', posted=None, head=HAND_HEAD, date=True):
    """An nzb written by hand; ``ids`` are written as given (so with XML escapes), one segment each."""
    stamp = ' date="%d"' % (posted if posted is not None else int(time.time()) - 48 * 3600) if date else ""
    out = [head, '<file poster="x@test.invalid"%s subject="%s">' % (stamp, subject % len(ids) if "%d" in subject
                                                                      else subject),
           "<groups><group>alt.binaries.test</group></groups><segments>"]
    for number, msgid in enumerate(ids, 1):
        out.append('<segment bytes="700000" number="%d">%s</segment>' % (number, msgid))
    out.append("</segments></file>\n</nzb>\n")
    return "".join(out)


class OddNzbTests(Base):
    def run_odd(self, source, name="Odd nzb", have=(), expect_state=None):
        sim = self.sim
        self.news.have.update(have)
        nzb_id = sim.add(source, name, run=False)
        runs = sim.pump()
        self.assert_calm(runs)
        if expect_state is not None:
            self.assertEqual(sim.param(nzb_id, "Completer_State"), expect_state)
        return nzb_id

    def assert_let_through(self, nzb_id):
        sim = self.sim
        self.assertEqual(sim.where(nzb_id), "queue")
        self.assertNotEqual(sim.status(nzb_id), "PAUSED")
        self.assertNotIn("GroupDelete", self.commands_for(nzb_id))
        self.assertEqual(sim.param(nzb_id, "Completer_State"), "released")

    def test_a_huge_nzb_is_sampled_not_read_through(self):
        sim = self.build()
        post = self.make_post(True, "big", data_files=20, segments=1200, par_files=1, par_segments=100)
        nzb_id = sim.add(post, "A huge release", run=False)
        self.assert_calm(sim.pump())
        self.assertEqual(sim.param(nzb_id, "Completer_State"), "passed")
        self.assertLessEqual(self.news.stats_total, 1200)
        self.assertGreater(self.news.stats_total, 50)

    def test_an_nzb_of_one_article_is_checked_like_any_other(self):
        sim = self.build()
        post = self.make_post(True, "one", data_files=1, segments=1, par_files=0)
        nzb_id = sim.add(post, "One article", run=False)
        self.assert_calm(sim.pump())
        self.assertEqual(sim.param(nzb_id, "Completer_State"), "passed")
        self.assertEqual(self.news.stats_total, 1)
        post = self.make_post(False, "one", data_files=1, segments=1, par_files=0)
        nzb_id = sim.add(post, "One missing article", run=False)
        self.assert_calm(sim.pump())  # a single article that is absent proves little: it is let through
        self.assert_let_through(nzb_id)

    def test_an_nzb_with_nothing_but_repair_files_is_let_through_not_failed(self):
        sim = self.build()
        for good in (True, False):
            with self.subTest(good=good):
                post = self.make_post(good, "par", data_files=0, par_files=1, par_segments=4)
                nzb_id = sim.add(post, "Repair files only %s" % good, run=False)
                self.assert_calm(sim.pump())
                self.assertEqual(sim.where(nzb_id), "queue")
                self.assertNotEqual(sim.status(nzb_id), "PAUSED")
                self.assertNotIn("GroupDelete", self.commands_for(nzb_id))
                self.assertIn(sim.param(nzb_id, "Completer_State"), ("passed", "released"))

    def test_a_gzip_nzb_is_read_like_a_plain_one(self):
        sim = self.build()
        post = self.make_post(True, "gz")
        nzb_id = sim.add(gzip.compress(post.xml.encode("utf-8")), "Zipped release", run=False)
        self.assert_calm(sim.pump())
        self.assertEqual(sim.param(nzb_id, "Completer_State"), "passed")
        self.assertGreaterEqual(self.news.stats_total, 20)
        self.assertLessEqual(set(self.news.stat_log), set(post.all_ids))

    def test_xml_escapes_in_message_ids_are_unescaped_before_asking(self):
        sim = self.build()
        ids = ["one&amp;two-%d@x.invalid" % number for number in range(5)] + ["&lt;wrapped-%d@x.invalid&gt;" % n
                                                                              for n in range(5)]
        self.news.have.update(["one&two-%d@x.invalid" % number for number in range(5)] +
                              ["wrapped-%d@x.invalid" % number for number in range(5)])
        nzb_id = sim.add(hand_nzb(ids), "Escaped ids", run=False)
        self.assert_calm(sim.pump())
        self.assertEqual(sim.param(nzb_id, "Completer_State"), "passed")
        self.assertEqual(sorted(self.news.stat_log), sorted(self.news.have))

    def test_message_ids_that_could_smuggle_a_command_are_never_sent(self):
        sim = self.build()
        ids = ["fine-%d@x.invalid" % number for number in range(8)]
        self.news.have.update(ids)
        evil = ["bad id@x.invalid", "bad&#10;QUIT@x.invalid", "bad&#13;&#10;GROUP x@x.invalid", "x" * 300]
        nzb_id = sim.add(hand_nzb(ids + evil), "Evil ids", run=False)
        self.assert_calm(sim.pump())
        self.assertEqual(set(self.news.stat_log), set(ids))
        self.assertEqual(sim.param(nzb_id, "Completer_State"), "passed")
        for line in self.news.commands:
            self.assertNotIn("QUIT@", line)

    def test_an_nzb_that_declares_xml_entities_is_not_expanded_and_the_download_is_let_through(self):
        sim = self.build()
        head = HAND_HEAD.replace("<nzb", '<!DOCTYPE nzb [<!ENTITY lol "lol">]>\n<nzb', 1)
        ids = ["ent-%d@x.invalid" % number for number in range(5)]
        nzb_id = self.run_odd(hand_nzb(ids, head=head), "Entities")
        self.assert_let_through(nzb_id)
        self.assertEqual(self.news.stats_total, 0)

    def test_an_nzb_without_dates_waits_for_the_hold_limit_and_a_good_one_passes_at_once(self):
        sim = self.build()
        post = self.make_post(True, "nd")
        good = sim.add(re.sub(r' date="\d+"', "", post.xml), "Undated good", run=False)
        self.assert_calm(sim.pump())
        self.assertEqual(sim.param(good, "Completer_State"), "passed")
        post = self.make_post(False, "nd")
        bad = sim.add(re.sub(r' date="\d+"', "", post.xml), "Undated bad", run=False)
        self.assert_calm(sim.pump())
        self.assertEqual(sim.param(bad, "Completer_State"), "waiting")  # it might be a post that is still spreading
        began = sim.now
        for _ in range(60):
            if sim.param(bad, "Completer_State") != "waiting":
                break
            sim.advance(300)
            self.assert_calm([sim.sweep()])
            if sim.now - began < 3600:
                self.assertEqual(sim.param(bad, "Completer_State"), "waiting")
        self.assertEqual(sim.param(bad, "Completer_State"), "failed")  # the waiting is bounded
        self.assertLessEqual(sim.now - began, 4 * 3600)
        self.assertEqual(sim.sonarr_view(bad), "failed")
        self.assertEqual(sim.edited_ids() - {bad, good}, set())

    def replace_file(self, nzb_id, data):
        with open(self.sim.entry(nzb_id).queued_file, "wb") as handle:
            handle.write(data)

    def test_an_nzb_file_that_is_not_an_nzb_when_it_is_checked_lets_the_download_through(self):
        post_xml = self.post("ok").xml
        cases = (("empty", b""), ("text", b"this is not xml"), ("binary", bytes(range(256)) * 10),
                 ("truncated", post_xml.encode()[:200]), ("no files", (HAND_HEAD + "</nzb>\n").encode()),
                 ("damaged gzip", b"\x1f\x8b\x08\x00garbage"), ("other nzb", self.post("x2").xml.encode()),
                 ("utf-16", post_xml.encode("utf-16")))
        for label, data in cases:
            with self.subTest(label):
                sim = self.build()
                nzb_id = self.add(True, "Replaced file %s" % label)
                self.replace_file(nzb_id, data)
                (run,) = [sim.run_queue_script(nzb_id)]
                self.assert_calm([run])
                self.assert_let_through(nzb_id)
                self.assertEqual(self.news.stats_total, 0)

    @staticmethod
    def remove_file(path):
        os.remove(path)

    @staticmethod
    def empty_file(path):
        with open(path, "wb"):
            pass

    @staticmethod
    def append_to_file(path):
        with open(path, "ab") as handle:
            handle.write(b"\n<!-- more -->\n")

    def test_a_missing_or_replaced_nzb_file_is_found_out_by_the_scheduled_check_too(self):
        for label, action in (("removed", self.remove_file), ("emptied", self.empty_file),
                              ("changed", self.append_to_file)):
            with self.subTest(label):
                sim = self.build()
                nzb_id = self.add(False, "File %s" % label)
                action(sim.entry(nzb_id).queued_file)
                sim.advance(300)
                self.assert_calm([sim.sweep()])
                self.assert_let_through(nzb_id)  # a download that cannot be checked is not failed
                self.assertEqual(self.news.stats_total, 0)

    def test_a_queue_script_told_about_a_file_that_is_not_there_does_not_crash(self):
        sim = self.build()
        nzb_id = self.add(True, "Vanished file")
        run = sim.run_queue_script(nzb_id, env_update={"NZBNA_QUEUEDFILE": os.path.join(sim.root, "nothing.nzb")})
        self.assert_calm([run])
        self.assert_let_through(nzb_id)
        run = sim.run_queue_script(nzb_id, env_update={"NZBNA_QUEUEDFILE": sim.root})  # a folder
        self.assert_calm([run])


# ======================================================================== what a run leaves on disk


class HygieneTests(Base):
    """Real processes: no cache folders next to the scripts, nothing on standard error, no warnings."""

    def test_every_kind_of_run_leaves_the_extension_folder_exactly_as_it_was(self):
        folder = tempfile.mkdtemp(prefix="nzbext-")
        self.addCleanup(shutil.rmtree, folder, True)
        copy = os.path.join(folder, "Completer")
        shutil.copytree(S.EXTENSION, copy, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        listing = sorted(os.path.join(root, name) for root, dirs, files in os.walk(copy) for name in dirs + files)
        patcher = mock.patch.object(S, "MAIN", os.path.join(copy, "main.py"))
        patcher.start()
        self.addCleanup(patcher.stop)
        sim = self.build(inprocess=False)  # the first scheduled check is a real process
        good = self.add(True, "Hygiene good", run=True)  # a scan and a queue script
        bad = self.add(False, "Hygiene bad", run=True)
        runs = [sim.sweep()] + [sim.command(name) for name in ("TestServers", "ReleaseAll", "Report", "Nonsense")]
        self.assert_calm(runs + sim.runs, codes=(0, 93, 94))
        self.assertEqual({run.kind for run in sim.runs}, {"scan", "queue", "sweep", "command"})
        self.assertEqual(sim.param(good, "Completer_State"), "passed")
        self.assertEqual(sim.sonarr_view(bad), "failed")
        after = sorted(os.path.join(root, name) for root, dirs, files in os.walk(copy) for name in dirs + files)
        self.assertEqual(after, listing)
        self.assertEqual([item for item in after if "__pycache__" in item or item.endswith(".pyc")], [])

    def run_by_hand(self, extra_args=(), env=None):
        base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.sim.root, "LANG": "C.UTF-8"}
        base.update(env or {})
        return subprocess.run([sys.executable] + list(extra_args) + [S.MAIN], env=base, cwd=self.sim.root,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=60)

    def test_started_by_hand_as_a_real_process_it_explains_itself_and_exits_with_2(self):
        self.build(boot=False)
        done = self.run_by_hand(["-W", "error"])
        self.assertEqual(done.returncode, 2)
        self.assertEqual(done.stderr, b"")
        lines = done.stdout.decode().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("[ERROR] "))

    def test_no_run_triggers_a_python_warning_of_any_kind(self):
        sim = self.build(inprocess=False)
        env = sim.base_env()
        env["NZBCP_COMMAND"] = "Report"
        done = self.run_by_hand(["-W", "error"], env)
        self.assertEqual(done.returncode, 93, done.stdout)
        self.assertEqual(done.stderr, b"")
        env = sim.base_env()
        env["NZBCP_COMMAND"] = "TestServers"
        done = self.run_by_hand(["-W", "error"], env)
        self.assertEqual(done.returncode, 93, done.stdout)
        self.assertEqual(done.stderr, b"")
        env = sim.base_env()
        env["NZBSP_TASKID"] = "1"
        done = self.run_by_hand(["-W", "error"], env)
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertEqual(done.stderr, b"")

    def test_every_source_file_compiles_without_warnings(self):
        found = []
        for root, _dirs, files in os.walk(S.EXTENSION):
            found.extend(os.path.join(root, name) for name in files if name.endswith(".py"))
        self.assertGreaterEqual(len(found), 8)
        import warnings
        for path in found:
            with open(path, "rb") as handle:
                source = handle.read()
            with warnings.catch_warnings():
                warnings.simplefilter("error")
                compile(source, path, "exec", dont_inherit=True)
