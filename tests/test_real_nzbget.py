"""Completer against a REAL NZBGet (opt-in).

Everything else in this suite talks to the fake NZBGet (``nzbgetsim``), which was written from NZBGet's source.
This module is the proof that the fake is right where Completer depends on it: it starts a throw-away NZBGet,
with Completer installed as an extension and the fake news server (``fakenntp``) as its only news server, and
drives it the way Sonarr and Radarr do, through the JSON-RPC interface.

It is skipped unless the environment variable ``COMPLETER_NZBGET`` names an NZBGet binary (26.0 or newer)::

    COMPLETER_NZBGET=/path/to/nzbget python3 -m unittest tests.test_real_nzbget

What it does, in short
    * Every test gets its own sandbox (a few share one when they only read): a temporary directory that holds
      NZBGet's configuration, queue, download folders, scripts folder (with a copy of ``Completer/``) and log, plus
      a free port on 127.0.0.1. NZBGet runs in the foreground (``-s``) in its own process group; it and everything
      it started is killed when the test ends.
    * NZBGet starts the extension through its ``#!/usr/bin/env python3`` line, as it does on a real machine. The
      sandbox puts a ``python3`` in front of the others on ``PATH`` that only writes the ``NZB*`` environment of the
      run into the sandbox (``dump/``), optionally waits while a "gate" file exists, and then replaces itself with the
      real interpreter, so Completer runs unchanged. The gates are how the tests look at a download while it is on
      hold, between the scan script and the queue script.
    * The sandbox's copy of the manifest differs from the shipped one in one respect only: the scheduled task runs
      when NZBGet starts and not every five minutes, so that no timer fires in the middle of a test (the tests
      that are about the timer say so, and the first test reads the shipped manifest unchanged).
    * Tests assert on state (queue and history as the RPC reports them, parameters, which files are paused, which
      articles were asked for), not on NZBGet's wording. The few places where NZBGet's log is read say so.
    * Downloads are tiny yEnc articles served by ``FakeNews.bodies`` (``nzbtools.Post.articles``), so a released
      download really completes and Sonarr's and Radarr's judgement of the history entry can be checked.

The same behaviour is also covered, against the fake NZBGet and with a simulated clock, by the other test modules;
where this module found a difference between the two, the fake was changed or the difference is written down in
the test that shows it.
"""

import base64
import collections
import http.client
import http.server
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PACKAGE = os.path.normpath(os.path.join(HERE, "..", "Completer"))
for _path in (PACKAGE, HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

_keep_bytecode = sys.dont_write_bytecode
sys.dont_write_bytecode = True  # importing Completer must not leave a __pycache__ in the repository
try:
    import fakenntp  # noqa: E402
    import nzbgetsim  # noqa: E402
    import nzbtools  # noqa: E402
    from completer import app, locks, log, settings  # noqa: E402
finally:
    sys.dont_write_bytecode = _keep_bytecode

BINARY = os.environ.get("COMPLETER_NZBGET", "").strip()
CONTROL_USER = "real-user"
CONTROL_PASSWORD = "real-pass-4711"
ARTICLE_BYTES = 40  # every article of the posts below holds this many bytes (see nzbtools.Post.articles)
WAIT = 90.0         # seconds a test waits for NZBGet or Completer; the machine may be busy, the tests are not timed


def _unavailable():
    """Why the tests cannot run here, or None."""
    if os.name != "posix":
        return "the tests start NZBGet as a POSIX process"
    if not BINARY:
        return "set COMPLETER_NZBGET to the path of an nzbget binary (26.0 or newer) to run Completer against the real NZBGet"
    if not (os.path.isfile(BINARY) and os.access(BINARY, os.X_OK)):
        return "COMPLETER_NZBGET does not name an executable file"
    try:
        said = subprocess.run([BINARY, "--version"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=60).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return "COMPLETER_NZBGET does not run"
    found = re.search(r"nzbget version:\s*(\d+)\.", said)
    if not found:
        return "COMPLETER_NZBGET does not look like NZBGet (--version says nothing useful)"
    if int(found.group(1)) < 26:
        return "Completer needs NZBGet 26.0 or newer (extensions), COMPLETER_NZBGET is version %s" % found.group(1)
    return None


UNAVAILABLE = _unavailable()
needs_nzbget = unittest.skipIf(UNAVAILABLE is not None, UNAVAILABLE or "")

# The script that stands in for ``python3`` on PATH inside the sandbox. It has to be a separate file: NZBGet
# starts Completer's main.py, whose first line is ``#!/usr/bin/env python3``, so ``env`` finds this one first.
SPY = r'''
import json
import os
import re
import sys
import time

ROOT = %(root)r
REAL = %(python)r
env = os.environ
if env.get("NZBCP_COMMAND"):
    kind, name = "command", env["NZBCP_COMMAND"]
elif "NZBNP_FILENAME" in env:
    kind, name = "scan", re.sub(r"\.nzb$", "", env.get("NZBNP_NZBNAME", ""))
elif env.get("NZBNA_EVENT"):
    kind, name = "queue", env.get("NZBNA_NZBNAME", "")
elif "NZBSP_TASKID" in env:
    kind, name = "sweep", ""
else:
    kind, name = "other", ""
record = {"kind": kind, "name": name, "argv": sys.argv[1:], "pid": os.getpid(), "parent": os.getppid(),
          "env": dict((key, value) for key, value in env.items() if key.startswith("NZB"))}
path = os.path.join(ROOT, "dump", "%%s.%%d.%%d" %% (kind, time.time_ns(), os.getpid()))
with open(path + ".tmp", "w") as handle:
    json.dump(record, handle)
os.replace(path + ".tmp", path + ".json")
end = time.time() + 120
while time.time() < end and os.getppid() != 1 and any(
        os.path.exists(os.path.join(ROOT, "gate", word)) for word in (kind, kind + "." + name)):
    time.sleep(0.05)
try:
    with open(os.path.join(ROOT, "proxy")) as handle:
        proxy_port = handle.read().strip()
except OSError:
    proxy_port = ""
if proxy_port:  # Completer talks to the recording proxy in front of NZBGet's control port
    for key in list(env):
        if key.upper() == "NZBOP_CONTROLPORT":
            os.environ[key] = proxy_port
os.execv(REAL, [REAL] + sys.argv[1:])
'''

Result = collections.namedtuple("Result", "accepted ending messages")


def free_port():
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]
    finally:
        probe.close()


def params_of(entry):
    """The parameters of a queue or history entry as a dictionary."""
    return dict((item["Name"], item["Value"]) for item in (entry or {}).get("Parameters", []))


def shipped_manifest():
    with open(os.path.join(PACKAGE, "manifest.json"), encoding="utf-8") as handle:
        return json.load(handle)


def files_under(folder):
    """Every file below a folder, as relative paths."""
    found = []
    for base, _folders, names in os.walk(folder):
        found.extend(os.path.relpath(os.path.join(base, name), folder) for name in names)
    return sorted(found)


def process_runs(pid, parent):
    """Whether process ``pid`` still runs as a child of ``parent`` (so the number was not given to someone else)."""
    try:
        with open("/proc/%d/stat" % pid) as handle:
            fields = handle.read().rsplit(")", 1)[1].split()
    except OSError:
        if os.path.isdir("/proc/self"):
            return False  # /proc exists and has no such process
        try:
            os.kill(pid, 0)  # no /proc on this system: all that can be asked is whether the number is in use
        except ProcessLookupError:
            return False
        except OSError:
            return True
        return True
    return fields[0] not in ("Z", "X") and int(fields[1]) == parent


class _ProxyHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        status, reply = self.server.proxy.pass_on(body, self.headers.get("Authorization", ""))
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)


class RpcProxy(object):
    """Stands in front of NZBGet's control port for the scripts it starts and writes down every call they make.

    The ``python3`` stand-in tells the scripts that NZBGet listens here. Every call is passed on unchanged and the
    answer comes back unchanged. For a change to a download (``editqueue``) the proxy also asks the real NZBGet
    whether that download carries Completer's token at that moment, which is what Completer promises to check
    before it touches anything.
    """

    def __init__(self, port, ask):
        """``port``: NZBGet's real control port. ``ask(method, *params)``: a call to the real NZBGet."""
        self.port = port
        self.ask = ask
        self.calls = []
        self._lock = threading.Lock()
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ProxyHandler)
        self._server.daemon_threads = True
        self._server.proxy = self
        self.listening = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(5)

    def pass_on(self, body, authorization):
        try:
            request = json.loads(body.decode("utf-8"))
            method, params = request["method"], request.get("params") or []
        except (ValueError, KeyError, TypeError, AttributeError):
            method, params = "?", []
        note = {"method": method, "params": params, "token": None}
        if isinstance(method, str) and method.lower() == "editqueue":
            note["token"] = self._carries_token(params)
        with self._lock:
            self.calls.append(note)
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        try:
            connection.request("POST", "/jsonrpc", body, {"Content-Type": "application/json",
                                                          "Authorization": authorization})
            answer = connection.getresponse()
            return answer.status, answer.read()
        except (OSError, http.client.HTTPException):
            return 502, b"NZBGet does not answer"
        finally:
            connection.close()

    def _carries_token(self, params):
        """True or False for a change that names exactly one download; None when it names none or several."""
        try:
            ids = params[2]
            if len(ids) != 1 or type(ids[0]) is not int:
                return None
            for entries in (self.ask("listgroups", 0), self.ask("history", False)):
                for entry in entries:
                    if entry["NZBID"] == ids[0]:
                        return bool(params_of(entry).get(settings.TOKEN))
        except (IndexError, KeyError, TypeError, ValueError, AssertionError, OSError):
            return None
        return False

    def snapshot(self):
        with self._lock:
            return [dict(note) for note in self.calls]


def arr_status(item):
    """How Sonarr and Radarr judge an entry of NZBGet's history: a port of ``GetHistory`` in their ``Nzbget.cs``.

    "failed", "warning", "completed", or None when the entry is skipped (deleted by hand and not marked bad).
    Written separately from ``nzbgetsim.sonarr_status``; the tests compare the two on real entries.
    """
    success = ("SUCCESS", "NONE")
    delete_failed = ("HEALTH", "DUPE", "SCAN", "COPY", "BAD")
    if item["DeleteStatus"] == "MANUAL":
        return "failed" if item["MarkStatus"] == "BAD" else None
    status = "completed"
    if item["ParStatus"] not in success:
        status = "failed"
    if item["UnpackStatus"] == "SPACE":
        status = "warning"
    elif item["UnpackStatus"] not in success:
        status = "failed"
    if item["MoveStatus"] not in success:
        status = "warning"
    if item["ScriptStatus"] not in success:
        status = "failed"
    if item["DeleteStatus"] not in success and item["DeleteStatus"].strip():
        status = "failed" if item["DeleteStatus"] in delete_failed else "warning"
    return status


def good_post(tag="alpha", **kwargs):
    """Two data files of five articles and two par2 files of three: every article will exist."""
    return nzbtools.make_post(tag, data_files=2, segments=5, par_files=2, par_segments=3,
                              size=ARTICLE_BYTES, par_size=ARTICLE_BYTES, **kwargs)


def bad_post(tag="beta", **kwargs):
    """Two data files of ten articles and no par2: only three of the twenty will exist (see ``serve``)."""
    return nzbtools.make_post(tag, data_files=2, segments=10, par_files=0, par_segments=1,
                              size=ARTICLE_BYTES, par_size=ARTICLE_BYTES, **kwargs)


class Sandbox(object):
    """One throw-away NZBGet: its own directories, configuration and port, with Completer installed."""

    def __init__(self, news, mode="Enforce", manifest=None, options=(), periodic=False, extensions="Completer"):
        """``mode``: Completer's Mode option (None: not in the configuration, so Completer's default applies).

        ``manifest``: a function that changes the copy of manifest.json that is installed (never the original).
        ``options``: more lines for nzbget.conf. ``extensions``: NZBGet's global Extensions option.
        ``periodic``: when the scheduled task runs. False: only when NZBGet starts (so no timer fires in the middle
        of a test); True: the schedule of the shipped manifest, every five minutes; a string: that ``taskTime``.
        """
        self.news = news
        self.mode = mode
        self.options = list(options)
        self.extensions = extensions
        self.port = free_port()
        self.root = tempfile.mkdtemp(prefix="completer-real-")
        self.proc = None
        self.proxy = None
        self._out = None
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            self._install(manifest, periodic)
        except BaseException:
            self.close()
            raise

    def _install(self, manifest, periodic):
        for sub in ("dst", "inter", "nzb", "queue", "tmp", "scripts", "bin", "dump", "gate"):
            os.mkdir(self.path(sub))
        installed = self.path("scripts", "Completer")
        shutil.copytree(PACKAGE, installed, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        manifest_path = os.path.join(installed, "manifest.json")
        with open(manifest_path, encoding="utf-8") as handle:
            data = json.load(handle)
        if periodic is False:
            data["taskTime"] = "*"
        elif periodic is not True:
            data["taskTime"] = periodic
        if manifest is not None:
            manifest(data)
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
        with open(self.path("bin", "spy.py"), "w") as handle:
            handle.write(SPY % {"root": self.root, "python": sys.executable})
        shim = self.path("bin", "python3")
        with open(shim, "w") as handle:
            handle.write("#!/bin/sh\nexec %s %s \"$@\"\n" % (shlex.quote(sys.executable),
                                                              shlex.quote(self.path("bin", "spy.py"))))
        os.chmod(shim, 0o755)
        with open(self.path("template.conf"), "w") as handle:
            handle.write("# nothing to offer to the settings page\n")
        self.proxy = RpcProxy(self.port, self.call)
        with open(self.path("proxy"), "w") as handle:
            handle.write(str(self.proxy.listening))
        self.write_conf()

    # ------------------------------------------------------------ files

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def write_conf(self):
        server = self.news
        lines = [
            "MainDir=" + self.root,
            "DestDir=" + self.path("dst"),
            "InterDir=" + self.path("inter"),
            "NzbDir=" + self.path("nzb"),
            "QueueDir=" + self.path("queue"),
            "TempDir=" + self.path("tmp"),
            "ScriptDir=" + self.path("scripts"),
            "LogFile=" + self.path("nzbget.log"),
            "LockFile=" + self.path("nzbget.lock"),
            "ConfigTemplate=" + self.path("template.conf"),
            "WebDir=",
            "WriteLog=append",
            "OutputMode=log",
            "DetailTarget=both",
            "ControlIP=127.0.0.1",
            "ControlPort=%d" % self.port,
            "ControlUsername=" + CONTROL_USER,
            "ControlPassword=" + CONTROL_PASSWORD,
            "SecureControl=no",
            "UpdateCheck=none",
            "CertCheck=no",
            "DiskSpace=0",
            "SystemHealthCheck=no",
            "NzbDirInterval=0",
            "DupeCheck=yes",
            "HealthCheck=park",
            "ParCheck=auto",
            "ParRename=no",
            "DirectRename=no",
            "Unpack=no",
            "Server1.Active=yes",
            "Server1.Name=fake",
            "Server1.Level=0",
            "Server1.Host=127.0.0.1",
            "Server1.Port=%d" % server.port,
            "Server1.Encryption=no",
            "Server1.Username=" + (server.username or ""),
            "Server1.Password=" + (server.password or ""),
            "Server1.Connections=4",
            "Server1.Retention=0",
            "Extensions=" + self.extensions,
        ]
        if self.mode is not None:
            lines.append("Completer:Mode=" + self.mode)
        lines.extend(self.options)
        with open(self.path("nzbget.conf"), "w") as handle:
            handle.write("\n".join(lines) + "\n")

    def log_text(self):
        try:
            with open(self.path("nzbget.log"), encoding="utf-8", errors="replace") as handle:
                return handle.read()
        except OSError:
            return ""

    def log_lines(self):
        """NZBGet's own log as a list of ``(kind, text)``."""
        lines = []
        for line in self.log_text().splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3:
                lines.append((parts[1], parts[2]))
        return lines

    def log_index(self, pattern, start=0):
        """Index of the first line of NZBGet's log from ``start`` on that matches the regular expression; -1 if none."""
        for number, (_kind, text) in enumerate(self.log_lines()):
            if number >= start and re.search(pattern, text):
                return number
        return -1

    def dumps(self, kind, name=None):
        """What the scripts of that kind found in their environment, oldest first: dictionaries with ``env``."""
        found = []
        for entry in sorted(os.listdir(self.path("dump"))):
            if entry.startswith(kind + ".") and entry.endswith(".json"):
                with open(self.path("dump", entry), encoding="utf-8") as handle:
                    record = json.load(handle)
                if name is None or record["name"] == name:
                    found.append(record)
        return found

    def gate(self, kind, name=None):
        """Make the scripts of that kind (of the download of that name) wait before they start Completer."""
        with open(self.path("gate", kind if name is None else "%s.%s" % (kind, name)), "w"):
            pass

    def ungate(self, kind, name=None):
        try:
            os.remove(self.path("gate", kind if name is None else "%s.%s" % (kind, name)))
        except OSError:
            pass

    # ------------------------------------------------------------ the process

    def start(self, paused=True, swept=True):
        """Start NZBGet and wait until it answers and (unless ``swept`` is False) the check it runs at start is done.

        ``paused``: start with downloading paused (``-P``), so that a released download stays where it is.
        """
        self.write_conf()
        before = len(self.dumps("sweep"))
        self._out = open(self.path("stdout.txt"), "ab")
        environment = {"PATH": self.path("bin") + os.pathsep + os.environ.get("PATH", os.defpath),
                       "HOME": self.root, "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
        if "TZ" in os.environ:  # the tests that wait for a minute to come read the same clock as NZBGet does
            environment["TZ"] = os.environ["TZ"]
        command = [BINARY, "-c", self.path("nzbget.conf"), "-s"] + (["-P"] if paused else [])
        self.proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=self._out, stderr=subprocess.STDOUT,
                                     cwd=self.root, env=environment, start_new_session=True)
        self.wait_for(self._answers, "NZBGet to answer its JSON-RPC interface")
        if swept:
            self.wait_for(lambda: len(self.dumps("sweep")) > before, "the scheduled check that NZBGet runs at start")
            self.wait_idle()

    def _answers(self):
        if self.proc.poll() is not None:
            raise AssertionError("NZBGet exited with status %s\n%s" % (self.proc.returncode, self.tail()))
        try:
            self.call("version")
        except (OSError, ValueError):
            return False
        return True

    def stop(self):
        """End NZBGet and everything it started; the files stay."""
        if self.proc is not None:
            group = self.proc.pid
            for number in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(group, number)
                except OSError:
                    pass
                try:
                    self.proc.wait(20 if number == signal.SIGTERM else 10)
                    break
                except subprocess.TimeoutExpired:
                    continue
            try:
                os.killpg(group, signal.SIGKILL)  # whatever outlived NZBGet itself (a script that was running)
            except OSError:
                pass
            self.proc = None
        if self._out is not None:
            self._out.close()
            self._out = None

    def restart(self, paused=True, swept=True):
        """Stop NZBGet and start it again on the same files (the queue is kept, as it is across a real restart)."""
        self.stop()
        self.start(paused, swept)

    def close(self):
        self.stop()
        if self.proxy is not None:
            self.proxy.close()
            self.proxy = None
        shutil.rmtree(self.root, ignore_errors=True)

    def tail(self, count=40):
        """The end of NZBGet's log, for the message of a test that timed out."""
        text = "\n".join("%s %s" % pair for pair in self.log_lines()[-count:])
        return "NZBGet's log ends with:\n" + text

    # ------------------------------------------------------------ JSON-RPC

    def call(self, method, *params):
        body = json.dumps({"method": method, "params": list(params)}).encode("utf-8")
        request = urllib.request.Request("http://127.0.0.1:%d/jsonrpc" % self.port, data=body)
        request.add_header("Content-Type", "application/json")
        login = base64.b64encode(("%s:%s" % (CONTROL_USER, CONTROL_PASSWORD)).encode("utf-8")).decode("ascii")
        request.add_header("Authorization", "Basic " + login)
        with self._opener.open(request, timeout=30) as reply:
            answer = json.loads(reply.read().decode("utf-8", "replace"))
        if answer.get("error"):
            raise AssertionError("NZBGet refused %s: %r" % (method, answer["error"]))
        return answer.get("result")

    def append(self, name, post, paused=False, category="", params=()):
        """Add a download the way Sonarr does (``append``); returns NZBGet's id for it."""
        xml = post if isinstance(post, str) else post.xml
        flat = []
        for key, value in params:
            flat.extend((key, value))
        nzb_id = self.call("append", name, base64.b64encode(xml.encode("utf-8")).decode("ascii"), category, 0,
                           False, paused, "", 0, "SCORE", False, *flat)
        if not isinstance(nzb_id, int) or nzb_id <= 0:
            raise AssertionError("append gave %r\n%s" % (nzb_id, self.tail()))
        return nzb_id

    def edit(self, command, text, *ids):
        """One ``editqueue`` command, as a client (not Completer) sends it."""
        return self.call("editqueue", command, text, list(ids))

    # ------------------------------------------------------------ what Completer asked of NZBGet (through the proxy)

    def rpc_calls(self):
        """Every call the scripts made to NZBGet so far: dictionaries with ``method``, ``params`` and ``token``."""
        return self.proxy.snapshot()

    def edits(self):
        """The changes the scripts asked for, as ``(command, text, ids, carried a token)``, oldest first."""
        return [(note["params"][0], note["params"][1], note["params"][2], note["token"])
                for note in self.rpc_calls() if str(note["method"]).lower() == "editqueue"]

    def violations(self, allowed_ids=None):
        """What the scripts asked of NZBGet that Completer promises never to do; empty when all is well.

        The promises: it uses only the four calls it needs; every change names exactly one download by its number,
        is one of Completer's six edit commands, is aimed at a download that carries Completer's token at that
        moment, and writes nothing but ``Completer_*`` parameters. ``allowed_ids``: the only downloads that may be
        touched (None: any).
        """
        problems = []
        for note in self.rpc_calls():
            method, params = note["method"], note["params"]
            if str(method).lower() not in ("listgroups", "history", "status", "editqueue"):
                problems.append("a script called %r, which Completer never uses" % (method,))
                continue
            if str(method).lower() != "editqueue":
                continue
            if len(params) != 3 or not isinstance(params[0], str) or not isinstance(params[1], str) \
                    or not isinstance(params[2], list):
                problems.append("editqueue was called with %r" % (params,))
                continue
            command, text, ids = params
            if len(ids) != 1 or type(ids[0]) is not int or ids[0] <= 0:
                problems.append("%s names %r, not exactly one download" % (command, ids))
                continue
            if command not in nzbgetsim.GROUP_COMMANDS + nzbgetsim.HISTORY_COMMANDS:
                problems.append("%s is not a command Completer uses" % command)
            if note["token"] is not True:
                problems.append("%s on download %d, which carried no Completer token at that moment" % (command, ids[0]))
            if command.endswith("SetParameter") and not re.match(r"^Completer_[A-Za-z]+=", text):
                problems.append("%s writes %r, which is not a parameter of Completer" % (command, text[:40]))
            if allowed_ids is not None and ids[0] not in allowed_ids:
                problems.append("%s touches download %d, outside %r" % (command, ids[0], sorted(allowed_ids)))
        return problems

    def beat(self, age=0):
        """Write the heartbeat file as if the scheduled check had reported ``age`` seconds ago."""
        return locks.Heartbeat(self.path("tmp")).beat(time.time() - age)

    def groups(self):
        return self.call("listgroups", 0)

    def group(self, nzb_id):
        for entry in self.groups():
            if entry["NZBID"] == nzb_id:
                return entry
        return None

    def files(self, nzb_id):
        """``{file name: paused}`` of a download in the queue."""
        return dict((item["Filename"], item["Paused"]) for item in self.call("listfiles", 0, 0, nzb_id))

    def history(self):
        return self.call("history", False)

    def history_item(self, nzb_id):
        for entry in self.history():
            if entry["NZBID"] == nzb_id:
                return entry
        return None

    def snapshot(self, *ids):
        """What NZBGet shows about queued downloads, for "nothing was done to these" checks."""
        found = {}
        for nzb_id in ids:
            entry = self.group(nzb_id)
            found[nzb_id] = None if entry is None else (
                entry["Status"], entry["NZBName"], entry["Category"], sorted(params_of(entry).items()),
                sorted(self.files(nzb_id).items()))
        return found

    def run_command(self, name):
        """Press one of the extension's buttons (NZBGet's ``startscript``).

        Returns ``Result(accepted, ending, messages)``: ``ending`` is what NZBGet said about how the command ended
        (``"successful"`` for exit code 93, ``"failed"`` for 94); ``messages`` are the lines the run added to the
        command log, each ``(kind, text)``.
        """
        before = max([item["ID"] for item in self.call("logscript", 0, 1000)] or [0])
        accepted = self.call("startscript", "Completer", name, "")
        marker = "Script Completer with command %s " % name

        def finished():
            fresh = self.call("logscript", before + 1, 0)
            return fresh if any(item["Text"].startswith(marker) for item in fresh) else None

        fresh = self.wait_for(finished, "the command %s to end" % name)
        ending = [item["Text"][len(marker):] for item in fresh if item["Text"].startswith(marker)][-1]
        return Result(accepted, ending, [(item["Kind"], item["Text"]) for item in fresh])

    # ------------------------------------------------------------ waiting

    def wait_for(self, check, what, timeout=WAIT):
        """Poll until ``check()`` is true (its value is returned); fail with the end of NZBGet's log otherwise."""
        end = time.monotonic() + timeout
        while True:
            value = check()
            if value:
                return value
            if time.monotonic() > end:
                raise AssertionError("gave up waiting for %s\n%s" % (what, self.tail()))
            time.sleep(0.1)

    def wait_for_dump(self, kind, name=None, count=1):
        return self.wait_for(lambda: len(self.dumps(kind, name)) >= count and self.dumps(kind, name),
                             "a %s script run%s" % (kind, "" if name is None else " for " + name))

    def wait_idle(self):
        """Wait until no script that NZBGet started is running or waiting to run."""
        def idle():
            if self.call("status")["QueueScriptCount"]:
                return False
            for kind in ("scan", "queue", "sweep", "command", "other"):
                for record in self.dumps(kind):
                    if record["parent"] == self.proc.pid and process_runs(record["pid"], record["parent"]):
                        return False
            return True

        self.wait_for(idle, "every script to end")

    def wait_for_state(self, nzb_id, state):
        return self.wait_for(lambda: params_of(self.group(nzb_id)).get(settings.STATE) == state and self.group(nzb_id),
                             "Completer_State=%s on download %d" % (state, nzb_id))

    def wait_for_history(self, nzb_id):
        return self.wait_for(lambda: self.history_item(nzb_id), "download %d to reach the history" % nzb_id)


class RealCase(unittest.TestCase):
    """Helpers shared by the tests: a news server and a started sandbox, both cleaned up when the test ends."""

    def news_with(self, *posts, **switches):
        """A news server that has every article of the ``posts`` and can send them."""
        news = fakenntp.FakeNews(**switches).start()
        self.addCleanup(news.stop)
        for post in posts:
            self.serve(news, post)
        return news

    @staticmethod
    def serve(news, post, only=None):
        """Give the news server the articles of a post (``only``: just the first that many ids)."""
        ids = post.all_ids if only is None else post.all_ids[:only]
        news.have.update(ids)
        news.bodies.update(post.articles(ARTICLE_BYTES))

    def sandbox(self, news, paused=True, swept=True, start=True, **kwargs):
        """A sandbox (``kwargs`` go to :class:`Sandbox`), started unless ``start`` is False.

        When the test ends, every change the scripts asked of NZBGet is checked against Completer's promises.
        """
        box = Sandbox(news, **kwargs)
        self.addCleanup(box.close)
        self.addCleanup(self.assert_promises_kept, box)
        if start:
            box.start(paused=paused, swept=swept)
        return box

    def assert_promises_kept(self, box):
        if box.proxy is not None:
            self.assertEqual(box.violations(), [])

    def tearDown(self):
        log.forget_secrets()  # the tests that run Completer's own code on NZBGet's environment register passwords


HELD_FILES = {"data.part01.rar": True, "data.part02.rar": True, "data.par2": True, "data.vol001+01.par2": True}
RELEASED_FILES = {"data.part01.rar": False, "data.part02.rar": False, "data.par2": False, "data.vol001+01.par2": True}
OWN = ("Completer_State", "Completer_Checks", "Completer_Verdict", "Completer_Info")


def no_queue_script(data):
    """The manifest of the sweep tests: NZBGet has no event to run the queue script for, so it never runs."""
    data["queueEvents"] = "URL_COMPLETED"


def source_texts():
    """The text of every module of Completer."""
    folder = os.path.join(PACKAGE, "completer")
    found = []
    for name in sorted(os.listdir(folder)):
        if name.endswith(".py"):
            with open(os.path.join(folder, name), encoding="utf-8") as handle:
                found.append(handle.read())
    return found


def variables_read(prefix):
    """The ``<prefix>_NAME`` environment variables that the code of Completer asks for, by name."""
    found = set()
    for text in source_texts():
        found.update(re.findall(r"[\"'](%s_[A-Z]+)[\"']" % prefix, text))
    return found


def options_read():
    """The NZBGet options (``NZBOP_<name>``) that the code of Completer asks for, by name."""
    found = set()
    for text in source_texts():
        found.update(re.findall(r"nzbget_option\(\s*env,\s*[\"'](\w+)[\"']", text))
    return found


def own_params(found):
    return dict((name, found.get(name)) for name in OWN)


class SharedSandbox(unittest.TestCase):
    """A class whose tests only read, or press buttons, shares one NZBGet to keep the module fast."""

    periodic = False
    news = None
    box = None

    @classmethod
    def setUpClass(cls):
        cls.news = fakenntp.FakeNews().start()
        cls.addClassCleanup(cls.news.stop)
        cls.addClassCleanup(log.forget_secrets)
        cls.box = Sandbox(cls.news, periodic=cls.periodic)
        cls.addClassCleanup(cls.box.close)
        cls.addClassCleanup(cls.check_promises)
        cls.box.start(paused=True)

    @classmethod
    def check_promises(cls):
        problems = cls.box.violations()
        if problems:
            raise AssertionError("the scripts broke Completer's promises: %r" % (problems,))


@needs_nzbget
class ExtensionTests(SharedSandbox):
    """What NZBGet makes of the extension folder, and what it tells the scheduled task."""

    periodic = True  # the schedule of the shipped manifest, unchanged

    def test_nzbget_lists_the_extension_by_its_folder_name_with_the_options_and_commands_of_the_manifest(self):
        manifest = shipped_manifest()
        listed = self.box.call("loadextensions", False)
        names = [item["Name"] for item in listed]
        self.assertIn("Completer", names)
        self.assertNotIn("main", names)
        found = listed[names.index("Completer")]
        self.assertEqual(os.path.realpath(found["Entry"]),
                         os.path.realpath(self.box.path("scripts", "Completer", "main.py")))
        for real, shipped in (("DisplayName", "displayName"), ("Version", "version"), ("Author", "author"),
                              ("NZBGetMinVersion", "nzbgetMinVersion"), ("Homepage", "homepage"),
                              ("License", "license"), ("About", "about"), ("QueueEvents", "queueEvents"),
                              ("TaskTime", "taskTime"), ("Description", "description"),
                              ("Requirements", "requirements")):
            self.assertEqual(found[real], manifest[shipped], real)
        self.assertEqual([found[key] for key in ("ScanScript", "QueueScript", "SchedulerScript", "PostScript",
                                                 "FeedScript")], [True, True, True, False, False])
        self.assertEqual([item["Name"] for item in found["Options"]], [item["name"] for item in manifest["options"]])
        for real, shipped in zip(found["Options"], manifest["options"]):
            self.assertEqual(real["Select"], shipped.get("select", []), real["Name"])
            if isinstance(shipped["value"], str):
                self.assertEqual(real["Value"], shipped["value"], real["Name"])
            else:
                self.assertEqual(float(real["Value"]), float(shipped["value"]), real["Name"])  # NZBGet shows 2 as 2.0
        self.assertEqual([(item["Name"], item["Action"]) for item in found["Commands"]],
                         [(item["name"], item["action"]) for item in manifest["commands"]])

    def test_the_options_of_the_extension_are_stored_under_their_prefixed_names(self):
        config = dict((item["Name"], item["Value"]) for item in self.box.call("config"))
        self.assertEqual(config.get("Completer:Mode"), "Enforce")
        self.assertEqual(config.get("Extensions"), "Completer")
        self.assertEqual(config.get("TempDir"), self.box.path("tmp"))
        self.assertEqual([name for name in config if name.startswith("Completer:")], ["Completer:Mode"])

    def test_the_scheduled_check_runs_when_nzbget_starts_and_the_heartbeat_file_appears(self):
        runs = self.box.dumps("sweep")
        self.assertGreaterEqual(len(runs), 1)
        self.assertEqual(runs[0]["parent"], self.box.proc.pid)  # NZBGet itself started it
        self.assertEqual([os.path.realpath(path) for path in runs[0]["argv"]],
                         [os.path.realpath(self.box.path("scripts", "Completer", "main.py"))])
        self.assertTrue(os.path.isfile(self.box.path("tmp", "completer-alive")))
        self.assertTrue(locks.Heartbeat(self.box.path("tmp")).alive(time.time()))

    def test_completers_own_readers_understand_the_environment_of_the_scheduled_task(self):
        env = self.box.dumps("sweep")[0]["env"]
        run = app.Run(env)  # builds its settings from the environment and does nothing else
        self.assertEqual(run.kind, "sweep")
        self.assertEqual(run.settings.mode, "enforce")
        self.assertTrue(run.settings.scheduler_active)
        self.assertEqual(run.settings.temp_dir, self.box.path("tmp"))
        control = settings.Control(env)
        self.assertEqual((control.host, control.port, control.username, control.password),
                         ("127.0.0.1", self.box.port, CONTROL_USER, CONTROL_PASSWORD))
        servers = settings.load_servers(env)
        self.assertEqual(len(servers), 1)
        server = servers[0]
        self.assertEqual((server.id, server.name, server.host, server.port, server.username, server.password,
                          server.connections, server.level, server.active, server.tls, server.retention),
                         (1, "fake", "127.0.0.1", self.news.port, self.news.username, self.news.password,
                          4, 0, True, False, 0))
        self.assertEqual(server.cert, "none")  # CertCheck=no: nothing is verified for any server

    def test_options_left_at_their_default_are_not_in_the_environment_so_completers_defaults_match_the_manifest(self):
        env = self.box.dumps("sweep")[0]["env"]
        self.assertEqual(sorted(key for key in env if key.startswith("NZBPO_")), ["NZBPO_MODE", "NZBPO_Mode"])
        defaults = settings.Settings(dict((key, value) for key, value in env.items() if not key.startswith("NZBPO_")))
        manifest = dict((item["name"], item["value"]) for item in shipped_manifest()["options"])
        self.assertEqual(defaults.mode, manifest["Mode"].lower())
        self.assertFalse(defaults.categories)
        self.assertEqual(defaults.connections, manifest["Connections"])
        self.assertEqual(defaults.fresh_seconds, int(manifest["FreshHours"] * 3600))
        self.assertEqual(defaults.max_articles, manifest["MaxArticles"])
        self.assertEqual(defaults.time_limit, manifest["TimeLimit"])
        self.assertEqual(defaults.max_fill, manifest["MaxFillPercent"] / 100.0)
        self.assertFalse(defaults.ignore_servers)
        self.assertEqual(defaults.verbose, manifest["Verbose"] == "yes")


@needs_nzbget
class SchedulerTests(RealCase):
    def test_the_scheduled_check_runs_again_by_itself_and_refreshes_the_heartbeat(self):
        # Proves that NZBGet repeats the task of the manifest (here: the first minute that comes, in the notation
        # of the shipped schedule); the heartbeat is what lets Completer hold downloads at all.
        while time.localtime().tm_sec > 35:  # stay clear of the end of a minute
            time.sleep(1)
        minute = (time.localtime().tm_min + 1) % 60
        box = self.sandbox(self.news_with(), periodic="*;*:%02d" % minute)
        self.assertEqual(len(box.dumps("sweep")), 1)
        box.beat(age=800)
        box.wait_for(lambda: len(box.dumps("sweep")) >= 2, "the next scheduled check", timeout=150)
        box.wait_idle()
        self.assertLess(locks.Heartbeat(box.path("tmp")).age(time.time()), 120)


@needs_nzbget
class CommandTests(SharedSandbox):
    """The buttons of the extension, run through NZBGet's own command mechanism."""

    @staticmethod
    def texts(result):
        return [text for _kind, text in result.messages]

    def test_testservers_logs_in_with_the_connections_a_check_uses_and_ends_successfully(self):
        result = self.box.run_command("TestServers")
        self.assertTrue(result.accepted)
        self.assertEqual(result.ending, "successful")  # NZBGet's word for exit code 93
        self.assertTrue(any("2 of 2 connections logged in" in text for text in self.texts(result)))

    def test_testservers_ends_failed_when_the_server_refuses_the_login(self):
        self.news.reject_login = True
        self.addCleanup(setattr, self.news, "reject_login", False)
        result = self.box.run_command("TestServers")
        self.assertEqual(result.ending, "failed")  # exit code 94
        self.assertTrue(any("could not log in" in text for text in self.texts(result)))

    def test_report_reads_the_history_and_ends_successfully(self):
        result = self.box.run_command("Report")
        self.assertEqual(result.ending, "successful")
        self.assertTrue(any("downloads in the history carry a verdict" in text for text in self.texts(result)))

    def test_releaseall_with_nothing_on_hold_ends_successfully(self):
        result = self.box.run_command("ReleaseAll")
        self.assertEqual(result.ending, "successful")
        self.assertTrue(any("0 downloads were held by Completer, 0 released" in text for text in self.texts(result)))

    def test_a_command_completer_does_not_know_ends_failed_with_an_error_in_the_log(self):
        result = self.box.run_command("NoSuchButton")
        self.assertEqual(result.ending, "failed")
        self.assertIn("ERROR", [kind for kind, _text in result.messages])

    def test_every_command_variable_completer_reads_is_in_the_environment_of_a_command(self):
        self.box.run_command("Report")
        env = self.box.dumps("command", "Report")[0]["env"]
        self.assertEqual(env["NZBCP_COMMAND"], "Report")
        self.assertEqual(app.Run(env).kind, "command")
        self.assertEqual(variables_read("NZBCP") - set(env), set())


@needs_nzbget
class HoldAndReleaseTests(RealCase):
    def test_a_new_download_is_paused_between_the_scan_script_and_the_queue_script_and_nothing_is_asked_yet(self):
        post = good_post("alpha")
        news = self.news_with(post)
        box = self.sandbox(news)
        box.gate("queue", "alpha")
        nzb_id = box.append("alpha.nzb", post)
        box.wait_for_dump("queue", "alpha")
        held = box.group(nzb_id)
        self.assertEqual(held["Status"], "PAUSED")
        self.assertEqual(held["ActiveDownloads"], 0)
        self.assertEqual(box.files(nzb_id), HELD_FILES)
        found = params_of(held)
        self.assertEqual((found["Completer:"], found["Completer_State"]), ("yes", "pending"))
        self.assertTrue(found["Completer_Token"])
        self.assertEqual(sorted(name for name in found if name.startswith("Completer")),
                         ["Completer:", "Completer_File", "Completer_Since", "Completer_State", "Completer_Sum",
                          "Completer_Token"])
        self.assertEqual((news.stats_total, news.connections_total), (0, 0))
        # the file the scan script saw is, under NZBGet's new name for it, the file the queue script reads
        scan = box.dumps("scan", "alpha")[0]["env"]
        queue = box.dumps("queue", "alpha")[0]["env"]
        self.assertEqual(queue["NZBNA_QUEUEDFILE"], scan["NZBNP_FILENAME"] + ".queued")
        self.assertEqual(found["Completer_File"], queue["NZBNA_QUEUEDFILE"])
        self.assertEqual(app.file_sum(queue["NZBNA_QUEUEDFILE"]), found["Completer_Sum"])
        subject = app.Subject.from_env(queue)
        self.assertEqual((subject.nzb_id, subject.token, subject.state), (nzb_id, found["Completer_Token"], "pending"))
        self.assertEqual(settings.parameter(scan, "Completer:"), "yes")
        self.assertEqual(scan["NZBNP_PAUSED"], "0")
        box.ungate("queue", "alpha")
        box.wait_for_state(nzb_id, "passed")

    def test_every_variable_completer_reads_is_in_the_environment_of_each_kind_of_script(self):
        post = good_post("alpha")
        box = self.sandbox(self.news_with(post))
        nzb_id = box.append("alpha.nzb", post)
        box.wait_for_state(nzb_id, "passed")
        box.wait_idle()
        for kind, prefix in (("scan", "NZBNP"), ("queue", "NZBNA"), ("sweep", "NZBSP")):
            env = box.dumps(kind)[0]["env"]
            wanted = variables_read(prefix)
            self.assertGreaterEqual(len(wanted), 1, kind)
            self.assertEqual(wanted - set(env), set(), kind)
            self.assertEqual(app.Run(env).kind, kind)
            for name in options_read():
                self.assertTrue("NZBOP_" + name in env or "NZBOP_" + name.upper() in env, (kind, name))
        scan = box.dumps("scan")[0]["env"]
        self.assertEqual(scan["NZBNP_NZBNAME"], "alpha.nzb")  # the real name carries the extension, the fake's does not
        queue = box.dumps("queue")[0]["env"]
        self.assertEqual((queue["NZBNA_NZBNAME"], queue["NZBNA_EVENT"], queue["NZBNA_NZBID"]),
                         ("alpha", "NZB_ADDED", str(nzb_id)))
        self.assertEqual(settings.parameter(queue, settings.TOKEN), params_of(box.group(nzb_id))[settings.TOKEN])

    def test_a_complete_post_is_released_with_only_the_par2_volumes_paused_and_then_downloads_like_any_other(self):
        post = good_post("alpha")
        news = self.news_with(post)
        box = self.sandbox(news, paused=False)  # NZBGet downloads as soon as it may
        nzb_id = box.append("alpha.nzb", post)
        item = box.wait_for_history(nzb_id)
        box.wait_idle()
        found = params_of(item)
        self.assertEqual(own_params(found)["Completer_State"], "passed")
        self.assertEqual((found["Completer_Checks"], found["Completer_Verdict"]), ("1", "pass/release"))
        self.assertEqual(box.edits(), [("GroupResume", "", [nzb_id], True), ("GroupPauseExtraPars", "", [nzb_id], True)])
        self.assertTrue(item["Status"].startswith("SUCCESS"))
        self.assertEqual((item["DeleteStatus"], item["MarkStatus"]), ("NONE", "NONE"))
        self.assertEqual(arr_status(item), "completed")
        self.assertEqual(nzbgetsim.sonarr_status(item), "completed")
        bodies = [line for line in news.commands if line.startswith("BODY")]
        self.assertEqual(len(bodies), len(post.all_ids) - 3)  # the three articles of the par2 volume stayed paused
        self.assertEqual(files_under(box.path("dst")), ["alpha/data.par2", "alpha/data.part01.rar", "alpha/data.part02.rar"])
        order = [box.log_index(pattern) for pattern in (
            r"Executing scan-script Completer for alpha\.nzb", r"Collection alpha added to queue",
            r"Executing queue-script Completer for alpha", r"alpha: passed and released",
            r"Downloading alpha/data\.part01\.rar", r"Collection alpha added to history")]
        self.assertEqual(order, sorted(order))
        self.assertNotIn(-1, order)

    def test_a_post_that_cannot_be_completed_ends_in_the_history_as_bad_and_nothing_is_downloaded(self):
        post = bad_post("beta")
        news = self.news_with()
        self.serve(news, post, only=3)
        box = self.sandbox(news, paused=False)  # a download that got through would start at once
        nzb_id = box.append("beta.nzb", post)
        item = box.wait_for_history(nzb_id)
        box.wait_idle()
        self.assertEqual((item["Status"], item["DeleteStatus"], item["MarkStatus"]), ("FAILURE/BAD", "BAD", "BAD"))
        self.assertTrue(item["Deleted"])
        found = params_of(item)
        self.assertEqual((found["Completer_State"], found["Completer_Checks"], found["Completer_Verdict"]),
                         ("failed", "1", "fail/fail"))
        self.assertEqual(arr_status(item), "failed")
        self.assertEqual(nzbgetsim.sonarr_status(item), "failed")
        self.assertEqual((item["DownloadedSizeLo"], item["SuccessArticles"]), (0, 0))
        self.assertEqual([line for line in news.commands if line.startswith("BODY")], [])
        self.assertEqual(box.groups(), [])
        self.assertEqual(box.edits(), [])  # the script's own "[NZB] MARK=BAD" is all it takes
        order = [box.log_index(pattern) for pattern in (r"Executing queue-script Completer for beta",
                                                        r"Marking beta as bad", r"Collection beta added to history")]
        self.assertEqual(order, sorted(order))
        self.assertNotIn(-1, order)

    def test_a_good_post_that_the_client_added_paused_is_checked_and_left_exactly_as_it_was(self):
        post = good_post("alpha")
        box = self.sandbox(self.news_with(post))
        nzb_id = box.append("alpha.nzb", post, paused=True)
        box.wait_idle()
        entry = box.group(nzb_id)
        found = params_of(entry)
        self.assertEqual(box.dumps("scan", "alpha")[0]["env"]["NZBNP_PAUSED"], "1")
        self.assertNotIn("Completer_Token", found)
        self.assertNotIn("Completer_State", found)
        self.assertEqual(found["Completer_Verdict"], "pass/nothing")
        self.assertEqual((entry["Status"], box.files(nzb_id)), ("PAUSED", HELD_FILES))
        self.assertEqual(box.edits(), [])

    def test_a_bad_post_that_the_client_added_paused_is_failed_without_ever_being_held(self):
        post = bad_post("beta")
        news = self.news_with()
        self.serve(news, post, only=3)
        box = self.sandbox(news)
        nzb_id = box.append("beta.nzb", post, paused=True)
        item = box.wait_for_history(nzb_id)
        self.assertEqual((item["Status"], item["DeleteStatus"], item["MarkStatus"]), ("FAILURE/BAD", "BAD", "BAD"))
        self.assertNotIn("Completer_Token", params_of(item))
        self.assertEqual(arr_status(item), "failed")
        self.assertEqual(box.edits(), [])

    def dry_run_case(self, mode):
        good, bad = good_post("alpha"), bad_post("beta")
        news = self.news_with(good)
        self.serve(news, bad, only=3)
        box = self.sandbox(news, mode=mode)
        id_good = box.append("alpha.nzb", good)
        id_bad = box.append("beta.nzb", bad)
        box.wait_for_state(id_good, "observed")
        box.wait_for_state(id_bad, "observed")
        box.wait_idle()
        for nzb_id, verdict in ((id_good, "pass/release"), (id_bad, "fail/fail")):
            found = params_of(box.group(nzb_id))
            self.assertNotIn("Completer_Token", found)
            self.assertEqual((found["Completer_State"], found["Completer_Verdict"]), ("observed", verdict))
            self.assertEqual(box.group(nzb_id)["Status"], "QUEUED")  # never paused
        self.assertFalse(any(box.files(id_bad).values()))
        self.assertEqual(box.edits(), [])
        self.assertEqual(box.history(), [])
        return box

    def test_dry_run_checks_and_records_a_verdict_but_never_holds_or_changes_anything(self):
        self.dry_run_case("DryRun")

    def test_dry_run_is_what_happens_when_the_mode_is_not_in_the_configuration_at_all(self):
        box = self.dry_run_case(None)
        self.assertEqual([key for key in box.dumps("queue", "alpha")[0]["env"] if key.startswith("NZBPO_")], [])

    def test_mode_off_does_nothing_at_all(self):
        good, bad = good_post("alpha"), bad_post("beta")
        news = self.news_with(good, bad)
        box = self.sandbox(news, mode="Off")
        ids = [box.append("alpha.nzb", good), box.append("beta.nzb", bad)]
        box.wait_idle()
        for nzb_id in ids:
            self.assertEqual(params_of(box.group(nzb_id)), {"*Unpack:": "no", "Completer:": "yes"})
            self.assertEqual(box.group(nzb_id)["Status"], "QUEUED")
        self.assertEqual((news.stats_total, news.connections_total, box.edits(), box.history()), (0, 0, [], []))

    def test_only_the_categories_that_are_asked_for_are_checked(self):
        good, bad = good_post("alpha"), bad_post("beta")
        news = self.news_with(good)
        self.serve(news, bad, only=3)
        box = self.sandbox(news, options=["Category1.Name=tv", "Category2.Name=movies", "Completer:Categories=tv"])
        id_tv = box.append("alpha.nzb", good, category="tv")
        id_other = box.append("beta.nzb", bad, category="movies")
        box.wait_for_state(id_tv, "passed")
        box.wait_idle()
        self.assertEqual(sorted(name for name in params_of(box.group(id_other)) if name.startswith("Completer")),
                         ["Completer:"])  # nothing of Completer's was written on it
        self.assertEqual(box.group(id_other)["Status"], "QUEUED")
        self.assertTrue(set(news.stat_log) <= set(good.all_ids))

    def test_a_download_is_held_only_while_the_scheduled_check_is_known_to_work(self):
        posts = [good_post("alpha"), good_post("beta"), good_post("gamma")]
        box = self.sandbox(self.news_with(*posts))
        box.beat(age=1000)  # the last report is older than 15 minutes
        stale = box.append("alpha.nzb", posts[0])
        box.wait_idle()
        os.remove(box.path("tmp", "completer-alive"))  # no report at all
        missing = box.append("beta.nzb", posts[1])
        box.wait_idle()
        for nzb_id in (stale, missing):
            self.assertNotIn("Completer_Token", params_of(box.group(nzb_id)))
            self.assertEqual(box.group(nzb_id)["Status"], "QUEUED")
        self.assertGreaterEqual(box.log_index(r"not put on hold because the scheduled check has not reported"), 0)
        box.beat(age=60)
        fresh = box.append("gamma.nzb", posts[2])
        box.wait_for_state(fresh, "passed")
        self.assertTrue(params_of(box.group(fresh))["Completer_Token"])

    def test_the_report_counts_what_was_held_and_what_was_removed_before_any_download(self):
        good, bad = good_post("alpha"), bad_post("beta")
        news = self.news_with(good)
        self.serve(news, bad, only=3)
        box = self.sandbox(news, paused=False)
        box.wait_for_history(box.append("alpha.nzb", good))
        box.wait_for_history(box.append("beta.nzb", bad))
        box.wait_idle()
        result = box.run_command("Report")
        texts = [text for _kind, text in result.messages]
        self.assertEqual(result.ending, "successful")
        self.assertTrue(any("2 downloads in the history carry a verdict (0 from a dry run, 2 while holding)" in text
                            for text in texts))
        self.assertTrue(any("removed by Completer before anything was downloaded: 1 downloads" in text for text in texts))


@needs_nzbget
class IsolationTests(RealCase):
    """The promise that matters most: Completer only ever acts on the one download it checked."""

    def test_a_verdict_is_applied_to_one_download_and_every_other_download_stays_as_it_was(self):
        alpha, beta, gamma, omega = good_post("alpha"), bad_post("beta"), good_post("gamma"), bad_post("omega")
        news = self.news_with(alpha, gamma)
        self.serve(news, beta, only=3)
        self.serve(news, omega, only=3)
        box = self.sandbox(news)
        id_gamma = box.append("gamma.nzb", gamma, paused=True)                      # paused by the client
        id_omega = box.append("omega.nzb", omega, params=[("Completer:", "no")])    # Completer switched off for it
        box.wait_idle()
        others = box.snapshot(id_gamma, id_omega)
        self.assertEqual(others[id_omega][0], "QUEUED")
        box.gate("queue", "alpha")
        box.gate("queue", "beta")
        id_alpha = box.append("alpha.nzb", alpha)
        id_beta = box.append("beta.nzb", beta)
        box.wait_for_dump("queue", "alpha")
        held_beta = box.snapshot(id_beta)
        self.assertEqual(held_beta[id_beta][0], "PAUSED")
        self.assertEqual(box.snapshot(id_gamma, id_omega), others)
        box.ungate("queue", "alpha")
        box.wait_for_state(id_alpha, "passed")
        box.wait_for_dump("queue", "beta")  # beta's script has started, and waits at the gate
        self.assertEqual(box.files(id_alpha), RELEASED_FILES)
        # alpha has been released; beta has not been touched by that, and neither have the two others
        self.assertEqual(box.snapshot(id_beta), held_beta)
        self.assertEqual(box.snapshot(id_gamma, id_omega), others)
        self.assertEqual(box.edits(), [("GroupResume", "", [id_alpha], True), ("GroupPauseExtraPars", "", [id_alpha], True)])
        box.ungate("queue", "beta")
        item = box.wait_for_history(id_beta)
        box.wait_idle()
        self.assertEqual((item["Status"], item["DeleteStatus"], item["MarkStatus"]), ("FAILURE/BAD", "BAD", "BAD"))
        self.assertEqual(box.snapshot(id_gamma, id_omega), others)
        self.assertEqual(sorted(entry["NZBID"] for entry in box.groups()), sorted([id_alpha, id_gamma, id_omega]))
        self.assertEqual(box.violations(allowed_ids={id_alpha, id_beta}), [])
        self.assertTrue(news.stat_log)
        # alpha, beta and gamma were checked (the client's pause does not exempt gamma); omega was never asked about
        self.assertTrue(set(news.stat_log) <= set(alpha.all_ids) | set(beta.all_ids) | set(gamma.all_ids))
        self.assertFalse(set(news.stat_log) & set(omega.all_ids))


@needs_nzbget
class SweepTests(RealCase):
    """The scheduled check as the safety net: the queue script never ran (NZBGet has no event to run it for)."""

    def start_three(self):
        alpha, beta, gamma = good_post("alpha"), bad_post("beta"), good_post("gamma")
        news = self.news_with(alpha, gamma)
        self.serve(news, beta, only=3)
        box = self.sandbox(news, manifest=no_queue_script)
        ids = (box.append("alpha.nzb", alpha), box.append("beta.nzb", beta), box.append("gamma.nzb", gamma, paused=True))
        box.wait_idle()
        return news, box, ids

    def test_a_download_that_nobody_checked_is_left_alone_while_new_and_checked_by_the_sweep_when_old(self):
        news, box, (id_alpha, id_beta, id_gamma) = self.start_three()
        self.assertEqual(box.dumps("queue"), [])
        for nzb_id in (id_alpha, id_beta):
            self.assertEqual(box.group(nzb_id)["Status"], "PAUSED")
            self.assertEqual(params_of(box.group(nzb_id))["Completer_State"], "pending")
        before = box.snapshot(id_alpha, id_beta, id_gamma)
        box.restart()  # the check that NZBGet runs at start, and the downloads are younger than the grace time
        self.assertEqual(box.snapshot(id_alpha, id_beta, id_gamma), before)
        self.assertEqual((news.stats_total, box.edits()), (0, []))
        aged = int(time.time()) - 400
        for nzb_id in (id_alpha, id_beta):
            self.assertTrue(box.edit("GroupSetParameter", "Completer_Since=%d" % aged, nzb_id))
        gamma = box.snapshot(id_gamma)
        box.restart()
        entry = box.group(id_alpha)
        self.assertEqual(params_of(entry)["Completer_State"], "passed")
        self.assertEqual((entry["Status"], box.files(id_alpha)), ("QUEUED", RELEASED_FILES))
        item = box.wait_for_history(id_beta)
        self.assertEqual((item["Status"], item["DeleteStatus"], item["MarkStatus"]), ("FAILURE/BAD", "MANUAL", "BAD"))
        self.assertEqual(params_of(item)["Completer_State"], "failed")
        self.assertEqual(arr_status(item), "failed")
        self.assertEqual(nzbgetsim.sonarr_status(item), "failed")
        self.assertEqual(box.snapshot(id_gamma), gamma)
        commands = [(command, text) for command, text, ids, _token in box.edits() if ids == [id_beta]]
        names = [command for command, _text in commands]
        self.assertIn(("GroupSetParameter", "Completer_State=failing"), commands)
        self.assertEqual(names[names.index("GroupDelete"):], ["GroupDelete", "HistoryMarkBad", "HistorySetParameter"])
        self.assertEqual(commands[-1], ("HistorySetParameter", "Completer_State=failed"))
        self.assertEqual(box.violations(allowed_ids={id_alpha, id_beta}), [])

    def test_changing_the_mode_to_off_lets_go_of_what_was_held_at_the_next_check(self):
        news, box, (id_alpha, id_beta, id_gamma) = self.start_three()
        gamma = box.snapshot(id_gamma)
        box.mode = "Off"
        box.restart()
        for nzb_id in (id_alpha, id_beta):
            entry = box.group(nzb_id)
            self.assertEqual(entry["Status"], "QUEUED")
            self.assertEqual(params_of(entry)["Completer_State"], "released")
        self.assertEqual(box.snapshot(id_gamma), gamma)
        self.assertEqual(news.stats_total, 0)

    def test_releaseall_releases_what_completer_holds_and_nothing_else(self):
        news, box, (id_alpha, id_beta, id_gamma) = self.start_three()
        gamma = box.snapshot(id_gamma)
        result = box.run_command("ReleaseAll")
        self.assertEqual(result.ending, "successful")
        self.assertTrue(any("2 downloads were held by Completer, 2 released" in text for _kind, text in result.messages))
        for nzb_id in (id_alpha, id_beta):
            self.assertEqual(box.group(nzb_id)["Status"], "QUEUED")
        self.assertEqual(box.snapshot(id_gamma), gamma)
        self.assertEqual(box.violations(allowed_ids={id_alpha, id_beta}), [])


def fingerprint_real(box, nzb_id):
    """The parts of a finished download that Completer influences, as the real NZBGet shows them."""
    entry = box.group(nzb_id)
    if entry is not None:
        return {"where": "queue", "status": entry["Status"], "files": box.files(nzb_id), "params": own_params(params_of(entry))}
    item = box.history_item(nzb_id)
    return {"where": "history", "status": item["Status"], "delete": item["DeleteStatus"], "mark": item["MarkStatus"],
            "arr": arr_status(item), "params": own_params(params_of(item))}


def fingerprint_fake(sim, nzb_id):
    """The same, from the fake NZBGet."""
    entry = sim.entry(nzb_id)
    if sim.where(nzb_id) == "queue":
        return {"where": "queue", "status": sim.status(nzb_id),
                "files": dict((item["name"], item["paused"]) for item in entry.files), "params": own_params(entry.param_dict())}
    return {"where": "history", "status": entry.text_status, "delete": entry.delete_status, "mark": entry.mark_status,
            "arr": sim.sonarr_view(nzb_id), "params": own_params(entry.param_dict())}


def commands_real(box, nzb_id):
    return [(command, text) for command, text, ids, _token in box.edits() if ids == [nzb_id]]


def commands_fake(sim, nzb_id):
    return [(command, text) for command, text, ids in sim.edits if ids == [nzb_id]]


@needs_nzbget
class ParityTests(RealCase):
    """The same scenario in the real NZBGet and in the fake one (``nzbgetsim``) ends the same way."""

    def sim_world(self, news, mode="Enforce"):
        sim = nzbgetsim.Sim(clock=nzbgetsim.SimClock(), options={"Mode": mode}).start()
        self.addCleanup(sim.stop)
        self.addCleanup(lambda: self.assertEqual(sim.violations, []))
        sim.add_server(news)
        self.assertEqual(sim.boot().code, 0)
        return sim

    def both(self, good_tags=("alpha",), bad_tags=("beta",), **box_args):
        """A real sandbox and a fake NZBGet over one news server: ``(news, box, sim, real posts, fake posts)``."""
        news = self.news_with()
        box = self.sandbox(news, **box_args)
        sim = self.sim_world(news)
        real, fake = {}, {}
        for tags, maker, only in ((good_tags, good_post, None), (bad_tags, bad_post, 3)):
            for tag in tags:
                real[tag] = maker(tag)
                fake[tag] = maker(tag, now=sim.now)
                self.serve(news, real[tag], only=only)
                self.serve(news, fake[tag], only=only)
        return news, box, sim, real, fake

    def test_a_complete_post_is_held_and_released_the_same_way(self):
        news, box, sim, real, fake = self.both(bad_tags=())
        real_id = box.append("alpha.nzb", real["alpha"])
        fake_id = sim.add(fake["alpha"], "alpha")
        box.wait_for_state(real_id, "passed")
        box.wait_idle()
        self.assertEqual(fingerprint_real(box, real_id), fingerprint_fake(sim, fake_id))
        self.assertEqual(commands_real(box, real_id), commands_fake(sim, fake_id))

    def test_a_post_that_cannot_be_completed_is_failed_the_same_way(self):
        news, box, sim, real, fake = self.both(good_tags=())
        real_id = box.append("beta.nzb", real["beta"])
        fake_id = sim.add(fake["beta"], "beta")
        box.wait_for_history(real_id)
        box.wait_idle()
        self.assertEqual(fingerprint_real(box, real_id), fingerprint_fake(sim, fake_id))
        self.assertEqual(commands_real(box, real_id), commands_fake(sim, fake_id))

    def test_the_sweep_settles_a_download_whose_queue_script_never_ran_the_same_way(self):
        news, box, sim, real, fake = self.both(manifest=no_queue_script)
        real_ids = dict((tag, box.append(tag + ".nzb", real[tag])) for tag in ("alpha", "beta"))
        fake_ids = dict((tag, sim.add(fake[tag], tag, run=False)) for tag in ("alpha", "beta"))
        box.wait_idle()
        aged = int(time.time()) - 400
        for nzb_id in real_ids.values():
            box.edit("GroupSetParameter", "Completer_Since=%d" % aged, nzb_id)
        sim.advance(400)
        box.restart()
        box.wait_for_history(real_ids["beta"])
        self.assertEqual(sim.sweep().code, 0)
        for tag in ("alpha", "beta"):
            self.assertEqual(fingerprint_real(box, real_ids[tag]), fingerprint_fake(sim, fake_ids[tag]), tag)
            self.assertEqual(commands_real(box, real_ids[tag]), commands_fake(sim, fake_ids[tag]), tag)

    def test_the_fake_never_invents_a_variable_that_the_real_nzbget_does_not_set(self):
        news, box, sim, real, fake = self.both(bad_tags=())
        box.wait_for_state(box.append("alpha.nzb", real["alpha"]), "passed")
        box.wait_idle()
        sim.add(fake["alpha"], "alpha")
        for kind, prefix in (("scan", "NZBNP"), ("queue", "NZBNA"), ("sweep", "NZBSP")):
            real_names = set(key for key in box.dumps(kind)[0]["env"] if key.startswith(prefix))
            fake_names = set(key for key in sim.runs_of(kind)[-1].env if key.startswith(prefix))
            self.assertEqual(fake_names - real_names, set(), kind)


if __name__ == "__main__":
    unittest.main()
