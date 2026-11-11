"""Output in the form NZBGet expects from an extension.

NZBGet reads the script's standard output line by line. A line starting with
``[INFO]``, ``[WARNING]``, ``[ERROR]`` or ``[DETAIL]`` becomes a log message of
that kind; a line starting with ``[NZB]`` is a command for the download the
script was started for.

Every message passes through :func:`clean`, which removes any credential that
was registered with :func:`add_secret`, so a password can never reach the log
even when a news server echoes it back in an error text.
"""

import sys
import threading

_lock = threading.Lock()
_secrets = []
_stream = None


def set_stream(stream):
    """Send the output somewhere else than standard output (used by the tests)."""
    global _stream
    _stream = stream


def add_secret(value):
    """Never let ``value`` appear in a message."""
    value = str(value or "")
    if len(value) >= 4 and value not in _secrets:
        _secrets.append(value)
        _secrets.sort(key=len, reverse=True)


def forget_secrets():
    del _secrets[:]


def clean(text, limit=900):
    """One printable line without credentials, short enough for NZBGet (it cuts lines at 1024)."""
    text = str(text)
    for secret in _secrets:
        if secret in text:
            text = text.replace(secret, "***")
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    return text[:limit]


def _write(line):
    stream = _stream if _stream is not None else sys.stdout
    with _lock:
        try:
            stream.write(line + "\n")
            stream.flush()
        except (OSError, ValueError):
            pass  # NZBGet went away; there is nobody left to tell


def info(text):
    _write("[INFO] " + clean(text))


def detail(text):
    _write("[DETAIL] " + clean(text))


def warning(text):
    _write("[WARNING] " + clean(text))


def error(text):
    _write("[ERROR] " + clean(text))


def nzb_command(text):
    """A command NZBGet applies to the one download this script was started for."""
    _write("[NZB] " + "".join(ch if ch.isprintable() else " " for ch in str(text)))
