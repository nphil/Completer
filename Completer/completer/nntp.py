"""A small NNTP client that can do one thing: ask whether articles exist.

It logs in exactly like NZBGet would (same host, port, encryption, account)
and sends ``STAT <message-id>``, which a server answers with one short line:
223 when it has the article, 430 when it does not. Nothing is downloaded.

Several questions are sent before the answers are read (pipelining), so even
a single connection gets through hundreds of articles per second.
"""

import collections
import re
import socket
import ssl
import threading
import time

PIPELINE = 20           # questions sent before reading the answers
CONNECT_TIMEOUT = 8.0   # seconds for connecting, encryption and login, each step
ANSWER_TIMEOUT = 10.0   # seconds to wait for one answer

_FAMILIES = {"ipv4": socket.AF_INET, "ipv6": socket.AF_INET6}
_LIMIT = re.compile(r"too many|connection limit|limit (reached|exceeded)|max(imum)?[ a-z]*connections"
                    r"|connections? exceeded|simultaneous", re.IGNORECASE)
# Answers to STAT that say something is wrong with the connection, not with the article.
_BROKEN = (400, 480, 481, 482, 483, 502, 503)
_GROUP = re.compile(r"^[A-Za-z0-9.+_-]{1,200}\Z")
# What may stand between the angle brackets of a request. nzbfile applies the same rule; it is repeated here
# so that nothing that could end the line (or start another one) can ever reach the wire. \Z, not $.
_MESSAGE_ID = re.compile(r"^[\x21-\x3b\x3d\x3f-\x7e]{1,248}\Z")


class NNTPError(Exception):
    """A connection could not be used. ``kind`` is a word that is safe to log."""

    def __init__(self, kind, code=0):
        Exception.__init__(self, kind)
        self.kind = kind
        self.code = code
        self.partial = {}

    def describe(self):
        words = {
            "dns": "its address could not be looked up",
            "connect": "the connection was refused",
            "timeout": "it did not answer in time",
            "tls": "the encrypted connection could not be set up",
            "certificate": "its certificate was not accepted",
            "busy": "it allows no more connections right now",
            "auth": "it rejected the login",
            "refused": "it refused the connection",
            "closed": "it closed the connection",
            "protocol": "it gave an answer that is not NNTP",
            "dropped": "it stopped answering questions",
        }
        text = words.get(self.kind, self.kind)
        return "%s (code %d)" % (text, self.code) if self.code else text


class Deadline(object):
    """A point in time after which no more work may be started."""

    def __init__(self, seconds):
        self._end = time.monotonic() + max(0.0, seconds)

    def left(self):
        return self._end - time.monotonic()

    def expired(self):
        return self.left() <= 0

    def cut(self):
        """Stop now (used when NZBGet asks the script to end)."""
        self._end = min(self._end, time.monotonic())

    def timeout(self, most):
        """A socket timeout that never reaches beyond the deadline."""
        return max(0.05, min(most, self.left()))


def tls_context(level, cert_store=""):
    """An encryption setup that checks certificates as strictly as NZBGet does for this server."""
    context = ssl.create_default_context()
    if hasattr(ssl, "VERIFY_X509_STRICT"):
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT  # NZBGet does not ask for this either
    if cert_store:
        try:
            context.load_verify_locations(cafile=cert_store)
        except (OSError, ssl.SSLError):
            pass  # the system's own certificate store is still loaded
    if level == "none":
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    elif level == "minimal":
        context.check_hostname = False
    return context


class Connection(object):
    """One logged-in connection to one news server."""

    def __init__(self, server, context=None):
        self.server = server
        self._context = context
        self._sock = None
        self._reader = None
        self._group = None

    def open(self, timeout):
        server = self.server
        try:
            found = socket.getaddrinfo(server.host, server.port,
                                       _FAMILIES.get(server.ip_version, socket.AF_UNSPEC), socket.SOCK_STREAM)
        except (OSError, UnicodeError):
            raise NNTPError("dns")
        sock = None
        problem = "connect"
        for family, kind, protocol, _name, address in found[:4]:
            candidate = socket.socket(family, kind, protocol)
            candidate.settimeout(timeout)
            try:
                candidate.connect(address)
            except socket.timeout:
                problem = "timeout"
                candidate.close()
                continue
            except OSError:
                candidate.close()
                continue
            sock = candidate
            break
        if sock is None:
            raise NNTPError(problem)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        if server.tls:
            context = self._context or tls_context(server.cert)
            try:
                sock = context.wrap_socket(sock, server_hostname=server.host)
            except ssl.SSLCertVerificationError:
                sock.close()
                raise NNTPError("certificate")
            except socket.timeout:
                sock.close()
                raise NNTPError("timeout")
            except (ssl.SSLError, OSError, ValueError):
                sock.close()
                raise NNTPError("tls")
        self._sock = sock
        self._reader = sock.makefile("rb")
        try:
            self._login()
        except NNTPError:
            self.close(polite=False)
            raise

    def _login(self):
        code, text = self._answer()
        if code not in (200, 201):
            raise NNTPError("busy" if code == 400 or _LIMIT.search(text) else "refused", code)
        server = self.server
        if not server.username:
            return
        self._send("AUTHINFO USER " + server.username)
        code, text = self._answer()
        if code == 381:
            self._send("AUTHINFO PASS " + server.password)
            code, text = self._answer()
        if code != 281:
            raise NNTPError("busy" if _LIMIT.search(text) else "auth", code)

    def _send(self, line):
        if "\r" in line or "\n" in line:
            raise NNTPError("protocol")
        try:
            self._sock.sendall(line.encode("utf-8") + b"\r\n")
        except socket.timeout:
            raise NNTPError("timeout")
        except OSError:
            raise NNTPError("closed")

    def _answer(self):
        try:
            line = self._reader.readline(4098)
        except socket.timeout:
            raise NNTPError("timeout")
        except (OSError, ValueError):
            raise NNTPError("closed")
        if not line:
            raise NNTPError("closed")
        text = line.decode("latin-1").rstrip("\r\n")
        if len(text) < 3 or not text[:3].isdigit():
            raise NNTPError("protocol")
        return int(text[:3]), text[3:].strip()

    def join(self, group, timeout):
        """Select a newsgroup first, for the servers NZBGet is told to do that with (JoinGroup)."""
        if not group or group == self._group or not _GROUP.match(group):
            return
        self._sock.settimeout(timeout)
        self._send("GROUP " + group)
        self._answer()  # whatever it says, the question by message-id can still be asked
        self._group = group

    def stat(self, msgids, timeout, depth=PIPELINE):
        """Ask about each message-id. Returns {msgid: True or False}; unclear answers are left out.

        A message-id that cannot be written as one line is not sent and stays unanswered.
        When the connection fails midway, the NNTPError carries the answers
        received so far in ``partial``.
        """
        answers = {}
        msgids = [msgid for msgid in msgids if _MESSAGE_ID.match(msgid)]
        self._sock.settimeout(timeout)
        try:
            for start in range(0, len(msgids), max(1, depth)):
                batch = msgids[start:start + max(1, depth)]
                request = "".join("STAT <%s>\r\n" % msgid for msgid in batch).encode("ascii")
                try:
                    self._sock.sendall(request)
                except socket.timeout:
                    raise NNTPError("timeout")
                except OSError:
                    raise NNTPError("closed")
                for msgid in batch:
                    code, _text = self._answer()
                    if code == 223:
                        answers[msgid] = True
                    elif code == 430:
                        answers[msgid] = False
                    elif code in _BROKEN:
                        raise NNTPError("dropped", code)
        except NNTPError as error:
            error.partial = answers
            raise
        return answers

    def close(self, polite=True):
        sock, self._sock = self._sock, None
        reader, self._reader = self._reader, None
        if sock is None:
            return
        if polite:
            try:
                sock.settimeout(1.0)
                sock.sendall(b"QUIT\r\n")
            except (OSError, ValueError):
                pass
        for item in (reader, sock):
            try:
                item.close()
            except (OSError, ValueError):
                pass


class ServerSession(object):
    """All questions to one server during one check, over a few connections that are kept open."""

    def __init__(self, server, connections, deadline, cert_store="", connection_factory=None):
        self.server = server
        self.limit = max(1, min(connections, server.connections or connections))
        self._deadline = deadline
        self._factory = connection_factory or Connection
        self._context = tls_context(server.cert, cert_store) if server.tls else None
        self._idle = []
        self._lock = threading.Lock()
        self._depth = PIPELINE
        self._closed = False
        self.reachable = None   # None: not tried yet; True: logged in at least once; False: never got in
        self.failure = None     # the last connection problem, as an NNTPError
        self.questions = 0
        self.seconds = 0.0

    def ask(self, msgids, groups=None):
        """Ask about every message-id. Returns {msgid: True or False} for the clear answers only."""
        answers = {}
        if not msgids or self._closed or self.reachable is False:
            return answers  # a server that could not be reached is not tried again during this check
        started = time.monotonic()
        queue = collections.deque()
        for start in range(0, len(msgids), PIPELINE):
            queue.append((msgids[start:start + PIPELINE], 0))
        lock = threading.Lock()
        workers = [threading.Thread(target=self._work, args=(queue, answers, lock, groups or {}), daemon=True)
                   for _ in range(min(self.limit, len(queue)))]
        for worker in workers:
            worker.start()
        for worker in workers:
            while worker.is_alive() and not self._deadline.expired():
                worker.join(0.1)
        with lock:
            result = dict(answers)
        with self._lock:
            self.questions += len(msgids)
            self.seconds += time.monotonic() - started
        return result

    def _work(self, queue, answers, lock, groups):
        connection = None
        failures = 0
        try:
            while failures < 2 and not self._deadline.expired() and not self._closed:
                with lock:
                    if not queue:
                        return
                    chunk, tries = queue.popleft()
                asking = False
                try:
                    if connection is None:
                        connection = self._connect()
                    if self.server.join_group:
                        connection.join(groups.get(chunk[0], ""), self._deadline.timeout(ANSWER_TIMEOUT))
                    asking = True
                    got = connection.stat(chunk, self._deadline.timeout(ANSWER_TIMEOUT), self._depth)
                    failures = 0
                    with lock:
                        answers.update(got)
                except NNTPError as error:
                    failures += 1
                    self.failure = error
                    if asking and error.kind == "timeout":
                        self._depth = 1  # maybe this server cannot take several questions at once
                    if connection is not None:
                        connection.close(polite=False)
                        connection = None
                    with lock:
                        answers.update(error.partial)
                        rest = [msgid for msgid in chunk if msgid not in error.partial]
                        if rest and tries < 1:
                            queue.append((rest, tries + 1))
                    if not self.reachable:
                        return  # it never let us in; one attempt per connection is enough
        finally:
            if connection is not None:
                with self._lock:
                    if self._closed:
                        connection.close()
                    else:
                        self._idle.append(connection)

    def _connect(self):
        with self._lock:
            if self._idle:
                return self._idle.pop()
        for attempt in (1, 2):
            connection = self._factory(self.server, self._context)
            try:
                connection.open(self._deadline.timeout(CONNECT_TIMEOUT))
                self.reachable = True
                return connection
            except NNTPError as error:
                self.failure = error
                if self.reachable is None:
                    self.reachable = False
                if error.kind != "busy" or attempt == 2 or self._deadline.left() < 2.0:
                    raise
                time.sleep(0.7)  # a slot may free up in a moment
        raise NNTPError("busy")

    def test(self):
        """Open as many connections as a check would use; returns how many logged in."""
        opened = []
        for _ in range(self.limit):
            try:
                opened.append(self._connect_new())
            except NNTPError as error:
                self.failure = error
                break
        with self._lock:
            self._idle.extend(opened)
        return len(opened)

    def _connect_new(self):
        connection = self._factory(self.server, self._context)
        connection.open(self._deadline.timeout(CONNECT_TIMEOUT))
        self.reachable = True
        return connection

    def close(self):
        with self._lock:
            self._closed = True
            idle, self._idle = self._idle, []
        for connection in idle:
            connection.close()
