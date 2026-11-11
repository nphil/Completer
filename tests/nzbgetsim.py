"""A fake NZBGet for the integration tests: queue, history, scripts and the JSON-RPC interface.

It behaves the way NZBGet 26.3 does in every place Completer depends on (each
rule below was read from NZBGet's source), so that the real ``main.py`` /
``completer.app`` can be run against it end to end:

* ``Sim.add()`` is the ``append`` of NZBGet: the nzb is stored in the nzb
  folder, the scan scripts in the global Extensions list run on it (their
  ``[NZB] ...`` output changes name, category, parameters and the paused flag),
  the file is renamed to ``<name>.queued``, the download enters the queue, its
  extra repair files are paused (unless ParCheck is ``force``), and the
  ``NZB_ADDED`` queue event is delivered to the queue scripts that want it.
* ``Sim.pump()`` runs the pending queue-script events, one at a time, like
  NZBGet's single queue-script lane. A script's ``[NZB] NZBPR_x=v`` output
  changes parameters of its own download, ``[NZB] MARK=BAD`` marks it bad and,
  when the script has ended, moves it to the history as bad.
* ``Sim.sweep()`` runs the scheduled task, ``Sim.command()`` a command button.
* The JSON-RPC interface (HTTP POST ``/jsonrpc``, basic authentication) knows
  ``listgroups``, ``history``, ``status`` and ``editqueue``. Every call is
  recorded in ``Sim.calls``. Calls Completer must never make (``FileDelete``,
  pausing, other methods, lists of ids...) are carried out too, so their
  effect shows, but they are also written to ``Sim.violations``.

Nothing here is Completer-specific except the checks in ``Sim.violations``:
every change must name exactly one download by a positive integer id; only
Completer's six edit commands may be used; the download must carry a
``Completer_Token`` at that moment; only ``Completer_*`` parameters may be
written; and ``allowed_ids`` (when a test sets it) limits which downloads may
be touched at all. ``SimTestCase`` fails a test that ends with any violation,
or with a secret in a script's output.

Time is simulated (``Sim.clock``; ``Sim.advance(seconds)``) for in-process
runs. Runs that need real processes (``inprocess=False``: concurrency,
SIGTERM) need ``clock=time.time``.
"""

import base64
import gzip
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ElementTree
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
EXTENSION = os.path.normpath(os.path.join(HERE, "..", "Completer"))
for _path in (EXTENSION, HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import fakenntp  # noqa: E402
import nzbtools  # noqa: E402
from completer import app, log  # noqa: E402

MAIN = os.path.join(EXTENSION, "main.py")
START = 1800001800  # minute 30 of an hour: the once-an-hour history look-up is not taken by chance
CONTROL_USER = "nzbget"
CONTROL_PASSWORD = "control-pass-4711"

SPECIAL = ".:*!\"$%&/()=`+~#'{}[]@- "  # NZBGet turns these into '_' in the second spelling of a variable
GROUP_COMMANDS = ("GroupResume", "GroupPauseExtraPars", "GroupSetParameter", "GroupDelete")
HISTORY_COMMANDS = ("HistoryMarkBad", "HistorySetParameter")
LOG_KINDS = ("[INFO] ", "[DETAIL] ", "[WARNING] ", "[ERROR] ", "[DEBUG] ")
SERVER_FIELDS = {"HOST": "Host", "PORT": "Port", "USERNAME": "Username", "PASSWORD": "Password",
                 "ENCRYPTION": "Encryption", "CONNECTIONS": "Connections", "LEVEL": "Level", "GROUP": "Group",
                 "OPTIONAL": "Optional", "RETENTION": "Retention", "ACTIVE": "Active", "NAME": "Name",
                 "JOINGROUP": "JoinGroup", "CERTVERIFICATION": "CertVerification", "IPVERSION": "IpVersion"}


def normalise(name):
    """NZBGet's second spelling of a variable name: whole name upper case, punctuation as '_'."""
    return "".join("_" if ch in SPECIAL else ch for ch in name).upper()


def export(env, prefix, name, value):
    """Set ``<prefix>_<name>`` and, when different, its normalised spelling, the way NZBGet does."""
    original = "%s_%s" % (prefix, name)
    env[original] = str(value)
    normalised = normalise(original)
    if normalised != original:
        env[normalised] = str(value)


def is_yes(value):
    return str(value).strip().lower() in ("yes", "true", "on", "1")


def atoi(text):
    match = re.match(r"\s*[-+]?\d+", str(text))
    return int(match.group(0)) if match else 0


def set_parameter(params, name, value):
    """NzbParameterList::SetParameter: names compare without case; an empty value removes the parameter."""
    for index, pair in enumerate(params):
        if pair[0].lower() == name.lower():
            if value == "":
                del params[index]
            else:
                pair[1] = value
            return
    if value != "":
        params.append([name, value])


def find_parameter(params, name, default=None):
    for key, value in params:
        if key.lower() == name.lower():
            return value
    return default


class SimClock(object):
    def __init__(self, now=START):
        self.now = float(now)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Call(object):
    """One JSON-RPC call NZBGet received."""

    def __init__(self, number, method, params):
        self.number = number
        self.method = method
        self.params = params
        self.result = None
        self.error = None
        self.problems = []

    @property
    def command(self):
        if self.method.lower() == "editqueue" and len(self.params) == 3:
            return self.params[0]
        return None

    @property
    def text(self):
        return self.params[1] if self.method.lower() == "editqueue" and len(self.params) == 3 else None

    @property
    def ids(self):
        return self.params[2] if self.method.lower() == "editqueue" and len(self.params) == 3 else None

    def __repr__(self):
        return "Call(%d, %s, %r)" % (self.number, self.method, self.params)


class RunResult(object):
    """What one script run did: exit code and everything it printed."""

    def __init__(self, kind, code, out, err="", env=None):
        self.kind = kind
        self.code = code
        self.out = out
        self.err = err
        self.env = env or {}

    @property
    def lines(self):
        return self.out.splitlines()

    @property
    def commands(self):
        """The ``[NZB]`` commands, without the tag."""
        return [line[6:] for line in self.lines if line.startswith("[NZB] ")]

    @property
    def messages(self):
        """(kind, text) of the log lines, kind in info/detail/warning/error/debug."""
        found = []
        for line in self.lines:
            for tag in LOG_KINDS:
                if line.startswith(tag):
                    found.append((tag[1:-2].lower(), line[len(tag):]))
        return found

    def says(self, fragment):
        return any(fragment in text for _kind, text in self.messages)

    def __repr__(self):
        return "RunResult(%s, code=%r, %d lines)" % (self.kind, self.code, len(self.lines))


class Entry(object):
    """One download, in the queue or in the history."""

    def __init__(self, nzb_id, name, category):
        self.id = nzb_id
        self.name = name
        self.category = category
        self.filename = ""
        self.queued_file = ""
        self.params = []  # [name, value] pairs
        self.files = []   # dicts: id, name, subject, size, posted, par, vol, paused
        self.delete_status = "NONE"
        self.mark_status = "NONE"
        self.priority = 0
        self.active = 0
        self.history_time = 0
        self.downloaded_mb = 0  # what NZBGet reports as DownloadedSizeMB once it is in the history

    def get(self, name, default=None):
        return find_parameter(self.params, name, default)

    def set(self, name, value):
        set_parameter(self.params, name, value)

    def param_dict(self):
        return {name: value for name, value in self.params}

    @property
    def size(self):
        return sum(item["size"] for item in self.files)

    @property
    def paused_size(self):
        return sum(item["size"] for item in self.files if item["paused"])

    @property
    def text_status(self):
        """The ``Status`` of a history entry (NzbInfo::MakeTextStatus)."""
        if self.mark_status == "BAD":
            return "FAILURE/BAD"
        if self.mark_status == "GOOD":
            return "SUCCESS/GOOD"
        if self.mark_status == "SUCCESS":
            return "SUCCESS/MARK"
        return {"HEALTH": "FAILURE/HEALTH", "MANUAL": "DELETED/MANUAL", "DUPE": "DELETED/DUPE",
                "BAD": "FAILURE/BAD", "GOOD": "DELETED/GOOD", "COPY": "DELETED/COPY",
                "SCAN": "FAILURE/SCAN"}.get(self.delete_status, "SUCCESS/ALL")

    @property
    def queue_status(self):
        """The ``Status`` of a queue entry as listgroups reports it."""
        if self.active > 0:
            return "DOWNLOADING"
        if self.paused_size > 0 and self.size == self.paused_size:
            return "PAUSED"
        return "QUEUED"

    @property
    def paused_files(self):
        return [item["name"] for item in self.files if item["paused"]]


def sonarr_status(item):
    """Port of the history part of Sonarr's and Radarr's ``Nzbget.cs``: how an entry of ``history`` is judged.

    Returns "failed", "warning", "completed", or None when the entry is skipped (deleted by hand, not marked bad).
    """
    success = ("SUCCESS", "NONE")
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
        status = "failed" if item["DeleteStatus"] in ("HEALTH", "DUPE", "SCAN", "COPY", "BAD") else "warning"
    return status


class _RpcError(Exception):
    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code = code
        self.message = message


class Background(object):
    """Runs a function in a thread so that tests can overlap two script runs."""

    def __init__(self, function, *args, **kwargs):
        self.result = None
        self.error = None
        self._thread = threading.Thread(target=self._run, args=(function, args, kwargs), daemon=True)
        self._thread.start()

    def _run(self, function, args, kwargs):
        try:
            self.result = function(*args, **kwargs)
        except BaseException as error:  # reported by wait()
            self.error = error

    def wait(self, timeout=120):
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise AssertionError("the background run did not finish")
        if self.error is not None:
            raise self.error
        return self.result


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *_args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.sim._serve(self, body)

    def do_GET(self):
        self.send_error(404)


class Sim(object):
    def __init__(self, clock=None, options=None, nzbget=None, extensions=("Completer",), inprocess=True,
                 root=None):
        self._own_root = root is None
        self.root = root or tempfile.mkdtemp(prefix="nzbsim-")
        self.nzb_dir = os.path.join(self.root, "nzb")
        self.temp_dir = os.path.join(self.root, "temp")
        os.makedirs(self.nzb_dir, exist_ok=True)
        os.makedirs(self.temp_dir, exist_ok=True)
        self.clock = clock if clock is not None else SimClock()
        self.inprocess = inprocess
        self.extensions = list(extensions)
        with open(os.path.join(EXTENSION, "manifest.json"), encoding="utf-8") as handle:
            self.manifest = json.load(handle)
        self.option_values = {item["name"]: str(item.get("value", "")) for item in self.manifest["options"]}
        self.option_values.update(options or {})
        self.nzbget = {"ControlIP": "127.0.0.1", "ControlUsername": CONTROL_USER,
                       "ControlPassword": CONTROL_PASSWORD, "ParCheck": "auto", "CertCheck": "yes",
                       "CertStore": "", "TempDir": self.temp_dir, "NzbDir": self.nzb_dir,
                       "HealthCheck": "park", "DupeCheck": "yes"}
        self.nzbget.update(nzbget or {})
        self.server_env = {}
        self.news = []    # the FakeNews servers wired in by add_server
        self.secrets = {CONTROL_PASSWORD}
        self.lock = threading.RLock()
        self.queue = []
        self.history = []
        self._limbo = []  # [calls_left, entry]: deleted downloads that are not in the history yet
        self.events = []  # (event, nzb_id, script)
        self.calls = []
        self.violations = []
        self.expect_violations = False
        self.runs = []
        self.log = []     # (kind, text) of every log line any script printed
        self.exits = []   # codes the watchdog would have ended a run with (in-process runs)
        self.faults = []
        self.allowed_ids = None
        self.download_rate = 0
        self.download_paused = False
        self.history_delay = 0  # history() calls before a freshly deleted download shows up there
        self.queue_scripts_running = 0
        self._next_id = 1
        self._next_file_id = 10001
        self._inprocess = threading.Lock()
        self._http = None
        self._thread = None
        self.port = 0

    # ------------------------------------------------------------ life cycle

    def start(self):
        self._listen(0)
        return self

    def _listen(self, port):
        server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        server.daemon_threads = True
        server.sim = self
        self._http = server
        self.port = server.server_address[1]
        self._thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()

    def rpc_down(self):
        """The control port stops listening: connections are refused, like an NZBGet that is not running."""
        server, self._http = self._http, None
        if server is not None:
            server.shutdown()
            server.server_close()
            self._thread.join(2.0)

    def rpc_up(self):
        if self._http is None:
            self._listen(self.port)

    def stop(self):
        self.rpc_down()
        if self._own_root:
            shutil.rmtree(self.root, ignore_errors=True)

    def __enter__(self):
        return self.start()

    def __exit__(self, *_exc):
        self.stop()

    def advance(self, seconds):
        if not isinstance(self.clock, SimClock):
            raise RuntimeError("this Sim runs on the real clock")
        self.clock.advance(seconds)

    @property
    def now(self):
        return self.clock()

    # ------------------------------------------------------------ configuration

    def add_server(self, news, number=None, **fields):
        """Make NZBGet's configuration point at a FakeNews server (``fields`` as for ``FakeNews.env_entry``)."""
        number = number or len(self.servers_configured()) + 1
        entry = news.env_entry(number, **fields)
        for key, value in entry.items():
            if key == "NZBOP_CertCheck":
                continue  # a global option of NZBGet: the Sim's own ``nzbget`` settings decide it
            self.server_env[key] = value
            match = re.match(r"^NZBOP_SERVER(\d+)_([A-Z0-9]+)$", key)
            if match:
                name = SERVER_FIELDS.get(match.group(2), match.group(2).capitalize())
                self.server_env["NZBOP_Server%s.%s" % (match.group(1), name)] = value
        self.secrets.add(fields.get("password", fakenntp.PASSWORD))
        if news not in self.news:
            self.news.append(news)
        return number

    def servers_configured(self):
        return {key for key in self.server_env if re.match(r"^NZBOP_SERVER\d+_HOST$", key)}

    def set_option(self, name, value):
        """A setting of the extension (``Completer:<name>`` in nzbget.conf); applies to the next run."""
        self.option_values[name] = str(value)

    def set_nzbget(self, name, value):
        """An NZBGet option such as ParCheck or Extensions."""
        if name == "Extensions":
            self.extensions = [part.strip() for part in re.split(r"[,;]", value) if part.strip()]
        else:
            self.nzbget[name] = value

    def _kinds(self, script):
        return set(self.manifest["kind"].split("/")) if script == self.manifest["name"] else set()

    def _events(self, script):
        return {part.strip() for part in re.split(r"[,;]", self.manifest.get("queueEvents", "")) if part.strip()}

    def base_env(self):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.root, "LANG": "C.UTF-8"}
        options = dict(self.nzbget)
        options["ControlPort"] = str(self.port)
        options["Extensions"] = ", ".join(self.extensions)
        options["ScriptOrder"] = ", ".join(self.extensions)
        for name, value in options.items():
            export(env, "NZBOP", name, value)
        env.update(self.server_env)
        for name, value in self.option_values.items():
            export(env, "NZBPO", name, value)
        return env

    def _export_parameters(self, env, params, script):
        prefix = script + ":"
        for name, value in params:
            export(env, "NZBPR", name, value)
        for name, value in params:
            if name.startswith(prefix) and len(name) > len(prefix):
                export(env, "NZBPR", name[len(prefix):], value)

    # ------------------------------------------------------------ adding a download

    @staticmethod
    def parse_nzb(path):
        """The files of an nzb as NZBGet's queue shows them."""
        found = []
        with open(path, "rb") as handle:
            raw = handle.read()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        root = ElementTree.fromstring(raw)
        for node in root.iter():
            if node.tag.rsplit("}", 1)[-1] != "file":
                continue
            subject = node.get("subject", "")
            quoted = re.search(r'"([^"]+)"', subject)
            name = quoted.group(1) if quoted else subject
            size = 0
            for segment in node.iter():
                if segment.tag.rsplit("}", 1)[-1] == "segment":
                    size += atoi(segment.get("bytes", "0"))
            found.append({"name": name, "subject": subject, "size": size, "posted": atoi(node.get("date", "0")),
                          "par": ".par2" in subject.lower(), "vol": ".vol" in name.lower(), "paused": False})
        return found

    def _init_pp_parameters(self, params, reset):
        """Scanner::InitPPParameters for the global Extensions list."""
        if reset:
            for script in self.extensions:
                set_parameter(params, script + ":", "")
        if find_parameter(params, "*Unpack:") is None:
            set_parameter(params, "*Unpack:", "yes")
        for script in self.extensions:
            if self._kinds(script) & {"QUEUE", "POST"} and find_parameter(params, script + ":") is None:
                set_parameter(params, script + ":", "yes")

    def add(self, source, name, category="", paused=False, queue_scripts=True, params=None, run=True):
        """Add a download. ``source`` is a ``nzbtools.Post``, an nzb text, or the path of an nzb file.

        ``queue_scripts``: True gives it ``<Extension>:`` = yes like any new download; False leaves the
        parameter out (a download that was queued before the extension existed); "no" sets it to no.
        ``run``: deliver the NZB_ADDED event now. Returns the NZBID.
        """
        base = re.sub(r"[^A-Za-z0-9._ -]", "_", name)[:100]  # a file system refuses names beyond 255 bytes
        path = os.path.join(self.nzb_dir, base + ".nzb")
        with self.lock:
            if hasattr(source, "xml"):
                data = source.xml.encode("utf-8")
            elif isinstance(source, (bytes, bytearray)):
                data = bytes(source)  # e.g. a gzip'd nzb
            elif os.path.isfile(str(source)):
                with open(source, "rb") as handle:
                    data = handle.read()
            else:
                data = source.encode("utf-8")
            with open(path, "wb") as handle:
                handle.write(data)
            scan = {"name": name, "category": category, "params": [list(pair) for pair in (params or {}).items()],
                    "paused": bool(paused), "priority": 0, "top": 0}
            if queue_scripts == "no":
                for script in self.extensions:
                    if self._kinds(script) & {"QUEUE", "POST"}:
                        set_parameter(scan["params"], script + ":", "no")
            if queue_scripts:
                self._init_pp_parameters(scan["params"], False)
        for script in self.extensions:
            if "SCAN" in self._kinds(script) and os.path.isfile(path):
                self._run_scan(script, path, scan)
        with self.lock:
            queued = path + ".queued"
            number = 2
            while os.path.exists(queued):
                queued = "%s.%d.queued" % (path, number)
                number += 1
            os.rename(path, queued)
            entry = Entry(self._next_id, scan["name"], scan["category"])
            self._next_id += 1
            entry.filename = path
            entry.queued_file = queued
            entry.priority = scan["priority"]
            entry.params = scan["params"]
            for item in self.parse_nzb(queued):
                item["id"] = self._next_file_id
                self._next_file_id += 1
                item["paused"] = bool(scan["paused"])
                entry.files.append(item)
            if self.nzbget["ParCheck"].strip().lower() != "force":
                self._pause_extra_pars(entry)
            self.queue.append(entry)
            self._emit("NZB_ADDED", entry)
        if run:
            self.pump()
        return entry.id

    def _run_scan(self, script, path, scan):
        env = self.base_env()
        env.update({"NZBNP_FILENAME": path, "NZBNP_URL": "", "NZBNP_NZBNAME": scan["name"],
                    "NZBNP_CATEGORY": scan["category"], "NZBNP_PRIORITY": str(scan["priority"]),
                    "NZBNP_TOP": str(scan["top"]), "NZBNP_PAUSED": "1" if scan["paused"] else "0",
                    "NZBNP_DUPEKEY": "", "NZBNP_DUPESCORE": "0", "NZBNP_DUPEMODE": "SCORE",
                    "NZBNP_DIRECTORY": self.nzb_dir})
        self._export_parameters(env, scan["params"], script)
        result = self._execute("scan", env)
        with self.lock:
            for command in result.commands:
                if command.startswith("NZBNAME="):
                    scan["name"] = command[8:]
                elif command.startswith("CATEGORY="):
                    scan["category"] = command[9:]
                    self._init_pp_parameters(scan["params"], True)
                elif command.startswith("NZBPR_"):
                    if "=" in command:
                        key, value = command[6:].split("=", 1)
                        set_parameter(scan["params"], key, value)
                    else:
                        self.violations.append("invalid scan command %r" % command)
                elif command.startswith("PRIORITY="):
                    scan["priority"] = atoi(command[9:])
                elif command.startswith("TOP="):
                    scan["top"] = 1 if atoi(command[4:]) else 0
                elif command.startswith("PAUSED="):
                    scan["paused"] = atoi(command[7:]) != 0
                elif command.split("=")[0] in ("DUPEKEY", "DUPESCORE", "DUPEMODE"):
                    pass
                else:
                    self.violations.append("invalid scan command %r" % command)
        return result

    # ------------------------------------------------------------ queue-script events

    def _emit(self, event, entry):
        for script in self.extensions:
            if "QUEUE" in self._kinds(script) and event in self._events(script) \
                    and is_yes(entry.get(script + ":", "")):
                self.events.append((event, entry.id, script))

    def pump(self):
        """Run pending queue-script events one at a time (NZBGet runs one queue script at a time)."""
        results = []
        while True:
            with self.lock:
                if not self.events:
                    return results
                rank = {"NZB_DOWNLOADED": 0, "NZB_DELETED": 0, "NZB_ADDED": 1}
                self.events.sort(key=lambda item: rank.get(item[0], 2))  # stable: arrival order within a rank
                event, nzb_id, script = self.events.pop(0)
                entry = self.find_queue(nzb_id)
                if entry is None or entry.delete_status != "NONE":
                    continue  # events of a deleted or bad download are dropped
            results.append(self.run_queue_script(nzb_id, event, script=script))

    def queue_env(self, entry, event="NZB_ADDED", script="Completer"):
        env = self.base_env()
        env.update({"NZBNA_NZBNAME": entry.name, "NZBNA_NZBID": str(entry.id), "NZBNA_FILENAME": entry.filename,
                    "NZBNA_DIRECTORY": "", "NZBNA_QUEUEDFILE": entry.queued_file, "NZBNA_URL": "",
                    "NZBNA_CATEGORY": entry.category, "NZBNA_PRIORITY": str(entry.priority),
                    "NZBNA_LASTID": str(entry.id), "NZBNA_DUPEKEY": "", "NZBNA_DUPESCORE": "0",
                    "NZBNA_DUPEMODE": "SCORE", "NZBNA_EVENT": event, "NZBNA_DELETESTATUS": entry.delete_status,
                    "NZBNA_URLSTATUS": "NONE", "NZBNA_MARKSTATUS": entry.mark_status})
        self._export_parameters(env, entry.params, script)
        return env

    def run_queue_script(self, nzb_id, event="NZB_ADDED", script="Completer", env_update=None, env_remove=(),
                         terminate_after=None):
        """Run the queue script for one download (in the queue or in the history), whatever the manifest says."""
        with self.lock:
            entry = self.find_queue(nzb_id) or self.find_history(nzb_id)
            if entry is None:
                raise KeyError("no download %r" % (nzb_id,))
            env = self.queue_env(entry, event, script)
            self.queue_scripts_running += 1
        env.update(env_update or {})
        for key in env_remove:
            env.pop(key, None)
        try:
            result = self._execute("queue", env, terminate_after=terminate_after)
        finally:
            with self.lock:
                self.queue_scripts_running -= 1
        with self.lock:
            mark_bad = False
            target = self.find_queue(nzb_id) or self.find_history(nzb_id)
            for command in result.commands:
                if command.startswith("NZBPR_"):
                    if "=" in command and target is not None:
                        key, value = command[6:].split("=", 1)
                        target.set(key, value)
                    elif "=" not in command:
                        self.violations.append("invalid queue command %r" % command)
                elif command == "MARK=BAD" or command.startswith("MARK=BAD"):
                    mark_bad = True
                    if target is not None:
                        target.mark_status = "BAD"
                else:
                    self.violations.append("invalid queue command %r" % command)
            if mark_bad:
                entry = self.find_queue(nzb_id)
                if entry is not None:
                    entry.delete_status = "BAD"
                    self._delete_group(entry)
        return result

    # ------------------------------------------------------------ scheduler and commands

    def sweep(self, terminate_after=None):
        """One run of the scheduled task."""
        env = self.base_env()
        env["NZBSP_TASKID"] = "1"
        return self._execute("sweep", env, terminate_after=terminate_after)

    boot = sweep  # NZBGet runs the '*' task when it starts: the first sweep writes the heartbeat

    def command(self, name, terminate_after=None):
        """A command button of the extension (exit code 93 = done, 94 = failed)."""
        env = self.base_env()
        env["NZBCP_COMMAND"] = name
        return self._execute("command", env, terminate_after=terminate_after)

    def background(self, function, *args, **kwargs):
        return Background(function, *args, **kwargs)

    def _execute(self, kind, env, terminate_after=None, timeout=120):
        if self.inprocess:
            with self._inprocess:
                log.forget_secrets()  # a fresh process knows no secrets yet
                stream = io.StringIO()
                exits = []
                try:
                    code = app.main(env, stream, self.clock, exits.append)
                finally:
                    log.set_stream(None)
                    log.forget_secrets()
            self.exits.extend(exits)
            result = RunResult(kind, code, stream.getvalue(), "", env)
        else:
            result = self._subprocess(kind, env, terminate_after, timeout)
        self._inspect(result)
        return result

    def _subprocess(self, kind, env, terminate_after, timeout):
        process = subprocess.Popen([sys.executable, MAIN], env=dict(env), cwd=self.root, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        timer = None
        if terminate_after is not None:
            timer = threading.Timer(terminate_after, lambda: process.send_signal(signal.SIGTERM))
            timer.daemon = True
            timer.start()
        try:
            out, err = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            out, err = process.communicate()
            with self.lock:
                self.violations.append("a %s run was still going after %d seconds" % (kind, timeout))
        finally:
            if timer is not None:
                timer.cancel()
        return RunResult(kind, process.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"),
                         env)

    def _inspect(self, result):
        """What every run must satisfy; the rest is for the tests."""
        with self.lock:
            self.runs.append(result)
            for line in result.lines:
                if line.startswith("[NZB] "):
                    continue
                for tag in LOG_KINDS:
                    if line.startswith(tag):
                        self.log.append((tag[1:-2].lower(), line[len(tag):]))
                        break
                else:
                    if line.strip():
                        self.violations.append("a %s run printed a line NZBGet cannot classify: %r" %
                                               (result.kind, line[:80]))
                if len(line) > 1000:
                    self.violations.append("a %s run printed a line of %d characters" % (result.kind, len(line)))
            if result.err.strip():
                self.violations.append("a %s run wrote to standard error: %r" % (result.kind, result.err[:120]))

    def runs_of(self, kind):
        return [run for run in self.runs if run.kind == kind]

    # ------------------------------------------------------------ JSON-RPC

    def _serve(self, handler, body):
        expected = "Basic " + base64.b64encode(("%s:%s" % (CONTROL_USER, CONTROL_PASSWORD)).encode()).decode()
        if handler.headers.get("Authorization", "") != expected:
            self._send(handler, 401, b"Unauthorized", "text/plain")
            return
        try:
            request = json.loads(body.decode("utf-8"))
            method = request["method"]
            params = request.get("params") or []
            if not isinstance(method, str) or not isinstance(params, list):
                raise ValueError("shape")
        except (ValueError, KeyError, TypeError, AttributeError):
            self._send(handler, 400, b"Bad request", "text/plain")
            return
        with self.lock:
            call = Call(len(self.calls) + 1, method, params)
            self.calls.append(call)
            fault = self._take_fault(method)
        if fault is not None and self._fault_reply(handler, fault):
            return
        try:
            with self.lock:
                call.result = self._dispatch(method, params, call)
            reply = {"version": "1.1", "result": call.result}
        except _RpcError as error:
            call.error = error.message
            reply = {"version": "1.1", "error": {"name": "JSONRPCError", "code": error.code,
                                                 "message": error.message}}
        self._send(handler, 200, json.dumps(reply).encode("utf-8"), "application/json")

    @staticmethod
    def _send(handler, code, body, kind):
        handler.send_response(code)
        handler.send_header("Content-Type", kind)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def fault(self, kind, method=None, times=1, delay=0.0, skip=0):
        """Make the next ``times`` calls (of ``method``, else of any) fail, after letting ``skip`` of them pass.

        kind: http500, error (JSON error answer), garbage (200 but not JSON), badstatus (not even HTTP),
        drop (connection closed without an answer), slow (answers after ``delay`` seconds).
        ``skip``: how many matching calls are answered normally first (to fail the second step of a two-step change).
        """
        with self.lock:
            self.faults.append({"kind": kind, "method": method, "times": times, "delay": delay, "skip": skip})

    def _take_fault(self, method):
        for fault in self.faults:
            if fault["method"] is not None and fault["method"].lower() != method.lower():
                continue
            if fault["skip"] > 0:
                fault["skip"] -= 1
                continue
            if fault["times"] is None or fault["times"] > 0:
                if fault["times"] is not None:
                    fault["times"] -= 1
                return fault
        return None

    def _fault_reply(self, handler, fault):
        """Carry out a fault; True when the call has been answered (or dropped)."""
        kind = fault["kind"]
        if kind == "slow":
            time.sleep(fault["delay"])
            return False
        if kind == "http500":
            self._send(handler, 500, b"Internal Server Error", "text/plain")
        elif kind == "error":
            body = json.dumps({"version": "1.1", "error": {"name": "JSONRPCError", "code": 3,
                                                           "message": "Invalid parameter"}}).encode()
            self._send(handler, 200, body, "application/json")
        elif kind == "garbage":
            self._send(handler, 200, b"this is not json", "text/html")
        elif kind == "badstatus":
            handler.wfile.write(b"not http at all\r\n\r\n")
        elif kind == "drop":
            pass
        else:
            raise ValueError("unknown fault %r" % kind)
        handler.close_connection = True
        return True

    def _dispatch(self, method, params, call):
        name = method.lower()
        if name == "listgroups":
            return [self._group_json(entry) for entry in self.queue]
        if name == "history":
            self._age_limbo()
            return [self._history_json(entry) for entry in self.history]
        if name == "status":
            return {"DownloadRate": self.download_rate, "DownloadPaused": self.download_paused,
                    "Download2Paused": self.download_paused, "ServerPaused": False, "PostPaused": False,
                    "ScanPaused": False, "QueueScriptCount": self.queue_scripts_running, "PostJobCount": 0,
                    "ThreadCount": 12, "UpTimeSec": 3600, "FreeDiskSpaceMB": 100000}
        if name == "editqueue":
            return self._editqueue(params, call)
        self._violation(call, "RPC method %s is not one Completer uses" % method)
        if name == "pausedownload":
            self.download_paused = True
            return True
        if name == "resumedownload":
            self.download_paused = False
            return True
        raise _RpcError(1, "Invalid procedure")

    def _violation(self, call, text):
        call.problems.append(text)
        self.violations.append("%s: %s" % (call, text))

    def _group_json(self, entry):
        remaining = entry.size
        return {"NZBID": entry.id, "NZBName": entry.name, "NZBNicename": entry.name, "Kind": "NZB", "URL": "",
                "NZBFilename": entry.filename, "DestDir": "", "FinalDir": "", "Category": entry.category,
                "ParStatus": "NONE", "ExParStatus": "NONE", "UnpackStatus": "NONE", "MoveStatus": "NONE",
                "ScriptStatus": "NONE", "DeleteStatus": entry.delete_status, "MarkStatus": entry.mark_status,
                "UrlStatus": "NONE", "FileSizeMB": remaining // 1048576, "FileCount": len(entry.files),
                "RemainingFileCount": len(entry.files), "RemainingSizeMB": remaining // 1048576,
                "PausedSizeMB": entry.paused_size // 1048576, "MinPostTime": min(
                    [item["posted"] for item in entry.files] or [0]), "MaxPostTime": max(
                    [item["posted"] for item in entry.files] or [0]),
                "ActiveDownloads": entry.active, "Status": entry.queue_status, "Health": 1000,
                "CriticalHealth": 900, "DupeKey": "", "DupeScore": 0, "DupeMode": "SCORE",
                "Deleted": entry.delete_status != "NONE", "Priority": entry.priority,
                "Parameters": [{"Name": name, "Value": value} for name, value in entry.params],
                "ScriptStatuses": [], "ServerStats": []}

    def _history_json(self, entry):
        size = sum(item["size"] for item in entry.files) or 0
        return {"ID": entry.id, "NZBID": entry.id, "Kind": "NZB", "Name": entry.name, "NZBName": entry.name,
                "NZBNicename": entry.name, "NZBFilename": entry.filename, "URL": "", "DestDir": "",
                "FinalDir": "", "Category": entry.category, "ParStatus": "NONE", "ExParStatus": "NONE",
                "UnpackStatus": "NONE", "MoveStatus": "NONE", "ScriptStatus": "NONE", "UrlStatus": "NONE",
                "DeleteStatus": entry.delete_status, "MarkStatus": entry.mark_status,
                "Status": entry.text_status, "HistoryTime": entry.history_time, "FileSizeMB": size // 1048576,
                "FileSizeLo": size & 0xFFFFFFFF, "FileSizeHi": size >> 32, "Health": 1000,
                "DownloadedSizeMB": entry.downloaded_mb,
                "Parameters": [{"Name": name, "Value": value} for name, value in entry.params],
                "ScriptStatuses": []}

    def _age_limbo(self):
        """Deleted downloads appear in the history a given number of history() calls after the deletion."""
        for item in list(self._limbo):
            if item[0] <= 0:
                self._limbo.remove(item)
                self.history.insert(0, item[1])
            else:
                item[0] -= 1

    def finish_deletions(self):
        """NZBGet has finally moved every deleted download that was still on its way to the history."""
        with self.lock:
            for item in list(self._limbo):
                self._limbo.remove(item)
                self.history.insert(0, item[1])

    def _editqueue(self, params, call):
        if len(params) != 3:
            self._violation(call, "editqueue was given %d parameters instead of 3" % len(params))
            raise _RpcError(3, "Invalid parameter")
        command, text, ids = params
        if not isinstance(command, str) or not isinstance(text, str) or not isinstance(ids, list):
            self._violation(call, "editqueue parameters have the wrong types")
            raise _RpcError(3, "Invalid parameter")
        clean_ids = [number for number in ids if type(number) is int]
        if len(ids) != 1 or len(clean_ids) != 1 or clean_ids[0] <= 0:
            self._violation(call, "editqueue was given %r, not exactly one positive integer id" % (ids,))
        if command in GROUP_COMMANDS:
            target = next((entry for entry in self.queue if entry.id in clean_ids), None)
            where = "queue"
        elif command in HISTORY_COMMANDS:
            target = next((entry for entry in self.history if entry.id in clean_ids), None)
            where = "history"
        else:
            target, where = None, ""
            self._violation(call, "%s is not a command Completer may use" % command)
        if where:
            if target is None:
                self._violation(call, "%s names %r, which is not in the %s" % (command, ids, where))
            elif not target.get("Completer_Token", ""):
                self._violation(call, "%s on download %d, which carries no Completer token" % (command, target.id))
        if command in ("GroupSetParameter", "HistorySetParameter"):
            if not re.match(r"^Completer_[A-Za-z]+=", text):
                self._violation(call, "%s writes %r, which is not a parameter of Completer" % (command, text[:40]))
        if self.allowed_ids is not None and any(number not in self.allowed_ids for number in clean_ids):
            self._violation(call, "%s touches %r, outside the downloads this run is about" % (command, ids))
        return self._apply(command, text, ids)

    def _apply(self, command, text, ids):
        """What NZBGet does with an edit (the unwanted commands too, so that their damage shows)."""
        ok = False
        for number in ids:
            if type(number) is not int:
                continue
            if command.startswith("File"):
                ok = self._edit_file(command, number) or ok
            elif command.startswith("Group"):
                entry = next((item for item in self.queue if item.id == number), None)
                if entry is not None:
                    ok = self._edit_group(command, text, entry) or ok
            elif command.startswith("History"):
                entry = next((item for item in self.history if item.id == number), None)
                if entry is not None:
                    ok = self._edit_history(command, text, entry) or ok
            else:
                raise _RpcError(3, "Invalid parameter (Command)")
        return ok

    def _edit_group(self, command, text, entry):
        if command == "GroupResume":
            for item in entry.files:
                item["paused"] = False
        elif command == "GroupPause":
            for item in entry.files:
                item["paused"] = True
        elif command == "GroupPauseExtraPars":
            self._pause_extra_pars(entry)
        elif command == "GroupSetParameter":
            if "=" in text:
                name, value = text.split("=", 1)
                entry.set(name, value)
        elif command in ("GroupDelete", "GroupFinalDelete", "GroupDupeDelete", "GroupParkDelete"):
            self._delete_group(entry)
        else:
            raise _RpcError(3, "Invalid parameter (Command)")
        return True

    def _edit_file(self, command, file_id):
        for entry in self.queue:
            for item in entry.files:
                if item["id"] == file_id:
                    if command == "FileDelete":
                        entry.files.remove(item)
                    elif command == "FilePause":
                        item["paused"] = True
                    elif command == "FileResume":
                        item["paused"] = False
                    else:
                        raise _RpcError(3, "Invalid parameter (Command)")
                    return True
        return False

    def _edit_history(self, command, text, entry):
        if command == "HistoryMarkBad":
            entry.mark_status = "BAD"
        elif command == "HistoryMarkGood":
            entry.mark_status = "GOOD"
        elif command == "HistorySetParameter":
            if "=" not in text:
                return False
            name, value = text.split("=", 1)
            entry.set(name, value)
        elif command in ("HistoryDelete", "HistoryFinalDelete"):
            self.history.remove(entry)
        else:
            raise _RpcError(3, "Invalid parameter (Command)")
        return True

    @staticmethod
    def _pause_extra_pars(entry):
        """QueueEditor::PausePars with extraParsOnly: keep the plain par2 files, pause the repair volumes."""
        pars = [item for item in entry.files if item["par"] and not item["vol"]]
        vols = [item for item in entry.files if item["par"] and item["vol"]]
        if pars:
            for item in vols:
                item["paused"] = True
            return
        smallest = None
        for item in vols:
            if smallest is None:
                smallest = item
            elif smallest["size"] > item["size"]:
                smallest["paused"] = True
                smallest = item
            else:
                item["paused"] = True

    def _delete_group(self, entry):
        """GroupDelete: the files go, the download moves to the history (after ``history_delay`` history calls)."""
        entry.files = []
        if entry.delete_status == "NONE":
            entry.delete_status = "MANUAL"
        entry.history_time = int(self.clock())
        if entry in self.queue:
            self.queue.remove(entry)
        if self.history_delay > 0:
            self._limbo.append([self.history_delay, entry])
        else:
            self.history.insert(0, entry)
        self._emit("NZB_DELETED", entry)

    # ------------------------------------------------------------ looking at the state

    def find_queue(self, nzb_id):
        return next((entry for entry in self.queue if entry.id == nzb_id), None)

    def find_history(self, nzb_id):
        return next((entry for entry in self.history if entry.id == nzb_id), None)

    def entry(self, nzb_id):
        found = self.find_queue(nzb_id) or self.find_history(nzb_id)
        if found is None:
            raise KeyError("no download %r" % (nzb_id,))
        return found

    def where(self, nzb_id):
        if self.find_queue(nzb_id):
            return "queue"
        if self.find_history(nzb_id):
            return "history"
        return None

    def param(self, nzb_id, name, default=None):
        return self.entry(nzb_id).get(name, default)

    def status(self, nzb_id):
        found = self.entry(nzb_id)
        return found.queue_status if self.find_queue(nzb_id) else found.text_status

    def snapshot(self, ids=None):
        """Everything observable about downloads (all of them, or ``ids``): to prove nothing changed."""
        with self.lock:
            shot = {}
            for where, entries in (("queue", self.queue), ("history", self.history)):
                for entry in entries:
                    if ids is None or entry.id in ids:
                        shot[entry.id] = {
                            "where": where, "name": entry.name, "category": entry.category,
                            "status": entry.queue_status if where == "queue" else entry.text_status,
                            "delete": entry.delete_status, "mark": entry.mark_status,
                            "params": sorted((name, value) for name, value in entry.params),
                            "files": [(item["id"], item["name"], item["paused"]) for item in entry.files]}
            return shot

    @property
    def edits(self):
        """Every editqueue call: (command, text, ids)."""
        return [(call.command, call.text, call.ids) for call in self.calls if call.method.lower() == "editqueue"]

    def edited_ids(self):
        found = set()
        for _command, _text, ids in self.edits:
            found.update(number for number in ids if type(number) is int)
        return found

    def sonarr_view(self, nzb_id):
        """How Sonarr/Radarr would judge the download now: failed, warning, completed, None (not listed / skipped)."""
        with self.lock:
            entry = self.find_history(nzb_id)
            return sonarr_status(self._history_json(entry)) if entry is not None else None

    # ------------------------------------------------------------ what a user does in the web interface

    def user_resume(self, nzb_id):
        with self.lock:
            for item in self.entry(nzb_id).files:
                item["paused"] = False

    def user_pause(self, nzb_id):
        with self.lock:
            for item in self.entry(nzb_id).files:
                item["paused"] = True

    def user_delete(self, nzb_id):
        with self.lock:
            entry = self.find_queue(nzb_id)
            if entry is not None:
                self._delete_group(entry)

    def assert_secrets_hidden(self):
        """No credential in anything a script printed."""
        for run in self.runs:
            for secret in self.secrets:
                if secret in run.out or secret in run.err:
                    raise AssertionError("a %s run printed a credential" % run.kind)


class SimTestCase(unittest.TestCase):
    """Base class: makes sims and fake news servers that are cleaned up, and checks every sim at the end.

    ``world()`` is the usual start of a test: a fake NZBGet wired to one fake news server. ``post()`` then
    makes posts whose dates fit that sim's clock.
    """

    sim = None

    def make_news(self, have=(), **switches):
        news = fakenntp.FakeNews(have=have, **switches).start()
        self.addCleanup(news.stop)
        return news

    def make_sim(self, **kwargs):
        sim = Sim(**kwargs).start()
        self.addCleanup(sim.stop)
        self.addCleanup(self._check_sim, sim)  # cleanups run last-in first-out: this one runs before stop()
        return sim

    def world(self, have=(), mode="Enforce", options=None, nzbget=None, inprocess=True, boot=True, **switches):
        """A fake NZBGet wired to one fake news server that holds the message-ids ``have``.

        ``mode`` is Completer's Mode option; ``options`` more options of the extension, ``nzbget`` NZBGet's own
        (ParCheck, ...), ``switches`` the behaviour switches of the news server. In Enforce mode the first
        scheduled check is run, as NZBGet does when it starts: Completer holds new downloads only while that
        safety net is known to be working. ``inprocess=False`` runs every script as a real process, which needs
        the real clock (``sim.advance`` is then not available).
        """
        news = self.make_news(have=have, **switches)
        merged = {"Mode": mode}
        merged.update(options or {})
        sim = self.make_sim(clock=SimClock() if inprocess else time.time, options=merged, nzbget=nzbget,
                            inprocess=inprocess)
        sim.add_server(news)
        self.sim = sim
        if boot and mode.lower() == "enforce":
            self.assertEqual(sim.boot().code, 0)
        return sim

    def post(self, tag, **kwargs):
        """A made-up post (see ``nzbtools.make_post``) whose dates are relative to the sim's clock."""
        kwargs.setdefault("now", self.sim.now if self.sim is not None else time.time())
        return nzbtools.make_post(tag, **kwargs)

    def _check_sim(self, sim):
        if not sim.expect_violations:
            self.assertEqual(sim.violations, [], "the fake NZBGet saw rule violations")
        sim.assert_secrets_hidden()
