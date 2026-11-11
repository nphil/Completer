"""Tests for completer.decide: the whole verdict table, the delays, and the rule that only 'fail' can fail."""

import itertools
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Completer"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from completer import decide as d  # noqa: E402
from completer import settings as cfg  # noqa: E402

VERDICTS = ("pass", "fail", "gone", "suspect", "error", "more", "", "PASS", "Fail", None, "anything-unknown")
BOOLS = (False, True)


class TableTests(unittest.TestCase):
    def check(self, got, action, state="", delay=0):
        self.assertEqual((got.action, got.state, got.delay), (action, state, delay))

    def test_pass(self):
        for fresh, last, checks in itertools.product(BOOLS, BOOLS, range(1, 10)):
            self.check(d.decide("pass", True, fresh, last, checks), d.RELEASE, cfg.PASSED)
            self.check(d.decide("pass", False, fresh, last, checks), d.NOTHING)

    def test_fail_and_gone_fresh_not_last_held_waits(self):
        for verdict in ("fail", "gone"):
            self.check(d.decide(verdict, True, True, False, 1), d.WAIT, cfg.WAITING, 600)
            self.check(d.decide(verdict, True, True, False, 2), d.WAIT, cfg.WAITING, 1200)
            self.check(d.decide(verdict, True, True, False, 3), d.WAIT, cfg.WAITING, 2400)
            self.check(d.decide(verdict, True, True, False, 4), d.WAIT, cfg.WAITING, 3600)

    def test_fail_and_gone_fresh_not_last_not_held_does_nothing(self):
        for verdict in ("fail", "gone"):
            self.check(d.decide(verdict, False, True, False, 1), d.NOTHING)

    def test_fail_final_fails_held_or_not(self):
        for held in BOOLS:
            self.check(d.decide("fail", held, False, False, 1), d.FAIL, cfg.FAILED)  # no longer fresh
            self.check(d.decide("fail", held, True, True, 8), d.FAIL, cfg.FAILED)    # fresh but out of time
            self.check(d.decide("fail", held, False, True, 8), d.FAIL, cfg.FAILED)

    def test_gone_final_releases_when_held(self):
        for fresh, last in ((False, False), (True, True), (False, True)):
            self.check(d.decide("gone", True, fresh, last, 3), d.RELEASE, cfg.RELEASED)
            self.check(d.decide("gone", False, fresh, last, 3), d.NOTHING)

    def test_suspect_held_waits_then_releases(self):
        self.check(d.decide("suspect", True, False, False, 1), d.WAIT, cfg.WAITING, 300)
        self.check(d.decide("suspect", True, False, False, 2), d.WAIT, cfg.WAITING, 900)
        self.check(d.decide("suspect", True, False, False, 3), d.RELEASE, cfg.RELEASED)
        self.check(d.decide("suspect", True, False, False, 9), d.RELEASE, cfg.RELEASED)

    def test_suspect_ignores_freshness(self):
        self.check(d.decide("suspect", True, True, False, 1), d.WAIT, cfg.WAITING, 300)
        self.check(d.decide("suspect", True, True, False, 2), d.WAIT, cfg.WAITING, 900)

    def test_suspect_last_releases(self):
        self.check(d.decide("suspect", True, False, True, 1), d.RELEASE, cfg.RELEASED)
        self.check(d.decide("suspect", True, True, True, 2), d.RELEASE, cfg.RELEASED)

    def test_suspect_not_held_does_nothing(self):
        for fresh, last, checks in itertools.product(BOOLS, BOOLS, range(1, 10)):
            self.check(d.decide("suspect", False, fresh, last, checks), d.NOTHING)

    def test_error_and_unknown_release_when_held(self):
        for verdict in ("error", "more", "", None, "garbage", "PASS"):
            for fresh, last, checks in itertools.product(BOOLS, BOOLS, range(1, 10)):
                self.check(d.decide(verdict, True, fresh, last, checks), d.RELEASE, cfg.RELEASED)
                self.check(d.decide(verdict, False, fresh, last, checks), d.NOTHING)

    def test_constants(self):
        self.assertEqual(d.MAX_CHECKS, 8)
        self.assertEqual(d.FRESH_DELAYS, (600, 1200, 2400, 3600))
        self.assertEqual(d.SUSPECT_DELAYS, (300, 900))
        self.assertEqual(d.SUSPECT_CHECKS, 3)
        self.assertEqual(d.HOLD_EXTRA, 3600)

    def test_decision_defaults(self):
        decision = d.Decision(d.NOTHING)
        self.assertEqual((decision.action, decision.state, decision.delay), (d.NOTHING, "", 0))


class DelayTests(unittest.TestCase):
    def test_fresh_delays_and_clamping(self):
        got = [d._delay(d.FRESH_DELAYS, n) for n in (-5, 0, 1, 2, 3, 4, 5, 8, 100)]
        self.assertEqual(got, [600, 600, 600, 1200, 2400, 3600, 3600, 3600, 3600])

    def test_suspect_delays_and_clamping(self):
        got = [d._delay(d.SUSPECT_DELAYS, n) for n in (-1, 0, 1, 2, 3, 50)]
        self.assertEqual(got, [300, 300, 300, 900, 900, 900])

    def test_wait_delay_through_decide(self):
        self.assertEqual(d.decide("fail", True, True, False, 0).delay, 600)
        self.assertEqual(d.decide("fail", True, True, False, 7).delay, 3600)
        self.assertEqual(d.decide("suspect", True, False, False, 0).delay, 300)


class IsLastTests(unittest.TestCase):
    FRESH = 7200

    def test_eight_checks_is_last(self):
        self.assertTrue(d.is_last(8, 0, 1000, self.FRESH))
        self.assertTrue(d.is_last(9, 0, 1000, self.FRESH))
        self.assertTrue(d.is_last(100, 500, 600, self.FRESH))
        self.assertFalse(d.is_last(7, 0, 1000, self.FRESH))
        self.assertFalse(d.is_last(0, 0, 1000, self.FRESH))

    def test_held_longer_than_fresh_window_plus_an_hour(self):
        limit = self.FRESH + 3600
        since = 1000
        self.assertFalse(d.is_last(1, since, since + limit - 1, self.FRESH))
        self.assertFalse(d.is_last(1, since, since + limit, self.FRESH))
        self.assertTrue(d.is_last(1, since, since + limit + 1, self.FRESH))

    def test_hold_limit_follows_the_fresh_window(self):
        self.assertTrue(d.is_last(1, 100, 100 + 3600 + 1, 0))
        self.assertFalse(d.is_last(1, 100, 100 + 3600, 0))
        self.assertFalse(d.is_last(1, 100, 100 + 3601, 72 * 3600))

    def test_since_zero_never_counts_time(self):
        self.assertFalse(d.is_last(1, 0, 10 ** 10, self.FRESH))
        self.assertFalse(d.is_last(7, 0, 10 ** 10, 0))

    def test_negative_since_never_counts_time(self):
        self.assertFalse(d.is_last(1, -5, 10 ** 10, self.FRESH))

    def test_clock_going_backwards_is_not_last(self):
        self.assertFalse(d.is_last(1, 5000, 1000, self.FRESH))


class PropertyTests(unittest.TestCase):
    """Over every combination that can reach decide(): the safety rules of the design."""

    def combinations(self):
        return itertools.product(VERDICTS, BOOLS, BOOLS, BOOLS, range(1, 13))

    def test_exhaustive_size(self):
        self.assertEqual(len(list(self.combinations())), len(VERDICTS) * 2 * 2 * 2 * 12)

    def test_fail_only_for_fail_verdict_and_never_while_fresh_and_not_last(self):
        failed = 0
        for verdict, held, fresh, last, checks in self.combinations():
            got = d.decide(verdict, held, fresh, last, checks)
            if got.action == d.FAIL:
                failed += 1
                self.assertEqual(verdict, "fail", (verdict, held, fresh, last, checks))
                self.assertFalse(fresh and not last, (verdict, held, fresh, last, checks))
                self.assertEqual(got.state, cfg.FAILED)
        self.assertGreater(failed, 0)

    def test_fail_verdict_fails_exactly_when_not_fresh_or_last(self):
        for held, fresh, last, checks in itertools.product(BOOLS, BOOLS, BOOLS, range(1, 13)):
            got = d.decide("fail", held, fresh, last, checks)
            self.assertEqual(got.action == d.FAIL, (not fresh) or last, (held, fresh, last, checks))

    def test_uncertain_verdicts_never_fail(self):
        for verdict in ("suspect", "gone", "error", "more", "", None, "anything-unknown", "pass", "PASS", "Fail"):
            for held, fresh, last, checks in itertools.product(BOOLS, BOOLS, BOOLS, range(1, 13)):
                got = d.decide(verdict, held, fresh, last, checks)
                self.assertNotEqual(got.action, d.FAIL, (verdict, held, fresh, last, checks))

    def test_wait_and_release_only_when_held(self):
        for verdict, held, fresh, last, checks in self.combinations():
            got = d.decide(verdict, held, fresh, last, checks)
            if not held:
                self.assertIn(got.action, (d.NOTHING, d.FAIL), (verdict, held, fresh, last, checks))
            if got.action in (d.WAIT, d.RELEASE):
                self.assertTrue(held, (verdict, held, fresh, last, checks))

    def test_wait_has_a_delay_and_waiting_state(self):
        for verdict, held, fresh, last, checks in self.combinations():
            got = d.decide(verdict, held, fresh, last, checks)
            if got.action == d.WAIT:
                self.assertEqual(got.state, cfg.WAITING)
                self.assertIn(got.delay, d.FRESH_DELAYS + d.SUSPECT_DELAYS)
                self.assertFalse(last, (verdict, held, fresh, last, checks))
            else:
                self.assertEqual(got.delay, 0)

    def test_nothing_left_waiting_forever(self):
        # once the last check has come (last=True) no verdict leads to another wait
        for verdict, held, fresh, checks in itertools.product(VERDICTS, BOOLS, BOOLS, range(1, 13)):
            self.assertNotEqual(d.decide(verdict, held, fresh, True, checks).action, d.WAIT)

    def test_suspect_stops_waiting_after_three_checks(self):
        for fresh, last, checks in itertools.product(BOOLS, BOOLS, range(3, 13)):
            self.assertNotEqual(d.decide("suspect", True, fresh, last, checks).action, d.WAIT)

    def test_release_state_is_passed_only_for_pass(self):
        for verdict, held, fresh, last, checks in self.combinations():
            got = d.decide(verdict, held, fresh, last, checks)
            if got.action == d.RELEASE:
                self.assertEqual(got.state, cfg.PASSED if verdict == "pass" else cfg.RELEASED)

    def test_every_action_has_a_known_state(self):
        for verdict, held, fresh, last, checks in self.combinations():
            got = d.decide(verdict, held, fresh, last, checks)
            self.assertIn(got.action, (d.RELEASE, d.WAIT, d.FAIL, d.NOTHING))
            if got.action == d.NOTHING:
                self.assertEqual(got.state, "")
            else:
                self.assertIn(got.state, (cfg.PASSED, cfg.WAITING, cfg.FAILED, cfg.RELEASED))


if __name__ == "__main__":
    unittest.main()
