"""Locks that cannot go stale, and the heartbeat that proves the scheduled check is alive.

Two runs of Completer must never work on the same download at the same time,
and only one scheduled check may run at once (NZBGet starts the scheduled
check again every time its settings page is opened). Both are done with
operating-system record locks on one small file: byte 0 means "a scheduled
check is running", byte N means "download N is being checked". The kernel
drops a lock when its process ends, however it ends, so a killed run can never
leave one behind. Where the system has no such locks (Windows) or the file
cannot be used, the run goes on without them: every change Completer makes is
verified and repeatable, so a missing lock never makes anything unsafe.

The heartbeat is a file with a time stamp that the scheduled check writes after
it has talked to NZBGet. The scan only puts a new download on hold while that
stamp is recent, which proves that the scheduled check, the safety net that
lets held downloads go again, is really running.
"""

import errno
import os
import tempfile
import threading

try:
    import fcntl
except ImportError:  # not available on Windows
    fcntl = None

SWEEP = 0                    # the lock byte for "a scheduled check is running"
HEARTBEAT_LIFE = 900         # seconds a heartbeat stays good: three missed five-minute rounds

_registry = threading.Lock()
_taken = set()               # (file, byte) held by this process, so that threads exclude each other as well
_files = {}                  # one descriptor per lock file for the life of the process (closing one drops every lock)


def folder(temp_dir):
    """Where NZBGet keeps temporary files, or the system's temporary folder."""
    chosen = temp_dir or tempfile.gettempdir()
    try:
        os.makedirs(chosen)
    except OSError:
        pass
    return chosen


def _open_shared(path):
    """Open (or create) a small file that every user of this machine may use; None when that is not possible."""
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
    except OSError:
        return None
    try:
        os.fchmod(descriptor, 0o666)  # the file may first be touched by another user than NZBGet's
    except (OSError, AttributeError):
        pass
    return descriptor


class Locks(object):
    def __init__(self, directory):
        self.path = os.path.join(directory, "completer-locks")
        self._mine = set()

    def _descriptor(self):
        with _registry:
            if self.path not in _files:
                _files[self.path] = _open_shared(self.path)
            return _files[self.path]

    def acquire(self, byte):
        """Take the lock without waiting. False when another run, or another thread of this one, has it."""
        key = (self.path, byte)
        with _registry:
            if key in _taken:
                return False
            _taken.add(key)
        if not self._lock_file(byte):
            with _registry:
                _taken.discard(key)
            return False
        self._mine.add(byte)
        return True

    def _lock_file(self, byte):
        if fcntl is None:
            return True
        descriptor = self._descriptor()
        if descriptor is None:
            return True
        try:
            fcntl.lockf(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, byte, 0)
        except OSError as error:
            return error.errno not in (errno.EACCES, errno.EAGAIN)  # anything else: no locks here, carry on
        except (ValueError, OverflowError):
            return True
        return True

    def release(self, byte):
        if byte not in self._mine:
            return
        self._mine.discard(byte)
        if fcntl is not None:
            descriptor = self._descriptor()
            if descriptor is not None:
                try:
                    fcntl.lockf(descriptor, fcntl.LOCK_UN, 1, byte, 0)
                except (OSError, ValueError, OverflowError):
                    pass
        with _registry:
            _taken.discard((self.path, byte))

    def release_all(self):
        for byte in sorted(self._mine):
            self.release(byte)


class Heartbeat(object):
    def __init__(self, directory):
        self.path = os.path.join(directory, "completer-alive")

    def beat(self, now):
        """Record that the scheduled check just talked to NZBGet. False when the file cannot be written."""
        temporary = "%s.%d" % (self.path, os.getpid())
        try:
            with open(temporary, "w") as handle:
                handle.write("%d\n" % int(now))
            try:
                os.chmod(temporary, 0o666)
            except OSError:
                pass
            os.replace(temporary, self.path)
            return True
        except OSError:
            try:
                os.remove(temporary)
            except OSError:
                pass
            return False

    def age(self, now):
        """Seconds since the last heartbeat, or None when there is none that can be read."""
        try:
            with open(self.path) as handle:
                return now - int(handle.read(40).strip())
        except (OSError, ValueError):
            return None

    def alive(self, now):
        age = self.age(now)
        return age is not None and -300 <= age <= HEARTBEAT_LIFE


class Unfinished(object):
    """A hint of which failures were started by the scheduled check but not finished.

    Taking a download out of the queue as failed takes two steps in NZBGet
    (remove it, then mark it bad in the history). The ids written here tell the
    next scheduled check where to look. The history entry itself must still
    carry Completer's token and the state "failing" before anything is done to
    it, so an entry left over here can never cause a change by itself.
    """

    def __init__(self, directory):
        self.path = os.path.join(directory, "completer-unfinished")

    def ids(self):
        try:
            with open(self.path) as handle:
                lines = handle.read(10000).split()
        except OSError:
            return set()
        return set(int(line) for line in lines if line.isdigit())

    def _write(self, ids):
        try:
            if not ids:
                os.remove(self.path)
                return
            temporary = "%s.%d" % (self.path, os.getpid())
            with open(temporary, "w") as handle:
                handle.write("".join("%d\n" % number for number in sorted(ids)[:200]))
            try:
                os.chmod(temporary, 0o666)
            except OSError:
                pass
            os.replace(temporary, self.path)
        except OSError:
            pass

    def add(self, nzb_id):
        self._write(self.ids() | {nzb_id})

    def remove(self, nzb_id):
        found = self.ids()
        if nzb_id in found:
            found.discard(nzb_id)
            self._write(found)
