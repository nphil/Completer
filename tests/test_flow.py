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
        self.assertEqual([name for name in sim.entry(nzb_id).paused_files if ".vol" not in name], [])

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
        calls_before = len(sim.calls)  # the first scheduled check read the queue
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
        self.assertEqual(len(sim.calls), calls_before)  # a scan script never talks to NZBGet

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
        self.start()
        nzb_id = self.add(self.serve(self.make("medium"), data_lost=0.2))
        self.assert_passed(nzb_id)

    def test_the_same_loss_fails_when_the_par2_files_are_missing_too(self):
        # Proves: par2 articles count; data that could be repaired is unrepairable once the repair data is gone.
        sim = self.start()
        nzb_id = self.add(self.serve(self.make("nopar"), data_lost=0.2, par_lost=1.0))
        self.assert_failed(nzb_id)
        self.assertEqual(sim.edits, [])

    def test_a_loss_beyond_what_even_a_complete_repair_set_covers_fails(self):
        self.start()
        nzb_id = self.add(self.serve(self.make("toomuch"), data_lost=0.6))
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


class SuspectDelayTests(FlowCase):
    def test_an_old_suspect_post_is_rechecked_after_five_and_then_fifteen_minutes(self):
        # Proves: the documented waits (300 s, then 900 s) hold for a post that is long past its fresh window.
        sim = self.start(broken_code=480)
        nzb_id = self.add(self.good("slow", age_hours=48))
        self.assertEqual(int(self.param(nzb_id, "Completer_Next")) - int(sim.now), 300)
        sim.advance(299)
        shot = sim.snapshot()
        sim.sweep()
        self.assertEqual(sim.snapshot(), shot)  # not due yet
        sim.advance(2)
        sim.sweep()
        self.assertEqual(self.param(nzb_id, "Completer_Checks"), "2")
        self.assertEqual(int(self.param(nzb_id, "Completer_Next")) - int(sim.now), 900)
        self.assertTrue(self.is_held(nzb_id))
        sim.advance(901)
        sim.sweep()
        self.assertEqual(self.param(nzb_id, "Completer_Checks"), "3")
        self.assert_let_through(nzb_id)


# ====================================================================== 6. an nzb that cannot be used: fail open


class UnusableNzbTests(FlowCase):
    def held_with_a_good_post(self):
        sim = self.start()
        nzb_id = self.add(self.good(), run=False)
        self.assertTrue(self.is_held(nzb_id))
        return sim, nzb_id

    def assert_let_go_unasked(self, nzb_id):
        self.assert_let_through(nzb_id)
        self.assertEqual(self.commands_of(nzb_id)[:2], RESUME)
        self.assertEqual(self.sim.news[0].stats_total, 0)
        self.assertEqual(self.sim.history, [])

    def test_a_garbage_nzb_lets_the_download_go(self):
        # Proves: an nzb that is not an nzb any more is never a reason to fail a download.
        sim, nzb_id = self.held_with_a_good_post()
        with open(sim.entry(nzb_id).queued_file, "w", encoding="utf-8") as handle:
            handle.write("this is not xml at all <<<")
        runs = sim.pump()
        self.assertEqual(runs[0].code, 0)
        self.assertNotIn("MARK=BAD", runs[0].commands)
        self.assert_let_go_unasked(nzb_id)
        self.assertTrue(runs[0].says("nzb"))  # the reason is in the log

    def test_a_file_that_is_another_valid_nzb_is_not_trusted(self):
        # Proves: the checksum taken at scan time stops Completer judging a different post under this name.
        sim, nzb_id = self.held_with_a_good_post()
        other = self.make("other")  # a post that does not exist on the server at all
        with open(sim.entry(nzb_id).queued_file, "w", encoding="utf-8") as handle:
            handle.write(other.xml)
        sim.pump()
        self.assert_let_go_unasked(nzb_id)

    def test_an_nzb_that_has_vanished_lets_the_download_go(self):
        sim, nzb_id = self.held_with_a_good_post()
        os.remove(sim.entry(nzb_id).queued_file)
        sim.pump()
        self.assert_let_go_unasked(nzb_id)

    def test_the_sweep_also_lets_a_download_with_a_garbage_nzb_go(self):
        sim, nzb_id = self.held_with_a_good_post()
        with open(sim.entry(nzb_id).queued_file, "w", encoding="utf-8") as handle:
            handle.write("garbage")
        sim.events[:] = []  # the queue script never runs
        sim.advance(100)
        sim.sweep()
        self.assert_let_go_unasked(nzb_id)


# ====================================================================== 7. no news server can be asked: fail open


class NoServerTests(FlowCase):
    def assert_let_go_with_a_warning(self, nzb_id):
        self.assert_let_through(nzb_id)
        self.assertEqual(self.commands_of(nzb_id)[:2], RESUME)
        self.assertTrue(self.warned())
        self.assertEqual(self.sim.history, [])
        self.assertNotIn("MARK=BAD", [c for run in self.sim.runs for c in run.commands])

    def test_a_stopped_news_server_lets_the_download_go(self):
        # Proves: unreachable servers make articles unknown, never missing, so nothing is failed.
        sim = self.start()
        post = self.good()
        sim.news[0].stop()  # connections are refused from now on
        nzb_id = self.add(post)
        self.assert_let_go_with_a_warning(nzb_id)
        self.assertEqual(self.param(nzb_id, "Completer_Verdict"), "error/release")

    def test_a_server_that_rejects_the_login_lets_the_download_go(self):
        sim = self.start(reject_login=True)
        nzb_id = self.add(self.good())
        self.assert_let_go_with_a_warning(nzb_id)

    def test_a_server_that_is_full_lets_the_download_go(self):
        # Proves: "too many connections" is a server problem, not a verdict on the post.
        sim = self.start(max_connections=1)
        blocker = socket.create_connection(("127.0.0.1", sim.news[0].port), timeout=5)
        self.addCleanup(blocker.close)
        self.assertTrue(blocker.recv(100).startswith(b"200"))  # it holds the only connection
        nzb_id = self.add(self.good())
        self.assert_let_go_with_a_warning(nzb_id)
        self.assertGreater(sim.news[0].rejected_total, 0)

    def test_a_server_that_hangs_up_at_once_lets_the_download_go(self):
        sim = self.start(hangup=True)
        nzb_id = self.add(self.good())
        self.assert_let_go_with_a_warning(nzb_id)

    def test_a_server_that_never_says_hello_lets_the_download_go(self):
        with mock.patch.object(nntp, "CONNECT_TIMEOUT", 0.3), mock.patch.object(nntp, "ANSWER_TIMEOUT", 0.3):
            sim = self.start(stall=True)
            nzb_id = self.add(self.good())
        self.assert_let_go_with_a_warning(nzb_id)

    def test_a_provider_that_denies_everything_is_not_believed(self):
        # Proves: a provider answering 430 to every question (it lies) never fails a download.
        sim = self.start(deny_all=True)
        nzb_id = self.add(self.good())
        self.assert_let_through(nzb_id)
        self.assertGreater(sim.news[0].stats_total, 0)
        self.assertEqual(sim.history, [])

    def test_when_every_server_is_ignored_there_is_nobody_to_ask_and_the_download_goes(self):
        sim = self.start(options={"IgnoreServers": "1"})
        nzb_id = self.add(self.good())
        self.assert_let_through(nzb_id)
        self.assertEqual(sim.news[0].connections_total, 0)

    def test_the_sweep_lets_a_pending_download_go_when_no_server_answers(self):
        sim = self.start()
        post = self.good()
        nzb_id = self.add(post, run=False)
        sim.news[0].stop()
        sim.advance(100)
        sim.sweep()
        self.assert_let_go_with_a_warning(nzb_id)


# ====================================================================== 8. a crash inside the check


class CrashTests(FlowCase):
    def crash(self):
        return mock.patch.object(app.checker, "check", side_effect=RuntimeError("boom"))

    def test_a_crash_in_the_queue_script_leaves_the_download_untouched(self):
        # Proves: an unexpected exception ends the run with code 1 before anything is changed.
        sim = self.start()
        nzb_id = self.add(self.good(), run=False)
        shot = sim.snapshot()
        edits = list(sim.edits)
        with self.crash():
            runs = sim.pump()
        self.assertEqual(runs[0].code, 1)
        self.assertEqual(sim.snapshot(), shot)
        self.assertEqual(sim.edits, edits)
        self.assertTrue(any(kind == "error" for kind, _text in sim.log))
        self.assertNotIn("boom", runs[0].out)  # nothing of the exception text, only where it happened

    def test_the_next_sweep_then_checks_the_download_properly(self):
        sim = self.start()
        nzb_id = self.add(self.good(), run=False)
        with self.crash():
            sim.pump()
        sim.advance(90)
        sim.sweep()
        self.assert_passed(nzb_id)
        self.assertEqual({ids[0] for _c, _t, ids in sim.edits}, {nzb_id})

    def test_a_crash_while_the_sweep_checks_one_download_changes_nothing(self):
        sim = self.start()
        nzb_id = self.add(self.good(), run=False)
        sim.advance(100)
        shot = sim.snapshot()
        with self.crash():
            run = sim.sweep()
        self.assertEqual(run.code, 0)
        self.assertEqual(sim.snapshot(), shot)
        self.assertEqual(sim.edits, [])
        sim.advance(SWEEP_EVERY)
        sim.sweep()
        self.assert_passed(nzb_id)

    def test_a_check_that_always_crashes_still_ends_with_the_download_released(self):
        # Proves: whatever goes wrong inside the check, the 30 minute limit lets the download go.
        sim = self.start()
        nzb_id = self.add(self.bad(), run=False)
        with self.crash():
            sim.pump()
            for _ in range(8):
                sim.advance(SWEEP_EVERY)
                sim.sweep()
        self.assert_let_through(nzb_id)
        self.assertEqual(sim.history, [])
        self.assertEqual(self.commands_of(nzb_id)[:2], RESUME)


# ====================================================================== 9. modes


class ModeTests(FlowCase):
    def own_parameters(self, nzb_id):
        return [name for name, _value in self.sim.entry(nzb_id).params if name.startswith("Completer_")]

    def test_a_dry_run_never_holds_a_download_and_never_talks_to_nzbget(self):
        # Proves: DryRun changes nothing: the scan prints nothing, nothing is paused, not one RPC call is made.
        sim = self.start(mode="DryRun")
        nzb_id = self.add(self.good(), run=False)
        scan = sim.runs_of("scan")[0]
        self.assertEqual((scan.code, scan.out), (0, ""))
        self.assertEqual(sim.status(nzb_id), "QUEUED")
        self.assertEqual(self.own_parameters(nzb_id), [])
        sim.pump()
        self.assertEqual(sim.calls, [])
        self.assertEqual(sim.status(nzb_id), "QUEUED")
        self.assertEqual(self.param(nzb_id, STATE), "observed")
        self.assertEqual(self.param(nzb_id, "Completer_Verdict"), "pass/release")
        self.assertTrue(self.param(nzb_id, "Completer_Info"))
        self.assertEqual(self.param(nzb_id, "Completer_Checks"), "1")
        self.assertIsNone(self.param(nzb_id, TOKEN))

    def test_a_bad_post_in_a_dry_run_stays_in_the_queue_and_the_note_says_it_would_be_failed(self):
        sim = self.start(mode="DryRun")
        nzb_id = self.add(self.bad())
        self.assertEqual(sim.calls, [])
        self.assertEqual(sim.where(nzb_id), "queue")
        self.assertEqual(sim.status(nzb_id), "QUEUED")
        self.assertEqual(sim.entry(nzb_id).mark_status, "NONE")
        self.assertEqual(self.param(nzb_id, STATE), "observed")
        self.assertEqual(self.param(nzb_id, "Completer_Verdict"), "fail/fail")
        self.assertIn("would be failed", self.param(nzb_id, "Completer_Info"))
        self.assertNotIn("MARK=BAD", [c for run in sim.runs for c in run.commands])
        self.assertEqual(sim.history, [])

    def test_a_fresh_bad_post_in_a_dry_run_is_observed_without_any_hold(self):
        sim = self.start(mode="DryRun")
        post = self.make("young", age_hours=0.3)
        nzb_id = self.add(self.serve(post, data_lost=0.4, par_lost=1.0))
        self.assertEqual(sim.calls, [])
        self.assertEqual(sim.status(nzb_id), "QUEUED")
        self.assertEqual(self.param(nzb_id, STATE), "observed")
        self.assertEqual(self.param(nzb_id, "Completer_Verdict"), "fail/wait")

    def test_off_does_nothing_at_all(self):
        # Proves: Off prints nothing, makes not one RPC call and writes nothing.
        sim = self.start(mode="Off")
        nzb_id = self.add(self.bad())
        self.assertTrue(sim.runs)
        self.assertTrue(all(run.out == "" and run.code == 0 for run in sim.runs))
        self.assertEqual(sim.calls, [])
        self.assertEqual(self.own_parameters(nzb_id), [])
        self.assertEqual(sim.status(nzb_id), "QUEUED")
        self.assertEqual(sim.news[0].stats_total, 0)
        sim.advance(HOUR)
        run = sim.sweep()  # even a scheduled check that happens to run does not act in Off mode
        self.assertEqual(sim.edits, [])
        self.assertEqual(sim.where(nzb_id), "queue")

    def test_switching_to_dry_run_while_a_download_is_held_lets_the_next_sweep_release_it(self):
        sim = self.start()
        nzb_id = self.add(self.bad(), run=False)  # would be failed if it were checked
        sim.set_option("Mode", "DryRun")
        sim.sweep()
        self.assert_let_through(nzb_id)
        self.assertEqual({ids[0] for _c, _t, ids in sim.edits}, {nzb_id})
        self.assertEqual(self.commands_of(nzb_id)[:2], RESUME)
        self.assertEqual(sim.news[0].stats_total, 0)  # released, not judged
        self.assertEqual(sim.history, [])

    def test_switching_to_off_while_a_download_is_held_lets_the_next_sweep_release_it(self):
        sim = self.start()
        nzb_id = self.add(self.bad(), run=False)
        sim.set_option("Mode", "Off")
        sim.sweep()
        self.assert_let_through(nzb_id)
        self.assertEqual(sim.news[0].stats_total, 0)
        self.assertEqual(sim.history, [])

    def test_the_downloads_own_queue_script_releases_it_too_when_the_mode_is_dry_run(self):
        sim = self.start()
        nzb_id = self.add(self.bad(), run=False)
        sim.set_option("Mode", "DryRun")
        sim.pump()
        self.assertEqual(self.commands_of(nzb_id), RESUME)
        self.assertEqual(sim.status(nzb_id), "QUEUED")
        self.assertEqual(self.param(nzb_id, STATE), "observed")  # released first, then only observed
        self.assertEqual(sim.entry(nzb_id).mark_status, "NONE")
        self.assertEqual(sim.where(nzb_id), "queue")

    def test_the_downloads_own_queue_script_releases_it_too_when_the_mode_is_off(self):
        sim = self.start()
        nzb_id = self.add(self.bad(), run=False)
        sim.set_option("Mode", "Off")
        sim.pump()
        self.assertEqual(sim.edits, [("GroupResume", "", [nzb_id]), ("GroupPauseExtraPars", "", [nzb_id])])
        self.assert_let_through(nzb_id)
        self.assertEqual(sim.news[0].stats_total, 0)

    def test_a_waiting_download_is_released_when_the_mode_changes(self):
        sim = self.start()
        post = self.make("w", age_hours=0.3)
        nzb_id = self.add(self.serve(post, data_lost=0.4, par_lost=1.0))
        self.assertEqual(self.param(nzb_id, STATE), "waiting")
        sim.set_option("Mode", "Off")
        sim.advance(SWEEP_EVERY)
        sim.sweep()
        self.assert_let_through(nzb_id)


# ====================================================================== 10. categories


class CategoryTests(FlowCase):
    def test_a_download_in_an_unwanted_category_is_not_held_not_checked_and_not_touched(self):
        # Proves: Completer leaves other categories completely alone.
        sim = self.start(options={"Categories": "tv, movies"})
        calls = len(sim.calls)
        nzb_id = self.add(self.bad(), category="Music", run=False)
        shot = sim.snapshot()
        self.assertEqual(sim.status(nzb_id), "QUEUED")
        sim.pump()
        self.assertEqual(sim.snapshot(), shot)
        self.assertEqual(sim.news[0].stats_total, 0)
        self.assertEqual(len(sim.calls), calls)
        self.assertTrue(all(run.out == "" for run in sim.runs_of("scan") + sim.runs_of("queue")))
        sim.advance(HOUR)
        sim.sweep()
        self.assertEqual(sim.snapshot(), shot)
        self.assertEqual(sim.edits, [])

    def test_a_download_without_a_category_is_not_covered_when_categories_are_listed(self):
        sim = self.start(options={"Categories": "tv"})
        nzb_id = self.add(self.bad(), category="")
        self.assertEqual(sim.where(nzb_id), "queue")
        self.assertEqual(sim.news[0].stats_total, 0)

    def test_a_wanted_category_is_held_checked_and_released(self):
        sim = self.start(options={"Categories": "tv, movies"})
        nzb_id = self.add(self.good(), category="movies")
        self.assert_passed(nzb_id)

    def test_a_wanted_category_with_a_bad_post_is_failed(self):
        sim = self.start(options={"Categories": "tv, movies"})
        nzb_id = self.add(self.bad(), category="tv")
        self.assert_failed(nzb_id)

    def test_category_names_are_compared_without_regard_to_case(self):
        sim = self.start(options={"Categories": "TV;Movies"})
        shown = self.add(self.good("a"), category="tv", run=False)
        self.assertEqual(sim.status(shown), "PAUSED")
        other = self.add(self.good("b"), category="MOVIES", run=False)
        self.assertEqual(sim.status(other), "PAUSED")
        sim.pump()
        self.assert_passed(shown)
        self.assert_passed(other)

    def test_a_category_removed_from_the_option_releases_the_held_download_at_the_next_sweep(self):
        sim = self.start(options={"Categories": "tv"})
        nzb_id = self.add(self.bad(), category="tv", run=False)
        self.assertTrue(self.is_held(nzb_id))
        sim.set_option("Categories", "movies")
        sim.sweep()
        self.assert_let_through(nzb_id)
        self.assertEqual(sim.news[0].stats_total, 0)
        self.assertEqual(sim.history, [])

    def test_a_category_removed_from_the_option_also_releases_it_in_its_own_queue_script(self):
        sim = self.start(options={"Categories": "tv"})
        nzb_id = self.add(self.bad(), category="tv", run=False)
        sim.set_option("Categories", "movies")
        sim.pump()
        self.assert_let_through(nzb_id)
        self.assertEqual(sim.news[0].stats_total, 0)


# ====================================================================== 11. downloads added paused by someone else


class PausedByOthersTests(FlowCase):
    def test_a_download_added_paused_gets_no_token_and_stays_paused_when_it_is_good(self):
        # Proves: Completer never resumes a download it did not hold.
        sim = self.start()
        nzb_id = self.add(self.good(), paused=True)
        self.assertIsNone(self.param(nzb_id, TOKEN))
        self.assertEqual(sim.runs_of("scan")[0].commands, [])
        self.assertEqual(sim.status(nzb_id), "PAUSED")
        self.assertEqual(sim.edits, [])
        self.assertEqual(self.param(nzb_id, "Completer_Verdict"), "pass/nothing")
        sim.advance(HOUR)
        sim.sweep()
        self.assertEqual(sim.status(nzb_id), "PAUSED")
        self.assertEqual(sim.edits, [])

    def test_a_download_added_paused_that_is_bad_is_still_failed_by_its_queue_script(self):
        # Deliberate: the verdict on the download's own nzb does not depend on who paused it.
        sim = self.start()
        nzb_id = self.add(self.bad(), paused=True)
        self.assertIsNone(self.param(nzb_id, TOKEN))
        self.assert_failed(nzb_id)
        self.assertEqual(sim.edits, [])

    def test_a_paused_download_of_a_fresh_bad_post_is_left_alone(self):
        # A download that is not held cannot be made to wait: there is nothing to release later.
        sim = self.start()
        post = self.make("freshpaused", age_hours=0.2)
        nzb_id = self.add(self.serve(post, data_lost=0.4, par_lost=1.0), paused=True)
        self.assertEqual(sim.where(nzb_id), "queue")
        self.assertEqual(sim.status(nzb_id), "PAUSED")
        self.assertEqual(sim.edits, [])
        self.assertEqual(self.param(nzb_id, "Completer_Verdict"), "fail/nothing")
        sim.advance(3 * HOUR)
        sim.sweep()
        self.assertEqual(sim.where(nzb_id), "queue")
        self.assertEqual(sim.edits, [])


# ====================================================================== 12. the safety net must be known to work


class SafetyNetTests(FlowCase):
    def assert_not_held_but_checked(self, nzb_id):
        self.assertIsNone(self.param(nzb_id, TOKEN))
        self.assertEqual(self.param(nzb_id, "Completer_Checks"), "1")

    def test_without_a_first_scheduled_check_new_downloads_are_not_held(self):
        # Proves: a download is only held while the sweep that would release it is known to be alive.
        sim = self.start(boot=False)
        nzb_id = self.add(self.good(), run=False)
        scan = sim.runs_of("scan")[0]
        self.assertEqual(scan.commands, [])
        self.assertTrue(scan.says("not put on hold"))
        self.assertTrue(self.warned())
        self.assertEqual(sim.status(nzb_id), "QUEUED")
        sim.pump()
        self.assert_not_held_but_checked(nzb_id)
        self.assertEqual(sim.edits, [])

    def test_a_bad_post_is_still_failed_by_the_queue_script_without_a_scheduled_check(self):
        sim = self.start(boot=False)
        nzb_id = self.add(self.bad())
        self.assertIsNone(self.param(nzb_id, TOKEN))
        self.assert_failed(nzb_id)
        self.assertEqual(sim.edits, [])

    def test_a_scheduled_check_that_is_too_old_means_no_hold_and_a_new_one_restores_it(self):
        sim = self.start()
        sim.advance(900)
        just_in_time = self.add(self.good("a"), run=False)
        self.assertIsNotNone(self.param(just_in_time, TOKEN))  # 900 seconds old: still good
        sim.advance(1)
        too_late = self.add(self.good("b"), run=False)
        self.assertIsNone(self.param(too_late, TOKEN))  # 901 seconds old: not trusted
        self.assertTrue(self.warned())
        sim.pump()
        self.assert_not_held_but_checked(too_late)
        self.assertEqual(self.commands_of(too_late), [])
        sim.sweep()
        again = self.add(self.good("c"), run=False)
        self.assertIsNotNone(self.param(again, TOKEN))
        self.assertEqual(sim.status(again), "PAUSED")

    def test_a_long_silence_of_the_sweep_leaves_the_checked_download_unheld_and_the_failure_intact(self):
        sim = self.start()
        sim.advance(2 * HOUR)
        nzb_id = self.add(self.bad())
        self.assertIsNone(self.param(nzb_id, TOKEN))
        self.assert_failed(nzb_id)

    def test_when_completer_is_not_in_the_extensions_list_nothing_runs_at_all(self):
        sim = self.start()
        runs = len(sim.runs)
        sim.set_nzbget("Extensions", "Other")
        nzb_id = self.add(self.bad())
        self.assertEqual(len(sim.runs), runs)
        self.assertEqual(sim.status(nzb_id), "QUEUED")
        self.assertIsNone(self.param(nzb_id, TOKEN))
        self.assertEqual(sim.news[0].stats_total, 0)

    def test_a_download_that_opted_out_with_completer_no_gets_no_hold_and_no_run(self):
        sim = self.start()
        before = len(sim.runs_of("queue"))
        nzb_id = self.add(self.bad(), queue_scripts="no")
        self.assertEqual(len(sim.runs_of("queue")), before)
        self.assertEqual(sim.runs_of("scan")[0].commands, [])
        self.assertEqual(sim.status(nzb_id), "QUEUED")
        self.assertIsNone(self.param(nzb_id, TOKEN))
        self.assertEqual(sim.news[0].stats_total, 0)
        self.assertEqual(sim.edits, [])

    def test_a_scan_that_finds_completer_missing_from_the_extensions_list_does_not_hold(self):
        # Proves: nothing is held when no scheduled check could release it (the list is read by the scan itself).
        sim = self.start()
        original = sim.base_env

        def without_completer():
            env = original()
            env["NZBOP_Extensions"] = env["NZBOP_EXTENSIONS"] = "Other"
            return env

        with mock.patch.object(sim, "base_env", without_completer):
            nzb_id = self.add(self.good(), run=False)
        self.assertEqual(sim.runs_of("scan")[0].commands, [])
        self.assertTrue(self.warned())
        self.assertEqual(sim.status(nzb_id), "QUEUED")


# ====================================================================== 13. the queue script never ran: the sweep takes over


class PendingTests(FlowCase):
    def pending(self, kind="good", **kwargs):
        sim = self.start(**kwargs)
        nzb_id = self.add(self.bad() if kind == "bad" else self.good(), run=False)
        self.assertTrue(self.is_held(nzb_id))
        return sim, nzb_id

    def test_a_young_pending_download_is_left_to_its_queue_script(self):
        sim, nzb_id = self.pending()
        sim.advance(60)
        shot = sim.snapshot()
        run = sim.sweep()
        self.assertEqual(sim.snapshot(), shot)
        self.assertEqual(sim.edits, [])
        self.assertEqual(sim.news[0].stats_total, 0)
        self.assertTrue(run.says("pending"))

    def test_after_ninety_seconds_the_sweep_checks_a_pending_good_post_and_releases_it(self):
        # Proves: the sweep is a full replacement for a queue script that did not run, on that download only.
        sim, nzb_id = self.pending()
        sim.advance(95)
        sim.sweep()
        self.assert_passed(nzb_id)
        self.assertEqual({ids[0] for _c, _t, ids in sim.edits}, {nzb_id})
        self.assertEqual([c for c in self.commands_of(nzb_id) if c != "GroupSetParameter"], RESUME)

    def test_after_ninety_seconds_the_sweep_fails_a_pending_bad_post_through_the_removal_path(self):
        sim, nzb_id = self.pending("bad")
        sim.advance(95)
        sim.sweep()
        self.assert_failed(nzb_id)
        self.assertEqual({ids[0] for _c, _t, ids in sim.edits}, {nzb_id})
        self.assertEqual([c for c in self.commands_of(nzb_id) if c != "GroupSetParameter"],
                         ["GroupDelete", "HistoryMarkBad", "HistorySetParameter"])

    def test_a_pending_download_that_was_never_checked_in_time_is_released(self):
        sim, nzb_id = self.pending("bad")
        sim.advance(1900)
        sim.sweep()
        self.assert_let_through(nzb_id)
        self.assertEqual(sim.news[0].stats_total, 0)  # let go, not judged
        self.assertEqual(sim.history, [])
        self.assertEqual(self.commands_of(nzb_id)[:2], RESUME)

    def test_a_pending_download_whose_nzb_file_vanished_is_released(self):
        sim, nzb_id = self.pending("bad")
        os.remove(sim.entry(nzb_id).queued_file)
        sim.advance(95)
        sim.sweep()
        self.assert_let_through(nzb_id)
        self.assertEqual(sim.history, [])

    def test_a_pending_download_whose_nzb_file_was_replaced_is_released_not_judged(self):
        sim, nzb_id = self.pending("good")
        with open(sim.entry(nzb_id).queued_file, "w", encoding="utf-8") as handle:
            handle.write(self.make("swapped").xml)
        sim.advance(95)
        sim.sweep()
        self.assert_let_through(nzb_id)
        self.assertEqual(sim.news[0].stats_total, 0)

    def test_a_start_time_far_in_the_future_is_not_trusted(self):
        # Proves: a pending download cannot be kept on hold by a time stamp that lies ahead.
        sim, nzb_id = self.pending("bad")
        sim.entry(nzb_id).set("Completer_Since", str(int(sim.now) + 7200))
        sim.advance(95)
        sim.sweep()
        self.assert_let_through(nzb_id)
        self.assertEqual(sim.history, [])

    def test_a_start_time_a_little_ahead_is_treated_as_young(self):
        sim, nzb_id = self.pending()
        sim.entry(nzb_id).set("Completer_Since", str(int(sim.now) + 120))
        sim.advance(95)
        shot = sim.snapshot()
        sim.sweep()
        self.assertEqual(sim.snapshot(), shot)
        sim.advance(200)
        sim.sweep()
        self.assert_passed(nzb_id)

    def test_a_missing_start_time_is_not_trusted_either(self):
        sim, nzb_id = self.pending("bad")
        sim.entry(nzb_id).set("Completer_Since", "")  # an empty value removes the parameter
        sim.advance(95)
        sim.sweep()
        self.assert_let_through(nzb_id)


# ====================================================================== 14. someone else resumed a held download


class ResumedByUserTests(FlowCase):
    def test_a_held_download_resumed_by_a_user_is_recorded_and_not_touched_again(self):
        sim = self.start()
        nzb_id = self.add(self.bad(), run=False)
        sim.user_resume(nzb_id)
        sim.advance(95)
        shot_files = sim.entry(nzb_id).files
        sim.sweep()
        self.assertEqual(self.param(nzb_id, STATE), "released")
        self.assertEqual(sim.status(nzb_id), "QUEUED")
        self.assertEqual(sim.entry(nzb_id).paused_files, [])
        self.assertEqual(set(self.commands_of(nzb_id)), {"GroupSetParameter"})  # never resumed or paused by us
        self.assertEqual(sim.news[0].stats_total, 0)  # not even checked
        shot = sim.snapshot()
        edits = list(sim.edits)
        sim.advance(2 * HOUR)
        sim.sweep()
        self.assertEqual((sim.snapshot(), sim.edits), (shot, edits))

    def test_a_user_resumed_download_is_never_failed_by_a_sweep_even_when_the_post_is_bad(self):
        # Proves: Completer fails only what it holds; a download someone resumed is not held.
        sim = self.start()
        nzb_id = self.add(self.bad(), run=False)
        sim.user_resume(nzb_id)
        sim.advance(SWEEP_EVERY)
        sim.sweep()
        self.assertEqual(sim.where(nzb_id), "queue")
        self.assertEqual(sim.entry(nzb_id).mark_status, "NONE")
        self.assertNotIn("GroupDelete", self.commands_of(nzb_id))

    def test_a_waiting_download_that_a_user_resumed_is_left_alone_by_the_sweep(self):
        sim = self.start()
        post = self.make("w2", age_hours=0.3)
        nzb_id = self.add(self.serve(post, data_lost=0.4, par_lost=1.0))
        self.assertEqual(self.param(nzb_id, STATE), "waiting")
        sim.user_resume(nzb_id)
        sim.advance(3 * HOUR)
        sim.sweep()
        self.assertEqual(self.param(nzb_id, STATE), "released")
        self.assertEqual(sim.where(nzb_id), "queue")
        self.assertEqual(sim.entry(nzb_id).paused_files, [])
        self.assertEqual(set(self.commands_of(nzb_id)), {"GroupSetParameter"})

    def test_the_queue_script_of_a_user_resumed_download_judges_its_own_nzb_as_usual(self):
        # What the code does: the queue script acts on the one download it was started for, held or not, so a
        # bad post is failed (MARK=BAD, no RPC edit) even when someone resumed it first.
        sim = self.start()
        nzb_id = self.add(self.bad(), run=False)
        sim.user_resume(nzb_id)
        sim.pump()
        self.assert_failed(nzb_id)
        self.assertEqual(sim.edits, [])

    def test_the_queue_script_of_a_user_resumed_good_download_does_not_touch_it(self):
        sim = self.start()
        nzb_id = self.add(self.good(), run=False)
        sim.user_resume(nzb_id)
        sim.pump()
        self.assertEqual(sim.edits, [])
        self.assertEqual(sim.status(nzb_id), "QUEUED")
        self.assertEqual(sim.entry(nzb_id).paused_files, [])

    def test_a_held_download_that_a_user_deleted_is_not_turned_into_a_failure(self):
        sim = self.start()
        nzb_id = self.add(self.bad(), run=False)
        sim.user_delete(nzb_id)
        self.assertEqual(sim.pump(), [])  # events of a deleted download are dropped by NZBGet
        sim.advance(95)
        sim.sweep()
        self.assertEqual(sim.edits, [])
        self.assertEqual(sim.where(nzb_id), "history")
        self.assertNotEqual(sim.sonarr_view(nzb_id), "failed")
        self.assertEqual(sim.entry(nzb_id).mark_status, "NONE")

    def test_a_download_renamed_while_it_is_being_checked_is_not_touched(self):
        # Proves: the guard re-reads the download right before each edit and refuses a different name.
        sim = self.start()
        nzb_id = self.add(self.bad(), run=False)
        sim.advance(95)
        real = app.checker.check

        def check_and_rename(*args, **kwargs):
            result = real(*args, **kwargs)
            sim.entry(nzb_id).name = "Renamed by somebody"
            return result

        with mock.patch.object(app.checker, "check", side_effect=check_and_rename):
            sim.sweep()
        self.assertEqual(sim.where(nzb_id), "queue")
        self.assertEqual(sim.entry(nzb_id).mark_status, "NONE")
        self.assertEqual(sim.edits, [])
        self.assertTrue(self.warned())


# ====================================================================== 15. several downloads at once


class IndependenceTests(FlowCase):
    def test_one_downloads_verdict_never_changes_another_downloads_state(self):
        # Proves the headline guarantee in the flow it was written for: A waits, B passes, A recovers, C fails.
        sim = self.start()
        young = self.make("young", age_hours=0.3)
        self.serve(young, data_lost=0.4, par_lost=1.0)
        a = self.add(young)
        self.assertEqual(self.param(a, STATE), "waiting")
        shot_a = sim.snapshot({a})

        b = self.add(self.good("b"))
        self.assert_passed(b)
        self.assertEqual(sim.snapshot({a}), shot_a)
        shot_b = sim.snapshot({b})

        c = self.add(self.bad("c"))
        self.assert_failed(c)
        self.assertEqual(sim.snapshot({a}), shot_a)
        self.assertEqual(sim.snapshot({b}), shot_b)

        sim.news[0].have.update(young.all_ids)
        sim.advance(1200)
        sim.sweep()
        self.assert_passed(a)
        self.assertEqual(sim.snapshot({b}), shot_b)
        self.assertEqual(sim.where(c), "history")
        self.assertEqual(sim.sonarr_view(c), "failed")
        self.assertIsNone(sim.sonarr_view(a))
        self.assertEqual(sim.queue[0].id, a)

    def test_two_pending_downloads_are_each_checked_by_their_own_queue_script(self):
        sim = self.start()
        a = self.add(self.bad("a"), run=False)
        b = self.add(self.good("b"), run=False)
        shot_b = sim.snapshot({b})
        sim.run_queue_script(a)
        sim.events[:] = [event for event in sim.events if event[1] != a]
        self.assert_failed(a)
        self.assertEqual(sim.snapshot({b}), shot_b)
        self.assertTrue(self.is_held(b))
        sim.pump()
        self.assert_passed(b)
        self.assertEqual(sim.where(a), "history")

    def test_a_sweep_over_five_downloads_touches_each_one_only_as_its_own_state_requires(self):
        sim = self.start()
        failing = self.add(self.bad("f"), run=False)
        passing = self.add(self.good("p"), run=False)
        vanished = self.add(self.good("v"), run=False)
        os.remove(sim.entry(vanished).queued_file)
        by_user = self.add(self.good("u"), paused=True, run=False)       # paused by whoever added it
        opted_out = self.add(self.bad("o"), queue_scripts="no", run=False)  # no Completer on this one
        others = sim.snapshot({by_user, opted_out})
        self.assertEqual(sim.edits, [])
        sim.advance(95)
        sim.sweep()
        self.assertEqual(sim.snapshot({by_user, opted_out}), others)
        self.assertEqual({ids[0] for _c, _t, ids in sim.edits}, {failing, passing, vanished})
        self.assertEqual([c for c in self.commands_of(failing) if c != "GroupSetParameter"],
                         ["GroupDelete", "HistoryMarkBad", "HistorySetParameter"])
        self.assertEqual([c for c in self.commands_of(passing) if c != "GroupSetParameter"], RESUME)
        self.assertEqual([c for c in self.commands_of(vanished) if c != "GroupSetParameter"], RESUME)
        self.assert_failed(failing)
        self.assert_passed(passing)
        self.assert_let_through(vanished)
        self.assertEqual(sim.status(by_user), "PAUSED")
        self.assertEqual(sim.status(opted_out), "QUEUED")
        self.assertEqual({entry.id for entry in sim.history}, {failing})

    def test_a_failure_by_the_sweep_leaves_a_user_paused_neighbour_with_all_its_files_alone(self):
        # The old extension deleted the files of every other paused download; here nothing else may change.
        sim = self.start()
        neighbour = self.add(self.good("n"), paused=True)
        before = sim.snapshot({neighbour})
        files = [dict(item) for item in sim.entry(neighbour).files]
        doomed = self.add(self.bad("d"), run=False)
        sim.advance(95)
        sim.sweep()
        self.assert_failed(doomed)
        self.assertEqual(sim.snapshot({neighbour}), before)
        self.assertEqual(sim.entry(neighbour).files, files)
        self.assertEqual(sim.edited_ids(), {doomed})


# ====================================================================== 16. several news servers


class ServerSetTests(FlowCase):
    def test_an_ignored_server_is_never_contacted(self):
        # Proves: IgnoreServers keeps Completer away from a server entirely, even when it would have the post.
        sim = self.start(options={"IgnoreServers": "1"})
        second = self.make_news()
        sim.add_server(second)  # server 2
        post = self.make("ign")
        self.serve(post, news=sim.news[0])  # only the ignored server has it
        nzb_id = self.add(post)
        self.assertEqual((sim.news[0].connections_total, sim.news[0].stats_total), (0, 0))
        self.assertGreater(second.stats_total, 0)
        self.assertEqual(self.param(nzb_id, "Completer_Verdict"), "gone/release")  # server 2 has nothing

    def test_a_post_that_only_the_not_ignored_server_has_passes(self):
        sim = self.start(options={"IgnoreServers": "2"})
        second = self.make_news()
        sim.add_server(second)
        post = self.make("ign2")
        self.serve(post, news=sim.news[0])
        nzb_id = self.add(post)
        self.assert_passed(nzb_id)
        self.assertEqual(second.connections_total, 0)

    def test_a_post_that_is_bad_on_the_only_server_that_counts_is_failed(self):
        sim = self.start(options={"IgnoreServers": "1"})
        second = self.make_news()
        sim.add_server(second)
        post = self.make("ign3")
        self.serve(post, news=sim.news[0])  # the ignored one has everything
        self.serve(post, data_lost=0.4, par_lost=1.0, news=second)
        nzb_id = self.add(post)
        self.assert_failed(nzb_id)
        self.assertEqual(sim.news[0].connections_total, 0)

    def test_an_article_that_only_the_second_level_server_has_is_found(self):
        sim = self.start()
        backup = self.make_news()
        sim.add_server(backup, level=1)
        post = self.make("lvl")
        self.serve(post, news=backup)
        nzb_id = self.add(post)
        self.assert_passed(nzb_id)
        self.assertGreater(sim.news[0].stats_total, 0)  # level 0 was asked first, said no
        self.assertGreater(backup.stats_total, 0)

    def test_a_post_missing_on_both_levels_is_failed(self):
        sim = self.start()
        backup = self.make_news()
        sim.add_server(backup, level=1)
        post = self.make("both")
        self.serve(post, data_lost=0.4, par_lost=1.0, news=sim.news[0])
        self.serve(post, data_lost=0.4, par_lost=1.0, news=backup)
        self.assert_failed(self.add(post))

    def test_a_stalled_first_level_server_does_not_stop_the_second_level_one_answering(self):
        with mock.patch.object(nntp, "CONNECT_TIMEOUT", 0.3), mock.patch.object(nntp, "ANSWER_TIMEOUT", 0.3):
            sim = self.start(stall=True)
            backup = self.make_news()
            sim.add_server(backup, level=1)
            post = self.make("stl")
            self.serve(post, news=backup)
            nzb_id = self.add(post)
        self.assert_passed(nzb_id)
        self.assertTrue(self.warned())  # the stalled server is reported

    def test_a_dead_first_level_server_makes_the_second_level_servers_no_inconclusive(self):
        # Proves: an article that the backup lacks is not "missing for certain" while a server that might
        # have it could not be asked, so a bad-looking post is let through, never failed on half the facts.
        sim = self.start()
        sim.news[0].stop()
        backup = self.make_news()
        sim.add_server(backup, level=1)
        post = self.make("dead0")
        self.serve(post, data_lost=0.4, par_lost=1.0, news=backup)
        nzb_id = self.add(post)
        self.assertEqual(self.param(nzb_id, STATE), "passed")
        self.assertEqual(sim.history, [])
        self.assertNotIn("GroupDelete", self.commands_of(nzb_id))
        self.assertTrue(self.warned())


# ====================================================================== 17. output and names


class OutputTests(FlowCase):
    NASTY = ("N" * 3000 + ' with "double" and \'single\' quotes,\nnew line, \x07 bell, \x1b[31m escape, '
             '\ttab, \x00 nul and a backslash \\ at the end')

    def assert_sane_output(self):
        self.assertTrue(self.sim.runs)
        for run in self.sim.runs:
            for line in run.lines:
                self.assertTrue(line.startswith(LOG_TAGS), repr(line[:60]))
                self.assertLess(len(line), 1024)

    def test_every_line_has_a_tag_NZBGet_knows_and_stays_short_with_a_monstrous_name(self):
        sim = self.start(options={"Verbose": "yes"})
        nzb_id = self.add(self.good(), self.NASTY, category='tv"\n\'\x07')
        self.assert_passed(nzb_id)
        self.assertEqual(sim.entry(nzb_id).name, self.NASTY)
        self.assert_sane_output()

    def test_a_monstrous_name_also_survives_failure_by_mark_bad_and_by_the_sweep(self):
        sim = self.start()
        by_script = self.add(self.bad("one"), self.NASTY)
        self.assert_failed(by_script)
        by_sweep = self.add(self.bad("two"), self.NASTY + " two", run=False)
        sim.advance(95)
        sim.sweep()
        self.assert_failed(by_sweep)
        self.assertEqual(sim.entry(by_sweep).name, self.NASTY + " two")
        self.assert_sane_output()

    def test_a_name_that_is_all_control_characters_does_no_harm(self):
        sim = self.start()
        nzb_id = self.add(self.good(), "\x01\x02\n\r\t\x7f")
        self.assert_passed(nzb_id)
        self.assert_sane_output()

    def test_a_unicode_name_works_end_to_end_when_passed(self):
        sim = self.start()
        name = "\u00dcn\u00efc\u00f6d\u00e9 R\u00e9l\u00e9ase \u65e5\u672c\u8a9e \U0001f3ac"
        nzb_id = self.add(self.good(), name)
        self.assert_passed(nzb_id)
        self.assertEqual(sim.entry(nzb_id).name, name)
        self.assert_sane_output()

    def test_a_unicode_name_works_end_to_end_when_failed_by_the_sweep(self):
        sim = self.start()
        name = "\u00dcn\u00efc\u00f6d\u00e9 R\u00e9l\u00e9ase \u65e5\u672c\u8a9e"
        nzb_id = self.add(self.bad(), name, run=False)
        sim.advance(95)
        sim.sweep()
        self.assert_failed(nzb_id)
        self.assertEqual(sim.entry(nzb_id).name, name)
        self.assert_sane_output()

    def test_a_unicode_name_works_when_failed_by_the_queue_script(self):
        sim = self.start()
        nzb_id = self.add(self.bad(), "Caf\u00e9 \u00e0 la cr\u00e8me")
        self.assert_failed(nzb_id)
        self.assert_sane_output()

    def test_the_info_text_is_short_and_free_of_control_characters(self):
        sim = self.start()
        nzb_id = self.add(self.bad(), self.NASTY)
        info = self.param(nzb_id, "Completer_Info")
        self.assertLessEqual(len(info), 300)
        self.assertTrue(info.isprintable())


# ====================================================================== other flows around the same machinery


class OtherFlowTests(FlowCase):
    def test_other_queue_events_cause_no_rpc_call_and_no_change(self):
        # Proves: only NZB_ADDED does anything; a delete or finish event of a held download is ignored at once.
        sim = self.start()
        nzb_id = self.add(self.bad(), run=False)
        shot = sim.snapshot()
        calls = len(sim.calls)
        for event in ("NZB_DELETED", "NZB_DOWNLOADED", "FILE_DOWNLOADED", "NZB_MARKED", "NZB_NAMED"):
            run = sim.run_queue_script(nzb_id, event=event)
            self.assertEqual((run.code, run.out), (0, ""), event)
        self.assertEqual(len(sim.calls), calls)
        self.assertEqual(sim.snapshot(), shot)
        self.assertEqual(sim.news[0].stats_total, 0)

    def test_a_queue_script_that_cannot_reach_nzbget_leaves_the_download_held_for_the_sweep(self):
        # Proves: no verdict is acted on blindly; the held download waits for the sweep to release it.
        sim = self.start()
        nzb_id = self.add(self.good(), run=False)
        sim.rpc_down()
        self.addCleanup(sim.rpc_up)
        sim.pump()
        self.assertTrue(self.is_held(nzb_id))
        self.assertEqual(self.param(nzb_id, STATE), "pending")
        self.assertTrue(self.warned())
        sim.rpc_up()
        sim.advance(95)
        sim.sweep()
        self.assert_passed(nzb_id)

    def test_a_sweep_that_cannot_reach_nzbget_changes_nothing(self):
        sim = self.start()
        nzb_id = self.add(self.bad(), run=False)
        sim.advance(100)
        shot = sim.snapshot()
        sim.rpc_down()
        self.addCleanup(sim.rpc_up)
        run = sim.sweep()
        self.assertEqual(run.code, 0)
        self.assertTrue(run.says("could not read"))
        sim.rpc_up()
        self.assertEqual(sim.snapshot(), shot)
        self.assertEqual(sim.edits, [])

    def test_a_failure_whose_second_step_did_not_finish_is_finished_by_a_later_sweep(self):
        # Proves: GroupDelete then HistoryMarkBad is repaired when NZBGet is slow to show the history entry.
        sim = self.start()
        nzb_id = self.add(self.bad(), run=False)
        sim.history_delay = 5  # the removed download appears in the history only after five history reads
        sim.advance(95)
        sim.sweep()
        hint = os.path.join(sim.temp_dir, "completer-unfinished")
        self.assertTrue(os.path.exists(hint))
        self.assertIsNone(sim.where(nzb_id))
        self.assertIsNone(sim.sonarr_view(nzb_id))
        sim.advance(SWEEP_EVERY)
        sim.sweep()
        self.assert_failed(nzb_id)
        self.assertFalse(os.path.exists(hint))
        self.assertEqual(sim.edited_ids(), {nzb_id})

    def test_a_failure_that_is_finished_is_not_repeated_by_later_sweeps(self):
        sim = self.start()
        nzb_id = self.add(self.bad(), run=False)
        sim.advance(95)
        sim.sweep()
        shot, edits = sim.snapshot(), list(sim.edits)
        for _ in range(5):
            sim.advance(SWEEP_EVERY)
            sim.sweep()
        self.assertEqual((sim.snapshot(), sim.edits), (shot, edits))

    def test_a_history_entry_without_completers_token_is_never_touched_even_when_it_says_failing(self):
        sim = self.start()
        stranger = self.add(self.good("s"), queue_scripts="no")
        sim.entry(stranger).set("Completer_State", "failing")  # claims to be ours, carries no token
        sim.user_delete(stranger)
        sim.advance(HOUR)
        sim.sweep()
        self.assertEqual(sim.edits, [])
        self.assertEqual(sim.entry(stranger).mark_status, "NONE")


if __name__ == "__main__":
    unittest.main()


# ====================================================================== Screen mode: checked while waiting, never held


class ScreenModeTests(FlowCase):
    """Screen mode (the recommended one): nothing is held, NZBGet's single queue-script line is never used for a
    check, and the scheduled check removes only downloads that cannot be completed, before NZBGet reaches them."""

    def start(self, **kwargs):
        kwargs.setdefault("mode", "Screen")
        return self.world(**kwargs)

    def test_a_new_download_is_marked_but_never_held_and_the_queue_script_asks_nobody(self):
        sim = self.start()
        news = sim.news[0]
        nzb_id = self.add(self.good())
        self.assertEqual(sim.status(nzb_id), "QUEUED")
        self.assertEqual(self.param(nzb_id, STATE), "unchecked")
        self.assertTrue(self.param(nzb_id, TOKEN))
        self.assertNotIn("PAUSED=1", sim.runs_of("scan")[0].commands)
        self.assertEqual(news.stats_total, 0)  # the queue script returned without asking a news server
        self.assertEqual(sim.edits, [])

    def test_the_scheduled_check_passes_a_good_download_without_touching_its_queue_state(self):
        sim = self.start()
        nzb_id = self.add(self.good())
        sim.sweep()
        self.assertEqual(sim.status(nzb_id), "QUEUED")
        self.assertEqual(self.param(nzb_id, STATE), "passed")
        self.assertEqual(set(self.commands_of(nzb_id)), {"GroupSetParameter"})

    def test_a_download_that_cannot_be_completed_is_removed_as_failed_and_only_that_one(self):
        sim = self.start()
        good_id = self.add(self.good("neighbour"))
        bad_id = self.add(self.bad("broken"))
        sim.sweep()
        self.assert_failed(bad_id)
        self.assertEqual(sim.status(good_id), "QUEUED")
        self.assertEqual(self.param(good_id, STATE), "passed")
        self.assertNotIn("GroupDelete", self.commands_of(good_id))
        self.assertEqual(sim.sonarr_view(good_id), None)

    def test_several_waiting_downloads_are_all_settled_in_one_scheduled_check(self):
        sim = self.start(options={"Connections": 2})
        ids = [self.add(self.good("g%d" % n)) for n in range(3)] + [self.add(self.bad("b%d" % n)) for n in range(2)]
        sim.sweep()
        for nzb_id in ids[:3]:
            self.assertEqual(self.param(nzb_id, STATE), "passed")
        for nzb_id in ids[3:]:
            self.assert_failed(nzb_id)

    def test_switching_from_enforce_to_screen_lets_go_of_what_was_held(self):
        sim = self.world(mode="Enforce")
        nzb_id = self.add(self.good(), run=False)
        self.assertTrue(self.is_held(nzb_id))
        sim.set_option("Mode", "Screen")
        sim.advance(SWEEP_EVERY)
        sim.sweep()
        self.assertEqual(sim.status(nzb_id), "QUEUED")
        self.assertFalse(self.is_held(nzb_id))
