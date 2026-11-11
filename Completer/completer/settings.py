"""Everything NZBGet tells the extension through its environment.

NZBGet starts an extension with its whole configuration in environment
variables: ``NZBOP_*`` for NZBGet's own options (news servers included),
``NZBPO_*`` for the options of this extension and ``NZBPR_*`` for the
parameters of the download the script was started for. Nothing is read from
disk and nothing here is ever printed.
"""

import re

from . import log

NAME = "Completer"  # the extension's name in manifest.json

# Per-download state, stored by NZBGet as parameters of that one download.
TOKEN = "Completer_Token"    # random id given at scan time; proves the download is ours
STATE = "Completer_State"
SINCE = "Completer_Since"    # when it was put on hold (unix time)
CHECKS = "Completer_Checks"  # how many times it has been checked
NEXT = "Completer_Next"      # when a waiting download is checked again (unix time)
FILE = "Completer_File"      # NZBGet's copy of the nzb, needed to check again later
INFO = "Completer_Info"      # last result in words

PENDING = "pending"      # on hold, first check not done yet
WAITING = "waiting"      # on hold, will be checked again by the scheduler
PASSED = "passed"
FAILED = "failed"
RELEASED = "released"    # resumed without a verdict (fail-open)
FAILING = "failing"      # being removed as failed by the scheduler
HELD = (PENDING, WAITING)

NO_PAR_ALLOWANCE = 0.15  # NZBGet's own estimate when it recognises no par2 files

_SPECIAL = ".:*!\"$%&/()=`+~#'{}[]@- "


def normalise(name):
    """The second spelling NZBGet gives every variable: upper case, punctuation as '_'."""
    return "".join("_" if ch in _SPECIAL else ch for ch in name).upper()


def _lookup(env, prefix, name, default):
    value = env.get(prefix + name)
    if value is None:
        value = env.get(prefix + normalise(name))
    return default if value is None else value


def nzbget_option(env, name, default=""):
    return _lookup(env, "NZBOP_", name, default)


def own_option(env, name, default=""):
    return _lookup(env, "NZBPO_", name, default)


def parameter(env, name, default=""):
    return _lookup(env, "NZBPR_", name, default)


def _yes(value, default=False):
    value = str(value).strip().lower()
    if value in ("yes", "true", "on", "1"):
        return True
    if value in ("no", "false", "off", "0"):
        return False
    return default


def _number(value, default, low, high, kind=int):
    try:
        number = kind(float(str(value).strip()))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _id_list(text):
    """'3, 7-11' -> {3, 7, 8, 9, 10, 11}"""
    found = set()
    for part in re.split(r"[,; ]+", str(text or "")):
        match = re.match(r"^(\d+)(?:-(\d+))?$", part)
        if not match:
            continue
        first = int(match.group(1))
        last = int(match.group(2) or first)
        if 0 < first <= last and last - first < 1000:
            found.update(range(first, last + 1))
    return frozenset(found)


class Server(object):
    """One news server as configured in NZBGet."""

    def __init__(self, number):
        self.id = number
        self.name = ""
        self.host = ""
        self.port = 119
        self.username = ""
        self.password = ""
        self.tls = False
        self.cert = "strict"      # none | minimal | strict
        self.ip_version = "auto"  # auto | ipv4 | ipv6
        self.connections = 0
        self.level = 0
        self.group = 0
        self.active = True
        self.optional = False
        self.retention = 0        # days, 0 = unlimited
        self.join_group = False

    def label(self):
        return "server %d (%s)" % (self.id, self.name) if self.name else "server %d" % self.id


def load_servers(env):
    """The news servers NZBGet uses, in NZBGet's numbering. Credentials stay in memory only."""
    numbers = set()
    for key in env:
        match = re.match(r"^NZBOP_SERVER(\d+)_HOST$", key)
        if match:
            numbers.add(int(match.group(1)))
    global_check = _yes(nzbget_option(env, "CertCheck", "yes"), True)
    servers = []
    for number in sorted(numbers):
        def get(field, default="", number=number):
            return env.get("NZBOP_SERVER%d_%s" % (number, field), default)

        server = Server(number)
        server.host = get("HOST").strip()
        if not server.host:
            continue
        server.name = log.clean(get("NAME").strip(), 40)
        server.port = _number(get("PORT"), 119, 1, 65535)
        server.username = get("USERNAME")
        server.password = get("PASSWORD")
        server.tls = _yes(get("ENCRYPTION"))
        cert = get("CERTVERIFICATION").strip().lower()
        if not global_check:
            server.cert = "none"  # NZBGet verifies nothing when its CertCheck option is off
        else:
            server.cert = cert if cert in ("none", "minimal", "strict") else "strict"
        ip_version = get("IPVERSION").strip().lower()
        server.ip_version = ip_version if ip_version in ("ipv4", "ipv6") else "auto"
        server.connections = _number(get("CONNECTIONS"), 0, 0, 1000)
        server.level = _number(get("LEVEL"), 0, 0, 99)
        server.group = _number(get("GROUP"), 0, 0, 99)
        server.active = _yes(get("ACTIVE", "yes"), True)
        server.optional = _yes(get("OPTIONAL"))
        server.retention = _number(get("RETENTION"), 0, 0, 100000)
        server.join_group = _yes(get("JOINGROUP"))
        log.add_secret(server.password)
        if len(server.username) >= 5:
            log.add_secret(server.username)
        servers.append(server)
    return servers


class Settings(object):
    """The extension's own options plus the few NZBGet options it depends on."""

    def __init__(self, env):
        mode = own_option(env, "Mode", "DryRun").strip().lower()
        self.mode = mode if mode in ("enforce", "off") else "dryrun"
        self.categories = frozenset(
            part.strip().lower() for part in re.split(r"[,;]", own_option(env, "Categories")) if part.strip())
        self.connections = _number(own_option(env, "Connections"), 2, 1, 8)
        self.fresh_seconds = int(_number(own_option(env, "FreshHours"), 2.0, 0.0, 72.0, float) * 3600)
        self.max_articles = _number(own_option(env, "MaxArticles"), 1200, 100, 20000)
        self.time_limit = _number(own_option(env, "TimeLimit"), 45, 10, 300)
        self.max_fill = _number(own_option(env, "MaxFillPercent"), 100.0, 0.0, 100.0, float) / 100.0
        self.ignore_servers = _id_list(own_option(env, "IgnoreServers"))
        self.verbose = _yes(own_option(env, "Verbose"))

        self.par_check_force = nzbget_option(env, "ParCheck").strip().lower() == "force"
        self.temp_dir = nzbget_option(env, "TempDir").strip()
        self.cert_store = nzbget_option(env, "CertStore").strip()
        extensions = re.split(r"[,;]", nzbget_option(env, "Extensions"))
        self.scheduler_active = NAME in [part.strip() for part in extensions]

    def wants(self, category):
        return not self.categories or str(category or "").strip().lower() in self.categories


class Control(object):
    """Where NZBGet's own remote-control interface listens."""

    def __init__(self, env):
        host = nzbget_option(env, "ControlIP", "127.0.0.1").strip()
        if host in ("", "0.0.0.0"):
            host = "127.0.0.1"
        elif host in ("::", "[::]"):
            host = "::1"
        self.host = host
        self.port = _number(nzbget_option(env, "ControlPort"), 6789, 1, 65535)
        self.username = nzbget_option(env, "ControlUsername")
        self.password = nzbget_option(env, "ControlPassword")
        log.add_secret(self.password)

    def url(self):
        host = "[%s]" % self.host if ":" in self.host else self.host
        return "http://%s:%d/jsonrpc" % (host, self.port)
