# Changelog

All notable changes to this project are written here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [1.0.0] - unreleased

### Added

- `Mode = Screen` (recommended for unattended use): new downloads are never held. The scheduled check looks at the
  downloads still waiting their turn, front first and as many side by side as `Connections` allows (one connection
  per server each), and removes the ones that cannot be completed before NZBGet downloads them. The queue script
  returns at once, so NZBGet's single queue-script line is never used for a check. Downloads that NZBGet is already
  unpacking or running a script for are never removed. Switching from `Enforce` lets go of anything still held.
- Checks settle a gone post within the time limit: a smaller first round (40 articles), and only a spread sample of 24
  of the articles that looked missing is asked about again (the rest only if one of those turns up). `TimeLimit`
  default raised from 45 to 90 seconds.

- The `Completer` extension for NZBGet 26 or newer (Python 3.9 or newer, standard library only, MIT licence).
- A check before the download: for a new nzb, Completer asks the news servers with `STAT` questions whether the
  articles exist. Nothing is downloaded.
- Holding of only the one new download while it is checked, and release of it when it can be completed. There is
  no global pause.
- Failing of a download that provably cannot be completed, by marking it bad the way NZBGet does, so Sonarr and
  Radarr blocklist the release and search again. Completer never deletes files.
- Sampling that starts small and stops as soon as the answer is certain, with a 99 percent margin for the
  uncertainty of a sample.
- The verdicts `pass`, `fail`, `suspect`, `gone` and `error`. Only `fail` can lead to a failure; the others let
  the download go (after a few rechecks for `suspect`).
- Use of the news servers the way NZBGet does: levels, groups, retention, optional servers, JoinGroup, TLS and
  certificate settings. A missing article counts as missing only when every server NZBGet would try says so;
  a server that cannot be asked never counts against a download.
- Repairability judged with NZBGet's own health rule, using the size of the repair (par2) files.
- A confirmation round that asks again about articles that looked missing, and a proof-of-life rule: when not one
  article is found anywhere, the download is left to NZBGet.
- Fresh posts: a recent post that looks incomplete is held and rechecked (after 10, 20, 40 and 60 minutes, at most
  8 checks) until its fresh window has ended; the check after that is final.
- Fill-server limit (`MaxFillPercent`) for block accounts.
- Options `Mode` (`DryRun`, `Enforce`, `Off`), `Categories`, `FreshHours`, `Connections`, `MaxArticles`,
  `TimeLimit`, `MaxFillPercent`, `IgnoreServers` and `Verbose`. The default mode is `DryRun`.
- Buttons `TestServers`, `Report` and `ReleaseAll` on NZBGet's settings page.
- A scheduled check every five minutes that rechecks, releases and finishes whatever is still held. A new download
  is only held while this scheduled check is known to be alive.
- Safety guards: every change to NZBGet names exactly one download and verifies its id, token and name right
  before the change; only a short list of edit commands exists; hard time budgets with a watchdog; operating
  system locks that cannot go stale; passwords are never written to the log.
- `tools/completer_cli.py`, an offline checker that runs the same check on nzb files using the servers of an
  `nzbget.conf`, without NZBGet.
- Tests, run by GitHub Actions on Python 3.9 to 3.14: unit tests, end-to-end flow tests against a fake NZBGet and a
  fake news server, tests for the rule that only the checked download is touched, and an optional set that runs
  Completer inside a real NZBGet 26. A check that no private addresses or paths are committed.
