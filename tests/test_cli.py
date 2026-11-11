"""Tests for tools/completer_cli.py: config parsing, verdict lines, exit codes and secrecy."""

import contextlib
import io
import os
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Completer"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))

import completer_cli  # noqa: E402
import nzbtools  # noqa: E402
from completer import checker, log, nntp, nzbfile, settings  # noqa: E402

TOOL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools", "completer_cli.py")
USER = "tester"
PASSWORD = "s3cret-pass-word"
HOST = "127.0.0.1"
NOW = 1700000000


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        server = self.server
        self.wfile.write(b"200 fake news server ready\r\n")
        user = None
        while True:
            line = self.rfile.readline(4096)
            if not line:
                return
            text = line.decode("latin-1").strip()
            upper = text.upper()
            if upper.startswith("AUTHINFO USER"):
                user = text.split(" ", 2)[2]
                self.wfile.write(b"381 password please\r\n")
            elif upper.startswith("AUTHINFO PASS"):
                if user == USER and text.split(" ", 2)[2] == server.password:
                    self.wfile.write(b"281 welcome\r\n")
                else:
                    self.wfile.write(("481 bad login for %s with %s\r\n" % (user, text.split(" ", 2)[2])).encode())
            elif upper.startswith("STAT"):
                msgid = text[text.index("<") + 1:text.rindex(">")]
                if msgid in server.articles:
                    self.wfile.write(("223 0 <%s>\r\n" % msgid).encode())
                else:
                    self.wfile.write(b"430 no such article\r\n")
            elif upper == "QUIT":
                self.wfile.write(b"205 bye\r\n")
                return
            else:
                self.wfile.write(b"500 what\r\n")


class FakeNntp(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, articles, password=PASSWORD):
        socketserver.TCPServer.__init__(self, (HOST, 0), _Handler)
        self.articles = set(articles)
        self.password = password
        self.thread = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.05})
        self.thread.daemon = True

    @property
    def port(self):
        return self.server_address[1]

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.shutdown()
        self.server_close()
        self.thread.join(5)


def config_text(port, extra=""):
    return ("# a comment\r\n\r\n"
            "MainDir=${AppDir}/downloads\r\n"
            "Server1.Active=yes\r\n"
            "Server1.Name=Made Up Provider\r\n"
            "Server1.Host=%s\r\n"
            "Server1.Port=%d\r\n"
            "Server1.Username=%s\r\n"
            "Server1.Password=%s\r\n"
            "Server1.Encryption=no\r\n"
            "Server1.Connections=4\r\n"
            "Server1.Level=0\r\n%s" % (HOST, port, USER, PASSWORD, extra))


class ConfigTests(unittest.TestCase):
    def test_parse_lines(self):
        text = ("# comment\r\n"
                "\r\n"
                "   \r\n"
                "Server1.Host = news.example.invalid \r\n"
                "Server1.Password=abc=def==\r\n"
                "#Server2.Host=commented.example.invalid\r\n"
                "no equals sign here\r\n"
                "Server1.Level=0\r\n"
                "Server1.Level=1\r\n")
        config = completer_cli.parse_config(text)
        self.assertEqual(config["Server1.Host"], "news.example.invalid")
        self.assertEqual(config["Server1.Password"], "abc=def==")
        self.assertEqual(config["Server1.Level"], "1")  # the last one wins
        self.assertNotIn("Server2.Host", config)
        self.assertEqual(len(config), 3)

    def test_env_shape(self):
        config = completer_cli.parse_config(
            "Server1.Host=news.example.invalid\n"
            "Server1.Port=563\n"
            "Server1.Username=tester\n"
            "Server1.Password=pw=with=equals\n"
            "Server1.Encryption=yes\n"
            "Server1.CertVerification=Strict\n"
            "Server1.Connections=8\n"
            "Server1.Level=0\n"
            "Server1.Optional=no\n"
            "Server1.Retention=3000\n"
            "Server2.Host=fill.example.invalid\n"
            "Server2.level=2\n"
            "Server2.Optional=yes\n"
            "Server2.JoinGroup=yes\n"
            "Server2.Connections=2\n"
            "Server2.Unknown=ignored\n"
            "CertCheck=yes\n"
            "CertStore=${AppDir}/cacert.pem\n"
            "ControlPassword=never-copied\n")
        env = completer_cli.build_env(config, 3, 500, "2")
        self.assertEqual(env["NZBOP_SERVER1_HOST"], "news.example.invalid")
        self.assertEqual(env["NZBOP_SERVER1_PASSWORD"], "pw=with=equals")
        self.assertEqual(env["NZBOP_SERVER1_CERTVERIFICATION"], "Strict")
        self.assertEqual(env["NZBOP_SERVER2_LEVEL"], "2")
        self.assertNotIn("NZBOP_SERVER2_UNKNOWN", env)
        self.assertEqual(env["NZBOP_CERTCHECK"], "yes")
        self.assertNotIn("NZBOP_CERTSTORE", env)  # unexpanded variable is dropped
        self.assertNotIn("never-copied", env.values())
        self.assertEqual(env["NZBPO_Connections"], "3")
        self.assertEqual(env["NZBPO_MaxArticles"], "500")
        self.assertEqual(env["NZBPO_IgnoreServers"], "2")
        self.assertEqual(env["NZBPO_Mode"], "DryRun")

        servers = settings.load_servers(env)
        log.forget_secrets()
        self.addCleanup(log.forget_secrets)
        self.assertEqual([s.id for s in servers], [1, 2])
        first, second = servers
        self.assertEqual((first.host, first.port, first.username, first.tls, first.connections, first.level,
                          first.retention, first.optional), ("news.example.invalid", 563, "tester", True, 8, 0, 3000, False))
        self.assertEqual((second.level, second.optional, second.join_group, second.connections), (2, True, True, 2))
        options = settings.Settings(env)
        self.assertEqual((options.connections, options.max_articles, options.mode), (3, 500, "dryrun"))
        self.assertEqual(options.ignore_servers, frozenset([2]))

    def test_cert_store_kept_when_plain(self):
        env = completer_cli.build_env({"CertStore": "/some/where/ca.pem"}, 2, 1200, "")
        self.assertEqual(env["NZBOP_CERTSTORE"], "/some/where/ca.pem")


class CliBase(unittest.TestCase):
    def setUp(self):
        log.forget_secrets()
        self.addCleanup(log.forget_secrets)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def serve(self, articles, password=PASSWORD):
        server = FakeNntp(articles, password).start()
        self.addCleanup(server.stop)
        return server

    def write_config(self, text, name="nzbget.conf"):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        return path

    def write_nzb(self, post, name):
        return post.write(os.path.join(self.dir, name))

    def run_inprocess(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = completer_cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def run_subprocess(self, argv):
        done = subprocess.run([sys.executable, TOOL] + argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, timeout=120)
        return done.returncode, done.stdout, done.stderr

    def expected_verdict(self, config_path, nzb_path, connections=2, max_articles=1200, ignore=""):
        """What the library says about the same input, called directly."""
        with open(config_path, "r", encoding="utf-8-sig") as handle:
            config = completer_cli.parse_config(handle.read())
        env = completer_cli.build_env(config, connections, max_articles, ignore)
        servers = settings.load_servers(env)
        outcome = checker.check(nzbfile.parse(nzb_path), servers, settings.Settings(env), nntp.Deadline(30), NOW)
        return outcome

    def assertNoSecrets(self, *texts):
        for text in texts:
            for secret in (PASSWORD, USER, HOST, "Made Up Provider"):
                self.assertNotIn(secret, text)

    @staticmethod
    def verdict_lines(out):
        return [line for line in out.splitlines() if line.split(" ", 1)[0] in ("PASS", "FAIL", "SUSPECT", "GONE", "ERROR")]


class RunTests(CliBase):
    def test_clean_post_passes(self):
        post = nzbtools.make_post("clean", segments=40, par_segments=5, now=NOW)
        server = self.serve(post.all_ids)
        config = self.write_config(config_text(server.port))
        nzb = self.write_nzb(post, "clean.nzb")
        for run in (self.run_inprocess, self.run_subprocess):
            code, out, err = run(["--config", config, "--time-limit", "30", "--now", str(NOW), nzb])
            self.assertEqual(code, 0, out + err)
            lines = self.verdict_lines(out)
            self.assertEqual(len(lines), 1, out)
            fields = lines[0].split()
            self.assertEqual(fields[:2], ["PASS", "clean.nzb"])
            self.assertIn("all there", lines[0])
            self.assertIn("summary: 1 nzb, 1 pass, 0 fail, 0 suspect, 0 gone, 0 error", out)
            self.assertNoSecrets(out, err)

    def test_mostly_missing_post_matches_library(self):
        post = nzbtools.make_post("lost", segments=40, par_segments=5, now=NOW)
        server = self.serve(post.data_ids[:6])
        config = self.write_config(config_text(server.port))
        nzb = self.write_nzb(post, "lost.nzb")
        outcome = self.expected_verdict(config, nzb)
        self.assertIn(outcome.verdict, ("fail", "gone"))
        log.forget_secrets()
        code, out, err = self.run_inprocess(["--config", config, "--time-limit", "30", "--now", str(NOW), nzb])
        self.assertEqual(code, 0, out + err)
        lines = self.verdict_lines(out)
        self.assertEqual(len(lines), 1, out)
        self.assertEqual(lines[0].split()[:2], [outcome.verdict.upper(), "lost.nzb"])
        self.assertIn(outcome.describe(), lines[0])
        self.assertNoSecrets(out, err)

    def test_several_nzbs_and_summary(self):
        good = nzbtools.make_post("good", segments=30, par_segments=5, now=NOW)
        bad = nzbtools.make_post("bad", segments=30, par_segments=5, now=NOW)
        server = self.serve(good.all_ids)
        config = self.write_config(config_text(server.port))
        paths = [self.write_nzb(good, "good.nzb"), os.path.join(self.dir, "missing.nzb"), self.write_nzb(bad, "bad.nzb")]
        code, out, err = self.run_inprocess(["--config", config, "--time-limit", "30", "--now", str(NOW)] + paths)
        self.assertEqual(code, 1)
        words = [line.split()[:2] for line in self.verdict_lines(out)]
        self.assertEqual(words[0], ["PASS", "good.nzb"])
        self.assertEqual(words[1], ["ERROR", "missing.nzb"])
        self.assertEqual(words[2][1], "bad.nzb")
        self.assertIn(words[2][0], ("FAIL", "GONE"))
        self.assertIn("summary: 3 nzb", out)
        self.assertIn("1 error", out)

    def test_verbose_lists_servers_without_secrets(self):
        post = nzbtools.make_post("verbose", segments=30, par_segments=5, now=NOW)
        server = self.serve(post.all_ids)
        config = self.write_config(config_text(server.port))
        nzb = self.write_nzb(post, "v.nzb")
        code, out, err = self.run_subprocess(["--config", config, "--time-limit", "30", "--now", str(NOW), "--verbose", nzb])
        self.assertEqual(code, 0, out + err)
        self.assertRegex(out, r"(?m)^\s+server 1: \d+ questions in")
        self.assertNoSecrets(out, err)
        quiet = self.run_subprocess(["--config", config, "--time-limit", "30", "--now", str(NOW), nzb])[1]
        self.assertNotIn("questions in", quiet)

    def test_wrong_password_never_echoed(self):
        post = nzbtools.make_post("login", segments=30, par_segments=5, now=NOW)
        server = self.serve(post.all_ids, password="another-different-pass")
        config = self.write_config(config_text(server.port))
        nzb = self.write_nzb(post, "login.nzb")
        outcome = self.expected_verdict(config, nzb)
        log.forget_secrets()
        code, out, err = self.run_subprocess(["--config", config, "--time-limit", "30", "--now", str(NOW), "--verbose", nzb])
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.verdict_lines(out)[0].split()[0], outcome.verdict.upper())
        self.assertIn("rejected the login", out)
        self.assertNoSecrets(out, err)
        self.assertNotIn("another-different-pass", out + err)

    def test_ignore_servers_leaves_no_usable_server(self):
        post = nzbtools.make_post("ign", segments=30, par_segments=5, now=NOW)
        config = self.write_config(config_text(9))
        nzb = self.write_nzb(post, "ign.nzb")
        code, out, err = self.run_inprocess(["--config", config, "--ignore-servers", "1-3", nzb])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("no usable news server", err)
        self.assertNoSecrets(out, err)


class ErrorTests(CliBase):
    def test_unreadable_and_invalid_nzb(self):
        server = self.serve([])
        config = self.write_config(config_text(server.port))
        junk = os.path.join(self.dir, "junk.nzb")
        with open(junk, "w") as handle:
            handle.write("this is not xml at all")
        gone = os.path.join(self.dir, "nothere.nzb")
        for run in (self.run_inprocess, self.run_subprocess):
            code, out, err = run(["--config", config, junk, gone])
            self.assertEqual(code, 1, out + err)
            lines = self.verdict_lines(out)
            self.assertEqual([line.split()[:2] for line in lines], [["ERROR", "junk.nzb"], ["ERROR", "nothere.nzb"]])
            self.assertIn("not valid XML", lines[0])
            self.assertIn("summary: 2 nzb, 0 pass, 0 fail, 0 suspect, 0 gone, 2 error", out)
            self.assertNoSecrets(out, err)

    def test_empty_config(self):
        nzb = self.write_nzb(nzbtools.make_post("e", segments=5, now=NOW), "e.nzb")
        for text in ("", "# only a comment\n\n", "Server1.Port=119\nServer1.Connections=2\n"):
            config = self.write_config(text)
            for run in (self.run_inprocess, self.run_subprocess):
                code, out, err = run(["--config", config, nzb])
                self.assertEqual(code, 2, out + err)
                self.assertEqual(out, "")
                self.assertIn("no usable news server", err)

    def test_server_without_connections_is_not_usable(self):
        nzb = self.write_nzb(nzbtools.make_post("e", segments=5, now=NOW), "e.nzb")
        config = self.write_config("Server1.Host=%s\nServer1.Connections=0\n" % HOST)
        code, out, err = self.run_inprocess(["--config", config, nzb])
        self.assertEqual(code, 2)
        self.assertNotIn(HOST, out + err)

    def test_config_missing(self):
        nzb = self.write_nzb(nzbtools.make_post("e", segments=5, now=NOW), "e.nzb")
        code, out, err = self.run_subprocess(["--config", os.path.join(self.dir, "none.conf"), nzb])
        self.assertEqual(code, 2)
        self.assertIn("cannot read the config file", err)

    def test_usage_errors(self):
        self.assertEqual(self.run_inprocess([])[0], 2)
        config = self.write_config(config_text(9))
        code, _out, err = self.run_subprocess(["--config", config])  # no nzb
        self.assertEqual(code, 2)
        self.assertIn("usage", err)
        self.assertEqual(self.run_inprocess(["--config", config, "--connections", "many", "x.nzb"])[0], 2)

    def test_unreachable_server_is_not_a_crash(self):
        # a port nobody listens on: the library answers (suspect, or error-free), the tool prints a verdict
        server = self.serve([])
        port = server.port
        server.stop()
        post = nzbtools.make_post("down", segments=30, par_segments=5, now=NOW)
        config = self.write_config(config_text(port))
        nzb = self.write_nzb(post, "down.nzb")
        outcome = self.expected_verdict(config, nzb)
        log.forget_secrets()
        started = time.monotonic()
        code, out, err = self.run_inprocess(["--config", config, "--time-limit", "20", "--now", str(NOW), "--verbose", nzb])
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual(code, 0, out + err)  # a verdict was produced, even if it is ERROR
        lines = self.verdict_lines(out)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].split()[0], outcome.verdict.upper())
        self.assertNotEqual(lines[0].split()[0], "PASS")
        self.assertNoSecrets(out, err)


if __name__ == "__main__":
    unittest.main()
