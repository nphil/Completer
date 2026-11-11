# Completer

**Completer is an NZBGet extension that asks your news servers, before anything is downloaded, whether a new download can be completed.** It holds only that one download for a few seconds, lets it go when it can be completed, and fails it the way NZBGet does (marked *bad*) when it provably cannot, so Sonarr and Radarr blocklist the release and search for another one.

```
new nzb arrives --> held --> asked --> pass   --> released
(one download)    (paused)  (STAT)     fail   --> failed (marked bad)
                                       unsure --> released
```

*Terms used below.* **nzb**: the small file that lists the pieces ("articles") of a download. **News server**: the Usenet provider NZBGet downloads from. **STAT**: a one-line question to a news server, "do you have this article?" It answers yes or no and sends no data. **Held**: paused by Completer, for this one download only.

> ### Completer only ever acts on the one download it checked
>
> A check is about **one** download: the one that was just added, or the one that Completer itself put on hold. Whatever the check finds is applied to **that download and to no other**. Every other download in the queue, whether it is paused, on hold for some other reason or already downloading, is never looked at by that check and never changed by it.
>
> How this is enforced: the nzb file that is examined is the one NZBGet names for that download, or a stored file whose size and checksum match what was scanned, so a different file is never judged. Every change names that download by its numeric id, and immediately before each change the queue is read again; the change is only sent when that id is still a plain nzb that carries the secret token Completer gave it and still has the same name. A test ([`tests/test_guards.py`](tests/test_guards.py)) fills a queue with many other paused downloads next to the one being checked and proves that not one of them receives a single command.
>
> The extension this one replaces does not have this property: see [Completion versus Completer](#completion-versus-completer).

## Contents

1. [Why it exists](#why-it-exists)
2. [Install](#install)
3. [Settings](#settings)
4. [The three buttons](#the-three-buttons)
5. [How it decides](#how-it-decides)
6. [What Sonarr and Radarr see](#what-sonarr-and-radarr-see)
7. [Safety guarantees](#safety-guarantees)
8. [Completion versus Completer](#completion-versus-completer)
9. [Limits](#limits)
10. [Uninstall and rollback](#uninstall-and-rollback)
11. [The offline tool](#the-offline-tool)
12. [Tests, layout, licence](#tests-project-layout-licence)

## Why it exists

A Usenet release can be incomplete: some of its articles were never posted or have been removed. NZBGet finds that out only while downloading, so the download runs for a while (sometimes for hours) before it fails. Asking the servers first takes seconds and costs no bandwidth.

The idea of checking before downloading comes from the NZBGet **Completion** extension. Completer is not a port of it: it is a new design with new code (MIT licence). The Completion extension, as of version 3.2, pauses every new nzb and the whole download queue while it checks, and has other weaknesses that are compared with Completer [in a table below](#completion-versus-completer). Completer was written so that a check can never disturb any other download.

## Install

You need NZBGet 26 or newer and Python 3.9 or newer on the machine that runs NZBGet.

1. Copy the `Completer` folder (the one that contains `main.py` and `manifest.json`) into NZBGet's scripts folder, for example `/opt/nzbget/scripts`. This is the folder named by NZBGet's `ScriptDir` option.
2. Make sure NZBGet's user can read the folder and the files in it.
3. Open NZBGet's web interface: **Settings > Extension Scripts**. `Completer` must be listed there.
4. In **Settings > Extension Scripts**, add `Completer` to **Extensions**, and put it **first** in **ScriptOrder**. It must be in the global Extensions list: that is what makes it run for every new download and what makes its scheduled check run.
5. Save, then reload NZBGet. Options are read when Completer starts, so every change in the settings needs a reload.
6. **Leave `Mode` on `DryRun` first.** In a dry run nothing is held, paused or failed. Completer checks every new download and writes what it *would* have done into NZBGet's log, for example:
   `Completer: Some.Release.Name: would pass: 120 of 1500 articles checked, all there`
   and for a download that cannot be completed
   `Completer: Some.Release.Name: would be failed: 31.2% of it is missing on every server and only 9.8% can be repaired (400 of 1500 articles checked)`.
   (The numbers here are only an example.) The verdict is also stored on the download as the parameters `Completer_Info` and `Completer_Verdict`.
7. After some downloads have come and gone, press **Report** (see below) to compare Completer's verdicts with what NZBGet really did.
8. When you are happy, press **TestServers** while NZBGet is downloading (see below), then set `Mode` to `Enforce`, save and reload.

Completer only starts holding downloads once its scheduled check has reported in (that happens when NZBGet starts it, then every five minutes). After an install or reload, the first downloads can therefore pass without being held; the log says so.

## Settings

Values outside the allowed range are moved to the nearest allowed value; a value that cannot be read gives the default. A `Mode` that is not one of the three words means `DryRun`.

| Option | Default | Meaning |
|---|---|---|
| `Mode` | `DryRun` | `DryRun`: check and log only; nothing is held, paused or failed. `Enforce`: hold new downloads while they are checked, release the ones that pass, fail the ones that cannot be completed. `Off`: check nothing and let go of anything still held. |
| `Categories` | empty | Only downloads in these categories are checked (comma separated, case does not matter). Empty means every category. |
| `FreshHours` | `2` | How long after it was posted a download counts as fresh (0 to 72 hours). A fresh download that looks incomplete is held and checked again instead of being failed at once; the check after the fresh time is final. `0` turns this off. |
| `Connections` | `2` | Connections opened to one news server while checking (1 to 8). Never more than the connections NZBGet is given for that server. |
| `MaxArticles` | `1200` | The most articles asked about for one download (100 to 20000). Most downloads need far fewer. |
| `TimeLimit` | `45` | The most seconds one check may take (10 to 300). When time runs out without a certain answer, the download is let through (or, if it looked bad but a server could not be asked, rechecked a few times first). |
| `MaxFillPercent` | `100` | Fail a download when more than this share of it (percent) exists only on fill servers, that is servers in a level above the first. `100` switches it off. |
| `IgnoreServers` | empty | Numbers of NZBGet news servers that Completer never asks, for example `3, 7-11`. Empty means every active server. |
| `Verbose` | `no` | `yes` writes the details of every check into the log: questions per server and how long they took. |

## The three buttons

They appear on the Completer settings page.

- **TestServers**: logs in to every active news server with as many connections as a check uses, and logs one line per server (`2 of 2 connections logged in`, or a warning saying how many worked and why the rest did not). It first says whether NZBGet is downloading right now: only a test made *while NZBGet downloads* tells you whether the provider has connections to spare. The button reports an error when any server fell short.
- **Report**: reads NZBGet's history and compares Completer's verdicts with what NZBGet finally did. It logs a table (per verdict: finished, failed on health, failed otherwise, removed, deleted), then the number of *misses* (said pass, but the download failed on health), *false alarms* (said fail or gone, but it finished fine, or a post that was still spreading), *right calls* (said fail or gone, and it failed too, with the amount that was downloaded before NZBGet gave up), the amount removed by Completer before anything was downloaded, and how many downloads are on hold right now.
- **ReleaseAll**: lets every download that Completer holds start now, and logs how many were held and how many were released. It changes nothing else.

## How it decides

In plain words: Completer asks about a **sample** of the articles, and keeps asking until the answer is certain, or until it hits `MaxArticles` or `TimeLimit`. It fails a download only when even the friendliest reading of the answers says it cannot be repaired.

**Sampling.** The articles are picked in a fixed, evenly spread order that follows no regular step, so a pattern of missing articles cannot hide. Content articles are asked in rounds up to 120, 400 and 1200 in total (never more than `MaxArticles`). A post with 200 articles or fewer is checked completely. The repair (par2) articles are only asked about once some content turns out to be missing or unknown. Because only a sample is asked, every share carries a margin (99 percent confidence). 400 content articles in a row without a single miss count as a pass.

**STAT.** Every question is `STAT <message-id>`; the answer is *found* or *not found*. Questions are sent in batches of 20 without waiting, so one connection gets through many articles per second. Completer logs in with the same host, port, encryption and account that NZBGet uses for that server.

**Servers, the way NZBGet uses them.** Servers are used in order of their **level**, and within a level by number. Servers that share a **group** number (and a level) count as one. A server whose **retention** (in days) is shorter than the age of the post is not asked about those articles. An **optional** server that rejects the login is passed over, as NZBGet does. Servers listed in `IgnoreServers`, inactive ones and ones without connections are left out. `JoinGroup`, encryption and certificate verification follow NZBGet's settings for each server.

**Missing versus unknown.** An article counts as *missing* only when **every** server NZBGet would try says "I do not have it". If a server could not be asked (unreachable, too slow, out of time), the article is *unknown*, and unknown never counts against a download.

**Repairability.** NZBGet can repair a download if the missing part is smaller than the repair data it has. Completer uses that rule: the share of content that may be missing is the size of the par2 files divided by the size of the content (15 percent when the nzb has no recognisable par2 files, which is NZBGet's own assumption), reduced by whatever share of the par2 files is itself missing. Gaps that the nzb already leaves out (missing segment numbers) count as lost before the check starts.

**Confirmation.** Before failing a download, Completer waits 1.5 seconds and asks again, on every server, about the articles that looked missing. A server can say "not here" by mistake for a moment. If there is no time left for that, the verdict is *suspect*, never *fail*.

**Proof of life.** If a check would fail a download but **not one** article was found anywhere, the answer cannot be told apart from a provider that answers nonsense. The verdict is then *gone*, and the download is left to NZBGet, which fails a completely gone post by itself within seconds.

**Verdicts.**

| Verdict | Meaning | What happens |
|---|---|---|
| `pass` | It can be completed: even counting every doubtful article as lost it can be repaired, or nothing is missing. | Released. |
| `fail` | Even counting only articles that are missing for certain, and assuming the best about the repair data, it cannot be repaired (or, with `MaxFillPercent` set, too much exists only on fill servers). | Failed, unless the post is fresh (see below). |
| `suspect` | It looks unrepairable only if the articles that some server could not be asked about are counted as lost. | Held and checked again after 5 minutes, then after 15 minutes; after 3 doubtful checks it is released. |
| `gone` | Nothing was found anywhere (see proof of life). | Released (when the post is fresh: rechecked first). |
| `error` | The nzb file cannot be used, there is no active server, or no server could be asked. | Released. |

**Fresh posts and the recheck schedule.** A post counts as *fresh* while the newest article date in the nzb is less than `FreshHours` old (an nzb without dates counts as fresh). The articles of a very new post may not have reached every server yet. So when a fresh download gets `fail` or `gone`, it stays held and is checked again after 10, 20, 40 and then 60 minutes (never sooner than a minute; the last recheck is aimed at 30 seconds after the fresh window ends). The scheduled check runs every five minutes, so a recheck happens at the next five-minute mark after it is due. Rechecking stops, and the verdict becomes final, when the post is no longer fresh, when 8 checks have been made, or when the download has been held for the fresh window plus one hour. At that point `fail` fails the download and `gone` releases it.

## What Sonarr and Radarr see

When Completer fails a download, it goes through NZBGet itself:

- When the failure is decided right after the download was added, Completer sends NZBGet the standard queue-script command `MARK=BAD`. NZBGet removes that one download from the queue and keeps it in its history marked **bad**.
- When it is decided later by the scheduled check, Completer asks NZBGet to remove that one download (`GroupDelete`) and then marks the history entry bad (`HistoryMarkBad`). If the run is interrupted between these two steps, the next scheduled check finishes the job.

Either way the download ends in NZBGet's history marked bad, which is what Sonarr and Radarr read from NZBGet. They then blocklist the release and search again. No file is deleted by Completer.

## Safety guarantees

Each of these can be traced to the code (`Completer/completer/`).

1. **Only the download that was checked.** This is the rule everything else serves. A run is started for one download (a queue-script run by NZBGet's id for it; a scheduled check looks at each download it holds on its own, one after the other). It reads that download's nzb file, which has to be the file NZBGet names for it or a stored file with the checksum that was scanned, judges only that, and applies the result only to the download it was about. It never loops over other downloads and never applies one download's verdict to another (`app.py`, `Run.queue`, `Run.sweep_one`, `Run.examine`; tested in `tests/test_guards.py`).
2. **One download per command.** Every change names exactly one download by its numeric id; an empty list or a list of several is never sent (`rpc.py`, `QueueGuard.edit_many`).
3. **Verified right before every change.** The queue (or history) is read again immediately beforehand, and the download with that id must be a plain nzb that still carries the random token Completer gave it and still has the same name. Otherwise nothing is sent (`rpc.py`, `QueueGuard.verify`).
4. **Only these edit commands exist:** `GroupResume`, `GroupPauseExtraPars`, `GroupSetParameter`, `GroupDelete` (queue), `HistoryMarkBad`, `HistorySetParameter` (history). The only parameters written are Completer's own (`rpc.py`). In addition, a scan or queue script prints the commands `PAUSED=1`, `MARK=BAD` and its own parameters for the download it was started for (`app.py`, `log.py`).
5. **Never `FileDelete`.** There is no code path that deletes files of a download.
6. **Never a global pause.** Completer never pauses the queue or the download as a whole. The only pause it causes is on the one new download it is about to check.
7. **Fail open everywhere.** An unexpected error, a time-out, a server that cannot be asked or a nzb that cannot be read lets the download go. Only a `fail` verdict, which needs certain missing articles, can lead to a failure (`decide.py`; `app.py`, `Run.go`).
8. **Bounded rechecks.** No download is checked more than 8 times, a hold ends at the latest one hour after the fresh window, a download that was never checked is released after 30 minutes, and doubtful (`suspect`) checks stop after 3 (`decide.py`, `app.py`).
9. **Hard time budgets with a watchdog.** A scan run has 30 seconds, a queue run `TimeLimit` seconds, the scheduled check 240 seconds and a button 100 seconds. A watchdog ends the process 12 seconds after its budget whatever it is stuck on. NZBGet's request to stop is honoured and nothing is changed after it (`app.py`).
10. **Locks that cannot go stale.** Two runs never work on the same download at once, and only one scheduled check runs at a time. This uses operating-system record locks that the kernel drops when the process ends, however it ends. Where such locks do not exist (Windows), the run goes on without them, which is safe because every change is verified and repeatable (`locks.py`).
11. **A heartbeat, so nothing is held unless the scheduled check is known to be alive.** The scheduled check writes a time stamp after it has talked to NZBGet. A new download is only held when Completer is in NZBGet's Extensions list and that stamp is younger than 15 minutes (`locks.py`, `app.py`, `Run.scan`). A download that its sender added paused is never held or resumed by Completer; it is still checked, and one that cannot be completed is still failed.
12. **Secrets are never logged.** The passwords of the news servers and NZBGet's control login are registered and removed from every log line, even when a server echoes one back in an error text (`log.py`, `settings.py`).

Files Completer creates are small and live in NZBGet's temporary folder: `completer-locks`, `completer-alive` and `completer-unfinished`. Completer talks to NZBGet through its own JSON-RPC interface on the address and port NZBGet reports for itself, with NZBGet's own login.

## Completion versus Completer

The left column states only what is known about version 3.2 of the Completion extension (GPLv3).

| Topic | Completion 3.2 | Completer |
|---|---|---|
| What is paused | Every new nzb and the whole download queue (global pause) while it checks. | Only the one new download, until it is checked. Never a global pause. |
| If a run dies | The pause stays. | A held download is released by the scheduled check; holds are bounded in time and number of checks. |
| Which download a verdict applies to | Its queue-script run loops over every scan-paused download in the queue, pairing each download's id with the nzb file of the download that triggered the run, so a verdict about one download can be applied to another. | A run is about one download. The nzb file comes from NZBGet for that download (or is found again by its stored checksum), and id, token and name are verified immediately before every change. |
| How it fails a download | Deletes the files of the download (`FileDelete`), so that it fails later. | Marks it bad through NZBGet. Never deletes files. |
| Locking | A lock file older than 30 minutes is overridden, so two loops can run side by side. | Operating-system locks that the kernel drops when a process ends; they cannot go stale. |
| Judging the failure percentage | On the first server only; an unreachable server can look like missing articles. | On every server NZBGet would use, in its order. Missing only if every server says so; a server that cannot be asked makes an article unknown, which never counts against a download. |
| Dry run | None. | The default mode, plus a `Report` button comparing verdicts with what NZBGet did. |
| Tests | None of note. | A unit test suite that runs on Python 3.9 to 3.14, using a fake news server. |
| Licence | GPLv3. | MIT. |

## Limits

- **STAT cannot see a damaged body.** It tells whether an article exists, not whether its content is intact. A download can pass and still fail later.
- **It needs spare connections.** Completer opens up to `Connections` connections per server while NZBGet may be downloading. Providers that cap the number of connections may refuse them. Use **TestServers** while NZBGet is downloading; lower `Connections` or add servers to `IgnoreServers` if it falls short. A server that refuses is treated as unable to answer, never as missing articles.
- **A post whose articles are all gone is left to NZBGet** (verdict `gone`), which fails it by itself.
- **Sampling is a statement about the whole download**, not about every article. A download that is only slightly beyond what can be repaired is not failed (it is released, and logged as "too close to call, left to NZBGet's own health check").
- **Requirements:** Python 3.9 or newer; NZBGet 26 or newer (its extension system, with the Extensions list and the scheduled task).
- **Nzb limits:** an nzb larger than 256 MB, or one that declares XML entities, is not checked (verdict `error`, so it is let through).

## Uninstall and rollback

1. Press **ReleaseAll**, or set `Mode` to `Off` and wait for the next scheduled check (five minutes). Either way every held download starts.
2. Remove `Completer` from **Extensions** and from **ScriptOrder**.
3. Save and reload NZBGet.
4. Delete the `Completer` folder from NZBGet's scripts folder.

## The offline tool

`tools/completer_cli.py` runs the same check on nzb files, outside NZBGet, using the news servers of an `nzbget.conf`. It reads the config and the nzb files, asks the servers whether the articles exist (nothing is downloaded), and writes nothing. Servers are only ever shown as `server <number>`; no host name, user name or password is printed.

```
python3 tools/completer_cli.py --config /opt/nzbget/nzbget.conf some.release.nzb
python3 tools/completer_cli.py --config /opt/nzbget/nzbget.conf --verbose --connections 4 *.nzb
python3 tools/completer_cli.py --config /opt/nzbget/nzbget.conf --ignore-servers 3,7-11 --max-articles 400 a.nzb b.nzb
```

Options: `--config` (required), `--connections` (1-8, default 2), `--max-articles` (100-20000, default 1200), `--time-limit` (seconds per nzb, default 60), `--ignore-servers`, `--now` (a unix time to take as "now"), `--verbose` (one line per server). One line per nzb: `PASS`, `FAIL`, `SUSPECT`, `GONE` or `ERROR`, then the file name and what was found, and a summary line at the end. The tool shows the plain verdict; it does not do the waiting for fresh posts that the extension does.

Exit code: `0` when every nzb produced a verdict (an `ERROR` verdict because no server could be asked still counts, so read the line), `1` when at least one nzb could not be read, `2` for a wrong command line or a config without a usable news server.

## Tests, project layout, licence

Run the tests (standard library only, no installation needed; a few minutes):

```
python -m unittest discover -s tests -v
```

They use a fake news server (`tests/fakenntp.py`) and a fake NZBGet (`tests/nzbgetsim.py`), so they never touch a real download or a real provider. The tests for the headline rule are in [`tests/test_guards.py`](tests/test_guards.py).

One more set is optional: it runs Completer inside a real NZBGet 26 (throw-away folders, a free local port, the fake news server as the only server) and checks, through a recording proxy on the control port, that every change names exactly one download. It is skipped unless it is pointed at an NZBGet program:

```
COMPLETER_NZBGET=/path/to/nzbget python -m unittest tests.test_real_nzbget
```

Layout:

```
Completer/                  the folder that goes into NZBGet's scripts folder
  main.py                   the program NZBGet starts
  manifest.json             name, version, options, buttons, schedule
  completer/
    app.py                  scan, queue, scheduled and button runs; the guards
    decide.py               what to do with a verdict (one small table)
    checker.py              asks the servers, counts missing / unknown
    stats.py                the arithmetic: margins, health rule, verdict
    nzbfile.py              reads the nzb, spreads the sample
    nntp.py                 a small client that only asks STAT
    rpc.py                  the only code allowed to change NZBGet's queue
    locks.py                kernel locks, heartbeat, unfinished-failure hints
    settings.py             options and server settings from NZBGet's environment
    commands.py             TestServers, Report, ReleaseAll
    log.py                  log output, secrets removed
tools/completer_cli.py      offline checker
tests/                      tests, fake news server (fakenntp.py), fake NZBGet (nzbgetsim.py)
  test_guards.py            the rule "only the download that was checked is ever touched"
  test_real_nzbget.py       optional, against a real NZBGet (COMPLETER_NZBGET)
.github/workflows/tests.yml continuous tests
```

Licence: [MIT](LICENSE), copyright 2026 nphil. Credit: the idea of checking before downloading comes from the NZBGet Completion extension.
