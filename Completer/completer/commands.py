"""The three command buttons Completer adds to NZBGet's settings page.

``TestServers``  logs in to every news server the way a check would and says how it went
``ReleaseAll``   lets every download that Completer holds start (the panic button)
``Report``       compares what Completer said about downloads with what NZBGet finally did

None of them changes anything except ``ReleaseAll``, which only resumes downloads
that carry Completer's token and are on hold.
"""

import threading

from . import log
from . import settings as cfg
from .app import COMMAND_ERROR, COMMAND_OK, Subject
from .nntp import Deadline, ServerSession
from .rpc import RpcError, params_of

RESULTS = ("finished", "failed on health", "failed otherwise", "removed", "deleted")


def _headroom(run):
    """Say how busy NZBGet is, because a test while it is idle proves nothing about free connections."""
    try:
        guard = run.guard()
        active = sum(int(group.get("ActiveDownloads") or 0) for group in guard.groups())
        rate = float((guard.rpc.call("status") or {}).get("DownloadRate") or 0)
    except (RpcError, TypeError, ValueError):
        log.info("Completer: NZBGet could not be asked how busy it is")
        return
    if active > 0:
        log.info("Completer: NZBGet is downloading right now (%d connections busy, %.1f MB/s), so the test below "
                 "runs while its own connections are open" % (active, rate / 1048576.0))
    else:
        log.info("Completer: NZBGet is not downloading right now, so this test says nothing about free "
                 "connections while it is")


def test_servers(run):
    settings = run.settings
    servers = run.usable_servers()
    if not servers:
        log.error("Completer: NZBGet has no active news server to test")
        return COMMAND_ERROR
    _headroom(run)
    deadline = Deadline(max(10.0, run.limit.left() - 10.0))
    results = {}

    def work(server):
        session = ServerSession(server, settings.connections, deadline, settings.cert_store)
        try:
            results[server.id] = (session.test(), session.limit, session.failure)
        finally:
            session.close()

    workers = [threading.Thread(target=work, args=(server,), daemon=True) for server in servers]
    for worker in workers:
        worker.start()
    for worker in workers:
        while worker.is_alive() and not deadline.expired():
            worker.join(0.1)
    everything = True
    for server in servers:
        if server.id not in results:
            log.warning("Completer: %s: no answer in time" % server.label())
            everything = False
            continue
        opened, wanted, failure = results[server.id]
        if opened >= wanted:
            log.info("Completer: %s: %d of %d connections logged in" % (server.label(), opened, wanted))
            continue
        everything = False
        why = failure.describe() if failure is not None else "unknown"
        optional = " (optional: NZBGet passes over it as well)" if server.optional and opened == 0 else ""
        if opened:
            log.warning("Completer: %s: only %d of %d connections logged in (%s)%s" %
                        (server.label(), opened, wanted, why, optional))
        else:
            log.warning("Completer: %s: could not log in (%s)%s" % (server.label(), why, optional))
        if failure is not None and failure.kind == "busy":
            log.warning("Completer: %s: it has no connection to spare, NZBGet may be using all of them. Lower this "
                        "server's Connections in NZBGet by %d so that Completer has room to check" %
                        (server.label(), wanted - opened))
    return COMMAND_OK if everything else COMMAND_ERROR


def release_all(run):
    guard = run.guard()
    try:
        groups = guard.groups()
    except RpcError as error:
        log.error("Completer: NZBGet could not be asked for its queue (%s)" % error)
        return COMMAND_ERROR
    held = []
    for entry in groups:
        subject = Subject.from_entry(entry)
        if entry.get("Kind") == "NZB" and subject.token and subject.state in cfg.HELD + (cfg.FAILING,):
            held.append(subject)
    failed = 0
    for subject in held:
        if not run.let_go(subject, "released by the ReleaseAll button", own=False):
            failed += 1
    log.info("Completer: ReleaseAll: %d downloads were held by Completer, %d released" %
             (len(held), len(held) - failed))
    return COMMAND_ERROR if failed else COMMAND_OK


def _result(item, params):
    """What finally happened to a download, in one word of RESULTS (or '' when it is not decided)."""
    status = str(item.get("Status", ""))
    if params.get(cfg.STATE) == cfg.FAILED and str(item.get("DeleteStatus", "")) in ("MANUAL", "BAD"):
        return "removed"
    if status.startswith(("SUCCESS", "WARNING")):
        return "finished"
    if status.startswith("DELETED"):
        return "deleted"
    if status.startswith("FAILURE"):
        return "failed on health" if "HEALTH" in status or item.get("DeleteStatus") == "HEALTH" \
            else "failed otherwise"
    return ""


def _megabytes(item, key):
    try:
        return float(item.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def report(run):
    guard = run.guard()
    try:
        history = guard.history()
        queue = guard.groups()
    except RpcError as error:
        log.error("Completer: NZBGet could not be asked for its history (%s)" % error)
        return COMMAND_ERROR
    table = {}
    dry = enforced = 0
    saved = would_save = 0.0
    misses = alarms = right = 0
    for item in history:
        if item.get("Kind") != "NZB":
            continue
        params = params_of(item)
        word = params.get(cfg.VERDICT, "")
        if not word:
            continue
        verdict = word.partition("/")[0]
        result = _result(item, params)
        if not result:
            continue
        if params.get(cfg.STATE) == cfg.OBSERVED:
            dry += 1
        else:
            enforced += 1
        table.setdefault(verdict, dict.fromkeys(RESULTS, 0))[result] += 1
        if result == "removed":
            saved += _megabytes(item, "FileSizeMB")
        elif verdict in ("fail", "gone"):
            if result == "finished":
                alarms += 1
            elif result.startswith("failed"):
                right += 1
                would_save += _megabytes(item, "DownloadedSizeMB")
        elif verdict == "pass" and result == "failed on health":
            misses += 1
    total = sum(sum(row.values()) for row in table.values())
    log.info("Completer report: %d downloads in the history carry a verdict (%d from a dry run, %d while "
             "holding)" % (total, dry, enforced))
    if total:
        log.info("Completer: %-8s %9s %17s %17s %8s %8s" % (("verdict",) + RESULTS))
        for verdict in ("pass", "fail", "gone", "suspect", "error"):
            row = table.get(verdict)
            if row:
                log.info("Completer: %-8s %9d %17d %17d %8d %8d" % ((verdict,) + tuple(row[name] for name in RESULTS)))
        log.info("Completer: passed, but failed on health afterwards (a miss): %d" % misses)
        log.info("Completer: flagged fail or gone, but finished fine (a false alarm, or a post that was still "
                 "spreading): %d" % alarms)
        log.info("Completer: flagged fail or gone, and failed too (right): %d, %.1f GB of it downloaded before "
                 "NZBGet gave up" % (right, would_save / 1024.0))
        log.info("Completer: removed by Completer before anything was downloaded: %d downloads of %.1f GB" %
                 (sum(row["removed"] for row in table.values()), saved / 1024.0))
    pending = waiting = 0
    oldest = 0.0
    now = run.clock()
    for entry in queue:
        subject = Subject.from_entry(entry)
        if entry.get("Kind") != "NZB" or not subject.token:
            continue
        if subject.state == cfg.PENDING:
            pending += 1
        elif subject.state == cfg.WAITING:
            waiting += 1
        else:
            continue
        if subject.since > 0:
            oldest = max(oldest, now - subject.since)
    log.info("Completer: on hold now: %d waiting for their first check, %d waiting for a recheck%s" %
             (pending, waiting, ", the oldest for %d minutes" % (oldest // 60) if pending + waiting else ""))
    return COMMAND_OK
