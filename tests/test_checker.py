"""Tests for completer/checker.py: verdicts, levels, groups, retention, unknown versus missing, and fail-open."""

import collections
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Completer"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import nzbtools  # noqa: E402
from completer import checker, log, nntp, nzbfile, settings  # noqa: E402
from fakenntp import PASSWORD, FakeNews  # noqa: E402

NOW = 1700000000


def wait_for(test, condition, seconds=5.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return
        time.sleep(0.02)
    test.fail("condition not reached in time")


class CheckerCase(unittest.TestCase):
    def setUp(self):
        log.forget_secrets()
        self.addCleanup(log.forget_secrets)
        patcher = mock.patch.object(checker, "CONFIRM_PAUSE", 0.05)  # the pause is only there for real servers
        patcher.start()
        self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def news(self, have=(), **switches):
        news = FakeNews(have=have, **switches).start()
        self.addCleanup(news.stop)
        return news

    @staticmethod
    def post(tag="a", **kwargs):
        kwargs.setdefault("now", NOW)
        return nzbtools.make_post(tag, **kwargs)

    def small(self, tag="s", **kwargs):
        """150 content articles and 10 repair articles: checked completely."""
        return self.post(tag, segments=50, **kwargs)

    def parse(self, post):
        return nzbfile.parse(post.write(os.path.join(self.dir, "%s.nzb" % post.tag)))

    @staticmethod
    def load(*specs):
        env = {}
        for number, (news, kwargs) in enumerate(specs, 1):
            env.update(news.env_entry(number, **kwargs))
        return settings.load_servers(env)

    def check(self, post, *specs, **options):
        seconds = options.pop("seconds", 30)
        env = {"NZBPO_" + key: str(value) for key, value in options.items()}
        servers = self.load(*specs)
        started = time.monotonic()
        outcome = checker.check(self.parse(post), servers, settings.Settings(env), nntp.Deadline(seconds), NOW)
        self.elapsed = time.monotonic() - started
        return outcome

    def assertNoSecrets(self, outcome, *more):
        texts = [outcome.describe(), outcome.reason] + list(outcome.servers) + list(outcome.unreachable) + \
            list(outcome.left_out)
        for text in texts:
            for secret in (PASSWORD,) + more:
                self.assertNotIn(secret, text)


class PassTests(CheckerCase):
    def test_complete_big_post_passes_after_a_clean_sample(self):
        post = self.post()
        news = self.news(have=post.all_ids)
        outcome = self.check(post, (news, {}))
        self.assertEqual(outcome.verdict, "pass")
        self.assertEqual(outcome.missing, 0)
        self.assertEqual(outcome.unknown, 0)
        self.assertEqual(outcome.population, len(post.data_ids))
        self.assertLess(news.stats_total, len(post.all_ids) // 2)
        self.assertEqual(news.stats_total, outcome.checked)
        self.assertLessEqual(outcome.checked, 400)
        self.assertEqual(len(set(news.stat_log)), news.stats_total)
        self.assertTrue(set(news.stat_log) <= set(post.data_ids))  # repair data is only asked about when needed
        self.assertEqual(outcome.describe(), "%d of %d articles checked, all there" % (outcome.checked, outcome.population))

    def test_small_post_is_checked_completely(self):
        post = self.small()
        self.assertLessEqual(len(post.data_ids), checker.SMALL)
        news = self.news(have=post.all_ids)
        outcome = self.check(post, (news, {}))
        self.assertEqual(outcome.verdict, "pass")
        self.assertEqual(outcome.checked, outcome.population)
        self.assertEqual(outcome.checked, len(post.data_ids))
        self.assertEqual(sorted(news.stat_log), sorted(post.data_ids))

    def test_outcome_has_one_line_per_server(self):
        post = self.small()
        news = self.news(have=post.all_ids)
        outcome = self.check(post, (news, {"name": "alpha"}))
        self.assertEqual(len(outcome.servers), 1)
        self.assertIn("alpha", outcome.servers[0])
        self.assertIn("150 questions", outcome.servers[0])
        self.assertGreater(outcome.seconds, 0.0)

    def test_few_gaps_within_the_repair_allowance_pass(self):
        post = self.post("g", skip=(10, 11, 12))
        news = self.news(have=post.all_ids)
        outcome = self.check(post, (news, {}))
        self.assertEqual(outcome.verdict, "pass")
        self.assertAlmostEqual(outcome.lost, 3.0 / 300, places=4)
        self.assertLess(outcome.lost, outcome.limit)

    def test_many_gaps_fail_like_missing_articles(self):
        post = self.post("g", skip=tuple(range(2, 61)))
        news = self.news(have=post.all_ids)
        outcome = self.check(post, (news, {}))
        self.assertEqual(outcome.verdict, "fail")
        self.assertEqual(outcome.cause, "missing")
        self.assertGreater(outcome.lost, outcome.limit)
        self.assertGreater(outcome.found, 0)
        self.assertEqual(outcome.missing, 0)  # every listed article is there; the gaps alone are too many


class FailTests(CheckerCase):
    def broken_post(self):
        post = self.post("f")
        have = list(post.par_ids) + post.data_ids[:100]  # two thirds of the content is gone, the repair data is whole
        return post, have

    def test_missing_on_every_server_fails(self):
        post, have = self.broken_post()
        news = self.news(have=have)
        outcome = self.check(post, (news, {}))
        self.assertEqual(outcome.verdict, "fail")
        self.assertEqual(outcome.cause, "missing")
        self.assertGreater(outcome.lost, outcome.limit)
        self.assertGreater(outcome.found, 0)
        self.assertGreater(outcome.missing, 0)
        self.assertEqual(outcome.unknown, 0)
        self.assertIn("missing on every server", outcome.describe())
        self.assertNoSecrets(outcome)

    def test_missing_on_two_servers_fails(self):
        post, have = self.broken_post()
        first = self.news(have=have)
        second = self.news(have=have[::2])
        outcome = self.check(post, (first, {}), (second, {"level": 1}))
        self.assertEqual(outcome.verdict, "fail")
        self.assertGreater(outcome.found, 0)

    def test_article_found_only_on_a_later_level_counts_as_found(self):
        post = self.small()
        first = self.news(have=[])
        second = self.news(have=post.all_ids)
        outcome = self.check(post, (first, {}), (second, {"level": 1}))
        self.assertEqual(outcome.verdict, "pass")
        self.assertEqual(outcome.missing, 0)
        self.assertEqual(outcome.found, len(post.data_ids))
        self.assertAlmostEqual(outcome.fill, 1.0)

    def test_post_that_is_nowhere_is_gone_not_failed(self):
        post = self.post("n")
        for name, switches in (("deny_all", {"have": post.all_ids, "deny_all": True}), ("empty", {"have": []})):
            with self.subTest(name):
                news = self.news(**switches)
                outcome = self.check(post, (news, {}))
                self.assertEqual(outcome.verdict, "gone")
                self.assertEqual(outcome.found, 0)
                self.assertTrue(outcome.describe().startswith("none of the"))

    def test_gone_across_two_servers(self):
        post = self.small()
        first = self.news(deny_all=True, have=post.all_ids)
        second = self.news()
        outcome = self.check(post, (first, {}), (second, {"level": 1}))
        self.assertEqual(outcome.verdict, "gone")

    def test_one_article_somewhere_is_proof_of_life(self):
        post = self.small()
        news = self.news(have=[post.par_ids[0]])
        outcome = self.check(post, (news, {}))
        self.assertEqual(outcome.verdict, "fail")
        self.assertEqual(outcome.found, 1)


class UnknownTests(CheckerCase):
    def setUp(self):
        super().setUp()
        self.post_ = self.post("u")
        self.have = list(self.post_.par_ids) + self.post_.data_ids[:100]

    def test_unreachable_server_keeps_articles_unknown_so_no_fail(self):
        cases = (
            ("login rejected", {"reject_login": True}, 20),
            ("hangs up", {"hangup": True}, 20),
            ("dropped before the first answer", {"drop_after_answers": 0, "drop_limit": 0}, 20),
            ("stalls", {"stall": True}, 3),
        )
        for name, switches, seconds in cases:
            with self.subTest(name):
                good = self.news(have=self.have)
                bad = self.news(have=self.post_.all_ids, **switches)
                outcome = self.check(self.post_, (good, {}), (bad, {}), seconds=seconds)
                self.assertNotEqual(outcome.verdict, "fail")
                self.assertIn(outcome.verdict, ("suspect", "pass"))
                self.assertEqual(outcome.missing, 0)
                self.assertGreater(outcome.unknown, 0)
                self.assertGreater(outcome.found, 0)
                if outcome.verdict == "suspect":
                    self.assertIn("was not found", outcome.describe())

    def test_unreachable_servers_are_explained(self):
        good = self.news(have=self.have)
        bad = self.news(reject_login=True, echo_password=True)
        outcome = self.check(self.post_, (good, {}), (bad, {"name": "second"}))
        self.assertEqual(outcome.verdict, "suspect")
        self.assertEqual(len(outcome.unreachable), 1)
        self.assertIn("second", outcome.unreachable[0])
        self.assertIn("rejected the login", outcome.unreachable[0])
        self.assertIn("rejected the login", outcome.describe())
        self.assertNoSecrets(outcome)

    def test_server_is_not_asked_when_the_first_has_everything(self):
        post = self.small()
        good = self.news(have=post.all_ids)
        bad = self.news(reject_login=True)
        outcome = self.check(post, (good, {}), (bad, {}))
        self.assertEqual(outcome.verdict, "pass")
        self.assertEqual(outcome.unknown, 0)
        self.assertEqual(outcome.unreachable, [])  # it was never needed, so never tried
        self.assertEqual(bad.connections_total, 0)

    def test_optional_server_that_rejects_login_is_left_out(self):
        good = self.news(have=self.have)
        bad = self.news(reject_login=True)
        outcome = self.check(self.post_, (good, {}), (bad, {"optional": True, "name": "spare"}))
        self.assertEqual(outcome.verdict, "fail")  # NZBGet skips an optional server it cannot log in to
        self.assertEqual(outcome.left_out, ["server 2 (spare)"])
        self.assertEqual(outcome.unreachable, [])
        self.assertEqual(outcome.unknown, 0)

    def test_non_optional_server_that_rejects_login_keeps_articles_unknown(self):
        good = self.news(have=self.have)
        bad = self.news(reject_login=True)
        outcome = self.check(self.post_, (good, {}), (bad, {"optional": False}))
        self.assertNotEqual(outcome.verdict, "fail")
        self.assertEqual(outcome.left_out, [])
        self.assertGreater(outcome.unknown, 0)

    def test_optional_server_that_is_busy_is_not_left_out(self):
        good = self.news(have=self.have)
        bad = self.news(max_connections=1)
        blocker = nntp.Connection(self.load((bad, {}))[0])
        blocker.open(5)
        self.addCleanup(blocker.close, False)
        outcome = self.check(self.post_, (good, {}), (bad, {"optional": True}))
        self.assertNotEqual(outcome.verdict, "fail")
        self.assertEqual(outcome.left_out, [])

    def test_flapping_articles_are_rescued_by_the_confirmation_pass(self):
        post = self.small()
        flapping = set(post.data_ids[:100])
        news = self.news(have=post.all_ids, flap=flapping)
        outcome = self.check(post, (news, {}))
        self.assertEqual(outcome.verdict, "pass")
        self.assertEqual(outcome.missing, 0)
        counts = collections.Counter(news.stat_log)
        self.assertTrue(all(counts[msgid] == 2 for msgid in flapping))

    def test_no_time_for_confirmation_is_suspect_never_fail(self):
        post = self.post("t")
        have = list(post.par_ids) + post.data_ids[:100]
        news = self.news(have=have)
        outcome = self.check(post, (news, {}), seconds=2)
        self.assertEqual(outcome.verdict, "suspect")
        self.assertIn("was not found", outcome.describe())
        self.assertLess(self.elapsed, 2 + 2.5)

    def test_gone_post_fails_in_time_when_not_here_answers_are_slow(self):
        # Real servers take a second or more per "not here". A mostly gone post must still end as fail within the
        # time limit: a small first round, then only a sample of the missing articles asked again.
        post = self.post("w")
        have = list(post.par_ids) + post.data_ids[:len(post.data_ids) // 10]
        news = self.news(have=have, slow=0.1)
        outcome = self.check(post, (news, {}), seconds=15)
        self.assertEqual(outcome.verdict, "fail")
        counts = collections.Counter(news.stat_log)
        self.assertLessEqual(sum(1 for n in counts.values() if n == 2), checker.CONFIRM_SAMPLE)
        self.assertLess(self.elapsed, 15)


class ServerSelectionTests(CheckerCase):
    def test_level_one_server_is_asked_only_about_what_level_zero_lacks(self):
        post = self.small()
        lacking = post.data_ids[:5]
        first = self.news(have=[msgid for msgid in post.all_ids if msgid not in lacking])
        second = self.news(have=post.all_ids)
        outcome = self.check(post, (first, {}), (second, {"level": 1}))
        self.assertEqual(outcome.verdict, "pass")
        self.assertEqual(len(first.stat_log), len(post.data_ids))
        self.assertEqual(sorted(second.stat_log), sorted(lacking))

    def test_level_order_follows_the_level_not_the_number(self):
        post = self.small()
        high = self.news(have=post.all_ids)
        low = self.news(have=post.all_ids)
        outcome = self.check(post, (high, {"level": 1}), (low, {"level": 0}))
        self.assertEqual(outcome.verdict, "pass")
        self.assertEqual(len(low.stat_log), len(post.data_ids))
        self.assertEqual(high.stat_log, [])

    def test_servers_of_one_group_count_as_one(self):
        post = self.small()
        first = self.news(have=post.all_ids)
        second = self.news(have=post.all_ids)
        outcome = self.check(post, (first, {"group": 1}), (second, {"group": 1}))
        self.assertEqual(outcome.verdict, "pass")
        self.assertEqual(len(first.stat_log), len(post.data_ids))
        self.assertEqual(second.connections_total, 0)

    def test_second_server_of_a_group_is_asked_when_the_first_is_unreachable(self):
        post = self.small()
        first = self.news(have=post.all_ids, reject_login=True)
        second = self.news(have=post.all_ids)
        outcome = self.check(post, (first, {"group": 1}), (second, {"group": 1}))
        self.assertEqual(outcome.verdict, "pass")
        self.assertEqual(len(second.stat_log), len(post.data_ids))
        self.assertEqual(outcome.unreachable, [])

    def test_group_number_counts_only_within_a_level(self):
        post = self.small()
        first = self.news(have=post.all_ids[:20])
        second = self.news(have=post.all_ids)
        outcome = self.check(post, (first, {"group": 1}), (second, {"group": 1, "level": 1}))
        self.assertEqual(outcome.verdict, "pass")
        self.assertGreater(len(second.stat_log), 0)

    def test_group_that_denies_everything_is_not_asked_twice(self):
        post = self.post("q")
        have = list(post.par_ids) + post.data_ids[:100]
        first = self.news(have=have)
        second = self.news(have=post.all_ids)
        outcome = self.check(post, (first, {"group": 2}), (second, {"group": 2}))
        self.assertEqual(outcome.verdict, "fail")
        self.assertEqual(second.connections_total, 0)

    def test_retention_shorter_than_the_age_means_not_asked(self):
        post = self.small()  # posted two days before NOW
        short = self.news(have=post.all_ids)
        long_ = self.news(have=post.all_ids)
        outcome = self.check(post, (short, {"retention": 1}), (long_, {}))
        self.assertEqual(outcome.verdict, "pass")
        self.assertEqual(short.connections_total, 0)
        self.assertEqual(len(long_.stat_log), len(post.data_ids))

    def test_retention_long_enough_is_asked(self):
        post = self.small()
        news = self.news(have=post.all_ids)
        outcome = self.check(post, (news, {"retention": 3}))
        self.assertEqual(outcome.verdict, "pass")
        self.assertEqual(len(news.stat_log), len(post.data_ids))

    def test_denial_of_a_short_retention_server_is_not_required(self):
        post = self.post("r")
        have = list(post.par_ids) + post.data_ids[:100]
        short = self.news(have=post.all_ids)  # would have everything, but is never asked
        real = self.news(have=have)
        outcome = self.check(post, (short, {"retention": 1}), (real, {}))
        self.assertEqual(outcome.verdict, "fail")
        self.assertEqual(short.stats_total, 0)
        self.assertEqual(outcome.unknown, 0)

    def test_only_short_retention_servers_means_nothing_is_missing_for_certain(self):
        post = self.small()
        news = self.news(have=[])
        outcome = self.check(post, (news, {"retention": 1}))
        self.assertNotEqual(outcome.verdict, "fail")
        self.assertEqual(news.stats_total, 0)

    def test_ignored_servers_are_not_asked(self):
        post = self.small()
        first = self.news(have=post.all_ids)
        second = self.news(have=post.all_ids)
        outcome = self.check(post, (first, {}), (second, {}), IgnoreServers="1")
        self.assertEqual(outcome.verdict, "pass")
        self.assertEqual(first.connections_total, 0)
        self.assertEqual(len(second.stat_log), len(post.data_ids))

    def test_inactive_and_zero_connection_servers_are_not_asked(self):
        post = self.small()
        off = self.news(have=post.all_ids)
        zero = self.news(have=post.all_ids)
        on = self.news(have=post.all_ids)
        outcome = self.check(post, (off, {"active": False}), (zero, {"connections": 0}), (on, {}))
        self.assertEqual(outcome.verdict, "pass")
        self.assertEqual(off.connections_total + zero.connections_total, 0)
        self.assertEqual(len(on.stat_log), len(post.data_ids))

    def test_join_group_servers_get_the_group_of_the_file(self):
        post = self.small()
        news = self.news(have=post.all_ids)
        outcome = self.check(post, (news, {"join_group": True}))
        self.assertEqual(outcome.verdict, "pass")
        self.assertEqual(set(news.groups_joined), {"alt.binaries.test"})


class FillTests(CheckerCase):
    def fill_servers(self, post):
        first = self.news(have=list(post.par_ids) + post.data_ids[::10])
        second = self.news(have=post.all_ids)
        return (first, {}), (second, {"level": 1})

    def test_post_found_mostly_on_fill_servers_passes_by_default(self):
        post = self.small()
        outcome = self.check(post, *self.fill_servers(post))
        self.assertEqual(outcome.verdict, "pass")
        self.assertGreater(outcome.fill, 0.8)

    def test_passes_with_full_fill_allowed(self):
        post = self.small()
        outcome = self.check(post, *self.fill_servers(post), MaxFillPercent=100)
        self.assertEqual(outcome.verdict, "pass")

    def test_fails_with_cause_fill_when_the_limit_is_below_the_share(self):
        post = self.small()
        outcome = self.check(post, *self.fill_servers(post), MaxFillPercent=50)
        self.assertEqual(outcome.verdict, "fail")
        self.assertEqual(outcome.cause, "fill")
        self.assertGreater(outcome.fill, 0.5)
        self.assertAlmostEqual(outcome.fill_limit, 0.5)
        self.assertEqual(outcome.missing, 0)
        self.assertIn("only on fill servers", outcome.describe())
        self.assertIn("50%", outcome.describe())

    def test_limit_above_the_share_passes(self):
        post = self.small()
        outcome = self.check(post, *self.fill_servers(post), MaxFillPercent=99)
        self.assertEqual(outcome.verdict, "pass")

    def test_post_on_the_first_level_has_no_fill(self):
        post = self.small()
        first = self.news(have=post.all_ids)
        second = self.news(have=post.all_ids)
        outcome = self.check(post, (first, {}), (second, {"level": 1}), MaxFillPercent=1)
        self.assertEqual(outcome.verdict, "pass")
        self.assertEqual(outcome.fill, 0.0)


class ErrorTests(CheckerCase):
    def test_no_server_at_all_is_an_error(self):
        outcome = self.check(self.small())
        self.assertEqual(outcome.verdict, "error")
        self.assertIn("no active news server", outcome.describe())

    def test_only_unusable_servers_is_an_error(self):
        post = self.small()
        news = self.news(have=post.all_ids)
        outcome = self.check(post, (news, {"active": False}), (news, {"connections": 0}))
        self.assertEqual(outcome.verdict, "error")
        outcome = self.check(post, (news, {}), IgnoreServers="1")
        self.assertEqual(outcome.verdict, "error")
        self.assertEqual(news.connections_total, 0)

    def test_no_reachable_server_is_an_error_with_reasons_and_no_secrets(self):
        post = self.small()
        first = self.news(have=post.all_ids, reject_login=True, echo_password=True)
        second = self.news(hangup=True)
        outcome = self.check(post, (first, {"name": "one", "password": "other-secret-1"}), (second, {"name": "two"}))
        self.assertEqual(outcome.verdict, "error")
        self.assertIn("no news server could be asked", outcome.describe())
        self.assertEqual(len(outcome.unreachable), 2)
        self.assertIn("rejected the login", outcome.describe())
        self.assertIn("one", outcome.describe())
        self.assertNoSecrets(outcome, "other-secret-1")

    def test_server_that_is_gone_entirely_is_an_error(self):
        post = self.small()
        news = self.news()
        servers = self.load((news, {}))
        news.stop()
        outcome = checker.check(self.parse(post), servers, settings.Settings({}), nntp.Deadline(10), NOW)
        self.assertEqual(outcome.verdict, "error")
        self.assertIn("connection was refused", outcome.describe())

    def test_check_never_raises_and_stays_within_the_deadline(self):
        post = self.small()
        cases = (
            ("hangup", {"hangup": True}, 10),
            ("garbage greeting", {"garbage_greeting": True}, 10),
            ("busy greeting", {"greeting": "400 go away"}, 10),
            ("broken answers", {"broken_code": 502}, 10),
            ("stall", {"stall": True}, 2),
            ("silent after login", {"stall": "stat"}, 2),
        )
        for name, switches, seconds in cases:
            with self.subTest(name):
                news = self.news(have=post.all_ids, **switches)
                outcome = self.check(post, (news, {}), seconds=seconds)
                self.assertIn(outcome.verdict, ("error", "suspect", "pass", "gone"))
                self.assertNotEqual(outcome.verdict, "fail")
                self.assertLess(self.elapsed, seconds + 3.0)
                self.assertNoSecrets(outcome)

    def test_misbehaving_server_beside_a_good_one_changes_nothing(self):
        post = self.small()
        good = self.news(have=post.all_ids)
        for switches in ({"hangup": True}, {"garbage_greeting": True}, {"broken_code": 480}):
            with self.subTest(**{k: str(v) for k, v in switches.items()}):
                bad = self.news(**switches)
                outcome = self.check(post, (good, {}), (bad, {}))
                self.assertEqual(outcome.verdict, "pass")


class SessionsClosedTests(CheckerCase):
    def test_all_connections_are_closed_with_quit(self):
        post = self.small()
        first = self.news(have=post.all_ids)
        second = self.news(have=post.all_ids)
        self.check(post, (first, {}), (second, {"level": 1}))
        for news in (first, second):
            wait_for(self, lambda: news.open_connections == 0)
            self.assertEqual(news.quits, news.logins_total)

    def test_connections_are_closed_after_an_error_too(self):
        post = self.small()
        news = self.news(have=post.all_ids, broken_code=502)
        self.check(post, (news, {}))
        wait_for(self, lambda: news.open_connections == 0)

    def test_every_injected_session_is_closed_exactly_once_per_check(self):
        post = self.small()
        made = []

        def factory(server, connections, deadline, cert_store):
            session = _Scripted(server, post.all_ids)
            session.args = (connections, deadline, cert_store)
            made.append(session)
            return session

        env = {}
        for number in (1, 2, 3):
            env.update(_env(number, level=number % 2))
        servers = settings.load_servers(env)
        outcome = checker.check(self.parse(post), servers, settings.Settings({}), nntp.Deadline(30), NOW, factory)
        self.assertEqual(outcome.verdict, "pass")
        self.assertEqual(len(made), 3)
        self.assertEqual([session.closed for session in made], [1, 1, 1])
        self.assertEqual(made[0].args[0], 2)  # the Connections option reaches the sessions


def _env(number, level=0):
    prefix = "NZBOP_SERVER%d_" % number
    return {prefix + "HOST": "server%d.invalid" % number, prefix + "PORT": "119", prefix + "CONNECTIONS": "4",
            prefix + "LEVEL": str(level), prefix + "NAME": "scripted%d" % number}


class _Scripted(object):
    """A session that answers from a set instead of a socket."""

    def __init__(self, server, have):
        self.server = server
        self.have = set(have)
        self.reachable = True
        self.failure = None
        self.questions = 0
        self.seconds = 0.0
        self.closed = 0
        self.asked = []

    def ask(self, msgids, groups=None):
        self.asked.extend(msgids)
        self.questions += len(msgids)
        return {msgid: msgid in self.have for msgid in msgids}

    def close(self):
        self.closed += 1


class ScriptedSessionTests(CheckerCase):
    def run_scripted(self, post, specs, **options):
        """specs: list of (level, group, have, reachable)."""
        made = []

        def factory(server, connections, deadline, cert_store):
            level, group, have, reachable = specs[server.id - 1]
            session = _Scripted(server, have)
            session.reachable = reachable
            made.append(session)
            return session

        env = {}
        for number, (level, group, _have, _reachable) in enumerate(specs, 1):
            env.update(_env(number, level))
            env["NZBOP_SERVER%d_GROUP" % number] = str(group)
        servers = settings.load_servers(env)
        options = {"NZBPO_" + key: str(value) for key, value in options.items()}
        outcome = checker.check(self.parse(post), servers, settings.Settings(options), nntp.Deadline(30), NOW, factory)
        return outcome, sorted(made, key=lambda session: session.server.id)

    def test_clean_big_post_asks_far_fewer_than_all(self):
        post = self.post("big", data_files=5, segments=400, par_segments=40)
        outcome, made = self.run_scripted(post, [(0, 0, post.all_ids, True)])
        self.assertEqual(outcome.verdict, "pass")
        self.assertEqual(len(post.data_ids), 2000)
        self.assertEqual(made[0].questions, outcome.checked)
        self.assertLessEqual(outcome.checked, 400)

    def test_asking_order_is_spread_over_the_whole_post(self):
        post = self.post("spread", data_files=1, segments=1000, par_segments=10)
        outcome, made = self.run_scripted(post, [(0, 0, post.all_ids, True)])
        asked = [post.data_ids.index(msgid) for msgid in made[0].asked[:100]]
        self.assertGreater(max(asked) - min(asked), 800)

    def test_first_server_is_the_one_with_the_lowest_level_and_number(self):
        post = self.small()
        outcome, made = self.run_scripted(post, [(1, 0, post.all_ids, True), (0, 0, post.all_ids, True)])
        self.assertEqual(outcome.verdict, "pass")
        self.assertEqual(len(made[1].asked), len(post.data_ids))
        self.assertEqual(made[0].asked, [])

    def test_dead_first_server_is_skipped_as_the_one_to_start_with(self):
        post = self.small()
        outcome, made = self.run_scripted(post, [(0, 0, [], False), (0, 0, post.all_ids, True)])
        self.assertEqual(outcome.verdict, "pass")
        self.assertEqual(len(made[1].asked), len(post.data_ids))

    def test_group_members_share_the_work_in_order(self):
        post = self.small()
        outcome, made = self.run_scripted(post, [(0, 4, post.all_ids, True), (0, 4, post.all_ids, True)])
        self.assertEqual(outcome.verdict, "pass")
        self.assertEqual(made[1].asked, [])

    def test_second_server_of_a_group_not_asked_about_what_the_first_denied(self):
        post = self.small()
        outcome, made = self.run_scripted(post, [(0, 4, post.all_ids[:10], True), (0, 4, post.all_ids, True)])
        self.assertEqual(made[1].asked, [])
        self.assertIn(outcome.verdict, ("fail", "suspect", "pass"))


class DescribeTests(unittest.TestCase):
    def outcome(self, verdict, **fields):
        outcome = checker.Outcome(verdict)
        outcome.checked, outcome.population = 120, 900
        for name, value in fields.items():
            setattr(outcome, name, value)
        return outcome

    def test_pass_all_there(self):
        self.assertEqual(self.outcome("pass").describe(), "120 of 900 articles checked, all there")

    def test_pass_with_some_missing(self):
        text = self.outcome("pass", missing=2, lost=0.01, limit=0.05).describe()
        self.assertIn("120 of 900 articles checked", text)
        self.assertIn("1.0% missing", text)
        self.assertIn("5.0% can be repaired", text)
        self.assertNotIn("too close", text)

    def test_pass_borderline_says_so(self):
        text = self.outcome("pass", missing=2, lost=0.06, limit=0.05, borderline=True).describe()
        self.assertIn("too close to call", text)

    def test_fail_missing(self):
        text = self.outcome("fail", lost=0.5, limit=0.05).describe()
        self.assertEqual(text, "50.0% of it is missing on every server and only 5.0% can be repaired (120 of 900 articles checked)")

    def test_fail_fill(self):
        text = self.outcome("fail", cause="fill", fill=0.9, fill_limit=0.5).describe()
        self.assertEqual(text, "90% of it is only on fill servers, more than the 50% allowed (120 of 900 articles checked)")

    def test_suspect_names_the_trouble(self):
        text = self.outcome("suspect", lost=0.4, unreachable=["server 2 could not be asked: it timed out"]).describe()
        self.assertIn("40.0% of it was not found", text)
        self.assertIn("server 2 could not be asked", text)

    def test_suspect_without_a_named_server(self):
        self.assertIn("not every server answered", self.outcome("suspect", lost=0.4).describe())

    def test_gone_leaves_it_to_nzbget(self):
        text = self.outcome("gone").describe()
        self.assertIn("none of the 120 articles checked exists on any server", text)
        self.assertIn("left to NZBGet", text)

    def test_error_is_its_reason(self):
        self.assertEqual(checker.Outcome("error", "no news server could be asked").describe(),
                         "no news server could be asked")

    def test_fresh_outcome_defaults(self):
        outcome = checker.Outcome("pass")
        self.assertEqual((outcome.cause, outcome.found, outcome.fresh, outcome.unreachable, outcome.left_out),
                         ("missing", 0, False, [], []))


class TargetTests(unittest.TestCase):
    def test_small_populations_are_asked_at_once(self):
        self.assertEqual(checker._targets(150, 1200, checker.DATA_STAGES), [150])
        self.assertEqual(checker._targets(checker.SMALL, 1200, checker.DATA_STAGES), [checker.SMALL])

    def test_stages_grow_to_the_limit(self):
        self.assertEqual(checker._targets(5000, 1200, checker.DATA_STAGES), [40, 120, 400, 1200])
        self.assertEqual(checker._targets(5000, 300, checker.DATA_STAGES), [40, 120, 300])
        self.assertEqual(checker._targets(300, 1200, checker.DATA_STAGES), [40, 120, 300])

    def test_nothing_to_ask(self):
        self.assertEqual(checker._targets(0, 1200, checker.DATA_STAGES), [])
        self.assertEqual(checker._targets(10, 0, checker.DATA_STAGES), [])


if __name__ == "__main__":
    unittest.main()
