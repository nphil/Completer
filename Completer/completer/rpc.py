"""Talking to NZBGet, and the only code that is allowed to change its queue.

Every change Completer ever makes goes through :class:`QueueGuard`. Its rules
are what make it impossible to touch a download that is not ours:

* a change names exactly one download, by its numeric id; a list that is
  empty, or longer than one, is never sent;
* immediately before a change (or a short series of changes to the same
  download) the queue is read again, and the download with that id must be a
  plain nzb that still carries the random token Completer gave it and still
  has the same name; otherwise nothing is sent;
* only a short list of commands exists here. There is no command for
  deleting files of a download, and none that acts on the whole queue;
* the only download parameters that can be written are Completer's own.
"""

import base64
import json
import urllib.error
import urllib.request

from .settings import PARAMETERS, TOKEN

# The complete list of things Completer can ask NZBGet to do to a download.
QUEUE_EDITS = ("GroupResume", "GroupPauseExtraPars", "GroupSetParameter", "GroupDelete")
HISTORY_EDITS = ("HistoryMarkBad", "HistorySetParameter")


class RpcError(Exception):
    """NZBGet could not be asked, or said no."""


class Refused(Exception):
    """A change was not sent because the download could not be verified."""


class Rpc(object):
    """NZBGet's JSON-RPC interface on its control port."""

    def __init__(self, control, timeout=20.0):
        self._url = control.url()
        login = ("%s:%s" % (control.username, control.password)).encode("utf-8")
        self._headers = {"Content-Type": "application/json",
                         "Authorization": "Basic " + base64.b64encode(login).decode("ascii")}
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.timeout = timeout

    def call(self, method, *params):
        # NZBGet reads non-ASCII text correctly only when it is sent as plain UTF-8.
        body = json.dumps({"method": method, "params": list(params)}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self._url, data=body, headers=self._headers)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                answer = json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as error:
            raise RpcError("NZBGet answered '%s' with HTTP %d" % (method, error.code))
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise RpcError("NZBGet could not be asked for '%s' (%s)" % (method, type(error).__name__))
        if not isinstance(answer, dict) or answer.get("error"):
            raise RpcError("NZBGet refused '%s'" % method)
        return answer.get("result")


class Target(object):
    """The one download a run is about: its id, the token Completer gave it, and its name."""

    def __init__(self, nzb_id, token, name):
        self.nzb_id = nzb_id
        self.token = token
        self.name = name


def params_of(item):
    """The parameters of a queue or history entry as a dictionary."""
    found = {}
    for entry in item.get("Parameters") or []:
        if isinstance(entry, dict) and "Name" in entry:
            found[str(entry["Name"])] = str(entry.get("Value", ""))
    return found


def _plain(text):
    return "".join(ch for ch in str(text) if " " <= ch <= "~")


def same_name(first, second):
    """Names are equal, or differ only in characters that NZBGet transports unreliably."""
    return first == second or (_plain(first) == _plain(second) and bool(_plain(first)))


class QueueGuard(object):
    def __init__(self, rpc):
        self.rpc = rpc

    def groups(self):
        result = self.rpc.call("listgroups", 0)
        if not isinstance(result, list):
            raise RpcError("NZBGet gave no queue")
        return [item for item in result if isinstance(item, dict)]

    def history(self):
        result = self.rpc.call("history", False)
        if not isinstance(result, list):
            raise RpcError("NZBGet gave no history")
        return [item for item in result if isinstance(item, dict)]

    def verify(self, target, history=False):
        """Read the queue (or history) now and return the entry of ``target``; Refused if it is not ours."""
        nzb_id = getattr(target, "nzb_id", None)
        if type(nzb_id) is not int or nzb_id <= 0 or not getattr(target, "token", ""):
            raise Refused("there is no verified download to act on")
        where = "history" if history else "queue"
        matches = [item for item in (self.history() if history else self.groups()) if item.get("NZBID") == nzb_id]
        if len(matches) != 1:
            raise Refused("download %d is not in the %s" % (nzb_id, where))
        item = matches[0]
        if item.get("Kind") != "NZB":
            raise Refused("download %d is not an nzb" % nzb_id)
        if history and item.get("ID", nzb_id) != nzb_id:
            raise Refused("history entry %d does not belong to download %d" % (item.get("ID", 0), nzb_id))
        if params_of(item).get(TOKEN) != target.token:
            raise Refused("download %d in the %s is not the one Completer put on hold" % (nzb_id, where))
        if target.name is not None and not same_name(item.get("NZBName") or item.get("Name") or "", target.name):
            raise Refused("download %d in the %s has a different name now" % (nzb_id, where))
        return item

    def edit_many(self, target, commands, history=False):
        """Send several allowed commands for the one download, after verifying it once.

        ``commands`` is a list of (command, text). Returns what NZBGet answered to each.
        """
        allowed = HISTORY_EDITS if history else QUEUE_EDITS
        for command, _text in commands:
            if command not in allowed:
                raise Refused("Completer does not use the command %s" % command)
        if not commands:
            return []
        self.verify(target, history)
        return [bool(self.rpc.call("editqueue", command, str(text), [target.nzb_id])) for command, text in commands]

    def edit(self, target, command, text=""):
        """Change the one verified download in the queue. Returns what NZBGet answered."""
        return self.edit_many(target, [(command, text)])[0]

    def edit_history(self, target, command, text=""):
        """Change the one verified download in the history."""
        return self.edit_many(target, [(command, text)], True)[0]

    def resume(self, target, pause_extra_pars=True):
        """Let a held download start: resume all its files, then pause its extra repair files again.

        That is what NZBGet itself does with a new download (unless ParCheck is
        'force'), so the download ends up exactly as if it had never been held.
        Returns whether NZBGet confirmed the resume.
        """
        commands = [("GroupResume", "")]
        if pause_extra_pars:
            commands.append(("GroupPauseExtraPars", ""))
        return self.edit_many(target, commands)[0]

    def set_parameters(self, target, values, history=False):
        """Write Completer's own parameters on the verified download; nothing else can be written."""
        command = "HistorySetParameter" if history else "GroupSetParameter"
        commands = []
        for name, value in (values.items() if hasattr(values, "items") else values):
            if name not in PARAMETERS:
                raise Refused("%s is not a parameter of Completer" % name)
            text = "".join(ch for ch in str(value) if ch.isprintable())
            commands.append((command, "%s=%s" % (name, text)))
        return self.edit_many(target, commands, history)
