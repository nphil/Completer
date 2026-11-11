"""The verdict flows of Completer end to end: scan script -> queue script -> scheduled check, against the fake NZBGet.

Every test runs the REAL ``main`` against ``nzbgetsim`` and a fake news server and then looks at what NZBGet
shows afterwards (queue, history, parameters) and at every call the fake NZBGet received. The headline guarantee
that these tests prove, again and again, is that Completer only ever acts on the one download it checked: the
sim records a violation (and ``SimTestCase`` fails the test) for any edit that names a list of ids, any id that is
not a download carrying Completer's token, any command Completer may not use.

Time is simulated (``sim.advance``); the places where the program sleeps are patched out.
"""

import os
import re
import socket
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import nzbgetsim as S  # noqa: E402
from completer import app, nntp  # noqa: E402

HOUR = 3600
SWEEP_EVERY = 300           # NZBGet starts the scheduled check every five minutes
RESUME = ["GroupResume", "GroupPauseExtraPars"]
STATE = "Completer_State"
TOKEN = "Completer_Token"
LOG_TAGS = ("[INFO] ", "[DETAIL] ", "[WARNING] ", "[ERROR] ", "[NZB] ")


def thinned(ids, share):
    """``ids`` without ``share`` of them, taken evenly all along the list (deterministic)."""
    return [msgid for number, msgid in enumerate(ids) if int((number + 1) * share) == int(number * share)]


class FlowCase(S.SimTestCase):
    """Helpers shared by every flow test. Posts have three data files and three par2 files, so that repair
    data can replace about 40 percent of the content (a plain ``make_post`` can repair only 3 percent)."""

    def setUp(self):
        patcher = mock.patch("time.sleep")  # the program sleeps while it waits for NZBGet or for a server
        patcher.start()
        self.addCleanup(patcher.stop)

    # ------------------------------------------------------------ building the scene

    def start(self, **kwargs):
        """An Enforce world whose news server has no articles yet; ``serve`` puts some there."""
        return self.world(**kwargs)

    def make(self, tag, **kwargs):
        kwargs.setdefault("par_files", 3)
        kwargs.setdefault("par_segments", 40)
        return self.post(tag, **kwargs)

    def serve(self, post, data_lost=0.0, par_lost=0.0, news=None):
        """Let the news server hold the post, minus a share of its data articles and of its par2 articles."""
        news = news or self.sim.news[0]
        news.have.update(thinned(post.data_ids, data_lost))
        news.have.update(thinned(post.par_ids, par_lost))
        return post

    def good(self, tag="good", **kwargs):
        """A complete post, served in full."""
        return self.serve(self.make(tag, **kwargs))

    def bad(self, tag="bad", **kwargs):
        """A post that cannot be completed: 40 percent of the data and all of the par2 files are gone."""
        return self.serve(self.make(tag, **kwargs), data_lost=0.4, par_lost=1.0)

    def add(self, post, name=None, **kwargs):
        return self.sim.add(post, name or "Release " + post.tag, **kwargs)

    # ------------------------------------------------------------ looking at the result

    def param(self, nzb_id, name, default=None):
        return self.sim.param(nzb_id, name, default)

    def edits_of(self, nzb_id):
        """(command, text) of every edit that named this download."""
        return [(command, text) for command, text, ids in self.sim.edits if ids == [nzb_id]]

    def commands_of(self, nzb_id):
        return [command for command, _text in self.edits_of(nzb_id)]

    def is_held(self, nzb_id):
        return self.sim.where(nzb_id) == "queue" and self.sim.status(nzb_id) == "PAUSED"

    def warned(self):
        return any(kind == "warning" for kind, _text in self.sim.log)

    def assert_passed(self, nzb_id):
        sim = self.sim
        self.assertEqual(sim.where(nzb_id), "queue")
        self.assertEqual(sim.status(nzb_id), "QUEUED")
        self.assertEqual(self.param(nzb_id, STATE), "passed")
        self.assertEqual(self.param(nzb_id, "Completer_Verdict"), "pass/release")
        self.assertEqual(sim.entry(nzb_id).paused_files and [n for n in sim.entry(nzb_id).paused_files
                                                             if ".vol" not in n], [])

    def assert_failed(self, nzb_id):
        sim = self.sim
        self.assertEqual(sim.where(nzb_id), "history")
        self.assertEqual(sim.entry(nzb_id).text_status, "FAILURE/BAD")
        self.assertEqual(sim.sonarr_view(nzb_id), "failed")
        self.assertEqual(self.param(nzb_id, STATE), "failed")
        self.assertTrue(self.param(nzb_id, "Completer_Info"))

    def assert_let_through(self, nzb_id, state="released"):
        """Started, in the queue, never failed: what every fail-open path ends in."""
        sim = self.sim
        self.assertEqual(sim.where(nzb_id), "queue")
        self.assertEqual(sim.status(nzb_id), "QUEUED")
        self.assertEqual(self.param(nzb_id, STATE), state)
        self.assertEqual(sim.entry(nzb_id).mark_status, "NONE")
        self.assertIsNone(sim.sonarr_view(nzb_id))

    def run_until_settled(self, nzb_id, sweeps=100, step=SWEEP_EVERY):
        """Sweep every five minutes for ``sweeps`` rounds; returns (seconds held, highest check count seen)."""
        sim = self.sim
        began = sim.now
        left = None
        highest = 0
        for _ in range(sweeps):
            sim.advance(step)
            sim.sweep()
            highest = max(highest, int(self.param(nzb_id, "Completer_Checks", "0")))
            if left is None and not self.is_held(nzb_id):
                left = sim.now - began
        return left, highest


# ====================================================================== 1. a complete post


class CompletePostTests(FlowCase):
    def test_the_scan_holds_exactly_the_new_download_and_says_so(self):
        # Proves: the hold is announced as parameters of the one download being scanned, and nothing else.
        sim = self.start()
        post = self.good()
        nzb_id = self.add(post, run=False)
        scan = sim.runs_of("scan")[0]
        self.assertEqual(scan.code, 0)
        names = [command.split("=", 1)[0] for command in scan.commands]
        self.assertEqual(names, ["NZBPR_Completer_Token", "NZBPR_Completer_State", "NZBPR_Completer_Since",
                                 "NZBPR_Completer_Sum", "NZBPR_Completer_File", "PAUSED"])
        self.assertTrue(re.match(r"^[0-9a-f]{16}$", self.param(nzb_id, TOKEN)))
        self.assertEqual(self.param(nzb_id, STATE), "pending")
        self.assertEqual(self.param(nzb_id, "Completer_Since"), str(int(sim.now)))
        self.assertRegex(self.param(nzb_id, "Completer_Sum"), r"^\d+-[0-9a-f]{8}$")
        self.assertEqual(self.param(nzb_id, "Completer_File"), sim.entry(nzb_id).queued_file)
        self.assertEqual(scan.commands[-1], "PAUSED=1")
        self.assertEqual(sim.status(nzb_id), "PAUSED")
        self.assertEqual(sim.calls, [])  # a scan script never talks to NZBGet

    def test_a_complete_post_is_released_and_only_that_download_is_edited(self):
        # Proves: pass -> GroupResume then GroupPauseExtraPars on that one id, state passed.
        sim = self.start()
        post = self.good()
        nzb_id = self.add(post, run=False)
        self.assertEqual(sim.status(nzb_id), "PAUSED")
        self.assertEqual(sim.edits, [])
        sim.pump()
        self.assertEqual(sim.edits, [("GroupResume", "", [nzb_id]), ("GroupPauseExtraPars", "", [nzb_id])])
        self.assert_passed(nzb_id)
        self.assertEqual(sim.runs_of("queue")[0].code, 0)
        self.assertEqual(self.param(nzb_id, "Completer_Checks"), "1")

    def test_the_repair_volumes_are_paused_again_after_the_release(self):
        # Proves: the download ends exactly as if it had never been held (NZBGet pauses extra pars on a new nzb).
        sim = self.start()
        nzb_id = self.add(self.good())
        volumes = [name for name in sim.entry(nzb_id).paused_files]
        self.assertEqual(sorted(volumes), ["data.vol001+01.par2", "data.vol002+02.par2"])
        self.assertEqual(sim.status(nzb_id), "QUEUED")

    def test_with_par_check_force_nothing_stays_paused_and_no_second_edit_is_sent(self):
        # Proves: when NZBGet keeps every par file on (ParCheck=force) Completer does not pause extra pars.
        sim = self.start(nzbget={"ParCheck": "force"})
        nzb_id = self.add(self.good())
        self.assertEqual(sim.edits, [("GroupResume", "", [nzb_id])])
        self.assertEqual(sim.entry(nzb_id).paused_files, [])
        self.assert_passed(nzb_id)

    def test_a_download_that_is_checked_and_passed_is_not_judged_by_sonarr_as_anything(self):
        # Proves: a passed download is simply in the queue; nothing about it reaches the history.
        sim = self.start()
        nzb_id = self.add(self.good())
        self.assertIsNone(sim.sonarr_view(nzb_id))
        self.assertEqual(sim.history, [])

    def test_a_download_in_flight_is_not_checked_a_second_time_by_the_sweep(self):
        # Proves: once passed, the sweep has nothing to do with the download (no news server question, no edit).
        sim = self.start()
        nzb_id = self.add(self.good())
        news = sim.news[0]
        asked, edits, shot = news.stats_total, list(sim.edits), sim.snapshot()
        sim.advance(2 * HOUR)
        run = sim.sweep()
        self.assertEqual(run.code, 0)
        self.assertEqual((news.stats_total, sim.edits, sim.snapshot()), (asked, edits, shot))
        self.assertEqual(self.param(nzb_id, STATE), "passed")


# ====================================================================== 2. a post that cannot be completed


class BadPostTests(FlowCase):
    def test_a_post_that_cannot_be_repaired_is_failed_through_mark_bad(self):
        # Proves: fail -> the queue script marks its own download bad; NZBGet moves it to the history as bad,
        # Sonarr/Radarr see a failure, and Completer itself sent no edit at all.
        sim = self.start()
        nzb_id = self.add(self.bad(), run=False)
        runs = sim.pump()
        self.assertEqual(len(runs), 1)
        self.assertIn("MARK=BAD", runs[0].commands)
        self.assertEqual(runs[0].code, 0)
        self.assert_failed(nzb_id)
        self.assertEqual(sim.sonarr_view(nzb_id), "failed")
        self.assertEqual(sim.edits, [])
        self.assertEqual(sim.entry(nzb_id).text_status, "FAILURE/BAD")
        self.assertEqual(sim.entry(nzb_id).files, [])  # the download is gone, nothing is left to fetch

    def test_a_failed_post_never_shows_up_as_passed(self):
        sim = self.start()
        nzb_id = self.add(self.bad())
        self.assertNotEqual(self.param(nzb_id, STATE), "passed")
        self.assertNotIn("passed", self.param(nzb_id, "Completer_Verdict"))
        self.assertTrue(self.param(nzb_id, "Completer_Verdict").startswith("fail/"))
        self.assertEqual(self.param(nzb_id, "Completer_Checks"), "1")
        self.assertEqual(sim.queue, [])

    def test_the_failure_leaves_a_readable_note_on_the_history_entry(self):
        sim = self.start()
        nzb_id = self.add(self.bad())
        info = self.param(nzb_id, "Completer_Info")
        self.assertTrue(info)
        self.assertLess(len(info), 400)
        self.assertEqual(sim.where(nzb_id), "history")
        self.assertEqual(self.param(nzb_id, STATE), "failed")

    def test_only_the_failed_download_is_removed_and_a_good_neighbour_is_untouched(self):
        # Proves: a failure removes the one download that was checked, not the others (the old extension's bug).
        sim = self.start()
        bystander = self.add(self.good("bystander"))
        shot = sim.snapshot({bystander})
        edits = list(sim.edits)
        bad_id = self.add(self.bad("worse"))
        self.assert_failed(bad_id)
        self.assertEqual(sim.snapshot({bystander}), shot)
        self.assertEqual(sim.edits, edits)
        self.assertEqual(sim.sonarr_view(bystander), None)


# ====================================================================== 3. repairable, unrepairable, nothing at all


class RepairabilityTests(FlowCase):
    def test_a_post_with_a_few_percent_missing_and_plenty_of_par2_passes(self):
        # Proves: loss that the repair data covers is not a reason to fail a download.
        sim = self.start()
        nzb_id = self.add(self.serve(self.make("small"), data_lost=0.05))
        self.assert_passed(nzb_id)
        self.assertEqual(sim.edits, [("GroupResume", "", [nzb_id]), ("GroupPauseExtraPars", "", [nzb_id])])

    def test_a_larger_loss_still_passes_while_the_par2_files_are_all_there(self):
        nzb_id = self.start() and self.add(self.serve(self.make("medium"), data_lost=0.2))
        self.assert_passed(nzb_id)

    def test_the_same_loss_fails_when_the_par2_files_are_missing_too(self):
        # Proves: par2 articles count; data that could be repaired is unrepairable once the repair data is gone.
        sim = self.start()
        nzb_id = self.add(self.serve(self.make("nopar"), data_lost=0.2, par_lost=1.0))
        self.assert_failed(nzb_id)
        self.assertEqual(sim.edits, [])

    def test_a_loss_beyond_what_even_a_complete_repair_set_covers_fails(self):
        nzb_id = self.start() and self.add(self.serve(self.make("toomuch"), data_lost=0.6))
        self.assert_failed(nzb_id)

    def test_a_post_with_nothing_found_anywhere_is_not_failed(self):
        # Proves: proof of life. When not one article exists, "not here" cannot be told from a lying provider,
        # so NZBGet is left to fail it by itself; Completer lets it go and never fails it.
        sim = self.start()
        news = sim.news[0]
        nzb_id = self.add(self.make("void"))
        self.assertGreater(news.stats_total, 0)  # the server really was asked, and said 430 every time
        self.assert_let_through(nzb_id)
        self.assertTrue(self.param(nzb_id, "Completer_Verdict").startswith("gone/"))
        self.assertNotEqual(self.param(nzb_id, "Completer_Verdict"), "fail/fail")
        self.assertEqual(self.commands_of(nzb_id), RESUME)

    def test_a_post_with_nothing_found_is_not_failed_even_when_it_is_old_and_checked_by_the_sweep(self):
        sim = self.start()
        nzb_id = self.add(self.make("void2"), run=False)
        sim.advance(100)
        sim.sweep()
        self.assert_let_through(nzb_id)
        self.assertEqual(sim.history, [])


# ====================================================================== 4. a fresh post that looks bad


class FreshPostTests(FlowCase):
    def hold_a_fresh_bad_post(self, **options):
        sim = self.start(options=options or None)
        post = self.make("fresh", age_hours=0.5)
        self.serve(post, data_lost=0.4, par_lost=1.0)
        nzb_id = self.add(post)
        return sim, post, nzb_id

    def test_a_fresh_post_that_looks_bad_waits_instead_of_failing(self):
        # Proves: articles may not have reached every server yet, so a young post gets time.
        sim, post, nzb_id = self.hold_a_fresh_bad_post()
        self.assertEqual(self.param(nzb_id, STATE), "waiting")
        self.assertEqual(sim.status(nzb_id), "PAUSED")
        self.assertEqual(self.param(nzb_id, "Completer_Checks"), "1")
        self.assertEqual(sim.edits, [])
        newest = post.files[0]["posted"]
        due = int(self.param(nzb_id, "Completer_Next"))
        self.assertGreater(due, sim.now)
        self.assertLessEqual(due, newest + 2 * HOUR + 30)
        self.assertEqual(self.param(nzb_id, "Completer_Verdict"), "fail/wait")
        self.assertEqual(sim.where(nzb_id), "queue")

    def test_a_sweep_before_the_recheck_is_due_changes_nothing(self):
        sim, _post, nzb_id = self.hold_a_fresh_bad_post()
        due = int(self.param(nzb_id, "Completer_Next"))
        shot = sim.snapshot()
        asked = sim.news[0].stats_total
        sim.advance(due - sim.now - 5)
        run = sim.sweep()
        self.assertEqual(run.code, 0)
        self.assertEqual(sim.snapshot(), shot)
        self.assertEqual(sim.edits, [])
        self.assertEqual(sim.news[0].stats_total, asked)
        self.assertTrue(run.says("waiting"))

    def test_when_the_articles_have_appeared_the_recheck_releases_it_as_passed(self):
        sim, post, nzb_id = self.hold_a_fresh_bad_post()
        due = int(self.param(nzb_id, "Completer_Next"))
        sim.news[0].have.update(post.all_ids)  # the post reached the server meanwhile
        sim.advance(due - sim.now + 1)
        sim.sweep()
        self.assertEqual(self.param(nzb_id, STATE), "passed")
        self.assertEqual(self.param(nzb_id, "Completer_Verdict"), "pass/release")
        self.assertEqual(sim.status(nzb_id), "QUEUED")
        self.assertEqual(self.param(nzb_id, "Completer_Checks"), "2")
        self.assertEqual({ids[0] for _c, _t, ids in sim.edits}, {nzb_id})
        self.assertEqual([c for c in self.commands_of(nzb_id) if c != "GroupSetParameter"], RESUME)

    def test_when_it_is_still_missing_the_recheck_waits_again_a_little_longer(self):
        sim, _post, nzb_id = self.hold_a_fresh_bad_post()
        first = int(self.param(nzb_id, "Completer_Next"))
        sim.advance(first - sim.now + 1)
        sim.sweep()
        self.assertEqual(self.param(nzb_id, STATE), "waiting")
        self.assertEqual(self.param(nzb_id, "Completer_Checks"), "2")
        second = int(self.param(nzb_id, "Completer_Next"))
        self.assertGreater(second, first)
        self.assertGreater(second, sim.now)
        self.assertTrue(self.is_held(nzb_id))
        self.assertEqual(self.commands_of(nzb_id), ["GroupSetParameter"] * len(self.commands_of(nzb_id)))

    def test_once_the_fresh_window_is_over_the_final_check_fails_it_through_the_sweep(self):
        # Proves: the sweep's failure path is GroupDelete, then HistoryMarkBad, then HistorySetParameter, all on
        # the one download, and Sonarr sees a failure.
        sim, post, nzb_id = self.hold_a_fresh_bad_post()
        before = len(sim.edits)
        sim.advance(2 * HOUR)  # the window (2 hours after the newest article) is over
        run = sim.sweep()
        self.assertEqual(run.code, 0)
        self.assert_failed(nzb_id)
        sweep_edits = sim.edits[before:]
        self.assertTrue(sweep_edits)
        self.assertEqual({ids[0] for _c, _t, ids in sweep_edits}, {nzb_id})
        self.assertEqual({len(ids) for _c, _t, ids in sweep_edits}, {1})
        commands = [command for command, _t, _i in sweep_edits if command != "GroupSetParameter"]
        self.assertEqual(commands, ["GroupDelete", "HistoryMarkBad", "HistorySetParameter"])
        self.assertEqual(sim.entry(nzb_id).delete_status, "MANUAL")
        self.assertEqual(sim.entry(nzb_id).mark_status, "BAD")
        self.assertEqual(sim.sonarr_view(nzb_id), "failed")

    def test_the_whole_life_of_a_fresh_bad_post_when_checked_every_five_minutes(self):
        # Proves: wait, wait again, ..., fail; Checks only goes up; the download is edited only by the sweep.
        sim, _post, nzb_id = self.hold_a_fresh_bad_post()
        seen = [int(self.param(nzb_id, "Completer_Checks"))]
        while self.is_held(nzb_id):
            sim.advance(SWEEP_EVERY)
            sim.sweep()
            seen.append(int(self.param(nzb_id, "Completer_Checks")))
            self.assertLess(len(seen), 40)
        self.assertEqual(seen, sorted(seen))
        self.assertGreater(len(set(seen)), 2)
        self.assert_failed(nzb_id)

    def test_a_download_that_is_only_just_too_old_to_wait_is_failed_at_once(self):
        # Proves: the fresh window is FreshHours after the newest article; beyond it there is no waiting.
        sim = self.start()
        post = self.make("edge", age_hours=2.05)
        nzb_id = self.add(self.serve(post, data_lost=0.4, par_lost=1.0))
        self.assert_failed(nzb_id)

    def test_with_fresh_hours_zero_a_young_bad_post_is_failed_at_once(self):
        sim = self.start(options={"FreshHours": "0"})
        post = self.make("nofresh", age_hours=0.1)
        nzb_id = self.add(self.serve(post, data_lost=0.4, par_lost=1.0))
        self.assert_failed(nzb_id)


# ====================================================================== 5. rechecks are bounded


class BoundedRecheckTests(FlowCase):
    def test_a_fresh_post_that_never_recovers_ends_failed_within_the_hold_limit(self):
        # Proves: a hold ends at the latest FreshHours + 1 h (+ one sweep) after it began, never held forever.
        sim = self.start()
        post = self.make("never", age_hours=0.0)
        nzb_id = self.add(self.serve(post, data_lost=0.4, par_lost=1.0))
        self.assertTrue(self.is_held(nzb_id))
        left, highest = self.run_until_settled(nzb_id, sweeps=100)
        self.assertIsNotNone(left)
        self.assertLessEqual(left, 2 * HOUR + HOUR + SWEEP_EVERY)
        self.assertLessEqual(highest, 8)
        self.assert_failed(nzb_id)

    def test_a_long_fresh_window_is_ended_by_the_eighth_check(self):
        # Proves: at most 8 checks per download, even when the fresh window would allow far more.
        sim = self.start(options={"FreshHours": "72"})
        post = self.make("long", age_hours=0.0)
        nzb_id = self.add(self.serve(post, data_lost=0.4, par_lost=1.0))
        left, highest = self.run_until_settled(nzb_id, sweeps=100)
        self.assertEqual(highest, 8)
        self.assertEqual(self.param(nzb_id, "Completer_Checks"), "8")
        self.assertIsNotNone(left)
        self.assertLess(left, 100 * SWEEP_EVERY)
        self.assert_failed(nzb_id)

    def test_a_post_that_recovers_late_is_released_not_failed(self):
        sim = self.start()
        post = self.make("late", age_hours=0.0)
        nzb_id = self.add(self.serve(post, data_lost=0.4, par_lost=1.0))
        for _ in range(5):
            sim.advance(SWEEP_EVERY)
            sim.sweep()
        self.assertTrue(self.is_held(nzb_id))
        sim.news[0].have.update(post.all_ids)
        left, highest = self.run_until_settled(nzb_id, sweeps=30)
        self.assertLessEqual(highest, 8)
        self.assert_passed(nzb_id)
        self.assertEqual(sim.history, [])

    def test_a_post_that_stays_suspect_is_let_through_after_at_most_three_checks(self):
        # Proves: when the articles cannot be known (the server answers 480), Completer never fails the
        # download; it waits a little and then fails open.
        sim = self.start(broken_code=480)
        nzb_id = self.add(self.good("suspect"))
        self.assertEqual(self.param(nzb_id, STATE), "waiting")
        self.assertEqual(self.param(nzb_id, "Completer_Verdict"), "suspect/wait")
        self.assertTrue(self.is_held(nzb_id))
        left, highest = self.run_until_settled(nzb_id, sweeps=100)
        self.assertLessEqual(highest, 3)
        self.assertIsNotNone(left)
        self.assertLessEqual(left, 30 * 60)
        self.assert_let_through(nzb_id)
        self.assertEqual(sim.history, [])
        self.assertNotIn("MARK=BAD", [c for run in sim.runs for c in run.commands])

    def test_a_server_that_stalls_after_login_makes_a_download_suspect_never_failed(self):
        with mock.patch.object(nntp, "ANSWER_TIMEOUT", 0.3), mock.patch.object(nntp, "CONNECT_TIMEOUT", 0.3):
            sim = self.start(stall="stat")
            nzb_id = self.add(self.good("stalled"))
            self.assertEqual(self.param(nzb_id, "Completer_Verdict"), "suspect/wait")
            left, highest = self.run_until_settled(nzb_id, sweeps=20)
        self.assertLessEqual(highest, 3)
        self.assert_let_through(nzb_id)

    def test_a_suspect_download_that_recovers_is_passed(self):
        sim = self.start(broken_code=502)
        nzb_id = self.add(self.good("flaky"))
        self.assertTrue(self.is_held(nzb_id))
        sim.news[0].broken_code = 0
        sim.advance(SWEEP_EVERY)
        sim.sweep()
        self.assert_passed(nzb_id)

    def test_a_suspect_check_is_never_a_failure_even_for_a_fresh_post(self):
        sim = self.start(broken_code=480)
        nzb_id = self.add(self.good("youngsuspect", age_hours=0.2))
        left, highest = self.run_until_settled(nzb_id, sweeps=100)
        self.assertEqual(sim.history, [])
        self.assertLessEqual(highest, 3)
        self.assert_let_through(nzb_id)


if __name__ == "__main__":
    unittest.main()
