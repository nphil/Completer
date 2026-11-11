"""Tests for completer.log: what NZBGet reads from standard output, and that secrets never reach it."""

import io
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Completer"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from completer import log  # noqa: E402


class LogCase(unittest.TestCase):
    def setUp(self):
        log.forget_secrets()
        self.out = io.StringIO()
        log.set_stream(self.out)
        self.addCleanup(log.set_stream, None)
        self.addCleanup(log.forget_secrets)

    def lines(self):
        return self.out.getvalue().split("\n")[:-1]


class CleanTests(LogCase):
    def test_secret_replaced(self):
        log.add_secret("hunter2hunter2")
        self.assertEqual(log.clean("login hunter2hunter2 failed"), "login *** failed")

    def test_every_occurrence_replaced(self):
        log.add_secret("swordfish")
        self.assertEqual(log.clean("swordfish and swordfish"), "*** and ***")

    def test_longest_secret_first_when_one_contains_the_other(self):
        log.add_secret("abcd")
        log.add_secret("abcdefgh")
        self.assertEqual(log.clean("x abcdefgh y"), "x *** y")
        self.assertNotIn("efgh", log.clean("x abcdefgh y"))

    def test_longest_first_whatever_the_registration_order(self):
        log.add_secret("abcdefgh")
        log.add_secret("abcd")
        self.assertEqual(log.clean("abcdefgh"), "***")
        self.assertEqual(log.clean("abcd"), "***")

    def test_short_secrets_not_registered(self):
        for value in ("", "a", "abc", None):
            log.add_secret(value)
        self.assertEqual(log.clean("abc a abcd"), "abc a abcd")

    def test_four_characters_are_registered(self):
        log.add_secret("wxyz")
        self.assertEqual(log.clean("a wxyz b"), "a *** b")

    def test_non_string_secret_is_converted(self):
        log.add_secret(123456)
        self.assertEqual(log.clean("n 123456"), "n ***")

    def test_forget_secrets(self):
        log.add_secret("wxyz1234")
        log.forget_secrets()
        self.assertEqual(log.clean("wxyz1234"), "wxyz1234")

    def test_control_characters_become_spaces(self):
        cleaned = log.clean("a\r\nb\tc\x00d\x1be")
        self.assertEqual(cleaned, "a  b c d e")
        for ch in cleaned:
            self.assertTrue(ch.isprintable())

    def test_limit(self):
        self.assertEqual(len(log.clean("x" * 5000)), 900)
        self.assertEqual(log.clean("abcdef", 3), "abc")
        self.assertEqual(log.clean("abc", 10), "abc")

    def test_non_string_input(self):
        self.assertEqual(log.clean(42), "42")
        self.assertEqual(log.clean(None), "None")

    def test_secret_spanning_a_control_character_is_not_a_leak_of_the_rest(self):
        # the secret is replaced before control characters are turned into spaces
        log.add_secret("pass\nword")
        self.assertEqual(log.clean("x pass\nword y"), "x *** y")


class PrefixTests(LogCase):
    def test_prefixes(self):
        log.info("one")
        log.detail("two")
        log.warning("three")
        log.error("four")
        self.assertEqual(self.lines(), ["[INFO] one", "[DETAIL] two", "[WARNING] three", "[ERROR] four"])

    def test_messages_are_cleaned(self):
        log.add_secret("topsecret")
        log.info("a topsecret\r\n[ERROR] forged")
        self.assertEqual(self.lines(), ["[INFO] a ***  [ERROR] forged"])

    def test_one_line_per_message(self):
        log.warning("x\ny\rz")
        self.assertEqual(len(self.lines()), 1)
        self.assertTrue(self.out.getvalue().endswith("\n"))

    def test_long_message_cut(self):
        log.info("x" * 3000)
        self.assertEqual(len(self.lines()[0]), len("[INFO] ") + 900)

    def test_nzbget_names_the_extension_so_the_name_is_not_repeated(self):
        # NZBGet shows "Completer: <text>" for every line; a text that starts with the name must not repeat it.
        log.info("Completer: one")
        log.detail("Completer: two")
        log.warning("Completer: three")
        log.error("Completer: four")
        self.assertEqual(self.lines(), ["[INFO] one", "[DETAIL] two", "[WARNING] three", "[ERROR] four"])

    def test_only_a_leading_name_is_dropped(self):
        log.info("Server 1: Completer: x")
        log.info("Completer:no space")
        log.info("completer: lower case")
        self.assertEqual(self.lines(), ["[INFO] Server 1: Completer: x", "[INFO] Completer:no space",
                                        "[INFO] completer: lower case"])


class NzbCommandTests(LogCase):
    def test_exact_line(self):
        log.nzb_command("PAUSED=1")
        self.assertEqual(self.out.getvalue(), "[NZB] PAUSED=1\n")

    def test_no_control_characters(self):
        log.nzb_command("NZBPR_Completer_Info=a\r\nb\x00c\td")
        self.assertEqual(self.lines(), ["[NZB] NZBPR_Completer_Info=a  b c d"])
        for ch in self.lines()[0]:
            self.assertTrue(ch.isprintable())

    def test_injected_second_command_cannot_start_a_line(self):
        log.nzb_command("MARK=BAD\n[NZB] PAUSED=0")
        self.assertEqual(len(self.lines()), 1)
        self.assertTrue(self.lines()[0].startswith("[NZB] "))

    def test_accepts_non_string(self):
        log.nzb_command(7)
        self.assertEqual(self.lines(), ["[NZB] 7"])


class StreamTests(LogCase):
    def test_set_stream_redirects(self):
        other = io.StringIO()
        log.set_stream(other)
        log.info("here")
        self.assertEqual(other.getvalue(), "[INFO] here\n")
        self.assertEqual(self.out.getvalue(), "")

    def test_none_goes_back_to_stdout(self):
        captured = io.StringIO()
        real = sys.stdout
        sys.stdout = captured
        self.addCleanup(setattr, sys, "stdout", real)
        log.set_stream(None)
        log.info("to stdout")
        sys.stdout = real
        self.assertEqual(captured.getvalue(), "[INFO] to stdout\n")
        self.assertEqual(self.out.getvalue(), "")

    def test_closed_stream_never_raises(self):
        self.out.close()
        log.info("x")
        log.detail("x")
        log.warning("x")
        log.error("x")
        log.nzb_command("x")

    def test_broken_stream_never_raises(self):
        class Broken(object):
            def write(self, text):
                raise OSError("broken pipe")

            def flush(self):
                raise OSError("broken pipe")

        log.set_stream(Broken())
        log.info("x")
        log.nzb_command("x")

    def test_flush_failure_never_raises(self):
        class FlushFails(io.StringIO):
            def flush(self):
                raise ValueError("I/O operation on closed file")

        log.set_stream(FlushFails())
        log.error("x")

    def test_each_line_is_flushed(self):
        class Counting(io.StringIO):
            flushes = 0

            def flush(self):
                self.flushes += 1

        stream = Counting()
        log.set_stream(stream)
        log.info("a")
        log.info("b")
        self.assertEqual(stream.flushes, 2)


class ThreadTests(LogCase):
    def test_concurrent_lines_never_interleave(self):
        class Chunks(object):
            """Records every write call separately and takes its time, to invite interleaving."""

            def __init__(self):
                self.writes = []

            def write(self, text):
                half = len(text) // 2
                self.writes.append(text[:half])
                threading.Event().wait(0.0001)
                self.writes.append(text[half:])

            def flush(self):
                pass

        stream = Chunks()
        log.set_stream(stream)
        threads_n, each = 8, 60

        def work(number):
            for index in range(each):
                log.info("thread %d line %d %s" % (number, index, "x" * 40))

        threads = [threading.Thread(target=work, args=(n,)) for n in range(threads_n)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
            self.assertFalse(thread.is_alive())
        lines = "".join(stream.writes).split("\n")
        self.assertEqual(lines[-1], "")
        lines = lines[:-1]
        self.assertEqual(len(lines), threads_n * each)
        seen = set()
        for line in lines:
            parts = line.split(" ")
            self.assertEqual(parts[0], "[INFO]", line)
            self.assertEqual(parts[1], "thread", line)
            self.assertEqual(parts[5], "x" * 40, line)
            self.assertEqual(len(parts), 6, line)
            seen.add((parts[2], parts[4]))
        self.assertEqual(len(seen), threads_n * each)


if __name__ == "__main__":
    unittest.main()
