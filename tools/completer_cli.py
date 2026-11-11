#!/usr/bin/env python3
"""Check nzb files against the news servers of an nzbget.conf, without NZBGet.

Usage::

    python3 tools/completer_cli.py --config /path/to/nzbget.conf \\
        [--connections 2] [--max-articles 1200] [--time-limit 60] \\
        [--ignore-servers 7-11] [--now UNIXTIME] [--verbose] NZB [NZB ...]

This is the same check the Completer extension runs inside NZBGet, using the
same library: it asks every news server of the config, with the NNTP ``STAT``
command, whether a sample of the articles named in each nzb exists.

What it does, and nothing else:

* it READS the config file and the nzb files you name;
* it ASKS the news servers whether articles exist (nothing is downloaded);
* it never writes a file, never contacts NZBGet and never changes anything.

Output: one line per nzb on standard output::

    PASS     <file name>  <what was found>
    FAIL     ...    (cannot be completed)
    SUSPECT  ...    (looks bad, but a server could not be asked)
    GONE     ...    (nothing found anywhere; could be a fault at the servers)
    ERROR    ...    (the file could not be read, or no server could be asked)

``--verbose`` adds one indented line per server. Servers are only ever
called ``server <number>``; no host name, port, user name or password from
the config is printed, and the passwords are also removed from any error text.

Exit code: 0 when every nzb produced a verdict (an ERROR verdict because no
server could be asked still counts: read the line), 1 when at least one nzb
could not be read, 2 for a wrong command line or a config file
without a usable news server.
"""

import argparse
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Completer"))

from completer import checker, log, nntp, nzbfile, settings  # noqa: E402

# The server fields of nzbget.conf (ServerN.Field) that the library looks at.
SERVER_FIELDS = ("NAME", "HOST", "PORT", "USERNAME", "PASSWORD", "ENCRYPTION", "CERTVERIFICATION", "IPVERSION",
                 "CONNECTIONS", "LEVEL", "GROUP", "ACTIVE", "OPTIONAL", "RETENTION", "JOINGROUP")
VERDICTS = ("pass", "fail", "suspect", "gone", "error")

_SERVER_KEY = re.compile(r"^server(\d+)\.(\w+)$", re.IGNORECASE)


def parse_config(text):
    """``{key: value}`` of an nzbget.conf: ``Key=Value`` lines, ``#`` comments, last one wins.

    Only the first ``=`` separates key and value, so values may contain ``=``.
    """
    found = {}
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            found[key] = value.strip()
    return found


def build_env(config, connections, max_articles, ignore_servers):
    """The environment NZBGet would give an extension, built from parsed config and options."""
    env = {}
    for key, value in config.items():
        match = _SERVER_KEY.match(key)
        if match:
            field = match.group(2).upper()
            if field in SERVER_FIELDS:
                env["NZBOP_SERVER%d_%s" % (int(match.group(1)), field)] = value
            continue
        lowered = key.lower()
        if lowered == "certcheck":
            env["NZBOP_CertCheck"] = env["NZBOP_CERTCHECK"] = value
        elif lowered == "certstore" and "${" not in value:  # an unexpanded variable is no usable path
            env["NZBOP_CertStore"] = env["NZBOP_CERTSTORE"] = value
    env["NZBPO_Connections"] = str(connections)
    env["NZBPO_MaxArticles"] = str(max_articles)
    env["NZBPO_IgnoreServers"] = ignore_servers or ""
    env["NZBPO_Mode"] = "DryRun"
    return env


def _out(text="", stream=None):
    print(log.clean(text), file=stream if stream is not None else sys.stdout)


def _parser():
    parser = argparse.ArgumentParser(
        prog="completer_cli.py",
        description="Check nzb files against the news servers of an nzbget.conf, without NZBGet. "
                    "Only reads files and asks the servers whether articles exist.")
    parser.add_argument("--config", required=True, help="path of nzbget.conf")
    parser.add_argument("--connections", type=int, default=2, help="connections per server (1-8, default 2)")
    parser.add_argument("--max-articles", type=int, default=1200,
                        help="most articles to ask about per nzb (100-20000, default 1200)")
    parser.add_argument("--time-limit", type=float, default=60.0, help="seconds allowed per nzb (default 60)")
    parser.add_argument("--ignore-servers", default="", help="server numbers to leave out, e.g. 3,7-11")
    parser.add_argument("--now", type=float, default=None, help="unix time to take as 'now' (default: the clock)")
    parser.add_argument("--verbose", action="store_true", help="also print one line per server")
    parser.add_argument("nzb", nargs="+", help="nzb file(s) to check")
    return parser


def _check_one(path, servers, options, time_limit, now):
    """(verdict, text, outcome, unreadable) for one nzb file."""
    try:
        nzb = nzbfile.parse(path)
    except (nzbfile.NzbError, OSError) as exc:
        return "error", str(exc) or exc.__class__.__name__, None, True
    try:
        outcome = checker.check(nzb, servers, options, nntp.Deadline(time_limit), now)
    except Exception as exc:  # the library should not raise; one bad nzb must not stop the others
        return "error", "the check failed (%s)" % exc.__class__.__name__, None, True
    return outcome.verdict, outcome.describe(), outcome, False


def main(argv=None):
    """Run the tool; returns the exit code."""
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    try:
        with open(args.config, "r", encoding="utf-8-sig", errors="replace") as handle:
            config = parse_config(handle.read())
    except OSError:
        _out("cannot read the config file", sys.stderr)
        return 2

    env = build_env(config, args.connections, args.max_articles, args.ignore_servers)
    all_servers = settings.load_servers(env)
    options = settings.Settings(env)
    for server in all_servers:
        server.name = ""  # a name is the owner's own wording; only the number is ever shown
        log.add_secret(server.host)
    usable = [server for server in all_servers
              if server.active and server.connections > 0 and server.id not in options.ignore_servers]
    if not usable:
        _out("the config has no usable news server (one that is active, has connections and is not ignored)",
             sys.stderr)
        return 2

    now = time.time() if args.now is None else args.now
    counts = dict.fromkeys(VERDICTS, 0)
    unreadable_count = 0
    for path in args.nzb:
        name = os.path.basename(path) or path
        verdict, text, outcome, unreadable = _check_one(path, all_servers, options, args.time_limit, now)
        unreadable_count += unreadable
        verdict = verdict if verdict in counts else "error"
        counts[verdict] += 1
        _out("%-7s %s  %s" % (verdict.upper(), name, text))
        if args.verbose and outcome is not None:
            for line in outcome.servers:
                _out("        " + line)
            for line in outcome.unreachable:
                _out("        could not be asked: " + line)
            for line in outcome.left_out:
                _out("        left out: " + line)

    _out("summary: %d nzb, %s" % (len(args.nzb), ", ".join("%d %s" % (counts[v], v) for v in VERDICTS)))
    return 1 if unreadable_count else 0


if __name__ == "__main__":
    sys.exit(main())
