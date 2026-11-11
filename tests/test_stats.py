"""Tests for completer.stats: confidence ranges, repair capacity and the verdict arithmetic."""

import math
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Completer"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from completer import stats  # noqa: E402
from completer.stats import CLEAN_SAMPLE, Tally, Z, capacity, judge, wilson  # noqa: E402


def tally(listed, checked, missing=0, unknown=0, gaps=0, fill=0):
    result = Tally(listed, gaps)
    result.checked, result.missing, result.unknown, result.fill = checked, missing, unknown, fill
    return result


def textbook_wilson(hits, sample, z):
    """The Wilson score interval as printed in the textbooks, for sampling from an unlimited supply."""
    share = hits / float(sample)
    centre = share + z * z / (2.0 * sample)
    spread = z * math.sqrt(share * (1.0 - share) / sample + z * z / (4.0 * sample * sample))
    scale = 1.0 + z * z / sample
    return max(0.0, (centre - spread) / scale), min(1.0, (centre + spread) / scale)


class WilsonTest(unittest.TestCase):
    def test_confidence_is_ninety_nine_percent(self):
        self.assertAlmostEqual(Z, 2.576, places=3)

    def test_nothing_sampled_nothing_known(self):
        self.assertEqual(wilson(0, 0), (0.0, 1.0))
        self.assertEqual(wilson(0, 0, 500), (0.0, 1.0))
        self.assertEqual(wilson(3, -1), (0.0, 1.0))

    def test_published_values_for_ten_articles(self):
        # The well-known 95 percent Wilson intervals for n = 10.
        expected = {0: (0.0, 0.2775), 1: (0.0179, 0.4042), 5: (0.2366, 0.7634), 10: (0.7225, 1.0)}
        for hits, (low, high) in expected.items():
            got_low, got_high = wilson(hits, 10, z=1.96)
            self.assertAlmostEqual(got_low, low, places=4, msg=hits)
            self.assertAlmostEqual(got_high, high, places=4, msg=hits)

    def test_default_values_follow_the_textbook_formula(self):
        for sample in (1, 2, 10, 50, 120, 400, 1200):
            for hits in sorted({0, 1, sample // 10, sample // 2, sample - 1, sample}):
                low, high = wilson(hits, sample)
                want_low, want_high = textbook_wilson(hits, sample, Z)
                self.assertAlmostEqual(low, want_low, places=12, msg=(hits, sample))
                self.assertAlmostEqual(high, want_high, places=12, msg=(hits, sample))

    def test_the_range_contains_the_share_and_stays_within_zero_and_one(self):
        for sample in range(1, 61):
            for hits in range(sample + 1):
                low, high = wilson(hits, sample)
                self.assertGreaterEqual(low, 0.0)
                self.assertLessEqual(high, 1.0)
                self.assertLessEqual(low, hits / float(sample) + 1e-12)
                self.assertGreaterEqual(high, hits / float(sample) - 1e-12)

    def test_the_range_grows_with_the_hits(self):
        for sample in (10, 60, 400):
            previous = wilson(0, sample)
            for hits in range(1, sample + 1):
                current = wilson(hits, sample)
                self.assertGreater(current[0], previous[0])
                self.assertGreater(current[1], previous[1])
                previous = current

    def test_the_range_is_symmetric(self):
        for sample, hits in ((10, 3), (120, 17), (400, 399)):
            low, high = wilson(hits, sample)
            other_low, other_high = wilson(sample - hits, sample)
            self.assertAlmostEqual(low, 1.0 - other_high, places=12)
            self.assertAlmostEqual(high, 1.0 - other_low, places=12)

    def test_a_larger_sample_narrows_the_range(self):
        widths = []
        for sample in (20, 50, 200, 1000, 5000):
            low, high = wilson(sample // 10, sample)
            widths.append(high - low)
        self.assertEqual(widths, sorted(widths, reverse=True))
        self.assertLess(widths[-1], 0.05)

    def test_everything_looked_at_gives_the_exact_share(self):
        self.assertEqual(wilson(3, 10, 10), (0.3, 0.3))
        self.assertEqual(wilson(0, 10, 10), (0.0, 0.0))
        self.assertEqual(wilson(10, 10, 10), (1.0, 1.0))
        self.assertEqual(wilson(3, 10, 5), (0.3, 0.3))  # more asked than there are: still exact
        self.assertEqual(wilson(1, 1, 1), (1.0, 1.0))

    def test_a_small_population_shrinks_the_range(self):
        infinite = wilson(5, 50)
        widths = [infinite[1] - infinite[0]]
        for population in (1000, 100, 60, 51):
            low, high = wilson(5, 50, population)
            widths.append(high - low)
            self.assertLessEqual(low, 0.1 + 1e-12)
            self.assertGreaterEqual(high, 0.1 - 1e-12)
        self.assertEqual(widths, sorted(widths, reverse=True))
        self.assertLess(widths[-1], 0.04)

    def test_finite_population_correction_scales_the_confidence_factor(self):
        for sample, population in ((50, 100), (120, 3000), (400, 1200)):
            scaled = Z * math.sqrt((population - sample) / float(population - 1))
            low, high = wilson(7, sample, population)
            want_low, want_high = textbook_wilson(7, sample, scaled)
            self.assertAlmostEqual(low, want_low, places=12)
            self.assertAlmostEqual(high, want_high, places=12)

    def test_a_population_that_is_unknown_means_unlimited(self):
        self.assertEqual(wilson(5, 50, None), wilson(5, 50))


class CapacityTest(unittest.TestCase):
    def test_with_all_repair_data_the_allowance_is_the_capacity(self):
        for allowed in (0.0, 0.01, 0.15, 0.5, 1.0):
            self.assertEqual(capacity(allowed, 0.0), allowed)

    def test_without_any_repair_data_nothing_can_be_repaired(self):
        for allowed in (0.01, 0.15, 1.0):
            self.assertEqual(capacity(allowed, 1.0), 0.0)

    def test_known_values(self):
        self.assertAlmostEqual(capacity(0.1, 0.5), 0.05 / 1.05)
        self.assertAlmostEqual(capacity(1.0, 0.5), 1.0 / 3.0)
        self.assertEqual(capacity(0.0, 0.5), 0.0)

    def test_it_falls_when_repair_data_is_lost_and_rises_with_the_allowance(self):
        for allowed in (0.05, 0.15, 0.6):
            values = [capacity(allowed, step / 20.0) for step in range(21)]
            self.assertEqual(values, sorted(values, reverse=True))
        for lost in (0.0, 0.2, 0.7):
            values = [capacity(step / 20.0, lost) for step in range(21)]
            self.assertEqual(values, sorted(values))

    def test_it_stays_between_zero_and_the_allowance(self):
        for allowed in (0.0, 0.07, 0.4, 1.0):
            for lost in (0.0, 0.01, 0.3, 0.99, 1.0):
                self.assertTrue(0.0 <= capacity(allowed, lost) <= allowed)

    def test_the_lost_share_is_clamped(self):
        self.assertEqual(capacity(0.2, -0.5), capacity(0.2, 0.0))
        self.assertEqual(capacity(0.2, 7.0), capacity(0.2, 1.0))

    def test_it_is_the_point_where_nzbget_stops_calling_a_download_repairable(self):
        # NZBGet: health = (content - failed content) / content, critical health =
        # (total - 2 * good repair data) / (total - good repair data); it is healthy while health >= critical.
        def repairable(content, repair, content_failed, repair_failed):
            total = content + repair
            good = repair - repair_failed
            health = (content - content_failed) / float(content)
            critical = (total - 2.0 * good) / (total - good)
            return health >= critical

        content = 1000000.0
        for repair in (10000.0, 50000.0, 150000.0, 400000.0, 1000000.0):
            allowed = repair / content
            for lost in (0.0, 0.1, 0.3, 0.5, 0.9):
                limit = capacity(allowed, lost)
                self.assertTrue(repairable(content, repair, content * limit * (1 - 1e-9), repair * lost),
                                (repair, lost))
                if limit < 0.99:
                    self.assertFalse(repairable(content, repair, content * (limit * (1 + 1e-6) + 1e-9),
                                                repair * lost), (repair, lost))


class TallyTest(unittest.TestCase):
    def test_a_new_tally_has_counted_nothing(self):
        result = Tally(500, 7)
        self.assertEqual((result.listed, result.gaps, result.checked, result.missing, result.unknown, result.fill),
                         (500, 7, 0, 0, 0, 0))
        self.assertEqual(result.total, 507)
        self.assertEqual(Tally(10).gaps, 0)

    def test_losses_are_exact_once_everything_was_asked(self):
        self.assertEqual(tally(100, 100, missing=10).lost(), (0.1, 0.1))
        self.assertEqual(tally(100, 100, missing=10, unknown=5).lost(), (0.1, 0.1))
        self.assertEqual(tally(100, 100, missing=10, unknown=5).lost(pessimistic=True), (0.15, 0.15))

    def test_gaps_are_lost_for_certain(self):
        self.assertEqual(tally(90, 90, gaps=10).lost(), (0.1, 0.1))
        low, high = tally(900, 90, missing=0, gaps=100).lost()
        self.assertAlmostEqual(low, 0.1)
        self.assertGreater(high, 0.1)
        self.assertEqual(tally(90, 90, missing=9, gaps=10).lost(), (0.19, 0.19))

    def test_with_nothing_asked_the_range_is_everything_but_the_gaps_are_certain(self):
        low, high = Tally(100, 5).lost()
        self.assertAlmostEqual(low, 5 / 105.0)
        self.assertEqual(high, 1.0)
        self.assertEqual(Tally(100).lost(), (0.0, 1.0))

    def test_unknown_counts_only_in_the_pessimistic_reading(self):
        certain = tally(3000, 120, unknown=40)
        sure_low, sure_high = certain.lost()
        pess_low, pess_high = certain.lost(pessimistic=True)
        self.assertEqual(sure_low, 0.0)
        self.assertLess(sure_high, 0.06)
        self.assertGreater(pess_low, 0.2)
        self.assertGreater(pess_high, pess_low)

    def test_an_empty_tally_loses_nothing(self):
        empty = Tally(0)
        self.assertEqual(empty.lost(), (0.0, 0.0))
        self.assertEqual(empty.lost(pessimistic=True), (0.0, 0.0))
        self.assertEqual(empty.estimate(), 0.0)
        self.assertEqual(empty.fill_share(), (0.0, 0.0))

    def test_the_estimate_is_the_plain_share_plus_the_gaps(self):
        self.assertAlmostEqual(tally(1000, 100, missing=10).estimate(), 0.1)
        self.assertAlmostEqual(tally(1000, 100, missing=10, unknown=20).estimate(), 0.1)
        self.assertAlmostEqual(tally(1000, 100, missing=10, unknown=20).estimate(pessimistic=True), 0.3)
        self.assertAlmostEqual(tally(900, 90, missing=9, gaps=100).estimate(), 0.19)
        self.assertAlmostEqual(Tally(900, 100).estimate(), 0.1)  # nothing asked: only the gaps are known

    def test_fill_share_is_the_share_found_only_on_fill_servers(self):
        self.assertEqual(tally(100, 100, fill=40).fill_share(), (0.4, 0.4))
        low, high = tally(90, 90, fill=40, gaps=10).fill_share()
        self.assertAlmostEqual(low, 0.4)
        self.assertAlmostEqual(high, 0.4)
        low, high = tally(3000, 120, fill=60).fill_share()
        self.assertLess(low, 0.5)
        self.assertGreater(high, 0.5)


class JudgeTest(unittest.TestCase):
    PAR_OK = tally(10, 10)

    def test_fail_when_even_the_certain_losses_are_too_many(self):
        self.assertEqual(judge(tally(3000, 120, missing=42), self.PAR_OK, 0.1), "fail")

    def test_fail_stays_fail_when_it_is_the_last_round(self):
        self.assertEqual(judge(tally(3000, 120, missing=42), self.PAR_OK, 0.1, final=True), "fail")

    def test_a_clean_sample_is_enough_proof(self):
        self.assertEqual(judge(tally(3000, CLEAN_SAMPLE), tally(10, 0), 0.0033), "pass")
        self.assertEqual(judge(tally(3000, CLEAN_SAMPLE), tally(10, 0), 0.0033), "pass")

    def test_a_smaller_clean_sample_is_not_yet_proof_for_a_tight_allowance(self):
        self.assertEqual(judge(tally(3000, CLEAN_SAMPLE - 1), tally(10, 0), 0.0033), "more")
        self.assertEqual(judge(tally(3000, 120), tally(10, 0), 0.02), "more")
        self.assertEqual(judge(tally(3000, 120), tally(10, 0), 0.02, final=True), "pass")

    def test_a_clean_sample_within_the_allowance_passes_at_once(self):
        # Even the worst reading of 120 clean answers (about 5 %) fits into a 15 % allowance.
        self.assertEqual(judge(tally(3000, 120), tally(0, 0), 0.15), "pass")

    def test_a_clean_post_that_was_asked_about_completely_passes(self):
        self.assertEqual(judge(tally(150, 150), tally(10, 0), 0.0033), "pass")

    def test_everything_repairable_passes(self):
        self.assertEqual(judge(tally(3000, 120, missing=10), self.PAR_OK, 0.5), "pass")
        # all of it gone, but there is as much repair data as content
        self.assertEqual(judge(tally(120, 120, missing=120), tally(0, 0), 1.0), "pass")

    def test_exact_numbers_within_the_allowance_pass(self):
        self.assertEqual(judge(tally(120, 120, missing=10), self.PAR_OK, 0.1), "pass")

    def test_suspect_when_only_the_unknown_make_it_unrepairable(self):
        self.assertEqual(judge(tally(3000, 120, unknown=40), self.PAR_OK, 0.1), "suspect")
        self.assertEqual(judge(tally(3000, 120, unknown=120), self.PAR_OK, 0.1), "suspect")
        self.assertEqual(judge(tally(3000, 120, unknown=40), self.PAR_OK, 0.1, final=True), "suspect")

    def test_unknown_never_counts_toward_a_failure(self):
        for unknown in (1, 10, 40, 100, 120):
            for allowed in (0.0033, 0.1, 0.5):
                for par in (self.PAR_OK, tally(10, 0), tally(10, 10, unknown=10), tally(0, 0)):
                    for final in (False, True):
                        verdict = judge(tally(3000, 120, unknown=unknown), par, allowed, final)
                        self.assertNotEqual(verdict, "fail", (unknown, allowed, final))

    def test_unknown_repair_data_never_makes_a_failure_either(self):
        verdict = judge(tally(1000, 1000, missing=60), tally(20, 20, unknown=20), 0.1)
        self.assertNotEqual(verdict, "fail")

    def test_more_while_the_sample_cannot_decide_yet(self):
        self.assertEqual(judge(tally(3000, 120, missing=10), self.PAR_OK, 0.1), "more")

    def test_the_last_round_gives_the_benefit_of_the_doubt(self):
        self.assertEqual(judge(tally(3000, 120, missing=10), self.PAR_OK, 0.1, final=True), "pass")

    def test_a_sample_that_covers_everything_decides_by_itself(self):
        # Nothing left to ask: not "more", even though it is not the final round.
        self.assertEqual(judge(tally(120, 120, missing=13), self.PAR_OK, 0.1), "fail")
        self.assertEqual(judge(tally(120, 120, missing=12), tally(10, 10, missing=0), 0.2), "pass")

    def test_exactly_as_much_as_can_be_repaired_still_passes(self):
        # NZBGet repairs while health >= critical health, so the limit itself is still fine.
        self.assertEqual(judge(tally(120, 120, missing=12), self.PAR_OK, 0.1), "pass")
        self.assertEqual(judge(tally(1000, 1000, missing=100), tally(20, 20), 0.1), "pass")
        self.assertEqual(judge(tally(1000, 1000, missing=101), tally(20, 20), 0.1), "fail")

    def test_gaps_count_as_lost(self):
        self.assertEqual(judge(tally(100, 100, gaps=30), self.PAR_OK, 0.1), "fail")
        self.assertEqual(judge(tally(100, 100, gaps=5), self.PAR_OK, 0.1), "pass")

    def test_many_gaps_fail_even_when_every_asked_article_exists(self):
        # 400 clean answers prove the sampled articles exist, not that the lost ones can be repaired.
        verdict = judge(tally(3000, 400, gaps=600), self.PAR_OK, 0.1)
        self.assertEqual(verdict, "fail")

    def test_lost_repair_data_lowers_what_can_be_repaired(self):
        content = tally(1000, 1000, missing=60)
        self.assertEqual(judge(content, tally(20, 20), 0.1), "pass")
        self.assertEqual(judge(content, tally(20, 20, missing=10), 0.1), "fail")
        self.assertEqual(judge(content, tally(20, 20, missing=20), 0.1), "fail")

    def test_unknown_repair_data_is_judged_by_the_benefit_of_the_doubt_once_all_was_asked(self):
        self.assertEqual(judge(tally(1000, 1000, missing=60), tally(20, 20, unknown=10), 0.1), "pass")

    def test_nothing_known_about_the_repair_data_counts_against_a_shortcut(self):
        content = tally(100, 100, missing=5)
        self.assertEqual(judge(content, tally(10, 0), 0.1), "more")
        self.assertEqual(judge(content, tally(10, 10), 0.1), "pass")
        self.assertEqual(judge(content, tally(10, 0), 0.1, final=True), "pass")
        self.assertEqual(judge(content, tally(0, 0), 0.1), "pass")  # a post without repair files has none to ask about

    def test_the_verdict_is_always_one_of_four_words(self):
        generator = random.Random(20240229)
        for _ in range(3000):
            listed = generator.randint(1, 3000)
            checked = generator.randint(1, listed)
            missing = generator.randint(0, checked)
            unknown = generator.randint(0, checked - missing)
            par_listed = generator.choice((0, 5, 40))
            par_checked = generator.randint(0, par_listed)
            par_missing = generator.randint(0, par_checked)
            verdict = judge(tally(listed, checked, missing, unknown, gaps=generator.choice((0, 0, 3))),
                            tally(par_listed, par_checked, par_missing),
                            generator.choice((0.0033, 0.05, 0.15, 0.5, 1.0)), final=generator.random() < 0.3)
            self.assertIn(verdict, ("pass", "fail", "suspect", "more"))

    def test_a_download_without_missing_articles_or_gaps_never_fails(self):
        generator = random.Random(7)
        for _ in range(3000):
            listed = generator.randint(1, 3000)
            checked = generator.randint(1, listed)
            unknown = generator.randint(0, checked)
            par_listed = generator.choice((0, 5, 40))
            par_checked = generator.randint(0, par_listed)
            par = tally(par_listed, par_checked, unknown=generator.randint(0, par_checked))
            verdict = judge(tally(listed, checked, 0, unknown), par, generator.choice((0.0033, 0.05, 0.15, 1.0)),
                            final=generator.random() < 0.5)
            self.assertNotEqual(verdict, "fail")

    def test_the_last_round_is_never_undecided(self):
        generator = random.Random(11)
        for _ in range(3000):
            listed = generator.randint(1, 3000)
            checked = generator.randint(1, listed)
            missing = generator.randint(0, checked)
            verdict = judge(tally(listed, checked, missing, generator.randint(0, checked - missing)),
                            tally(40, generator.randint(0, 40)), generator.choice((0.0033, 0.1, 0.5)), final=True)
            self.assertNotEqual(verdict, "more")

    def test_more_missing_articles_never_turn_a_failure_into_a_pass(self):
        # Hold everything else equal: once a sample fails, a worse sample must not pass.
        for allowed in (0.0033, 0.05, 0.15, 0.5):
            failed = False
            for missing in range(0, 121):
                verdict = judge(tally(3000, 120, missing), self.PAR_OK, allowed, final=True)
                if verdict == "fail":
                    failed = True
                elif failed:
                    self.fail("%d missing passed after fewer had failed (allowed %s)" % (missing, allowed))

    def test_module_constants(self):
        self.assertEqual(CLEAN_SAMPLE, 400)
        self.assertIs(stats.Z, Z)


if __name__ == "__main__":
    unittest.main()
