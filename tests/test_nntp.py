"""Tests for completer/nntp.py: deadlines, encryption levels, one connection, and the per-server session."""

import os
import socket
import ssl
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Completer"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from completer import log, nntp, settings  # noqa: E402
from fakenntp import PASSWORD, FakeNews  # noqa: E402

TIMEOUT = 5.0


def wait_for(test, condition, seconds=5.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return
        time.sleep(0.02)
    test.fail("condition not reached in time")


def ids(count, tag="m"):
    return ["%s%d@test.invalid" % (tag, number) for number in range(count)]


class DeadlineTests(unittest.TestCase):
    def test_left_counts_down_and_expired_follows(self):
        deadline = nntp.Deadline(30)
        self.assertGreater(deadline.left(), 29.0)
        self.assertLessEqual(deadline.left(), 30.0)
        self.assertFalse(deadline.expired())

    def test_zero_and_negative_seconds_are_expired_at_once(self):
        self.assertTrue(nntp.Deadline(0).expired())
        self.assertTrue(nntp.Deadline(-5).expired())
        self.assertLessEqual(nntp.Deadline(-5).left(), 0.0)

    def test_expires_after_its_time(self):
        deadline = nntp.Deadline(0.15)
        self.assertFalse(deadline.expired())
        wait_for(self, deadline.expired, 3.0)
        self.assertLessEqual(deadline.left(), 0.0)

    def test_cut_ends_it_now(self):
        deadline = nntp.Deadline(300)
        deadline.cut()
        self.assertTrue(deadline.expired())

    def test_cut_never_extends(self):
        deadline = nntp.Deadline(0)
        deadline.cut()
        self.assertTrue(deadline.expired())

    def test_timeout_never_beyond_deadline(self):
        deadline = nntp.Deadline(0.5)
        self.assertLessEqual(deadline.timeout(100), 0.5)
        self.assertLessEqual(deadline.timeout(0.2), 0.2)
        self.assertGreater(deadline.timeout(100), 0.0)

    def test_timeout_has_a_floor_when_nothing_is_left(self):
        deadline = nntp.Deadline(0)
        self.assertEqual(deadline.timeout(10), 0.05)
        self.assertEqual(deadline.timeout(0.01), 0.05)

    def test_timeout_uses_the_smaller_of_most_and_left(self):
        deadline = nntp.Deadline(100)
        self.assertEqual(deadline.timeout(3.0), 3.0)


class TlsContextTests(unittest.TestCase):
    def test_strict_checks_certificate_and_name(self):
        context = nntp.tls_context("strict")
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_minimal_checks_certificate_but_not_name(self):
        context = nntp.tls_context("minimal")
        self.assertFalse(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_none_checks_nothing(self):
        context = nntp.tls_context("none")
        self.assertFalse(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_NONE)

    def test_unknown_level_is_as_strict_as_strict(self):
        context = nntp.tls_context("whatever")
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_unreadable_cert_store_is_ignored(self):
        context = nntp.tls_context("strict", os.path.join(os.path.dirname(os.path.abspath(__file__)), "no-such-store.pem"))
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)


class ErrorTextTests(unittest.TestCase):
    KINDS = ("dns", "connect", "timeout", "tls", "certificate", "busy", "auth", "refused", "closed", "protocol",
             "dropped")

    def test_every_kind_has_words_of_its_own(self):
        seen = set()
        for kind in self.KINDS:
            text = nntp.NNTPError(kind).describe()
            self.assertNotEqual(text, kind)
            self.assertIn(" ", text)
            self.assertNotIn("(code", text)
            seen.add(text)
        self.assertEqual(len(seen), len(self.KINDS))

    def test_code_is_appended(self):
        for kind in self.KINDS:
            error = nntp.NNTPError(kind, 481)
            self.assertTrue(error.describe().endswith(" (code 481)"), error.describe())
            self.assertEqual(error.describe(), nntp.NNTPError(kind).describe() + " (code 481)")

    def test_unknown_kind_is_shown_as_it_is(self):
        self.assertEqual(nntp.NNTPError("odd").describe(), "odd")
        self.assertEqual(nntp.NNTPError("odd", 5).describe(), "odd (code 5)")

    def test_fields(self):
        error = nntp.NNTPError("busy", 400)
        self.assertEqual((error.kind, error.code, error.partial), ("busy", 400, {}))
        self.assertEqual(str(error), "busy")


class NewsCase(unittest.TestCase):
    """Starts fake servers and turns them into settings.Server objects."""

    def setUp(self):
        log.forget_secrets()
        self.addCleanup(log.forget_secrets)

    def news(self, have=(), **switches):
        news = FakeNews(have=have, **switches).start()
        self.addCleanup(news.stop)
        return news

    def server(self, news, **kwargs):
        return settings.load_servers(news.env_entry(1, **kwargs))[0]

    def connection(self, news, **kwargs):
        connection = nntp.Connection(self.server(news, **kwargs))
        connection.open(TIMEOUT)
        self.addCleanup(connection.close, False)
        return connection

    def session(self, news, connections=2, seconds=30, cap=10, **kwargs):
        """A session; ``cap`` is the connection limit configured for the server itself."""
        session = nntp.ServerSession(self.server(news, connections=cap, **kwargs), connections, nntp.Deadline(seconds))
        self.addCleanup(session.close)
        return session


class OpenTests(NewsCase):
    def test_login_with_account(self):
        news = self.news()
        self.connection(news)
        self.assertEqual(news.logins_total, 1)
        self.assertEqual(news.passwords_seen, [PASSWORD])

    def test_server_without_login_needs_no_account(self):
        news = self.news(username=None)
        self.connection(news, username="")
        self.assertEqual(news.connections_total, 1)
        self.assertEqual(news.passwords_seen, [])
        self.assertEqual(news.commands, [])

    def test_wrong_password_is_auth(self):
        news = self.news()
        connection = nntp.Connection(self.server(news, password="not-the-password"))
        with self.assertRaises(nntp.NNTPError) as caught:
            connection.open(TIMEOUT)
        self.assertEqual(caught.exception.kind, "auth")
        self.assertEqual(caught.exception.code, 481)

    def test_refused_login_on_user_step_is_auth(self):
        news = self.news(reject_login=502)
        with self.assertRaises(nntp.NNTPError) as caught:
            nntp.Connection(self.server(news)).open(TIMEOUT)
        self.assertEqual(caught.exception.kind, "auth")
        self.assertEqual(caught.exception.code, 502)

    def test_every_limit_style_is_busy(self):
        for style in ("greeting", "greeting-text", "login"):
            with self.subTest(style=style):
                news = self.news(max_connections=1, limit_style=style)
                self.connection(news)
                with self.assertRaises(nntp.NNTPError) as caught:
                    nntp.Connection(self.server(news)).open(TIMEOUT)
                self.assertEqual(caught.exception.kind, "busy")
                self.assertEqual(news.rejected_total, 1)

    def test_garbage_greeting_is_protocol(self):
        news = self.news(garbage_greeting=True)
        with self.assertRaises(nntp.NNTPError) as caught:
            nntp.Connection(self.server(news)).open(TIMEOUT)
        self.assertEqual(caught.exception.kind, "protocol")

    def test_unexpected_greeting_code_is_refused(self):
        news = self.news(greeting="502 access denied")
        with self.assertRaises(nntp.NNTPError) as caught:
            nntp.Connection(self.server(news)).open(TIMEOUT)
        self.assertEqual((caught.exception.kind, caught.exception.code), ("refused", 502))

    def test_hangup_is_closed(self):
        news = self.news(hangup=True)
        with self.assertRaises(nntp.NNTPError) as caught:
            nntp.Connection(self.server(news)).open(TIMEOUT)
        self.assertEqual(caught.exception.kind, "closed")

    def test_stalled_greeting_times_out_in_time(self):
        news = self.news(stall=True)
        started = time.monotonic()
        with self.assertRaises(nntp.NNTPError) as caught:
            nntp.Connection(self.server(news)).open(0.4)
        elapsed = time.monotonic() - started
        self.assertEqual(caught.exception.kind, "timeout")
        self.assertGreaterEqual(elapsed, 0.3)
        self.assertLess(elapsed, 3.0)

    def test_refused_port_is_connect(self):
        news = self.news()
        server = self.server(news)
        news.stop()
        with self.assertRaises(nntp.NNTPError) as caught:
            nntp.Connection(server).open(TIMEOUT)
        self.assertEqual(caught.exception.kind, "connect")

    def test_unresolvable_host_is_dns(self):
        news = self.news()
        server = self.server(news)
        server.host = "nowhere.invalid"
        with mock.patch("socket.getaddrinfo", side_effect=socket.gaierror(-2, "unknown")):
            with self.assertRaises(nntp.NNTPError) as caught:
                nntp.Connection(server).open(TIMEOUT)
        self.assertEqual(caught.exception.kind, "dns")

    def test_failed_login_leaves_no_connection_open(self):
        news = self.news(reject_login=True)
        with self.assertRaises(nntp.NNTPError):
            nntp.Connection(self.server(news)).open(TIMEOUT)
        wait_for(self, lambda: news.open_connections == 0)


class StatTests(NewsCase):
    def test_223_and_430_are_found_and_missing(self):
        news = self.news(have=["a@test.invalid"])
        connection = self.connection(news)
        answers = connection.stat(["a@test.invalid", "b@test.invalid"], TIMEOUT)
        self.assertEqual(answers, {"a@test.invalid": True, "b@test.invalid": False})

    def test_empty_question_list_sends_nothing(self):
        news = self.news()
        connection = self.connection(news)
        self.assertEqual(connection.stat([], TIMEOUT), {})
        self.assertEqual(news.stats_total, 0)

    def test_questions_are_pipelined(self):
        have = ids(40)
        news = self.news(have=have)
        connection = self.connection(news)
        answers = connection.stat(have, TIMEOUT)
        self.assertEqual(len(answers), 40)
        self.assertTrue(all(answers.values()))
        self.assertGreater(news.max_burst, 1)
        self.assertLessEqual(news.max_burst, nntp.PIPELINE)

    def test_depth_one_is_not_pipelined(self):
        have = ids(10)
        news = self.news(have=have)
        connection = self.connection(news)
        connection.stat(have, TIMEOUT, depth=1)
        self.assertEqual(news.max_burst, 1)

    def test_answers_come_back_in_order_of_the_questions(self):
        wanted = ids(25)
        news = self.news(have=wanted[::2])
        connection = self.connection(news)
        answers = connection.stat(wanted, TIMEOUT)
        self.assertEqual(list(answers), wanted)
        self.assertEqual(news.stat_log, wanted)
        self.assertEqual([answers[msgid] for msgid in wanted], [number % 2 == 0 for number in range(25)])

    def test_unclear_codes_stay_unanswered(self):
        news = self.news(broken_code=501)
        connection = self.connection(news)
        self.assertEqual(connection.stat(ids(5), TIMEOUT), {})
        self.assertEqual(news.stats_total, 5)

    def test_broken_codes_drop_the_connection(self):
        for code in (400, 480, 481, 482, 483, 502, 503):
            with self.subTest(code=code):
                news = self.news(have=ids(3))
                connection = self.connection(news)
                news.broken_code = code
                with self.assertRaises(nntp.NNTPError) as caught:
                    connection.stat(ids(3), TIMEOUT)
                self.assertEqual((caught.exception.kind, caught.exception.code), ("dropped", code))
                self.assertEqual(caught.exception.partial, {})

    def test_partial_answers_travel_with_a_broken_code(self):
        news = self.news()
        connection = nntp.Connection(self.server(news))
        sock = _ScriptSocket()
        connection._sock = sock
        connection._reader = _Lines(b"223 0 <x>\r\n430 gone\r\n501 odd\r\n480 login first\r\n223 0 <y>\r\n")
        with self.assertRaises(nntp.NNTPError) as caught:
            connection.stat(["a@t", "b@t", "c@t", "d@t", "e@t"], TIMEOUT)
        self.assertEqual(caught.exception.kind, "dropped")
        self.assertEqual(caught.exception.code, 480)
        self.assertEqual(caught.exception.partial, {"a@t": True, "b@t": False})
        self.assertEqual(sock.sent, b"".join(b"STAT <%s>\r\n" % name for name in (b"a@t", b"b@t", b"c@t", b"d@t", b"e@t")))

    def test_hangup_midway_keeps_the_answers_received(self):
        wanted = ids(10)
        news = self.news(have=wanted, drop_after_answers=3)
        connection = self.connection(news)
        with self.assertRaises(nntp.NNTPError) as caught:
            connection.stat(wanted, TIMEOUT)
        self.assertEqual(caught.exception.kind, "closed")
        self.assertEqual(caught.exception.partial, {msgid: True for msgid in wanted[:3]})

    def test_stalled_answer_times_out(self):
        news = self.news(stall="stat")
        connection = self.connection(news)
        started = time.monotonic()
        with self.assertRaises(nntp.NNTPError) as caught:
            connection.stat(ids(3), 0.4)
        self.assertEqual(caught.exception.kind, "timeout")
        self.assertLess(time.monotonic() - started, 3.0)

    def test_unsafe_message_ids_are_never_sent(self):
        news = self.news(have=["ok@test.invalid", "x"])
        connection = self.connection(news)
        unsafe = ["bad\r\nQUIT", "bad\nQUIT", "bad\rQUIT", "with space@test.invalid", "<in@angle>", "open<@x", "close>@x",
                  "a" * 249, "", "trailing\n", "tab\there", "\x00nul", "\u00e9\u00e9"]
        answers = connection.stat(unsafe + ["ok@test.invalid"], TIMEOUT)
        self.assertEqual(answers, {"ok@test.invalid": True})
        self.assertEqual(news.stat_log, ["ok@test.invalid"])
        self.assertEqual(news.quits, 0)
        self.assertTrue(all(line.startswith("AUTHINFO") for line in news.commands), news.commands)

    def test_only_unsafe_ids_send_nothing(self):
        news = self.news()
        connection = self.connection(news)
        self.assertEqual(connection.stat(["a b", "", "c\r\nd"], TIMEOUT), {})
        self.assertEqual(news.stats_total, 0)

    def test_longest_allowed_id_is_sent(self):
        longest = "a" * 248
        news = self.news(have=[longest])
        connection = self.connection(news)
        self.assertEqual(connection.stat([longest], TIMEOUT), {longest: True})

    def test_join_sends_group_once(self):
        news = self.news()
        connection = self.connection(news)
        connection.join("alt.binaries.test", TIMEOUT)
        connection.join("alt.binaries.test", TIMEOUT)
        self.assertEqual(news.groups_joined, ["alt.binaries.test"])
        connection.join("alt.binaries.other", TIMEOUT)
        self.assertEqual(news.groups_joined, ["alt.binaries.test", "alt.binaries.other"])

    def test_join_ignores_unsafe_or_empty_group_names(self):
        news = self.news()
        connection = self.connection(news)
        for name in ("", None, "a b", "alt.x\r\nQUIT", "alt.x\nQUIT", "alt.x\n", "x" * 201, "al\u00e9"):
            connection.join(name, TIMEOUT)
        self.assertEqual(news.groups_joined, [])
        self.assertEqual(news.quits, 0)
        self.assertTrue(all(line.startswith("AUTHINFO") for line in news.commands), news.commands)

    def test_close_says_quit_and_is_safe_twice(self):
        news = self.news()
        connection = nntp.Connection(self.server(news))
        connection.open(TIMEOUT)
        connection.close()
        connection.close()
        wait_for(self, lambda: news.quits == 1)

    def test_impolite_close_says_nothing(self):
        news = self.news()
        connection = nntp.Connection(self.server(news))
        connection.open(TIMEOUT)
        connection.close(polite=False)
        wait_for(self, lambda: news.open_connections == 0)
        self.assertEqual(news.quits, 0)


class _ScriptSocket(object):
    def __init__(self):
        self.sent = b""

    def settimeout(self, _value):
        pass

    def sendall(self, data):
        self.sent += data


class _Lines(object):
    def __init__(self, data):
        self._lines = data.splitlines(True)

    def readline(self, _limit=-1):
        return self._lines.pop(0) if self._lines else b""


class SessionTests(NewsCase):
    def test_limit_is_the_smaller_of_wanted_and_allowed(self):
        news = self.news()
        self.assertEqual(nntp.ServerSession(self.server(news, connections=3), 8, nntp.Deadline(5)).limit, 3)
        self.assertEqual(nntp.ServerSession(self.server(news, connections=3), 2, nntp.Deadline(5)).limit, 2)
        self.assertEqual(nntp.ServerSession(self.server(news, connections=3), 3, nntp.Deadline(5)).limit, 3)

    def test_limit_without_server_cap_and_never_below_one(self):
        news = self.news()
        server = self.server(news)
        server.connections = 0
        self.assertEqual(nntp.ServerSession(server, 4, nntp.Deadline(5)).limit, 4)
        self.assertEqual(nntp.ServerSession(server, 0, nntp.Deadline(5)).limit, 1)

    def test_ask_returns_the_clear_answers(self):
        wanted = ids(30)
        news = self.news(have=wanted[:10])
        session = self.session(news)
        answers = session.ask(wanted)
        self.assertEqual(answers, {msgid: number < 10 for number, msgid in enumerate(wanted)})
        self.assertTrue(session.reachable)
        self.assertEqual(session.questions, 30)

    def test_ask_with_nothing_to_ask(self):
        news = self.news()
        session = self.session(news)
        self.assertEqual(session.ask([]), {})
        self.assertEqual(news.connections_total, 0)
        self.assertIsNone(session.reachable)

    def test_unclear_answers_are_left_out(self):
        news = self.news(broken_code=501)
        session = self.session(news)
        self.assertEqual(session.ask(ids(10)), {})

    def test_broken_answers_end_in_no_answers_and_a_failure(self):
        news = self.news(broken_code=502)
        session = self.session(news)
        self.assertEqual(session.ask(ids(10)), {})
        self.assertEqual(session.failure.kind, "dropped")

    def test_many_ids_use_several_connections_but_never_more_than_the_limit(self):
        wanted = ids(200)
        news = self.news(have=wanted, slow=0.01)
        session = self.session(news, connections=3)
        answers = session.ask(wanted)
        self.assertEqual(len(answers), 200)
        self.assertTrue(all(answers.values()))
        self.assertGreaterEqual(news.max_concurrent, 2)
        self.assertLessEqual(news.max_concurrent, 3)
        self.assertLessEqual(news.connections_total, 3)

    def test_server_cap_limits_connections(self):
        wanted = ids(120)
        news = self.news(have=wanted, slow=0.005)
        session = self.session(news, connections=8, cap=2)
        self.assertEqual(session.limit, 2)
        self.assertEqual(len(session.ask(wanted)), 120)
        self.assertLessEqual(news.max_concurrent, 2)

    def test_connections_stay_open_for_the_next_question(self):
        wanted = ids(10)
        news = self.news(have=wanted)
        session = self.session(news, connections=1)
        session.ask(wanted[:5])
        session.ask(wanted[5:])
        self.assertEqual(news.connections_total, 1)
        self.assertEqual(news.stats_total, 10)

    def test_unreachable_server_is_marked_and_not_tried_again(self):
        news = self.news(have=ids(5), reject_login=True)
        session = self.session(news)
        self.assertEqual(session.ask(ids(5)), {})
        self.assertIs(session.reachable, False)
        self.assertEqual(session.failure.kind, "auth")
        before = news.connections_total
        self.assertGreaterEqual(before, 1)
        self.assertEqual(session.ask(ids(5)), {})
        self.assertEqual(news.connections_total, before)
        self.assertEqual(news.stats_total, 0)

    def test_connection_dropped_midway_is_retried_and_answers_are_complete(self):
        wanted = ids(30)
        news = self.news(have=wanted[:20], drop_after_answers=5)
        session = self.session(news, connections=1)
        answers = session.ask(wanted)
        self.assertEqual(answers, {msgid: number < 20 for number, msgid in enumerate(wanted)})
        self.assertEqual(news.drops, 1)
        self.assertTrue(session.reachable)

    def test_server_that_hangs_up_for_good_gives_no_endless_loop(self):
        news = self.news(have=ids(10), drop_after_answers=0, drop_limit=0)
        session = self.session(news, connections=1, seconds=20)
        started = time.monotonic()
        self.assertEqual(session.ask(ids(10)), {})
        self.assertLess(time.monotonic() - started, 10.0)
        self.assertEqual(session.failure.kind, "closed")

    def test_stalling_server_ends_at_the_deadline(self):
        news = self.news(have=ids(10), stall="stat")
        session = self.session(news, seconds=1.0)
        started = time.monotonic()
        answers = session.ask(ids(10))
        elapsed = time.monotonic() - started
        self.assertEqual(answers, {})
        self.assertGreaterEqual(elapsed, 0.5)
        self.assertLess(elapsed, 4.0)
        self.assertTrue(session.reachable)

    def test_busy_server_is_retried_once_after_a_pause(self):
        wanted = ids(5)
        news = self.news(have=wanted, max_connections=1)
        holder = nntp.Connection(self.server(news))
        holder.open(TIMEOUT)
        timer = threading.Timer(0.2, holder.close)
        timer.start()
        self.addCleanup(timer.cancel)
        self.addCleanup(holder.close, False)
        session = self.session(news, connections=1)
        answers = session.ask(wanted)
        self.assertEqual(answers, {msgid: True for msgid in wanted})
        self.assertGreaterEqual(news.rejected_total, 1)
        self.assertTrue(session.reachable)

    def test_busy_server_that_stays_busy_is_given_up_after_one_retry(self):
        news = self.news(have=ids(5), max_connections=1)
        holder = self.connection(news)
        self.assertTrue(holder)
        session = self.session(news, connections=1)
        self.assertEqual(session.ask(ids(5)), {})
        self.assertIs(session.reachable, False)
        self.assertEqual(session.failure.kind, "busy")
        self.assertEqual(news.rejected_total, 2)

    def test_test_counts_logged_in_connections(self):
        news = self.news(max_connections=2)
        session = self.session(news, connections=3, cap=3)
        self.assertEqual(session.limit, 3)
        self.assertEqual(session.test(), 2)
        self.assertEqual(session.failure.kind, "busy")
        self.assertTrue(session.reachable)
        self.assertEqual(news.logins_total, 2)

    def test_test_counts_all_when_the_server_allows_them(self):
        news = self.news()
        session = self.session(news, connections=3)
        self.assertEqual(session.test(), 3)
        self.assertIsNone(session.failure)
        self.assertEqual(news.logins_total, 3)

    def test_test_with_wrong_password_counts_zero(self):
        news = self.news()
        session = self.session(news, connections=3, password="nope-nope")
        self.assertEqual(session.test(), 0)
        self.assertEqual(session.failure.kind, "auth")

    def test_connections_opened_by_test_are_used_by_ask(self):
        wanted = ids(10)
        news = self.news(have=wanted)
        session = self.session(news, connections=2)
        self.assertEqual(session.test(), 2)
        session.ask(wanted)
        self.assertEqual(news.connections_total, 2)

    def test_close_sends_quit_and_later_ask_returns_nothing(self):
        wanted = ids(10)
        news = self.news(have=wanted)
        session = self.session(news, connections=2)
        self.assertEqual(session.test(), 2)
        session.close()
        wait_for(self, lambda: news.quits == 2)
        self.assertEqual(session.ask(wanted), {})
        self.assertEqual(news.stats_total, 0)
        session.close()

    def test_connection_finishing_after_close_is_closed_too(self):
        wanted = ids(40)
        news = self.news(have=wanted, slow=0.02)
        session = self.session(news, connections=1)
        results = []
        thread = threading.Thread(target=lambda: results.append(session.ask(wanted)), daemon=True)
        thread.start()
        wait_for(self, lambda: news.stats_total >= 1)
        session.close()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        wait_for(self, lambda: news.open_connections == 0)

    def test_join_group_is_used_when_the_server_wants_it(self):
        wanted = ids(5)
        news = self.news(have=wanted)
        session = self.session(news, connections=1, join_group=True)
        groups = {msgid: "alt.binaries.test" for msgid in wanted}
        self.assertEqual(len(session.ask(wanted, groups)), 5)
        self.assertEqual(news.groups_joined, ["alt.binaries.test"])

    def test_group_is_not_joined_when_the_server_does_not_want_it(self):
        wanted = ids(5)
        news = self.news(have=wanted)
        session = self.session(news, connections=1)
        session.ask(wanted, {msgid: "alt.binaries.test" for msgid in wanted})
        self.assertEqual(news.groups_joined, [])

    def test_hostile_group_name_is_never_sent(self):
        wanted = ids(5)
        news = self.news(have=wanted)
        session = self.session(news, connections=1, join_group=True)
        groups = {msgid: "alt.x\r\nQUIT" for msgid in wanted}
        self.assertEqual(len(session.ask(wanted, groups)), 5)
        self.assertEqual(news.groups_joined, [])
        self.assertEqual(news.quits, 0)


class SecrecyTests(NewsCase):
    def test_password_is_never_in_error_text(self):
        wrong = "wrong-pass-9876"
        news = self.news(echo_password=True)
        for style in ("auth", "busy"):
            with self.subTest(kind=style):
                if style == "busy":
                    news.max_connections = 1
                    news.limit_style = "login"
                    first = self.connection(news)
                    self.assertTrue(first)
                connection = nntp.Connection(self.server(news, password=wrong))
                with self.assertRaises(nntp.NNTPError) as caught:
                    connection.open(TIMEOUT)
                error = caught.exception
                self.assertEqual(error.kind, style)
                for text in (error.describe(), str(error), repr(error), repr(error.args)):
                    self.assertNotIn(wrong, text)
                    self.assertNotIn(PASSWORD, text)
        self.assertIn(wrong, news.passwords_seen)

    def test_session_failure_text_has_no_password(self):
        wrong = "wrong-pass-5432"
        news = self.news(echo_password=True)
        session = self.session(news, password=wrong)
        session.ask(ids(3))
        self.assertEqual(session.failure.kind, "auth")
        self.assertNotIn(wrong, session.failure.describe())
        self.assertNotIn(wrong, str(session.failure))

    def test_echoed_password_in_answers_does_not_matter(self):
        wanted = ids(4)
        news = self.news(have=wanted[:2], echo_password=True)
        connection = self.connection(news)
        self.assertEqual(connection.stat(wanted, TIMEOUT),
                         {wanted[0]: True, wanted[1]: True, wanted[2]: False, wanted[3]: False})


if __name__ == "__main__":
    unittest.main()
