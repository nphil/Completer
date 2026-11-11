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


# ======================================================================================================
# B. Downloads that carry a token and wait: each is only ever rechecked for itself, and only when due
# ======================================================================================================

class WaitingBacklogTests(GuardCase):
    """Three fresh bad downloads wait (first check at minute 30), three more joined five minutes later."""

    def setUp(self):
        super(WaitingBacklogTests, self).setUp()
        sim = self.world()
        self.posts = {}
        self.early = [self.queue_fresh(sim, "early%d" % number, "Early %d" % number) for number in range(3)]
        sim.advance(300)
        self.late = [self.queue_fresh(sim, "late%d" % number, "Late %d" % number) for number in range(3)]
        self.every = self.early + self.late
        for nzb_id in self.every:
            self.assertEqual(sim.param(nzb_id, "Completer_State"), "waiting")
            self.assertEqual(sim.status(nzb_id), "PAUSED")
        self.assertLess(max(float(sim.param(nzb_id, "Completer_Next")) for nzb_id in self.early),
                        min(float(sim.param(nzb_id, "Completer_Next")) for nzb_id in self.late))

    def queue_fresh(self, sim, tag, name, age_hours=0.5, category="tv"):
        post = self.fresh_bad(tag) if age_hours == 0.5 else self.bad(tag, age_hours=age_hours)
        nzb_id = sim.add(post, name, category=category)
        self.posts[nzb_id] = post
        return nzb_id

    def test_a_sweep_leaves_waiting_downloads_alone_until_they_are_due(self):
        sim = self.sim
        before = sim.snapshot()
        calls = len(sim.calls)
        for _ in range(3):  # to 60 seconds before the first one is due
            self.sweep(step=80)
        self.assertEqual(sim.snapshot(), before)
        self.assertEqual([call.method for call in sim.calls[calls:]], ["listgroups"] * 3)  # it only looked

    def test_only_the_downloads_that_are_due_are_rechecked_and_only_for_themselves(self):
        sim = self.sim
        late_before = sim.snapshot(self.late)
        sim.advance(350)  # the first three are due now, the others in 250 seconds
        sim.allowed_ids = set(self.early)
        self.sweep()
        self.assertEqual(sim.snapshot(self.late), late_before)
        for nzb_id in self.early:
            self.assertEqual(sim.param(nzb_id, "Completer_Checks"), "2")
            self.assertEqual(sim.status(nzb_id), "PAUSED")
        self.assertEqual(sim.edited_ids(), set(self.early))
        self.assert_calls_are_clean(self.early)
        for nzb_id in self.late:
            self.assertEqual(sim.param(nzb_id, "Completer_Checks"), "1")

    def test_a_recheck_changes_nothing_but_the_completer_parameters_of_that_download(self):
        sim = self.sim
        sim.advance(350)
        before = sim.snapshot(self.early)
        self.sweep()
        after = sim.snapshot(self.early)
        for nzb_id in self.early:
            self.assertEqual(before[nzb_id]["files"], after[nzb_id]["files"])
            for key in ("where", "name", "category", "delete", "mark", "status"):
                self.assertEqual(before[nzb_id][key], after[nzb_id][key], key)
            old, new = dict(before[nzb_id]["params"]), dict(after[nzb_id]["params"])
            changed = {name for name in set(old) | set(new) if old.get(name) != new.get(name)}
            self.assertLessEqual(changed, set(PARAMETER_NAMES))
            self.assertEqual(old["Completer_Token"], new["Completer_Token"])  # the token is never rewritten
            self.assertEqual(old["Completer_Sum"], new["Completer_Sum"])

    def test_a_due_download_whose_articles_arrived_is_released_and_its_neighbours_are_not(self):
        sim = self.sim
        sim.advance(350)
        arrived = self.early[0]
        sim.news[0].have.update(self.posts[arrived].all_ids)
        others = [nzb_id for nzb_id in self.every if nzb_id != arrived]
        late_before = sim.snapshot(self.late)
        sim.allowed_ids = set(self.early)
        self.sweep()
        self.assertEqual(sim.status(arrived), "QUEUED")
        self.assertEqual(sim.param(arrived, "Completer_State"), "passed")
        for nzb_id in others:
            self.assertEqual(sim.status(nzb_id), "PAUSED")
        self.assertEqual(sim.snapshot(self.late), late_before)
        self.assertEqual(sim.param(self.early[1], "Completer_State"), "waiting")

    def test_a_download_that_fails_on_recheck_does_not_drag_down_its_lookalikes(self):
        # A is in the last minute of its freshness and fails at its recheck; B has A's name plus a suffix, C
        # has exactly A's name, all in one category, all waiting with a token of their own
        sim = self.sim
        victim = self.queue_fresh(sim, "victim", "Show.S01E01.1080p-GRP", age_hours=1.99)
        brother = self.queue_fresh(sim, "brother", "Show.S01E01.1080p-GRP.REPACK")
        twin = self.queue_fresh(sim, "twin", "Show.S01E01.1080p-GRP")
        self.assertEqual(sim.param(victim, "Completer_State"), "waiting")
        others = [nzb_id for nzb_id in sim.snapshot() if nzb_id != victim]
        before = sim.snapshot(others)
        sim.allowed_ids = {victim}
        self.sweep(step=120)  # only the victim is due
        self.assertEqual(sim.where(victim), "history")
        self.assertEqual(sim.sonarr_view(victim), "failed")
        self.assertEqual(sim.snapshot(others), before)
        self.assertEqual(sim.edited_ids(), {victim})
        self.assertEqual((sim.status(brother), sim.status(twin)), ("PAUSED", "PAUSED"))
        self.assert_calls_are_clean({victim})


# ======================================================================================================
# C. Forged or inconsistent identity in the queue script's environment
# ======================================================================================================

class ForgedIdentityTests(GuardCase):
    """NZBGet always tells the script the truth; these runs are told something else. Whatever the lie, no
    download other than the script's own is edited, and not even that one is edited through the RPC."""

    def pair(self, good, names=("Alpha", "Beta")):
        sim = self.world()
        make = self.good if good else self.bad
        first = sim.add(make("fa"), names[0], category="tv", run=False)
        second = sim.add(make("fb"), names[1], category="tv", run=False)
        sim.events[:] = []
        sim.allowed_ids = set()  # no edit of any download is expected
        return sim, first, second

    def forged_run(self, good, **env):
        """Run A's queue script with a lie in its environment; B must come out exactly as it was."""
        sim, first, second = self.pair(good)
        before = sim.snapshot({second})
        run = sim.run_queue_script(first, **env)
        self.assertEqual(run.code, 0)
        self.assertEqual(run.commands, [])  # not one [NZB] parameter, not even on the download that was meant
        self.assertEqual(self.edit_calls(), [])
        self.assertEqual(sim.param(first, "Completer_State"), "pending")  # left for the scheduled check
        self.assertEqual(sim.snapshot({second}), before)
        self.assertEqual(sim.status(second), "PAUSED")
        self.assertEqual(sim.param(second, "Completer_State"), "pending")
        self.assertTrue(any(kind == "warning" for kind, _text in run.messages))  # it says why it did not act
        self.assert_calls_are_clean()
        return run

    def test_the_id_of_another_download_with_the_token_of_this_one_is_refused(self):
        for good in (True, False):
            with self.subTest(good=good):
                self.forged_run(good, env_update={"NZBNA_NZBID": "2"})

    def test_the_right_id_with_a_wrong_token_is_refused(self):
        wrong = {"NZBPR_Completer_Token": "deadbeefdeadbeef", "NZBPR_COMPLETER_TOKEN": "deadbeefdeadbeef"}
        for good in (True, False):
            with self.subTest(good=good):
                self.forged_run(good, env_update=wrong)

    def test_the_token_of_another_download_is_refused(self):
        for good in (True, False):
            with self.subTest(good=good):
                sim, first, second = self.pair(good)
                theirs = sim.param(second, "Completer_Token")
                before = sim.snapshot({second})
                run = sim.run_queue_script(first, env_update={"NZBPR_Completer_Token": theirs,
                                                              "NZBPR_COMPLETER_TOKEN": theirs})
                self.assertEqual(run.code, 0)
                self.assertEqual(self.edit_calls(), [])
                self.assertEqual(sim.snapshot({second}), before)

    def test_the_right_id_and_token_with_another_name_is_refused(self):
        for good in (True, False):
            with self.subTest(good=good):
                self.forged_run(good, env_update={"NZBNA_NZBNAME": "Something else entirely"})

    def test_the_id_of_a_download_that_does_not_exist_is_refused(self):
        for good in (True, False):
            with self.subTest(good=good):
                self.forged_run(good, env_update={"NZBNA_NZBID": "99999"})

    def test_a_download_the_user_deleted_meanwhile_is_not_edited(self):
        for good in (True, False):
            with self.subTest(good=good):
                sim, first, second = self.pair(good)
                sim.user_delete(first)
                before = sim.snapshot({second})
                run = sim.run_queue_script(first)  # NZBGet's script was started before the delete
                self.assertEqual(run.code, 0)
                self.assertEqual(self.edit_calls(), [])
                self.assertEqual(sim.snapshot({second}), before)
                self.assertEqual(sim.where(first), "history")
                self.assertTrue(any(kind == "warning" for kind, _text in run.messages))

    def test_an_id_that_is_not_a_positive_number_ends_the_run_at_once(self):
        # zero, negative, text, empty, or missing: not a download. Where the number does not even parse
        # ('nan', 'inf') the run ends through its catch-all: exit 1 and an [ERROR] line, never a traceback.
        sim, first, second = self.pair(True)
        before = sim.snapshot()
        values = ["0", "-1", "-0", "-7", "abc", "", "   ", "None", "0x1", "#1", "1,2", "nan", "inf", "-inf",
                  "1e999", "-1e999"]
        for value in values:
            with self.subTest(value=value):
                calls = len(sim.calls)
                run = sim.run_queue_script(first, env_update={"NZBNA_NZBID": value})
                self.assertIn(run.code, (0, 1))
                self.assertEqual(len(sim.calls), calls)  # not even a look at the queue
                self.assertEqual(run.commands, [])
                self.assertEqual(sim.snapshot(), before)
        calls = len(sim.calls)
        run = sim.run_queue_script(first, env_remove=["NZBNA_NZBID"])
        self.assertEqual((run.code, run.commands, len(sim.calls)), (0, [], calls))
        self.assertEqual(sim.snapshot(), before)

    def test_two_downloads_with_the_same_name_are_told_apart_by_id_and_token(self):
        sim = self.world()
        first = sim.add(self.good("t1"), "Twin", category="tv", run=False)
        second = sim.add(self.good("t2"), "Twin", category="tv", run=False)
        sim.events[:] = []
        self.assertNotEqual(sim.param(first, "Completer_Token"), sim.param(second, "Completer_Token"))
        first_before = sim.snapshot({first})
        sim.allowed_ids = {second}
        self.assertEqual(sim.run_queue_script(second).code, 0)
        self.assertEqual(sim.status(second), "QUEUED")
        self.assertEqual(sim.edited_ids(), {second})
        self.assertEqual(sim.snapshot({first}), first_before)  # same name, other id and token: untouched
        self.assert_calls_are_clean({second})

    def test_two_downloads_with_the_same_name_get_their_own_verdicts_from_the_sweep(self):
        sim = self.world()
        wrong = sim.add(self.bad("t1"), "Twin", category="tv", run=False)
        right = sim.add(self.good("t2"), "Twin", category="tv", run=False)
        sim.events[:] = []
        sim.allowed_ids = {wrong, right}
        self.sweep(step=100)
        self.assertEqual(sim.where(wrong), "history")
        self.assertEqual(sim.sonarr_view(wrong), "failed")
        self.assertEqual(sim.where(right), "queue")
        self.assertEqual(sim.status(right), "QUEUED")
        self.assertEqual(len(sim.entry(right).files), 3)  # the whole post: nothing was deleted from it
        self.assertEqual(sim.edited_ids(), {wrong, right})
        self.assert_calls_are_clean({wrong, right})

    def test_the_token_of_a_twin_does_not_open_the_other_twin(self):
        sim = self.world()
        first = sim.add(self.good("t1"), "Twin", category="tv", run=False)
        second = sim.add(self.good("t2"), "Twin", category="tv", run=False)
        sim.events[:] = []
        sim.allowed_ids = set()
        theirs = sim.param(second, "Completer_Token")
        before = sim.snapshot({second})
        run = sim.run_queue_script(first, env_update={"NZBPR_Completer_Token": theirs,
                                                      "NZBPR_COMPLETER_TOKEN": theirs})
        self.assertEqual(run.code, 0)
        self.assertEqual(self.edit_calls(), [])
        self.assertEqual(sim.snapshot({second}), before)


# ======================================================================================================
# D. Events other than NZB_ADDED do nothing at all
# ======================================================================================================

class OtherEventTests(GuardCase):
    EVENTS = ("NZB_DELETED", "NZB_MARKED", "NZB_DOWNLOADED", "URL_COMPLETED", "FILE_DOWNLOADED", "SOMETHING_NEW",
              "nzb_added", "NZB_ADDED ", " NZB_ADDED", "NZB_ADDED2", "NZB", "0")

    def held(self, mode="Enforce"):
        """A download that is on hold with everything in order: the environment of a valid check."""
        sim = self.world()
        nzb_id = sim.add(self.good("held"), "Held", category="tv", run=False)
        sim.events[:] = []
        sim.set_option("Mode", mode)  # it was held under Enforce; the event runs may find another mode
        self.assertEqual(sim.status(nzb_id), "PAUSED")
        self.assertTrue(sim.param(nzb_id, "Completer_Token"))
        return sim, nzb_id

    def test_every_other_event_makes_no_call_and_no_change(self):
        for mode in ("Enforce", "DryRun", "Off"):
            sim, nzb_id = self.held(mode)
            sim.allowed_ids = set()
            before = sim.snapshot()
            for event in self.EVENTS:
                with self.subTest(mode=mode, event=event):
                    calls = len(sim.calls)
                    run = sim.run_queue_script(nzb_id, event=event)
                    self.assertEqual(run.code, 0)
                    self.assertEqual(len(sim.calls), calls)
                    self.assertEqual([line for line in run.lines if line.startswith("[NZB]")], [])
                    self.assertEqual(sim.snapshot(), before)

    def test_the_events_of_a_download_that_was_deleted_do_nothing_either(self):
        sim, nzb_id = self.held()
        sim.user_delete(nzb_id)  # the event NZBGet sends for it is NZB_DELETED
        before = sim.snapshot()
        calls = len(sim.calls)
        for event in ("NZB_DELETED", "NZB_MARKED", "NZB_DOWNLOADED", "URL_COMPLETED", "FILE_DOWNLOADED"):
            run = sim.run_queue_script(nzb_id, event=event)
            self.assertEqual(run.code, 0)
            self.assertEqual(run.commands, [])
        self.assertEqual(len(sim.calls), calls)
        self.assertEqual(sim.snapshot(), before)
        self.assertEqual(sim.pump(), [])  # and the sim itself sends nothing more for a deleted download

    def test_a_run_without_an_event_does_nothing_and_says_it_was_started_by_hand(self):
        sim, nzb_id = self.held()
        before = sim.snapshot()
        calls = len(sim.calls)
        for update, remove in (({"NZBNA_EVENT": ""}, ()), ({}, ("NZBNA_EVENT",))):
            run = sim.run_queue_script(nzb_id, env_update=update, env_remove=remove)
            self.assertEqual(run.code, 2)  # "nothing to do": NZBGet never starts it like this
            self.assertEqual(run.commands, [])
        self.assertEqual(len(sim.calls), calls)
        self.assertEqual(sim.snapshot(), before)

    def test_the_manifest_asks_for_no_event_but_nzb_added(self):
        # NZBGet only starts the script for the events named in the manifest: if that list ever grows, the
        # new event must be handled (or ignored) knowingly
        sim = self.world(mode="DryRun")
        self.assertEqual(sim._events("Completer"), {"NZB_ADDED"})


# ======================================================================================================
# F. History edits only finish what Completer itself started failing
# ======================================================================================================

class HistoryCase(GuardCase):
    """Helpers to put crafted entries into the history and to run sweeps where the history is read."""

    OURS = {"Completer_Token": "0123456789abcdef", "Completer_State": "failing", "Completer_Checks": "1"}

    def craft(self, nzb_id, name="Crafted", params=None, delete="MANUAL", mark="NONE"):
        entry = S.Entry(nzb_id, name, "tv")
        for key, value in (self.OURS if params is None else params).items():
            entry.set(key, value)
        entry.delete_status = delete
        entry.mark_status = mark
        entry.history_time = int(self.sim.now)
        self.sim.history.insert(0, entry)
        return entry

    def tweak_history(self, changes):
        """Make the history answer differently for some entries: {nzb_id: {field: value}} (what a broken or
        hostile NZBGet might say); the sim's own state is not touched."""
        original = self.sim._history_json

        def tweaked(entry):
            item = original(entry)
            item.update(changes.get(entry.id, {}))
            return item

        patcher = mock.patch.object(self.sim, "_history_json", tweaked)
        patcher.start()
        self.addCleanup(patcher.stop)

    def to_minute(self, minute):
        """Advance the clock to the start of the given minute of the next hour."""
        now = int(self.sim.now)
        self.sim.advance(now - now % HOUR + HOUR + minute * 60 - self.sim.now)
        self.assertEqual(time.gmtime(self.sim.now).tm_min, minute)

    @property
    def hint(self):
        return os.path.join(self.sim.temp_dir, "completer-unfinished")

    def write_hint(self, content):
        with open(self.hint, "wb") as handle:
            handle.write(content if isinstance(content, bytes) else content.encode("ascii"))

    def hint_ids(self):
        if not os.path.exists(self.hint):
            return None
        with open(self.hint, "rb") as handle:
            return handle.read().decode("ascii", "replace").split()

    def history_reads(self):
        return len([call for call in self.sim.calls if call.method.lower() == "history"])


class HistoryEditTests(HistoryCase):
    def setUp(self):
        super(HistoryEditTests, self).setUp()
        sim = self.world()
        self.queue = [sim.add(self.good("q%d" % number), "Queued %d" % number, paused=True, queue_scripts=False,
                              run=False) for number in range(3)]
        self.queue_before = sim.snapshot(self.queue)
        sim.allowed_ids = set()

    def assert_queue_untouched(self):
        self.assertEqual(self.sim.snapshot(self.queue), self.queue_before)

    def test_a_download_the_user_deleted_is_never_marked_bad(self):
        sim = self.sim
        sim.allowed_ids = None  # the adding below is Completer's normal work; what follows is the test
        waiting = sim.add(self.fresh_bad("w"), "Waiting", category="tv")  # on hold, with a token
        passed = sim.add(self.good("p"), "Passed", category="tv")        # released, state passed
        plain = sim.add(self.good("x"), "Plain", category="tv", paused=True, queue_scripts=False, run=False)
        sim.allowed_ids = set()
        for nzb_id in (waiting, passed, plain):
            sim.user_delete(nzb_id)
        before = sim.snapshot()
        edits = len(self.edit_calls())
        self.write_hint("%d\n%d\n%d\n" % (waiting, passed, plain))
        self.to_minute(0)
        for _ in range(3):  # in the hour's first minutes, and with a hint saying where to look
            self.sweep(step=60)
        self.assertGreater(self.history_reads(), 0)  # it did look at the history
        self.assertEqual(len(self.edit_calls()), edits)
        self.assertEqual(sim.snapshot(), before)
        for nzb_id in (waiting, passed, plain):
            self.assertEqual(sim.entry(nzb_id).mark_status, "NONE")
            self.assertNotEqual(sim.sonarr_view(nzb_id), "failed")
        self.assert_queue_untouched()

    def test_an_entry_that_completer_was_failing_is_finished(self):
        sim = self.sim
        self.craft(500)
        self.craft(501, params={})  # another download in the history, nothing to do with Completer
        sim.allowed_ids = {500}
        self.to_minute(0)
        self.sweep()
        self.assertEqual(sim.entry(500).mark_status, "BAD")
        self.assertEqual(sim.param(500, "Completer_State"), "failed")
        self.assertEqual(sim.sonarr_view(500), "failed")
        self.assertEqual([(call.command, call.ids) for call in self.edit_calls()],
                         [("HistoryMarkBad", [500]), ("HistorySetParameter", [500])])
        self.assertEqual(sim.entry(501).mark_status, "NONE")
        self.assert_queue_untouched()

    def test_an_interrupted_failure_is_finished_by_a_later_sweep_and_only_that_one(self):
        sim = self.sim
        bystander = self.craft(700, name="Deleted by hand", params={})  # a history entry that is not Completer's
        deleted = sim.add(self.good("d"), "Deleted by the user", category="tv", paused=True, queue_scripts=False,
                          run=False)
        sim.user_delete(deleted)
        shot = sim.snapshot({bystander.id, deleted})
        victim = sim.add(self.bad("v"), "Victim", category="tv", run=False)
        sim.events[:] = []
        sim.allowed_ids = {victim}
        sim.history_delay = 5  # NZBGet is slower than the 5 polls to show the removed download in the history
        self.sweep(step=100)
        self.assertIsNone(sim.where(victim))  # removed, not in the history yet: the second step is missing
        self.assertEqual(self.hint_ids(), [str(victim)])  # ...and the hint says where to look
        self.assertNotEqual(sim.sonarr_view(victim), "failed")
        self.sweep(step=300)
        self.sweep(step=300)
        self.assertEqual(sim.where(victim), "history")
        self.assertEqual(sim.sonarr_view(victim), "failed")
        self.assertEqual(sim.param(victim, "Completer_State"), "failed")
        self.assertIsNone(self.hint_ids())  # done: the hint is gone
        self.assertEqual(sim.snapshot({bystander.id, deleted}), shot)
        self.assertEqual(sim.edited_ids(), {victim})
        self.assert_calls_are_clean({victim})
        self.assert_queue_untouched()

    def test_a_history_that_is_very_slow_is_caught_by_the_hourly_look(self):
        sim = self.sim
        bystander = self.craft(700, name="Deleted by hand", params={})
        victim = sim.add(self.bad("v"), "Victim", category="tv", run=False)
        sim.events[:] = []
        sim.allowed_ids = {victim}
        sim.history_delay = 6  # even the next sweep's look does not see it yet
        self.sweep(step=100)
        self.sweep(step=100)
        self.assertIsNone(self.hint_ids())  # the hint said "not in the history": it is dropped
        sim.finish_deletions()
        self.assertNotEqual(sim.sonarr_view(victim), "failed")
        shot = sim.snapshot({bystander.id})
        self.to_minute(2)
        self.sweep(step=60)
        self.assertEqual(sim.sonarr_view(victim), "failed")
        self.assertEqual(sim.param(victim, "Completer_State"), "failed")
        self.assertEqual(sim.snapshot({bystander.id}), shot)
        self.assertEqual(sim.edited_ids(), {victim})
        self.assert_calls_are_clean({victim})
        self.assert_queue_untouched()

    def test_entries_that_are_not_ours_or_not_failing_are_left_alone(self):
        sim = self.sim
        ours = dict(self.OURS)
        no_token = {"Completer_State": "failing"}
        cases = [
            ("no token", dict(params=no_token)),
            ("empty token", dict(params=dict(ours, Completer_Token=""))),
            ("finished without deleting", dict(delete="NONE")),
            ("deleted as bad", dict(delete="BAD")),
            ("deleted for health", dict(delete="HEALTH")),
            ("deleted as duplicate", dict(delete="DUPE")),
            ("deleted as good", dict(delete="GOOD")),
            ("deleted as a copy", dict(delete="COPY")),
            ("scan failure", dict(delete="SCAN")),
            ("state upper case", dict(params=dict(ours, Completer_State="FAILING"))),
            ("state with a space", dict(params=dict(ours, Completer_State="failing "))),
            ("state failed", dict(params=dict(ours, Completer_State="failed"))),
            ("state pending", dict(params=dict(ours, Completer_State="pending"))),
            ("state waiting", dict(params=dict(ours, Completer_State="waiting"))),
            ("state passed", dict(params=dict(ours, Completer_State="passed"))),
            ("state released", dict(params=dict(ours, Completer_State="released"))),
            ("state observed", dict(params=dict(ours, Completer_State="observed"))),
            ("state missing", dict(params={"Completer_Token": ours["Completer_Token"]})),
            ("someone else's parameter", dict(params={"Other_Token": "x", "Other_State": "failing"})),
        ]
        tweaks = {}
        ids = []
        for offset, (label, kwargs) in enumerate(cases):
            ids.append(self.craft(600 + offset, name=label, **kwargs).id)
        # what a broken NZBGet might say about otherwise finishable entries
        weird = [("kind URL", {"Kind": "URL"}), ("kind lower case", {"Kind": "nzb"}), ("no kind", {"Kind": None}),
                 ("id as text", {"NZBID": "%d"}), ("id zero", {"NZBID": 0}), ("id negative", {"NZBID": -4}),
                 ("id true", {"NZBID": True}), ("id float", {"NZBID": 1.0}), ("id is another", {"ID": 99999})]
        for offset, (label, change) in enumerate(weird):
            nzb_id = self.craft(650 + offset, name=label).id
            ids.append(nzb_id)
            tweaks[nzb_id] = {key: (value % nzb_id if isinstance(value, str) and "%d" in value else value)
                              for key, value in change.items()}
            if tweaks[nzb_id].get("NZBID") == 1.0:
                tweaks[nzb_id]["NZBID"] = float(nzb_id)
        self.tweak_history(tweaks)
        before = sim.snapshot()
        self.write_hint("\n".join(str(number) for number in ids))
        self.to_minute(1)
        for _ in range(2):
            self.sweep(step=60)
        self.assertGreater(self.history_reads(), 0)
        self.assertEqual(self.edit_calls(), [])
        self.assertEqual(sim.snapshot(), before)
        self.assert_queue_untouched()
        # every hint is cleaned up but one: an entry that is Completer's in every way, except that NZBGet shows it
        # with an inconsistent id. The guard refuses it, so it is kept for the next look (and still never edited).
        self.assertEqual(self.hint_ids(), [str(ids[-1])])

    def test_an_entry_that_is_already_marked_bad_only_gets_the_missing_note(self):
        # an earlier run was interrupted after the mark: Completer writes its own note and marks nothing again
        sim = self.sim
        entry = self.craft(900, name="Marked before the interruption", mark="BAD")
        bystander = self.craft(901, name="Marked by the user", params={}, mark="BAD")
        sim.allowed_ids = {entry.id}
        shot = sim.snapshot([bystander.id])
        self.write_hint("900\n901\n")
        self.sweep()
        self.assertEqual([(call.command, call.text, call.ids) for call in self.edit_calls()],
                         [("HistorySetParameter", "Completer_State=failed", [900])])
        self.assertEqual(entry.mark_status, "BAD")
        self.assertEqual(sim.param(900, "Completer_State"), "failed")
        self.assertEqual(sim.snapshot([bystander.id]), shot)
        self.assertIsNone(self.hint_ids())
        self.assert_calls_are_clean({900})
        self.assert_queue_untouched()

    def test_a_hint_for_ids_that_are_not_in_the_history_does_nothing_and_goes_away(self):
        sim = self.sim
        bystander = self.craft(800, params={"Completer_Token": "feedfeedfeedfeed", "Completer_State": "passed"})
        self.write_hint("9001\n9002\n999999999999\n")
        before = sim.snapshot()
        self.sweep()
        self.assertGreater(self.history_reads(), 0)
        self.assertEqual(self.edit_calls(), [])
        self.assertIsNone(self.hint_ids())  # nothing to wait for: the file is cleaned up
        self.assertEqual(sim.snapshot(), before)
        self.assertEqual(bystander.mark_status, "NONE")

    def test_a_hint_naming_other_peoples_entries_does_nothing(self):
        sim = self.sim
        # a user-deleted download without any parameter, a finished one, and one of someone else's tools
        deleted = self.craft(810, params={})
        finished = self.craft(811, params={}, delete="NONE")
        foreign = self.craft(812, params={"Other_State": "failing", "Other_Token": "abc"})
        queued = self.queue[0]
        self.write_hint("%d\n%d\n%d\n%d\n" % (deleted.id, finished.id, foreign.id, queued))
        before = sim.snapshot()
        for _ in range(3):
            self.sweep(step=300)
        self.assertEqual(self.edit_calls(), [])
        self.assertEqual(sim.snapshot(), before)
        self.assertEqual({deleted.mark_status, finished.mark_status, foreign.mark_status}, {"NONE"})

    def test_a_damaged_hint_file_cannot_hurt(self):
        sim = self.sim
        entry = self.craft(820, params={})
        before = sim.snapshot()
        for content in (b"", b"\x00\xff\xfe" * 500, b"abc\n-1\n0\n+5\n3.5\n" * 300, b"%d\n" % entry.id + b"9" * 20000,
                        ("1 " * 6000).encode("ascii"), b"\n\n\n", ("%d\n" % entry.id).encode("ascii") * 400):
            self.write_hint(content)
            self.sweep()
        self.assertEqual(self.edit_calls(), [])
        self.assertEqual(sim.snapshot(), before)

    def test_an_entry_whose_token_changed_after_it_was_read_is_not_finished(self):
        # the guard reads the history again right before the edit and wants the same token
        sim = self.sim
        self.craft(830)
        original = app.Run.finish_removal

        def changed(run, target, *args, **kwargs):
            sim.entry(target.nzb_id).set("Completer_Token", "ffffffffffffffff")
            return original(run, target, *args, **kwargs)

        before_state = sim.snapshot()
        self.to_minute(0)
        with mock.patch.object(app.Run, "finish_removal", changed):
            run = sim.sweep()
        self.assertEqual(run.code, 0)
        self.assertEqual(self.edit_calls(), [])
        self.assertTrue(any(kind == "warning" for kind, _text in run.messages))
        self.assertEqual(sim.entry(830).mark_status, "NONE")
        after = sim.snapshot()
        self.assertEqual({key: value for key, value in after[830].items() if key != "params"},
                         {key: value for key, value in before_state[830].items() if key != "params"})

    def test_an_entry_that_has_another_name_after_it_was_read_is_not_finished(self):
        sim = self.sim
        self.craft(840, name="Original name")
        original = app.Run.finish_removal

        def renamed(run, target, *args, **kwargs):
            sim.entry(target.nzb_id).name = "A different download"
            return original(run, target, *args, **kwargs)

        self.to_minute(0)
        with mock.patch.object(app.Run, "finish_removal", renamed):
            run = sim.sweep()
        self.assertEqual(run.code, 0)
        self.assertEqual(self.edit_calls(), [])
        self.assertEqual(sim.entry(840).mark_status, "NONE")

    def test_an_entry_that_vanished_after_it_was_read_is_not_edited(self):
        sim = self.sim
        self.craft(850)
        original = app.Run.finish_removal

        def vanished(run, target, *args, **kwargs):
            sim.history[:] = [entry for entry in sim.history if entry.id != target.nzb_id]
            return original(run, target, *args, **kwargs)

        self.to_minute(0)
        with mock.patch.object(app.Run, "finish_removal", vanished):
            run = sim.sweep()
        self.assertEqual(run.code, 0)
        self.assertEqual(self.edit_calls(), [])

    def test_the_history_is_not_read_without_a_reason(self):
        # minute 5 to 59 and no hint: not even a look, so a busy NZBGet is not asked for a long history
        sim = self.sim
        self.craft(860)
        for _ in range(4):
            self.sweep(step=300)  # minutes 35, 40, 45, 50
        self.assertEqual(self.history_reads(), 0)
        self.assertEqual(self.edit_calls(), [])
        self.assertEqual(sim.entry(860).mark_status, "NONE")
        self.to_minute(4)  # the first minutes of the hour are the safety net
        sim.allowed_ids = {860}
        self.sweep()
        self.assertGreater(self.history_reads(), 0)
        self.assertEqual(sim.entry(860).mark_status, "BAD")
        self.assertEqual(sim.edited_ids(), {860})

    def test_a_history_that_cannot_be_read_changes_nothing(self):
        sim = self.sim
        self.craft(870)
        for kind in ("http500", "garbage", "error", "drop"):
            sim.fault(kind, method="history", times=None)
            self.to_minute(0)
            self.assertEqual(sim.sweep().code, 0)
            sim.faults[:] = []
        self.assertEqual(self.edit_calls(), [])
        self.assertEqual(sim.entry(870).mark_status, "NONE")


# ======================================================================================================
# E. A long, mixed, pseudo-random day in the queue
# ======================================================================================================

class RandomDayTests(GuardCase):
    """Thirty downloads of every kind, a user who resumes, pauses and deletes, an NZBGet that now and then fails,
    and time that moves in jumps: whatever happens, Completer only edits downloads that carry its token."""

    KINDS = ("good", "bad", "fresh_bad", "paused_by_user", "unwanted_category", "no_queue_script")
    TOTAL = 30
    STEPS = 150

    def setUp(self):
        super(RandomDayTests, self).setUp()
        self.rng = random.Random(20261111)
        self.sim = self.world(options={"Categories": "tv, movies", "Verbose": "yes"})
        self.kind_of = {}
        self.touched_by_user = set()
        self.ever_token = set()
        self.untouched = {}       # id -> snapshot: downloads Completer has no business with
        self.paused_by_user = {}  # id -> snapshot after the one queue script run that is its own business
        self.created = 0

    # ------------------------------------------------------------ what the test does

    def create(self):
        sim, rng = self.sim, self.rng
        kind = rng.choice(self.KINDS)
        tag = "day%02d" % self.created
        self.created += 1
        post = {"good": self.good, "bad": self.bad, "fresh_bad": self.fresh_bad,
                "paused_by_user": self.good,  # (a bad one is failed by its own queue script, on purpose)
                "unwanted_category": rng.choice((self.good, self.bad)),
                "no_queue_script": rng.choice((self.good, self.bad))}[kind](tag)
        category = rng.choice(("tv", "movies")) if kind != "unwanted_category" else "music"
        paused = kind == "paused_by_user" or (kind == "no_queue_script" and rng.random() < 0.5)
        nzb_id = sim.add(post, "Download %s" % tag, category=category, paused=paused,
                         queue_scripts=kind != "no_queue_script", run=True)
        self.kind_of[nzb_id] = kind
        if kind in ("unwanted_category", "no_queue_script"):
            self.untouched[nzb_id] = sim.snapshot({nzb_id})[nzb_id]
        elif kind == "paused_by_user":
            self.paused_by_user[nzb_id] = sim.snapshot({nzb_id})[nzb_id]
        return nzb_id

    def user(self, action):
        sim = self.sim
        queue = [entry.id for entry in sim.queue]
        if not queue:
            return
        nzb_id = self.rng.choice(queue)
        getattr(sim, "user_" + action)(nzb_id)
        self.touched_by_user.add(nzb_id)
        for tracked in (self.untouched, self.paused_by_user):
            if nzb_id in tracked:
                tracked[nzb_id] = sim.snapshot({nzb_id})[nzb_id]  # what the user did is the new normal

    def sweep_with(self, fault):
        sim, rng = self.sim, self.rng
        sim.advance(rng.choice((30, 60, 120, 300, 600, 900, 1800, 3600)))
        if fault:
            sim.fault(rng.choice(("http500", "garbage", "error", "drop")),
                      method=rng.choice(("listgroups", "editqueue", "history")), times=rng.choice((1, 2)))
        self.assertEqual(sim.sweep().code, 0)
        sim.faults[:] = []

    def audit(self):
        sim = self.sim
        for entry in sim.queue + sim.history:
            if entry.get("Completer_Token", ""):
                self.ever_token.add(entry.id)
        self.assertEqual(sim.edited_ids() - self.ever_token, set())
        for tracked in (self.untouched, self.paused_by_user):
            for nzb_id, shot in tracked.items():
                self.assertEqual(sim.snapshot({nzb_id})[nzb_id], shot, "download %d was changed" % nzb_id)

    def held(self):
        sim = self.sim
        found = [entry.id for entry in sim.queue if entry.get("Completer_Token", "")
                 and entry.get("Completer_State", "") in ("pending", "waiting", "failing")]
        found += [entry.id for entry in sim.history
                  if entry.get("Completer_Token", "") and entry.get("Completer_State", "") == "failing"]
        return found

    # ------------------------------------------------------------ the day

    def test_a_day_of_random_events_only_edits_downloads_with_the_token(self):
        sim, rng = self.sim, self.rng
        for _ in range(5):
            self.create()
        for _ in range(self.STEPS):
            roll = rng.random()
            if roll < 0.25 and self.created < self.TOTAL:
                self.create()
            elif roll < 0.35:
                self.user("resume")
            elif roll < 0.42:
                self.user("pause")
            elif roll < 0.47:
                self.user("delete")
            else:
                faulty = rng.random() < 0.15
                self.sweep_with(faulty)
                if not faulty:
                    for entry in sim.queue:  # a pending download is never held much beyond its limit
                        if entry.get("Completer_State", "") == "pending" and entry.queue_status == "PAUSED" \
                                and entry.get("Completer_Token", ""):
                            self.assertLessEqual(sim.now() - float(entry.get("Completer_Since", "0")), 1800 + 1,
                                                 "download %d is stuck on hold" % entry.id)
            self.audit()
        while self.created < self.TOTAL:
            self.create()
        self.assertEqual(self.created, self.TOTAL)
        # enough sweeps for everything to be settled, at a pace that crosses the hour's first minutes too
        for _ in range(40):
            if not self.held():
                break
            sim.advance(900)
            self.assertEqual(sim.sweep().code, 0)
            self.audit()
        self.assertEqual(self.held(), [], "something is still on hold or half failed")
        self.assertEqual(sim.violations, [])
        self.assert_calls_are_clean()
        self.assertTrue(sim.edited_ids())  # the day was not an empty one
        self.say_nothing_secret()
        # the verdicts: a good download never fails; a bad one nobody touched fails
        for nzb_id, kind in self.kind_of.items():
            view = sim.sonarr_view(nzb_id)
            if kind in ("good", "paused_by_user", "unwanted_category", "no_queue_script") \
                    and nzb_id not in self.touched_by_user:  # (a download the user deleted counts as failed)
                self.assertNotEqual(view, "failed", "download %d (%s) was failed" % (nzb_id, kind))
            if kind in ("bad", "fresh_bad") and nzb_id not in self.touched_by_user:
                self.assertEqual(view, "failed", "download %d (%s) was not failed" % (nzb_id, kind))
                self.assertEqual(sim.where(nzb_id), "history")
        # what the user did is the only reason a download is still paused
        for entry in sim.queue:
            if entry.id not in self.touched_by_user and self.kind_of[entry.id] in ("good", "bad", "fresh_bad"):
                self.assertNotEqual(entry.queue_status, "PAUSED", "download %d is still paused" % entry.id)
        self.assertFalse(sim.download_paused)


# ======================================================================================================
# G. The nzb file a download names is the one that was scanned, or it is not used
# ======================================================================================================

class FileParameterTests(GuardCase):
    """``Completer_File`` is a parameter anyone with access to NZBGet could change. A sweep must not fail a
    download because of the content of some other file, and must never be fooled into reading or editing
    anything else."""

    KINDS = ("directory", "symlink_loop", "dangling_symlink", "empty", "binary", "huge", "gzip_bomb_header",
             "entities", "text", "relative", "nul", "long", "missing", "etc_passwd", "device", "empty_name")

    def setUp(self):
        super(FileParameterTests, self).setUp()
        self.fresh_world()

    def fresh_world(self):
        self.small_limit = False
        self.sim = self.world()
        self.hostile = os.path.join(self.sim.root, "hostile")  # removed with the sim's own temporary directory
        os.makedirs(self.hostile)

    def pending(self, good, name="Victim", tag="victim"):
        """A download that is on hold and whose queue script has not run: only the sweep can decide it."""
        sim = self.sim
        make = self.good if good else self.bad
        nzb_id = sim.add(make(tag), name, category="tv", run=False)
        sim.events[:] = []
        self.assertEqual(sim.param(nzb_id, "Completer_State"), "pending")
        return nzb_id

    def point_at(self, nzb_id, path, keep_sum=True):
        entry = self.sim.entry(nzb_id)
        entry.set("Completer_File", path)
        if not keep_sum:
            entry.params[:] = [pair for pair in entry.params if pair[0] != "Completer_Sum"]

    def plain(self, tag="plain", name="Plain"):
        """A download of somebody else's that Completer has never seen: paused, no parameters."""
        nzb_id = self.sim.add(self.bad(tag), name, category="tv", paused=True, queue_scripts=False, run=False)
        self.sim.events[:] = []
        return nzb_id

    def settle(self, victim, others=(), bystanders=()):
        """Sweep after the pending grace. ``others`` are held downloads that are swept on their own account;
        ``bystanders`` have no token. Nothing else may be edited and nothing may blow up."""
        sim = self.sim
        sim.allowed_ids = {victim} | set(others)
        before = sim.snapshot(bystanders)
        sim.advance(100)
        with mock.patch.object(nzbfile, "MAX_BYTES", 1000 if self.small_limit else nzbfile.MAX_BYTES):
            run = sim.sweep()
        self.assertEqual(run.code, 0)
        self.assertNotIn("Traceback", run.out + run.err)
        self.assertEqual(sim.snapshot(bystanders), before)
        self.assert_calls_are_clean({victim} | set(others))
        return run

    def assert_released_not_failed(self, victim):
        sim = self.sim
        self.assertEqual(sim.where(victim), "queue")
        self.assertNotEqual(sim.status(victim), "PAUSED")
        self.assertEqual(sim.param(victim, "Completer_State"), "released")
        self.assertEqual(sim.entry(victim).mark_status, "NONE")
        self.assertEqual(sim.entry(victim).delete_status, "NONE")

    # ------------------------------------------------------------ the nzb of another download

    def test_a_file_parameter_naming_a_bad_nzb_does_not_fail_a_good_download(self):
        sim = self.sim
        other = self.pending(False, "Other", "other")  # a bad post, also on hold: it fails on its own account
        victim = self.pending(True)
        bystander = self.plain()
        self.point_at(victim, sim.entry(other).queued_file)
        self.settle(victim, [other], [bystander])
        self.assert_released_not_failed(victim)
        self.assertEqual(sim.sonarr_view(other), "failed")

    def test_a_file_parameter_naming_a_good_nzb_does_not_release_a_bad_download_as_good(self):
        sim = self.sim
        other = self.pending(True, "Other", "other")
        victim = self.pending(False)
        bystander = self.plain()
        self.point_at(victim, sim.entry(other).queued_file)
        run = self.settle(victim, [other], [bystander])
        self.assert_released_not_failed(victim)  # released without a verdict, never "passed" on borrowed content
        self.assertEqual(sim.param(other, "Completer_State"), "passed")
        self.assertTrue(run.says("not available") or run.says("released"))

    def test_the_pair_swapped_both_ways_fails_neither_and_edits_only_themselves(self):
        sim = self.sim
        first = self.pending(True, "First", "first")
        second = self.pending(False, "Second", "second")
        bystander = self.plain()
        first_file, second_file = sim.entry(first).queued_file, sim.entry(second).queued_file
        self.point_at(first, second_file)
        self.point_at(second, first_file)
        self.settle(first, [second], [bystander])
        for nzb_id in (first, second):
            self.assert_released_not_failed(nzb_id)

    def watch(self):
        """Record every nzb that is judged (by download id) and every file that is summed."""
        judged, summed = [], []
        examine, file_sum = app.Run.examine, app.file_sum

        def examined(run, subject, path, seconds):
            judged.append(subject.nzb_id)
            return examine(run, subject, path, seconds)

        def summed_up(path):
            summed.append(path)
            return file_sum(path)

        for target, name, wrapper in ((app.Run, "examine", examined), (app, "file_sum", summed_up)):
            patcher = mock.patch.object(target, name, wrapper)
            patcher.start()
            self.addCleanup(patcher.stop)
        return judged, summed

    def assert_not_available(self, victim):
        self.assert_released_not_failed(victim)
        self.assertIn("not available", self.sim.param(victim, "Completer_Info"))

    def test_without_a_checksum_nothing_is_judged_from_a_file_and_the_download_is_released(self):
        # the checksum taken at scan time is the only proof that a file is the scanned nzb: without it the sweep
        # reads no file at all, even when the parameter names the download's very own nzb
        for good in (True, False):
            for target in ("own", "other"):
                with self.subTest(good=good, file=target):
                    self.fresh_world()
                    sim = self.sim
                    other = self.pending(not good, "Other", "other")
                    victim = self.pending(good)
                    bystander = self.plain()
                    sim.user_delete(other)  # out of the way: the victim is the only held download
                    shot = sim.snapshot([other])
                    named = sim.entry(victim if target == "own" else other).queued_file
                    self.point_at(victim, named, keep_sum=False)
                    judged, summed = self.watch()
                    self.settle(victim, [], [bystander, other])
                    self.assertEqual((judged, summed), ([], []))
                    self.assert_not_available(victim)
                    self.assertEqual(sim.edited_ids(), {victim})
                    self.assertEqual(sim.snapshot([other]), shot)

    def test_a_token_without_a_checksum_is_released_while_its_neighbour_is_still_judged_by_its_own_file(self):
        sim = self.sim
        other = self.pending(False, "Other", "other")  # carries its checksum: judged and failed on its own account
        victim = self.pending(True)
        bystander = self.plain()
        self.point_at(victim, sim.entry(other).queued_file, keep_sum=False)
        judged, _summed = self.watch()
        self.settle(victim, [other], [bystander])
        self.assertEqual(judged, [other])
        self.assert_not_available(victim)
        self.assertEqual(sim.sonarr_view(other), "failed")
        self.assertEqual(sim.edited_ids(), {victim, other})

    def test_a_wrong_checksum_with_the_file_of_another_download_is_released_and_never_judged(self):
        for good in (True, False):
            for wrong in ("0-00000000000000000000000000000000", "", "garbage", "-1"):
                with self.subTest(good=good, sum=wrong):
                    self.fresh_world()
                    sim = self.sim
                    other = self.pending(not good, "Other", "other")
                    victim = self.pending(good)
                    sim.user_delete(other)
                    self.point_at(victim, sim.entry(other).queued_file)
                    sim.entry(victim).set("Completer_Sum", wrong)
                    judged, _summed = self.watch()
                    self.settle(victim)
                    self.assertEqual(judged, [])
                    self.assert_not_available(victim)
                    self.assertEqual(sim.edited_ids(), {victim})

    def test_a_file_parameter_that_names_the_nzb_of_a_download_that_is_gone(self):
        sim = self.sim
        other = self.pending(False, "Other", "other")
        victim = self.pending(True)
        stale = sim.entry(other).queued_file
        self.point_at(victim, stale)
        sim.user_delete(other)
        os.remove(stale)
        self.settle(victim, [], [other])
        self.assert_released_not_failed(victim)

    # ------------------------------------------------------------ files that are not an nzb at all

    def hostile_file(self, kind):
        """Make something unusual inside the temporary directory (or name something outside it)."""
        path = os.path.join(self.hostile, kind)
        if kind == "directory":
            os.makedirs(path)
        elif kind == "symlink_loop":
            os.symlink(path + "_b", path)
            os.symlink(path, path + "_b")
        elif kind == "dangling_symlink":
            os.symlink(os.path.join(self.hostile, "nowhere"), path)
        elif kind == "empty":
            open(path, "wb").close()
        elif kind == "binary":
            with open(path, "wb") as handle:
                handle.write(bytes(bytearray(range(256))) * 8)
        elif kind == "huge":
            with open(path, "wb") as handle:
                handle.write(b"<nzb>" + b" " * 5000)
            self.small_limit = True  # "huge" for the sweep that follows: the limit is lowered around it
        elif kind == "gzip_bomb_header":
            with open(path, "wb") as handle:
                handle.write(b"\x1f\x8b" + b"\x00" * 20)
        elif kind == "entities":
            with open(path, "wb") as handle:
                handle.write(b'<?xml version="1.0"?><!DOCTYPE nzb [<!ENTITY a "x">]><nzb></nzb>')
        elif kind == "text":
            with open(path, "wb") as handle:
                handle.write(b"root:x:0:0:not an nzb\n")
        elif kind == "relative":
            with open(os.path.join(self.hostile, "text"), "wb") as handle:
                handle.write(b"root:x:0:0:not an nzb\n")
            return os.path.join(self.hostile, "..", "hostile", "text")
        elif kind == "nul":
            return path + "\x00.nzb"
        elif kind == "long":
            return os.path.join(self.hostile, "x" * 5000)
        elif kind == "missing":
            return os.path.join(self.hostile, "does-not-exist")
        elif kind == "etc_passwd":
            return "/etc/passwd"
        elif kind == "device":
            return "/dev/null"
        elif kind == "empty_name":
            return ""
        return path

    def test_a_file_that_is_not_the_scanned_nzb_is_never_used_and_the_download_is_released(self):
        for kind in self.KINDS:
            with self.subTest(kind=kind):
                self.fresh_world()
                sim = self.sim
                bystander = self.plain()
                victim = self.pending(False)
                self.point_at(victim, self.hostile_file(kind))
                self.settle(victim, [], [bystander])
                self.assert_released_not_failed(victim)
                self.assertEqual(sim.edited_ids(), {victim})
                self.assertEqual(sim.status(bystander), "PAUSED")

    def test_a_file_that_is_not_an_nzb_without_a_checksum_is_never_read_and_never_a_reason_to_fail(self):
        # No checksum, so no file is read at all: the download is released without a verdict.
        for kind in self.KINDS:
            with self.subTest(kind=kind):
                self.fresh_world()
                sim = self.sim
                victim = self.pending(False)
                self.point_at(victim, self.hostile_file(kind), keep_sum=False)
                judged, summed = self.watch()
                self.settle(victim)
                self.assertEqual((judged, summed), ([], []))
                self.assert_not_available(victim)
                self.assertEqual(sim.edited_ids(), {victim})

    def test_a_queue_script_with_a_hostile_file_name_in_its_environment_judges_nothing_else(self):
        for kind in ("etc_passwd", "directory", "text", "missing", "empty"):
            with self.subTest(kind=kind):
                self.fresh_world()
                sim = self.sim
                bystander = self.plain()
                victim = self.pending(True)
                sim.allowed_ids = {victim}
                before = sim.snapshot([bystander])
                run = sim.run_queue_script(victim, env_update={"NZBNA_QUEUEDFILE": self.hostile_file(kind)})
                self.assertEqual(run.code, 0)
                self.assertNotIn("Traceback", run.out + run.err)
                self.assertEqual(sim.snapshot([bystander]), before)
                self.assertEqual(sim.entry(victim).mark_status, "NONE")
                self.assertEqual(sim.where(victim), "queue")
                self.assert_calls_are_clean({victim})


# ======================================================================================================
# H. The download changes while it is being checked
# ======================================================================================================

class MidCheckChangeTests(GuardCase):
    """The check takes time (it asks news servers). Meanwhile the user may delete, rename or resume the
    download. Completer verifies the identity again right before every change, so it edits nothing it cannot
    prove to be the download it checked."""

    def setUp(self):
        super(MidCheckChangeTests, self).setUp()
        self.sim = self.world()
        self.plains = []
        for number in range(3):  # somebody else's paused downloads, next to the victim
            self.plains.append(self.sim.add(self.bad("plain%d" % number), "Plain %d" % number, category="tv",
                                            paused=True, queue_scripts=False, run=False))
        self.sim.events[:] = []
        self.plain_shot = self.sim.snapshot(self.plains)
        self.seen = []  # snapshots of the victim right after the change that was made during the check

    def make_victim(self, good):
        sim = self.sim
        nzb_id = sim.add((self.good if good else self.bad)("victim"), "Victim", category="tv", run=False)
        sim.events[:] = []
        sim.allowed_ids = {nzb_id}
        return nzb_id

    def during_check(self, action, when="after"):
        """Run ``action(id)`` while the check is going on: just before it asks the servers, or right after."""
        original = app.Run.examine

        def wrapper(run, subject, path, seconds):
            if when == "before":
                action(subject.nzb_id)
            result = original(run, subject, path, seconds)
            if when == "after":
                action(subject.nzb_id)
            self.seen.append(self.sim.snapshot({subject.nzb_id}))
            return result

        patcher = mock.patch.object(app.Run, "examine", wrapper)
        patcher.start()
        self.addCleanup(patcher.stop)

    def actions(self):
        sim = self.sim

        def rename(nzb_id):
            sim.entry(nzb_id).name = "Something else"

        def retoken(nzb_id):
            sim.entry(nzb_id).set("Completer_Token", "0123456789abcdef")

        def untoken(nzb_id):
            entry = sim.entry(nzb_id)
            entry.params[:] = [pair for pair in entry.params if pair[0] != "Completer_Token"]

        def to_other_state(nzb_id):
            sim.entry(nzb_id).set("Completer_State", "failed")

        return {"delete": sim.user_delete, "rename": rename, "token replaced": retoken, "token removed": untoken}

    def assert_nothing_else_changed(self):
        sim = self.sim
        self.assertEqual(sim.snapshot(self.plains), self.plain_shot)
        self.assert_calls_are_clean()

    def test_a_queue_script_whose_download_changes_during_the_check_edits_nothing(self):
        for name in ("delete", "rename", "token replaced", "token removed"):
            for when in ("before", "after"):
                for good in (True, False):
                    with self.subTest(change=name, when=when, good=good):
                        self.setUp()
                        victim = self.make_victim(good)
                        self.during_check(self.actions()[name], when)
                        run = self.sim.run_queue_script(victim)
                        self.assertEqual(run.code, 0)
                        self.assertNotIn("Traceback", run.out + run.err)
                        self.assertEqual(self.edit_calls(), [])  # not one edit of the victim, nor of anything else
                        self.assert_nothing_else_changed()

    def test_a_sweep_whose_download_changes_during_the_check_edits_nothing(self):
        for name in ("delete", "rename", "token replaced", "token removed"):
            for when in ("before", "after"):
                for good in (True, False):
                    with self.subTest(change=name, when=when, good=good):
                        self.setUp()
                        sim = self.sim
                        victim = self.make_victim(good)
                        self.during_check(self.actions()[name], when)
                        sim.advance(100)
                        run = sim.sweep()
                        self.assertEqual(run.code, 0)
                        self.assertNotIn("Traceback", run.out + run.err)
                        self.assertEqual(self.edit_calls(), [])
                        self.assertEqual(sim.snapshot({victim}), self.seen[-1])  # exactly as the change left it
                        self.assert_nothing_else_changed()
                        # and the next sweeps do not come back to it either
                        self.seen[:] = []
                        for _ in range(3):
                            sim.advance(1800)
                            self.assertEqual(sim.sweep().code, 0)
                        if name in ("rename", "token replaced"):
                            # still a download on hold that carries a token: the limit lets it go, it and only it
                            self.assert_calls_are_clean({victim})
                        else:
                            self.assertEqual(self.edit_calls(), [])
                        self.assert_nothing_else_changed()

    def test_a_download_deleted_while_its_failure_is_being_carried_out_is_not_marked_bad(self):
        # The removal is the one edit that is attempted before the identity changes; after the user's own
        # deletion the history entry is not Completer's to mark.
        sim = self.sim
        victim = self.make_victim(False)
        self.during_check(sim.user_delete, "after")
        sim.advance(100)
        self.assertEqual(sim.sweep().code, 0)
        entry = sim.entry(victim)
        self.assertEqual(sim.where(victim), "history")
        self.assertEqual(entry.mark_status, "NONE")
        for _ in range(3):
            sim.advance(1800)
            self.assertEqual(sim.sweep().code, 0)
        self.assertEqual(sim.entry(victim).mark_status, "NONE")
        self.assertEqual(self.edit_calls(), [])
        self.assert_nothing_else_changed()

    def test_a_user_who_starts_a_good_download_during_the_check_is_not_paused_again(self):
        for path in ("sweep", "queue"):
            with self.subTest(path=path):
                self.setUp()
                sim = self.sim
                victim = self.make_victim(True)
                self.during_check(sim.user_resume, "after")
                if path == "sweep":
                    sim.advance(100)
                    self.assertEqual(sim.sweep().code, 0)
                else:
                    self.assertEqual(sim.run_queue_script(victim).code, 0)
                self.assertEqual(sim.where(victim), "queue")
                self.assertNotEqual(sim.status(victim), "PAUSED")
                self.assertIn(sim.param(victim, "Completer_State"), ("passed", "released"))
                self.assertEqual(sim.entry(victim).mark_status, "NONE")
                self.assertEqual(sim.edited_ids() - {victim}, set())
                self.assert_nothing_else_changed()
                self.assertFalse(any(call.command == "GroupPause" for call in self.edit_calls()))

    def test_a_user_who_starts_a_bad_download_during_the_check_only_affects_that_download(self):
        for path in ("sweep", "queue"):
            with self.subTest(path=path):
                self.setUp()
                sim = self.sim
                victim = self.make_victim(False)
                self.during_check(sim.user_resume, "after")
                if path == "sweep":
                    sim.advance(100)
                    self.assertEqual(sim.sweep().code, 0)
                else:
                    self.assertEqual(sim.run_queue_script(victim).code, 0)
                self.assertEqual(sim.edited_ids() - {victim}, set())
                self.assert_nothing_else_changed()

    def test_the_user_deleting_the_download_before_the_queue_script_runs_edits_nothing(self):
        for good in (True, False):
            with self.subTest(good=good):
                self.setUp()
                sim = self.sim
                victim = self.make_victim(good)
                sim.user_delete(victim)
                shot = sim.snapshot({victim})
                self.assertEqual(sim.run_queue_script(victim).code, 0)
                self.assertEqual(self.edit_calls(), [])
                self.assertEqual(sim.snapshot({victim})[victim]["files"], shot[victim]["files"])
                self.assert_nothing_else_changed()

    def before_the_queue_script_looks(self, action):
        """The user changes the download after the scan, at the moment the queue script first asks NZBGet."""
        original = app.Run._still_held

        def wrapper(run, subject):
            action(subject.nzb_id)
            return original(run, subject)

        patcher = mock.patch.object(app.Run, "_still_held", wrapper)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_download_changed_between_the_scan_and_its_queue_script_is_finished_by_the_scheduled_check(self):
        # the queue script refuses to write to what it cannot prove to be the scanned download and exits quietly;
        # the state stays pending, so that the scheduled check adopts the live name and token and finishes the job
        sim_actions = {
            "renamed": lambda sim: (lambda nzb_id: setattr(sim.entry(nzb_id), "name", "Renamed by the user")),
            "token replaced": lambda sim: (lambda nzb_id: sim.entry(nzb_id).set("Completer_Token",
                                                                               "0123456789abcdef")),
        }
        for change in sorted(sim_actions):
            for good in (True, False):
                with self.subTest(change=change, good=good):
                    self.setUp()
                    sim = self.sim
                    victim = self.make_victim(good)
                    self.before_the_queue_script_looks(sim_actions[change](sim))
                    run = sim.run_queue_script(victim)
                    self.assertEqual(run.code, 0)
                    self.assertNotIn("Traceback", run.out + run.err)
                    self.assertEqual(run.commands, [])  # no [NZB] parameter at all
                    self.assertEqual(self.edit_calls(), [])  # and no edit
                    self.assertTrue(any(kind == "warning" for kind, _text in run.messages))
                    self.assertEqual(sim.param(victim, "Completer_State"), "pending")
                    self.assertEqual(sim.status(victim), "PAUSED")
                    self.assert_nothing_else_changed()
                    sim.advance(100)  # past the time the queue script is given
                    self.assertEqual(sim.sweep().code, 0)
                    self.assertEqual(sim.edited_ids(), {victim})
                    self.assert_nothing_else_changed()
                    if good:
                        self.assertEqual(sim.where(victim), "queue")
                        self.assertEqual(sim.status(victim), "QUEUED")
                        self.assertEqual(sim.param(victim, "Completer_State"), "passed")
                        self.assertEqual(len(sim.entry(victim).files), 3)
                    else:
                        self.assertEqual(sim.where(victim), "history")
                        self.assertEqual(sim.sonarr_view(victim), "failed")

    def test_a_download_deleted_between_the_scan_and_its_queue_script_is_not_edited_by_anything(self):
        for good in (True, False):
            with self.subTest(good=good):
                self.setUp()
                sim = self.sim
                victim = self.make_victim(good)
                self.before_the_queue_script_looks(sim.user_delete)
                run = sim.run_queue_script(victim)
                self.assertEqual(run.code, 0)
                self.assertNotIn("Traceback", run.out + run.err)
                self.assertEqual(run.commands, [])
                self.assertEqual(self.edit_calls(), [])
                self.assertEqual(sim.where(victim), "history")
                self.assertEqual(sim.param(victim, "Completer_State"), "pending")
                shot = sim.snapshot({victim})
                for _ in range(3):
                    sim.advance(100)
                    self.assertEqual(sim.sweep().code, 0)
                self.assertEqual(self.edit_calls(), [])
                self.assertEqual(sim.snapshot({victim}), shot)  # a download the user removed stays as it is
                self.assert_nothing_else_changed()


# ======================================================================================================
# I. Tokens and parameters
# ======================================================================================================

class TokenTests(GuardCase):
    LEGAL_OUTPUT = re.compile(r"^\[NZB\] (PAUSED=1|MARK=BAD|NZBPR_Completer_(Token|State|Since|Checks|Next|File|Sum|"
                              r"Info|Verdict)=[^\r\n]*)$")
    PREFIXES = ("[INFO] ", "[DETAIL] ", "[WARNING] ", "[ERROR] ", "[DEBUG] ", "[NZB] ")

    def tiny(self, tag, **kwargs):
        post = self.post(tag, **dict({"data_files": 1, "segments": 3, "par_files": 1, "par_segments": 1}, **kwargs))
        self.sim.news[0].have.update(post.all_ids)
        return post

    def tokens(self):
        return [entry.get("Completer_Token", "") for entry in self.sim.queue + self.sim.history
                if entry.get("Completer_Token", "")]

    def assert_output_is_legal(self):
        """Every line a run printed is a log line or one of the three kinds of command, and nothing else."""
        for run in self.sim.runs:
            self.assertEqual(run.err, "", run)
            for line in run.lines:
                self.assertTrue(line.startswith(self.PREFIXES), "stray output line %r" % line[:120])
                if line.startswith("[NZB] "):
                    self.assertRegex(line, self.LEGAL_OUTPUT)

    def test_fifty_downloads_get_fifty_different_sixteen_digit_hex_tokens(self):
        sim = self.world(options={"Verbose": "yes"})
        for number in range(50):
            sim.add(self.tiny("tok%02d" % number), "Token %02d" % number, category="tv", run=False)
        tokens = self.tokens()
        self.assertEqual(len(tokens), 50)
        self.assertEqual(len(set(tokens)), 50)
        for token in tokens:
            self.assertRegex(token, r"^[0-9a-f]{16}\Z")

    def test_a_token_is_never_printed_outside_the_parameter_command(self):
        sim = self.world(options={"Verbose": "yes"})
        ids = []
        for number in range(12):
            make = (self.tiny, self.tiny, lambda tag: self.bad(tag, age_hours=0.5))[number % 3]
            ids.append(sim.add(make("shy%02d" % number), "Shy %02d" % number, category="tv", run=number % 2 == 0))
        for _ in range(10):  # the sweeps do the rest: rechecks, releases, failures
            sim.advance(900)
            self.assertEqual(sim.sweep().code, 0)
        self.assertEqual(len(self.tokens()), 12)
        self.say_nothing_secret()
        for run in sim.runs:
            for token in self.tokens():
                self.assertNotIn(token, run.err)
        self.assert_output_is_legal()

    def test_only_the_nine_parameters_of_completer_are_ever_written(self):
        sim = self.world(options={"Verbose": "yes"})
        for number in range(9):
            make = (self.tiny, self.bad, lambda tag: self.bad(tag, age_hours=0.5))[number % 3]
            sim.add(make("nine%02d" % number), "Nine %02d" % number, category="tv", run=number % 2 == 0)
        for _ in range(10):
            sim.advance(900)
            self.assertEqual(sim.sweep().code, 0)
        written = set()
        for run in sim.runs:
            for command in run.commands:
                if command.startswith("NZBPR_"):
                    written.add(command[len("NZBPR_"):].split("=", 1)[0])
                else:
                    self.assertIn(command, ("PAUSED=1", "MARK=BAD"))
        for text in (call.text for call in self.edit_calls() if call.text):
            written.add(text.split("=", 1)[0])
        self.assertTrue(written)
        self.assertLessEqual(written, set(PARAMETER_NAMES))
        self.assertGreaterEqual(len(written), 7)  # the run did exercise most of them
        for entry in sim.queue + sim.history:  # and the downloads carry nothing else that is new
            names = {name for name, _value in entry.params}
            self.assertLessEqual(names - set(PARAMETER_NAMES), {"*Unpack:", "Completer:"})
        self.assert_calls_are_clean()
        self.assert_output_is_legal()

    HOSTILE_NAMES = ("Evil\nNZBPR_Completer_State=passed", "Evil\n[NZB] MARK=BAD", "Evil\r\n[NZB] PAUSED=0",
                     "[NZB] MARK=BAD", "Evil\x1b[2J\x07", "Evil\u2028[NZB] MARK=BAD", "Evil\x85[NZB] MARK=BAD",
                     "Evil\x0b[NZB] MARK=BAD", "Evil\x0c[NZB] MARK=BAD", "Evil\x1c[NZB] MARK=BAD",
                     "100%s%n{0}{x}", "<script>alert(1)</script>", "x" * 5000, "\u65e5\u672c\u8a9e = \u2603",
                     "NZBNAME=Other\nCATEGORY=tv", "a=b=c", "\ud7ff\ufffe")

    def test_a_download_name_cannot_forge_a_command_in_the_output(self):
        for good in (True, False):
            sim = self.world(options={"Verbose": "yes"})
            for number, name in enumerate(self.HOSTILE_NAMES):
                tag = "evil%02d" % number
                post = self.tiny(tag) if good else self.bad(tag)
                sim.add(post, name, category="tv", run=True)
            sim.advance(900)
            self.assertEqual(sim.sweep().code, 0)
            with self.subTest(good=good):
                self.assert_output_is_legal()
                marks = [line for run in sim.runs for line in run.lines if line == "[NZB] MARK=BAD"]
                self.assertEqual(len(marks), 0 if good else len(self.HOSTILE_NAMES))  # one per bad download, no more
                pauses = [line for run in sim.runs for line in run.lines if line == "[NZB] PAUSED=1"]
                self.assertEqual(len(pauses), len(self.HOSTILE_NAMES))  # exactly one per scan
                for entry in sim.queue + sim.history:
                    self.assertNotEqual(entry.get("Completer_State", ""), "passed" if not good else "none")
                if good:
                    self.assertEqual(sim.history, [])
                    for entry in sim.queue:
                        self.assertEqual(entry.get("Completer_State"), "passed")
                        self.assertNotEqual(sim.status(entry.id), "PAUSED")
                else:
                    self.assertEqual(sim.queue, [])
                    self.assertEqual(len(sim.history), len(self.HOSTILE_NAMES))

    def test_a_hostile_name_does_not_forge_a_command_when_the_sweep_does_the_work(self):
        sim = self.world(options={"Verbose": "yes"})
        for number, name in enumerate(self.HOSTILE_NAMES[:8]):
            sim.add(self.bad("sweepevil%02d" % number), name, category="tv", run=False)
        sim.events[:] = []
        sim.advance(100)
        self.assertEqual(sim.sweep().code, 0)
        self.assert_output_is_legal()
        self.assert_calls_are_clean()
        self.assertEqual(sim.queue, [])
        for entry in sim.history:
            self.assertEqual(sim.sonarr_view(entry.id), "failed")


# ======================================================================================================
# J. There is no path to deleting files, pausing the queue, or editing more than one download
# ======================================================================================================

class SourceTests(unittest.TestCase):
    """Read the program, do not run it: the dangerous commands are not in it at all."""

    FORBIDDEN_WORDS = re.compile(r"\b(FileDelete|FilePause|FileResume|FileMoveTop|GroupPause|GroupFinalDelete|"
                                 r"GroupMoveOffset|GroupMoveTop|GroupMoveBottom|HistoryDelete|HistoryFinalDelete|"
                                 r"HistoryReturn|HistoryProcess|PostDelete|pausedownload2?|resumedownload|"
                                 r"pausepost|pausescan|deletefile|loadlog|writelog)\b", re.IGNORECASE)
    COMMAND_WORDS = re.compile(r"^(Group|History)[A-Z][A-Za-z]*\Z")  # (the File* commands are forbidden outright)
    SIX_COMMANDS = {"GroupResume", "GroupPauseExtraPars", "GroupSetParameter", "GroupDelete", "HistoryMarkBad",
                    "HistorySetParameter"}
    METHODS = {"listgroups", "history", "status", "editqueue"}

    @classmethod
    def setUpClass(cls):
        cls.trees = {}
        names = sorted(name for name in os.listdir(SOURCES) if name.endswith(".py"))
        for name in names:
            with open(os.path.join(SOURCES, name), "rb") as handle:
                cls.trees[name] = ast.parse(handle.read(), name)
        with open(os.path.join(TEMPLATE, "main.py"), "rb") as handle:
            cls.trees["main.py"] = ast.parse(handle.read(), "main.py")

    @staticmethod
    def docstrings(tree):
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = node.body
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                        and isinstance(body[0].value.value, str):
                    found.add(id(body[0].value))
        return found

    def strings(self):
        """Every string constant that is code, not a docstring: (file, line, text)."""
        for name, tree in self.trees.items():
            skip = self.docstrings(tree)
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
                    yield name, node.lineno, node.value

    def names(self):
        """Every identifier the program uses: variables, attributes, functions, arguments, imports."""
        for name, tree in self.trees.items():
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    yield name, node.lineno, node.id
                elif isinstance(node, ast.Attribute):
                    yield name, node.lineno, node.attr
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    yield name, node.lineno, node.name
                elif isinstance(node, ast.arg):
                    yield name, node.lineno, node.arg
                elif isinstance(node, ast.keyword) and node.arg:
                    yield name, node.lineno, node.arg
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    for alias in node.names:
                        yield name, node.lineno, alias.name

    def test_the_sources_were_found(self):
        self.assertGreaterEqual(len(self.trees), 8)
        for required in ("app.py", "rpc.py", "checker.py", "main.py"):
            self.assertIn(required, self.trees)

    def test_no_string_in_the_program_names_a_command_to_delete_files_or_pause(self):
        for name, line, text in self.strings():
            for word in re.findall(r"[A-Za-z]+", text):
                self.assertNotRegex(word, self.FORBIDDEN_WORDS, "%s:%d: %r" % (name, line, text[:80]))

    def test_no_identifier_in_the_program_is_a_pausing_or_deleting_command(self):
        for name, line, text in self.names():
            self.assertNotRegex(text, self.FORBIDDEN_WORDS, "%s:%d: %r" % (name, line, text))

    def test_the_only_commands_named_in_the_program_are_the_six(self):
        found = set()
        for name, line, text in self.strings():
            if self.COMMAND_WORDS.match(text):
                found.add(text)
                self.assertIn(text, self.SIX_COMMANDS, "%s:%d" % (name, line))
        self.assertEqual(found, self.SIX_COMMANDS)  # and every one of the six is really there

    def test_the_six_commands_are_listed_in_one_place_each(self):
        listed = {}
        for name, tree in self.trees.items():
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Tuple):
                    targets = [target.id for target in node.targets if isinstance(target, ast.Name)]
                    for target in targets:
                        if target in ("QUEUE_EDITS", "HISTORY_EDITS"):
                            listed[target] = tuple(element.value for element in node.value.elts)
        self.assertEqual(listed, {"QUEUE_EDITS": ("GroupResume", "GroupPauseExtraPars", "GroupSetParameter",
                                                  "GroupDelete"),
                                  "HISTORY_EDITS": ("HistoryMarkBad", "HistorySetParameter")})

    def rpc_calls(self):
        """Every ``<something>.call(...)`` in the program: (file, enclosing class and function names, call)."""
        found = []

        def collect(name, node, owner):
            for child in ast.iter_child_nodes(node):
                where = owner
                if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    where = owner + (child.name,)
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and child.func.attr == "call":
                    found.append((name, where, child))
                collect(name, child, where)

        for name, tree in self.trees.items():
            collect(name, tree, ())
        return found

    def test_every_call_to_nzbget_names_one_of_four_methods_with_a_literal(self):
        seen = {}
        for name, where, call in self.rpc_calls():
            first = call.args[0] if call.args else None
            self.assertIsInstance(first, ast.Constant, "%s:%d: the method of a call is not a literal" %
                                  (name, call.lineno))
            self.assertIn(first.value, self.METHODS)
            seen.setdefault(first.value, []).append((name, where))
        self.assertEqual(set(seen), self.METHODS)

    def test_there_is_one_editqueue_call_and_it_sends_the_one_verified_id(self):
        sites = [(name, where, call) for name, where, call in self.rpc_calls()
                 if call.args and isinstance(call.args[0], ast.Constant) and call.args[0].value == "editqueue"]
        self.assertEqual(len(sites), 1)
        name, where, call = sites[0]
        self.assertEqual((name, where[:1], where[-1:]), ("rpc.py", ("QueueGuard",), ("edit_many",)))
        self.assertEqual(len(call.args), 4)
        self.assertEqual(call.keywords, [])
        expected = ast.dump(ast.parse("[target.nzb_id]", mode="eval").body)
        self.assertEqual(ast.dump(call.args[3]), expected)  # a list of exactly one element: the target's id
        # ... and it is sent only after the one verification and the check against the six commands
        function = next(node for node in ast.walk(self.trees["rpc.py"])
                        if isinstance(node, ast.FunctionDef) and node.name == "edit_many")
        source = ast.dump(function)
        self.assertLess(source.index("'verify'"), source.index("'editqueue'"))
        self.assertLess(source.index("Refused"), source.index("'verify'"))

    def test_nothing_but_the_rpc_module_talks_http_and_nothing_but_the_checker_talks_nntp(self):
        for name, tree in self.trees.items():
            for node in ast.walk(tree):
                modules = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and not node.level:
                    modules = [node.module or ""]
                for module in modules:
                    root = module.split(".")[0]
                    if root in ("urllib", "http", "requests", "xmlrpc"):
                        self.assertEqual(name, "rpc.py", module)
                    if root in ("socket", "ssl"):
                        self.assertEqual(name, "nntp.py", module)
                    self.assertNotIn(root, ("subprocess", "shutil", "ctypes", "pickle", "marshal", "importlib",
                                            "pty", "multiprocessing", "asyncio", "sqlite3"), "%s imports %s" %
                                     (name, module))

    def test_the_program_runs_no_commands_and_executes_no_text(self):
        for name, tree in self.trees.items():
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    if isinstance(func, ast.Name):
                        self.assertNotIn(func.id, ("exec", "eval", "compile", "__import__", "execfile"), name)
                    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "os":
                        self.assertFalse(func.attr.startswith(("exec", "spawn", "popen", "system", "fork")),
                                         "%s:%d os.%s" % (name, node.lineno, func.attr))

    def test_only_the_lock_module_writes_or_deletes_files(self):
        for name, tree in self.trees.items():
            if name == "locks.py":
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "os":
                    self.assertNotIn(func.attr, ("remove", "unlink", "rmdir", "removedirs", "rename", "replace",
                                                 "makedirs", "mkdir", "chmod", "chown", "truncate", "open", "symlink",
                                                 "link"), "%s:%d os.%s" % (name, node.lineno, func.attr))
                if isinstance(func, ast.Name) and func.id == "open" or \
                        isinstance(func, ast.Attribute) and func.attr == "open" and \
                        isinstance(func.value, ast.Name) and func.value.id in ("gzip", "io", "os"):
                    modes = [arg.value for arg in node.args[1:2] if isinstance(arg, ast.Constant)]
                    modes += [kw.value.value for kw in node.keywords if kw.arg == "mode"
                              and isinstance(kw.value, ast.Constant)]
                    for mode in modes:
                        self.assertEqual(mode, "rb", "%s:%d opens a file for something else than reading" %
                                         (name, node.lineno))


if __name__ == "__main__":
    unittest.main()
