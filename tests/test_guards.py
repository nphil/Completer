"""The safety guarantees of Completer, proved against the fake NZBGet.

The one that matters most: **Completer only ever acts on the one download it checked.** The extension it
replaces applied the verdict of one download to every other paused download and deleted their files. Here a
backlog of other downloads sits in the queue (some good, some bad, with and without Completer's parameters),
one download is checked, and every observable detail of the backlog must be exactly as it was.

The rules the fake NZBGet enforces on every single call (one positive integer id, only Completer's own
edit commands and parameters, only downloads that carry a token, ``allowed_ids``) fail a test at its end;
the tests below add the explicit assertions over the recorded calls and over what ends up in the queue.
"""

import ast
import os
import random
import re
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import nzbgetsim as S  # noqa: E402
from completer import app, nzbfile  # noqa: E402

TEMPLATE = S.EXTENSION
SOURCES = os.path.join(TEMPLATE, "completer")
PARAMETER_NAMES = ("Completer_Token", "Completer_State", "Completer_Since", "Completer_Checks", "Completer_Next",
                   "Completer_File", "Completer_Sum", "Completer_Info", "Completer_Verdict")
SIX = ("GroupResume", "GroupPauseExtraPars", "GroupSetParameter", "GroupDelete", "HistoryMarkBad",
       "HistorySetParameter")
READS = ("listgroups", "history", "status", "editqueue")
SMALL = {"data_files": 2, "segments": 20, "par_files": 1, "par_segments": 4}  # 44 articles: checked completely
HOUR = 3600


class GuardCase(S.SimTestCase):
    """Shared helpers: small posts that are good or bad on the one fake news server, and call audits."""

    def setUp(self):
        patcher = mock.patch.object(time, "sleep", lambda seconds: None)  # the checker and the history loop sleep
        patcher.start()
        self.addCleanup(patcher.stop)

    # ------------------------------------------------------------ posts

    def good(self, tag, age_hours=48, **kwargs):
        """A post whose articles every server has: its verdict is pass."""
        post = self.post(tag, age_hours=age_hours, **dict(SMALL, **kwargs))
        self.sim.news[0].have.update(post.all_ids)
        return post

    def bad(self, tag, age_hours=48, **kwargs):
        """A post that lost half of its content and all its repair data, but not all of it: verdict fail."""
        post = self.post(tag, age_hours=age_hours, **dict(SMALL, **kwargs))
        self.sim.news[0].have.update(post.data_ids[:len(post.data_ids) // 2])
        return post

    def fresh_bad(self, tag, **kwargs):
        """The same, but posted half an hour ago: the articles may still be on their way, so it waits."""
        return self.bad(tag, age_hours=0.5, **kwargs)

    # ------------------------------------------------------------ audits

    def methods(self):
        return {call.method.lower() for call in self.sim.calls}

    def edit_calls(self):
        return [call for call in self.sim.calls if call.method.lower() == "editqueue"]

    def assert_calls_are_clean(self, allowed=None):
        """Every recorded call is a read or one of Completer's edits, and every edit names exactly one id."""
        sim = self.sim
        self.assertLessEqual(self.methods(), set(READS))
        self.assertFalse(sim.download_paused)
        for call in sim.calls:
            self.assertFalse(call.problems, call)
            if call.method.lower() != "editqueue":
                continue
            self.assertEqual(len(call.params), 3, call)
            self.assertIn(call.command, SIX, call)
            self.assertEqual(len(call.ids), 1, call)
            self.assertIs(type(call.ids[0]), int, call)
            self.assertGreater(call.ids[0], 0, call)
            if call.command in ("GroupSetParameter", "HistorySetParameter"):
                self.assertRegex(call.text, r"^Completer_[A-Za-z]+=", call)
                self.assertIn(call.text.split("=", 1)[0], PARAMETER_NAMES, call)
            else:
                self.assertEqual(call.text, "", call)
        if allowed is not None:
            self.assertLessEqual(sim.edited_ids(), set(allowed))

    def sweep(self, times=1, step=0):
        for _ in range(times):
            if step:
                self.sim.advance(step)
            self.assertEqual(self.sim.sweep().code, 0)

    def say_nothing_secret(self):
        """No line other than an [NZB] command carries a token."""
        tokens = {value for entry in self.sim.queue + self.sim.history for name, value in entry.params
                  if name == "Completer_Token"}
        for run in self.sim.runs:
            for line in run.lines:
                if line.startswith("[NZB] "):
                    continue
                for token in tokens:
                    self.assertNotIn(token, line)


# ======================================================================================================
# A. The regression: a backlog of other downloads is never touched
# ======================================================================================================

class BacklogChecks(object):
    """Twelve paused downloads of other people sit in the queue; one more is checked; nothing else changes.

    ``QUEUE_SCRIPTS`` False: the backlog never had Completer's switch (``Completer:``). True: it has the
    switch but no token, because its queue script never ran (they were added paused, so the scan left them).
    """

    QUEUE_SCRIPTS = False

    def setUp(self):
        super(BacklogChecks, self).setUp()
        sim = self.world(options={"Verbose": "yes"})
        self.backlog = []
        for number in range(12):
            tag = "backlog%02d" % number
            post = self.good(tag) if number % 2 == 0 else self.bad(tag)
            self.backlog.append(sim.add(post, "Backlog %02d" % number, category="tv", paused=True,
                                        queue_scripts=self.QUEUE_SCRIPTS, run=False))
        sim.events[:] = []  # none of their queue scripts ran
        self.before = sim.snapshot(self.backlog)
        self.file_counts = {nzb_id: len(sim.entry(nzb_id).files) for nzb_id in self.backlog}
        for nzb_id in self.backlog:
            self.assertEqual(sim.param(nzb_id, "Completer_Token", ""), "")  # the scan left them alone
            self.assertEqual(sim.status(nzb_id), "PAUSED")

    def trigger(self, post, name="The trigger", run=True):
        """Add the one download that is checked; the only id Completer may edit."""
        sim = self.sim
        nzb_id = sim.add(post, name, category="tv", run=False)
        sim.allowed_ids = {nzb_id}
        sim.events[:] = [event for event in sim.events if event[1] == nzb_id]
        if run:
            sim.pump()
        else:
            sim.events[:] = []
        return nzb_id

    def assert_backlog_untouched(self, allowed):
        sim = self.sim
        self.assertEqual(sim.snapshot(self.backlog), self.before)
        for nzb_id in self.backlog:
            self.assertEqual(sim.where(nzb_id), "queue")
            self.assertEqual(len(sim.entry(nzb_id).files), self.file_counts[nzb_id])
            self.assertEqual(sim.status(nzb_id), "PAUSED")
        self.assert_calls_are_clean(allowed)
        self.assertEqual(sim.edited_ids() & set(self.backlog), set())

    def test_a_bad_download_is_failed_and_the_backlog_is_not(self):
        trigger = self.trigger(self.bad("t1"))
        self.assertEqual(self.sim.where(trigger), "history")
        self.assertEqual(self.sim.sonarr_view(trigger), "failed")
        self.assertEqual(self.sim.param(trigger, "Completer_State"), "failed")
        self.assert_backlog_untouched({trigger})

    def test_a_good_download_is_released_and_the_backlog_is_not(self):
        trigger = self.trigger(self.good("t1"))
        self.assertEqual(self.sim.where(trigger), "queue")
        self.assertEqual(self.sim.status(trigger), "QUEUED")
        self.assertEqual(self.sim.param(trigger, "Completer_State"), "passed")
        self.assertEqual(self.sim.edited_ids(), {trigger})
        self.assert_backlog_untouched({trigger})

    def test_a_fresh_bad_download_waits_and_the_backlog_is_not_touched(self):
        trigger = self.trigger(self.fresh_bad("t1"))
        self.assertEqual(self.sim.status(trigger), "PAUSED")
        self.assertEqual(self.sim.param(trigger, "Completer_State"), "waiting")
        self.assertGreater(float(self.sim.param(trigger, "Completer_Next")), self.sim.now)
        self.assert_backlog_untouched({trigger})

    def test_sweeps_while_the_trigger_waits_do_not_touch_the_backlog(self):
        trigger = self.trigger(self.fresh_bad("t1"))
        self.sweep(3, step=120)
        self.assert_backlog_untouched({trigger})
        self.assertEqual(self.sim.status(trigger), "PAUSED")

    def test_a_long_run_of_sweeps_ends_with_the_trigger_failed_and_the_backlog_intact(self):
        trigger = self.trigger(self.fresh_bad("t1"))
        for _ in range(50):  # 50 rounds of five minutes: past the two fresh hours of the post
            self.sweep(step=300)
            self.assert_backlog_untouched({trigger})
        self.assertEqual(self.sim.where(trigger), "history")
        self.assertEqual(self.sim.sonarr_view(trigger), "failed")
        self.assertEqual({call.ids[0] for call in self.edit_calls()}, {trigger})
        self.assertIn("GroupDelete", [call.command for call in self.edit_calls()])  # the sweep's own route

    def test_a_pending_download_that_the_sweep_fails_is_the_only_one_removed(self):
        trigger = self.trigger(self.bad("t1"), run=False)  # its queue script never ran
        self.sweep(step=100)
        self.assertEqual(self.sim.where(trigger), "history")
        self.assertEqual(self.sim.sonarr_view(trigger), "failed")
        self.assertEqual(self.sim.edited_ids(), {trigger})
        self.assert_backlog_untouched({trigger})

    def test_the_commands_do_not_touch_the_backlog(self):
        trigger = self.trigger(self.fresh_bad("t1"))
        for name, code in (("TestServers", 93), ("Report", 93)):
            edits = len(self.edit_calls())
            self.assertEqual(self.sim.command(name).code, code)
            self.assertEqual(len(self.edit_calls()), edits, name)  # these two never change anything
            self.assert_backlog_untouched({trigger})
        self.assertEqual(self.sim.command("ReleaseAll").code, 93)
        self.assertEqual(self.sim.status(trigger), "QUEUED")  # the one download Completer was holding
        self.assertEqual(self.sim.edited_ids(), {trigger})
        self.assert_backlog_untouched({trigger})

    def test_switching_to_dry_run_or_off_does_not_touch_the_backlog(self):
        trigger = self.trigger(self.fresh_bad("t1"))
        self.sim.set_option("Mode", "DryRun")
        self.sweep(step=60)  # a held download is let go when the mode is no longer Enforce
        self.assertEqual(self.sim.status(trigger), "QUEUED")
        second = self.trigger(self.bad("t2"), name="Second")
        self.sim.allowed_ids = {trigger, second}
        self.sweep(step=60)
        self.assertEqual(self.sim.where(second), "queue")  # a dry run fails nothing
        self.assertEqual(self.sim.param(second, "Completer_State", ""), "observed")  # all a dry run writes
        self.assertEqual(self.sim.param(second, "Completer_Token", ""), "")
        self.assert_backlog_untouched({trigger, second})
        self.sim.set_option("Mode", "Off")
        calls = len(self.sim.calls)
        third = self.sim.add(self.bad("t3"), "Third", category="tv")
        self.assertEqual(len(self.sim.calls), calls)  # Off: the queue script does not even ask NZBGet
        self.sweep(3, step=300)
        self.sim.allowed_ids = {trigger, second, third}
        self.assertEqual(self.sim.status(third), "QUEUED")
        self.assert_backlog_untouched({trigger, second})

    def test_a_download_with_the_name_of_a_backlog_download_is_not_confused_with_it(self):
        # the sweep's route goes through the guard, which names downloads by id and checks the token and name
        trigger = self.trigger(self.bad("t1"), name="Backlog 01", run=False)
        self.sweep(step=100)
        self.assertEqual(self.sim.where(trigger), "history")
        self.assertEqual(self.sim.where(self.backlog[1]), "queue")
        self.assertEqual(self.sim.edited_ids(), {trigger})
        self.assert_backlog_untouched({trigger})

    def test_three_new_downloads_one_after_the_other_each_only_touch_themselves(self):
        sim = self.sim
        ids = []
        for kind, post in (("bad", self.bad("t1")), ("good", self.good("t2")), ("fresh", self.fresh_bad("t3"))):
            ids.append(self.trigger(post, name="Trigger " + kind))
            sim.allowed_ids = set(ids)
        self.assertEqual(sim.where(ids[0]), "history")
        self.assertEqual(sim.status(ids[1]), "QUEUED")
        self.assertEqual(sim.status(ids[2]), "PAUSED")
        self.assert_backlog_untouched(set(ids))


class BacklogWithoutSwitchTests(BacklogChecks, GuardCase):
    QUEUE_SCRIPTS = False


class BacklogWithSwitchTests(BacklogChecks, GuardCase):
    QUEUE_SCRIPTS = True


if __name__ == "__main__":
    unittest.main()
