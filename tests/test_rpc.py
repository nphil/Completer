"""Tests for completer.rpc: the JSON-RPC client and the QueueGuard that gates every change to the queue."""

import base64
import glob
import json
import os
import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Completer"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from completer import rpc as rpc_module  # noqa: E402
from completer.rpc import (HISTORY_EDITS, QUEUE_EDITS, QueueGuard, Refused, Rpc, RpcError,  # noqa: E402
                           Target, params_of, same_name)
from completer.settings import PARAMETERS, TOKEN  # noqa: E402

PASSWORD = "pw-Zx9-not-a-real-secret"
USER = "tester"
READS = ("listgroups", "history", "status")


class StubControl(object):
    def __init__(self, port, host="127.0.0.1", username=USER, password=PASSWORD):
        self.host, self.port, self.username, self.password = host, port, username, password

    def url(self):
        return "http://%s:%d/jsonrpc" % (self.host, self.port)


class _Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Loopback(object):
    """A tiny HTTP server that records every request and answers with a configurable reply."""

    def __init__(self, test):
        self.requests = []
        self.status = 200
        self.reply = json.dumps({"result": "ok"}).encode("utf-8")
        self.hang = False
        self.release = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                outer.requests.append({"path": self.path, "body": body, "headers": dict(self.headers)})
                if outer.hang:
                    outer.release.wait(10)
                    return
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(outer.reply)))
                self.end_headers()
                self.wfile.write(outer.reply)

            def log_message(self, *args):
                pass

        self.server = _Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05})
        self.thread.daemon = True
        self.thread.start()
        test.addCleanup(self.stop)

    def stop(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class RpcCallTests(unittest.TestCase):
    def setUp(self):
        self.srv = Loopback(self)
        self.rpc = Rpc(StubControl(self.srv.port), timeout=5.0)

    def test_request_shape_and_result(self):
        self.srv.reply = json.dumps({"version": "1.1", "result": [1, 2]}).encode()
        self.assertEqual(self.rpc.call("listgroups", 0), [1, 2])
        req = self.srv.requests[0]
        self.assertEqual(req["path"], "/jsonrpc")
        self.assertEqual(json.loads(req["body"].decode("utf-8")), {"method": "listgroups", "params": [0]})
        self.assertIn("json", req["headers"]["Content-Type"])

    def test_basic_auth_header(self):
        self.rpc.call("status")
        expected = "Basic " + base64.b64encode(("%s:%s" % (USER, PASSWORD)).encode()).decode()
        self.assertEqual(self.srv.requests[0]["headers"]["Authorization"], expected)

    def test_non_ascii_sent_as_plain_utf8(self):
        name = u"Caf\u00e9 \u65e5\u672c\u8a9e"
        self.rpc.call("editqueue", "GroupSetParameter", u"x=" + name, [7])
        body = self.srv.requests[0]["body"]
        self.assertNotIn(b"\\u", body)
        self.assertIn(name.encode("utf-8"), body)
        self.assertEqual(json.loads(body.decode("utf-8"))["params"][1], u"x=" + name)

    def test_missing_result_is_none(self):
        self.srv.reply = b"{}"
        self.assertIsNone(self.rpc.call("status"))

    def _assert_error(self, *fragments):
        with self.assertRaises(RpcError) as ctx:
            self.rpc.call("listgroups", 0)
        self.assertNotIn(PASSWORD, str(ctx.exception))
        self.assertNotIn(base64.b64encode(("%s:%s" % (USER, PASSWORD)).encode()).decode(), str(ctx.exception))
        for fragment in fragments:
            self.assertIn(fragment, str(ctx.exception))

    def test_http_401(self):
        self.srv.status = 401
        self._assert_error("401")

    def test_http_500(self):
        self.srv.status = 500
        self._assert_error("500")

    def test_connection_refused(self):
        rpc = Rpc(StubControl(free_port()), timeout=5.0)
        with self.assertRaises(RpcError) as ctx:
            rpc.call("status")
        self.assertNotIn(PASSWORD, str(ctx.exception))

    def test_invalid_json(self):
        self.srv.reply = b"this is not json"
        self._assert_error()

    def test_non_dict_answer(self):
        for reply in (b"[1, 2]", b"\"text\"", b"5", b"null"):
            self.srv.reply = reply
            self._assert_error()

    def test_error_member(self):
        self.srv.reply = json.dumps({"error": {"name": "JSONRPCError", "message": PASSWORD}, "result": True}).encode()
        self._assert_error()

    def test_timeout(self):
        self.srv.hang = True
        rpc = Rpc(StubControl(self.srv.port), timeout=0.3)
        with self.assertRaises(RpcError) as ctx:
            rpc.call("status")
        self.assertNotIn(PASSWORD, str(ctx.exception))
        self.assertEqual(len(self.srv.requests), 1)

    def test_proxy_environment_ignored(self):
        dead = free_port()
        proxy = "http://127.0.0.1:%d" % dead
        env = {"http_proxy": proxy, "HTTP_PROXY": proxy, "all_proxy": proxy, "ALL_PROXY": proxy}
        with mock.patch.dict(os.environ, env):
            os.environ.pop("no_proxy", None)
            os.environ.pop("NO_PROXY", None)
            rpc = Rpc(StubControl(self.srv.port), timeout=5.0)
            self.assertEqual(rpc.call("status"), "ok")
        self.assertEqual(len(self.srv.requests), 1)


class HelperTests(unittest.TestCase):
    def test_params_of(self):
        item = {"Parameters": [{"Name": "a", "Value": "1"}, {"Name": 5, "Value": 6}, "junk", {"Value": "x"},
                               {"Name": "novalue"}]}
        self.assertEqual(params_of(item), {"a": "1", "5": "6", "novalue": ""})
        self.assertEqual(params_of({}), {})
        self.assertEqual(params_of({"Parameters": None}), {})

    def test_same_name(self):
        self.assertTrue(same_name("Show.S01", "Show.S01"))
        self.assertTrue(same_name(u"Caf\u00e9", u"Caf\u00e9"))
        self.assertTrue(same_name(u"Caf\u00e9.S01", u"Caf\u00ea.S01"))
        self.assertTrue(same_name(u"Caf\u00e9.S01", "Caf.S01"))
        self.assertFalse(same_name("Show.S01", "Other.S02"))
        self.assertFalse(same_name(u"\u00e9\u00e8", u"\u65e5\u672c"))
        self.assertFalse(same_name(u"\u00e9", u"\u00e8" + u"\u00e0"))

    def test_target(self):
        target = Target(5, "tok", "name")
        self.assertEqual((target.nzb_id, target.token, target.name), (5, "tok", "name"))


class FakeRpc(object):
    """Serves canned queue/history and records every call."""

    def __init__(self, groups=None, history=None, accept=True):
        self.groups = groups if groups is not None else []
        self.hist = history if history is not None else []
        self.accept = accept
        self.calls = []
        self.error = None
        self.after_read = None

    def call(self, method, *params):
        self.calls.append((method,) + params)
        if self.error is not None:
            raise self.error
        if method == "listgroups":
            result = self.groups
            if self.after_read:
                self.after_read()
            return result
        if method == "history":
            return self.hist
        if method == "editqueue":
            return self.accept(params) if callable(self.accept) else self.accept
        raise AssertionError("unexpected rpc method " + method)

    def edits(self):
        return [c for c in self.calls if c[0] == "editqueue"]


def assert_only_safe_calls(calls, nzb_id=None):
    for call in calls:
        method = call[0]
        assert method in ("editqueue",) + READS, "unsafe rpc method %r" % (method,)
        if method == "editqueue":
            assert len(call) == 4, "editqueue has the wrong shape: %r" % (call,)
            _m, command, text, ids = call
            assert command in QUEUE_EDITS + HISTORY_EDITS, "unexpected command %r" % (command,)
            assert isinstance(text, str)
            assert isinstance(ids, list) and len(ids) == 1, "ids must be a one-element list: %r" % (ids,)
            assert type(ids[0]) is int and ids[0] > 0, "bad id %r" % (ids[0],)
            if nzb_id is not None:
                assert ids[0] == nzb_id, "edit for %r, expected %r" % (ids[0], nzb_id)


def entry(nzb_id=5, token="tok", name="Show.S01", kind="NZB", **extra):
    item = {"NZBID": nzb_id, "NZBName": name, "Kind": kind,
            "Parameters": [{"Name": TOKEN, "Value": token}] if token is not None else []}
    item.update(extra)
    return item


class GuardCase(unittest.TestCase):
    def make(self, groups=None, history=None, **kw):
        self.rpc = FakeRpc(groups, history, **kw)
        self.guard = QueueGuard(self.rpc)
        self.target = Target(5, "tok", "Show.S01")
        return self.guard

    def tearDown(self):
        rpc = getattr(self, "rpc", None)
        if rpc is not None:
            assert_only_safe_calls(rpc.calls)

    def refused(self, *args, **kw):
        with self.assertRaises(Refused):
            self.guard.verify(*args, **kw)
        self.assertEqual(self.rpc.edits(), [])


class ShapeTests(GuardCase):
    def test_groups_and_history_filtering(self):
        guard = self.make([entry(), "junk", 5, None, entry(6)], [entry(7), [1]])
        self.assertEqual([i["NZBID"] for i in guard.groups()], [5, 6])
        self.assertEqual([i["NZBID"] for i in guard.history()], [7])
        self.assertEqual(self.rpc.calls, [("listgroups", 0), ("history", False)])

    def test_non_list(self):
        for bad in ({"a": 1}, "x", None, 3):
            guard = self.make()
            self.rpc.groups = self.rpc.hist = bad
            with self.assertRaises(RpcError):
                guard.groups()
            with self.assertRaises(RpcError):
                guard.history()


class VerifyTests(GuardCase):
    def test_accepts_exact_match(self):
        item = entry()
        guard = self.make([entry(9, "other", "Else"), item])
        self.assertIs(guard.verify(self.target), item)

    def test_accepts_name_differing_in_non_ascii(self):
        guard = self.make([entry(name=u"Caf\u00e9.S01")])
        self.assertEqual(guard.verify(Target(5, "tok", u"Caf\u00ea.S01"))["NZBID"], 5)

    def test_name_none_skips_name_check(self):
        guard = self.make([entry(name="Anything")])
        self.assertEqual(guard.verify(Target(5, "tok", None))["NZBID"], 5)

    def test_falls_back_to_name_field(self):
        item = entry()
        del item["NZBName"]
        item["Name"] = "Show.S01"
        guard = self.make([item])
        self.assertIs(guard.verify(self.target), item)

    def test_bad_ids(self):
        guard = self.make([entry(5), entry(0), entry(-3), entry(True)])
        for bad in (0, -1, -5, None, True, "5", 5.0):
            self.refused(Target(bad, "tok", "Show.S01"))
        self.assertEqual(self.rpc.calls, [])  # refused before even reading the queue
        self.assertIsNotNone(guard)

    def test_missing_attributes(self):
        self.make([entry()])
        self.refused(object())

    def test_empty_or_missing_token(self):
        self.make([entry(token="")])
        for token in ("", None):
            self.refused(Target(5, token, "Show.S01"))

    def test_not_in_queue(self):
        self.make([entry(6)])
        self.refused(self.target)

    def test_present_twice(self):
        self.make([entry(), entry()])
        self.refused(self.target)

    def test_wrong_kind(self):
        for kind in ("URL", None, "nzb", ""):
            self.make([entry(kind=kind)])
            self.refused(self.target)

    def test_missing_kind(self):
        item = entry()
        del item["Kind"]
        self.make([item])
        self.refused(self.target)

    def test_token_mismatch(self):
        self.make([entry(token="other")])
        self.refused(self.target)

    def test_token_missing_on_entry(self):
        self.make([entry(token=None)])
        self.refused(self.target)

    def test_name_mismatch(self):
        self.make([entry(name="Different.Release")])
        self.refused(self.target)

    def test_history_accepts_and_checks_id(self):
        item = entry(ID=5)
        guard = self.make([], [item])
        self.assertIs(guard.verify(self.target, history=True), item)
        self.assertEqual(self.rpc.calls, [("history", False)])

    def test_history_without_id_field_ok(self):
        guard = self.make([], [entry()])
        self.assertEqual(guard.verify(self.target, history=True)["NZBID"], 5)

    def test_history_id_differs(self):
        self.make([], [entry(ID=99)])
        self.refused(self.target, history=True)

    def test_history_does_not_look_at_queue(self):
        self.make([entry()], [])
        self.refused(self.target, history=True)

    def test_queue_does_not_look_at_history(self):
        self.make([], [entry()])
        self.refused(self.target)

    def test_rpc_error_propagates(self):
        self.make([entry()])
        self.rpc.error = RpcError("down")
        with self.assertRaises(RpcError):
            self.guard.verify(self.target)
        with self.assertRaises(RpcError):
            self.guard.edit(self.target, "GroupResume")
        self.assertEqual(self.rpc.edits(), [])


class EditTests(GuardCase):
    def test_forbidden_commands_in_queue(self):
        self.make([entry()])
        bad = ["FileDelete", "GroupFinalDelete", "GroupPause", "GroupDupeDelete", "PausePost", "HistoryDelete",
               "GroupMoveTop", "MadeUpCommand", "", "groupresume", "HistoryMarkBad", "HistorySetParameter"]
        for command in bad:
            with self.assertRaises(Refused):
                self.guard.edit(self.target, command)
            with self.assertRaises(Refused):
                self.guard.edit_many(self.target, [("GroupResume", ""), (command, "")])
        self.assertEqual(self.rpc.calls, [])

    def test_forbidden_commands_in_history(self):
        self.make([], [entry()])
        bad = ["FileDelete", "GroupFinalDelete", "HistoryDelete", "HistoryFinalDelete", "GroupDelete",
               "GroupResume", "GroupPauseExtraPars", "GroupSetParameter", "PostProcess", "HistoryReturn"]
        for command in bad:
            with self.assertRaises(Refused):
                self.guard.edit_history(self.target, command)
        self.assertEqual(self.rpc.calls, [])

    def test_allowed_commands_exact(self):
        self.assertEqual(QUEUE_EDITS, ("GroupResume", "GroupPauseExtraPars", "GroupSetParameter", "GroupDelete"))
        self.assertEqual(HISTORY_EDITS, ("HistoryMarkBad", "HistorySetParameter"))

    def test_each_allowed_queue_command(self):
        self.make([entry()])
        for command in QUEUE_EDITS:
            self.assertTrue(self.guard.edit(self.target, command, "t"))
        self.assertEqual(self.rpc.edits(), [("editqueue", c, "t", [5]) for c in QUEUE_EDITS])

    def test_each_allowed_history_command(self):
        self.make([], [entry()])
        for command in HISTORY_EDITS:
            self.assertTrue(self.guard.edit_history(self.target, command, "t"))
        self.assertEqual(self.rpc.edits(), [("editqueue", c, "t", [5]) for c in HISTORY_EDITS])
        assert_only_safe_calls(self.rpc.calls, 5)

    def test_empty_command_list_reads_nothing(self):
        self.make([entry()])
        self.assertEqual(self.guard.edit_many(self.target, []), [])
        self.assertEqual(self.guard.edit_many(Target(0, "", ""), [], True), [])
        self.assertEqual(self.rpc.calls, [])

    def test_unverified_target_sends_nothing(self):
        self.make([entry(token="other")])
        with self.assertRaises(Refused):
            self.guard.edit(self.target, "GroupResume")
        with self.assertRaises(Refused):
            self.guard.edit_history(Target(5, "tok", "Show.S01"), "HistoryMarkBad")
        self.assertEqual(self.rpc.edits(), [])

    def test_queue_read_right_before_edits(self):
        self.make([entry()])
        self.guard.edit(self.target, "GroupResume")
        self.guard.edit(self.target, "GroupDelete")
        self.assertEqual([c[0] for c in self.rpc.calls],
                         ["listgroups", "editqueue", "listgroups", "editqueue"])
        assert_only_safe_calls(self.rpc.calls, 5)

    def test_several_commands_verified_once(self):
        self.make([entry()])
        result = self.guard.edit_many(self.target, [("GroupResume", ""), ("GroupPauseExtraPars", ""),
                                                     ("GroupSetParameter", "a=b")])
        self.assertEqual(result, [True, True, True])
        self.assertEqual([c[0] for c in self.rpc.calls], ["listgroups"] + ["editqueue"] * 3)
        assert_only_safe_calls(self.rpc.calls, 5)

    def test_returns_nzbget_boolean(self):
        self.make([entry()], [entry()], accept=False)
        self.assertIs(self.guard.edit(self.target, "GroupResume"), False)
        self.assertIs(self.guard.edit_history(self.target, "HistoryMarkBad"), False)
        self.rpc.accept = 1
        self.assertIs(self.guard.edit(self.target, "GroupResume"), True)
        self.rpc.accept = None
        self.assertIs(self.guard.edit_history(self.target, "HistoryMarkBad"), False)

    def test_download_vanishes_between_edits(self):
        guard = self.make([entry()])
        self.assertTrue(guard.edit(self.target, "GroupResume"))
        self.rpc.groups = []
        with self.assertRaises(Refused):
            guard.edit(self.target, "GroupDelete")
        self.assertEqual(len(self.rpc.edits()), 1)

    def test_id_reused_by_other_download_is_refused(self):
        guard = self.make([entry()])
        self.assertTrue(guard.edit(self.target, "GroupResume"))
        self.rpc.groups = [entry(token="someone-else", name="Other")]
        with self.assertRaises(Refused):
            guard.edit(self.target, "GroupDelete")
        self.assertEqual(len(self.rpc.edits()), 1)

    def test_rpc_error_during_edit_propagates(self):
        self.make([entry()])

        def boom(params):
            raise RpcError("nope")
        self.rpc.accept = boom
        with self.assertRaises(RpcError):
            self.guard.edit(self.target, "GroupResume")

    def test_text_is_string(self):
        self.make([entry()])
        self.guard.edit(self.target, "GroupSetParameter", 42)
        self.assertEqual(self.rpc.edits(), [("editqueue", "GroupSetParameter", "42", [5])])


class ResumeTests(GuardCase):
    def test_resume_sends_both(self):
        self.make([entry()])
        self.assertTrue(self.guard.resume(self.target))
        self.assertEqual(self.rpc.edits(), [("editqueue", "GroupResume", "", [5]),
                                            ("editqueue", "GroupPauseExtraPars", "", [5])])

    def test_resume_without_extra_pars(self):
        self.make([entry()])
        self.assertTrue(self.guard.resume(self.target, pause_extra_pars=False))
        self.assertEqual(self.rpc.edits(), [("editqueue", "GroupResume", "", [5])])

    def test_resume_rejected(self):
        self.make([entry()], accept=False)
        self.assertIs(self.guard.resume(self.target), False)
        assert_only_safe_calls(self.rpc.calls, 5)

    def test_resume_of_foreign_download_refused(self):
        self.make([entry(token="x")])
        with self.assertRaises(Refused):
            self.guard.resume(self.target)
        self.assertEqual(self.rpc.edits(), [])


class SetParametersTests(GuardCase):
    def test_sends_name_value(self):
        self.make([entry()])
        self.guard.set_parameters(self.target, {PARAMETERS[1]: "held", PARAMETERS[2]: 123})
        self.assertEqual(self.rpc.edits(), [("editqueue", "GroupSetParameter", PARAMETERS[1] + "=held", [5]),
                                            ("editqueue", "GroupSetParameter", PARAMETERS[2] + "=123", [5])])
        self.assertEqual([c[0] for c in self.rpc.calls].count("listgroups"), 1)

    def test_accepts_pairs(self):
        self.make([entry()])
        self.guard.set_parameters(self.target, [(PARAMETERS[0], "v")])
        self.assertEqual(len(self.rpc.edits()), 1)

    def test_all_declared_parameters_allowed(self):
        self.make([entry()])
        self.guard.set_parameters(self.target, dict((name, "1") for name in PARAMETERS))
        self.assertEqual(len(self.rpc.edits()), len(PARAMETERS))

    def test_foreign_name_refused_nothing_sent(self):
        self.make([entry()])
        for name in ("*Unpack:Password", "Other", "completer_token", "", PARAMETERS[0] + "x", "*Unpack:"):
            with self.assertRaises(Refused):
                self.guard.set_parameters(self.target, {name: "x"})
        with self.assertRaises(Refused):
            self.guard.set_parameters(self.target, [(PARAMETERS[1], "ok"), ("Other", "bad")])
        self.assertEqual(self.rpc.calls, [])

    def test_control_characters_stripped(self):
        self.make([entry()])
        evil = "ok\r\n*Unpack:Password=x\x00\t\x1b[0m"
        self.guard.set_parameters(self.target, {PARAMETERS[3]: evil})
        text = self.rpc.edits()[0][2]
        self.assertEqual(text, PARAMETERS[3] + "=ok*Unpack:Password=x[0m")
        self.assertFalse(any(ch in text for ch in "\r\n\t\x00\x1b"))
        self.assertEqual(len(self.rpc.edits()), 1)

    def test_history(self):
        self.make([], [entry()])
        self.guard.set_parameters(self.target, {PARAMETERS[1]: "failed"}, history=True)
        self.assertEqual(self.rpc.edits(), [("editqueue", "HistorySetParameter", PARAMETERS[1] + "=failed", [5])])
        self.assertEqual([c[0] for c in self.rpc.calls], ["history", "editqueue"])

    def test_foreign_download_refused(self):
        self.make([entry(token="x")])
        with self.assertRaises(Refused):
            self.guard.set_parameters(self.target, {PARAMETERS[1]: "v"})
        self.assertEqual(self.rpc.edits(), [])

    def test_empty_values_send_nothing(self):
        self.make([entry()])
        self.assertEqual(self.guard.set_parameters(self.target, {}), [])
        self.assertEqual(self.rpc.calls, [])


class StaticGuardTests(unittest.TestCase):
    FORBIDDEN = ("FileDelete", "pausedownload", "GroupDupeDelete", "GroupFinalDelete")

    def test_forbidden_strings_absent_from_production_code(self):
        folder = os.path.dirname(os.path.abspath(rpc_module.__file__))
        files = sorted(glob.glob(os.path.join(folder, "*.py")))
        self.assertTrue(files)
        offenders = []
        for path in files:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            for word in self.FORBIDDEN:
                if word in text:
                    offenders.append("%s mentions %s" % (os.path.basename(path), word))
        self.assertEqual(offenders, [], "Completer must never be able to send these NZBGet commands: %s"
                         % "; ".join(offenders))


if __name__ == "__main__":
    unittest.main()
