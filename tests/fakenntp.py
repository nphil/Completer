"""A fake news server for the tests: plain NNTP over TCP on 127.0.0.1.

It understands exactly what Completer needs: the greeting, ``AUTHINFO``,
``GROUP``, ``STAT <message-id>`` and ``QUIT``. Many requests may arrive in one
packet (pipelining); they are answered one by one, in order. There is one
thread per connection. Nothing here touches the network beyond the loopback
interface, and everything is closed again by :meth:`FakeNews.stop`.

Typical use::

    with FakeNews(have=post.all_ids) as news:
        env = news.env_entry(1)                        # NZBOP_SERVER1_* variables
        env.update(other.env_entry(2, level=1))        # more servers: just merge the dictionaries
        servers = settings.load_servers(env)           # what Completer reads from NZBGet
        ...
        assert news.stats_total == len(news.stat_log)

or, in a test case::

    news = FakeNews(have=ids, slow=0.01).start()
    self.addCleanup(news.stop)

What the server has
    ``have`` is a set of message-ids (without angle brackets). ``STAT`` of an id in
    it is answered ``223 0 <id>``, anything else ``430 no such article``.

Login
    By default the account is ``tester`` / ``s3cret-pass-word`` and has to be
    used (``AUTHINFO USER`` -> 381, ``AUTHINFO PASS`` -> 281, anything else -> 481).
    ``username=None`` makes the server accept anybody without asking. Until the
    login is done, ``STAT`` is answered ``480 authentication required``.

Behaviour switches (plain attributes, may be changed at any time, also while a
test is running; give them to the constructor as keywords or assign them)
    reject_login        ``True`` or ``481``: every password is refused with 481. Any other
                        number (e.g. ``502``) is the answer to ``AUTHINFO USER``.
    max_connections     0 = unlimited. The next simultaneous connection is turned away,
                        in the style of ``limit_style``: ``"greeting"`` (``400 too many
                        connections`` as the greeting), ``"greeting-text"`` (``502 maximum
                        connections exceeded`` as the greeting) or ``"login"`` (a normal
                        greeting, ``481 maximum connections exceeded`` to ``AUTHINFO PASS``).
    drop_after_answers  close the connection after this many ``STAT`` answers, in the middle of
                        a pipeline (``0``: before the first answer; ``None``: never). The
                        answers already given are delivered first. Only ``drop_limit`` many
                        connections are dropped (default 1, so a retry works; 0 = always).
    stall               ``True``: accept the connection and never say anything, not even the
                        greeting. ``"stat"``: log in normally, then never answer a ``STAT``.
    slow                seconds waited before every ``STAT`` answer.
    flap                ids that answer ``430`` the first time they are asked and ``223`` after that.
    deny_all            ``430`` for every ``STAT``, whatever ``have`` says (a provider that lies).
    broken_code         answer every ``STAT`` with this code (e.g. ``480`` or ``502``).
    echo_password       every error text, and the ``430`` line, contains the password the client
                        sent (the configured one before it has sent any), to prove that the
                        client never lets a server's text reach a log.
    garbage_greeting    the greeting is not NNTP at all.
    greeting            the greeting line (default ``200 FakeNews ready``). A greeting that does
                        not start with 200 or 201 is followed by closing the connection.
    hangup              close every connection immediately, without a word.

Counters (reset by :meth:`reset`)
    connections_total   connections accepted (also the ones turned away)
    rejected_total      connections turned away because of ``max_connections``
    max_concurrent      most connections that were open at the same moment (turned away ones excluded)
    logins_total        successful logins
    stats_total         ``STAT`` commands received
    stat_log            the message-ids asked, in the order they were received
    drops               connections closed because of ``drop_after_answers``
    quits               ``QUIT`` commands received
    max_burst           most command lines that arrived in one packet (proves pipelining)
    commands            every received line except ``STAT`` (``AUTHINFO PASS`` included, it is made up)
    passwords_seen      the passwords received
    groups_joined       the groups selected with ``GROUP``
"""

import socket
import threading

USERNAME = "tester"
PASSWORD = "s3cret-pass-word"

_SWITCHES = ("reject_login", "max_connections", "limit_style", "drop_after_answers", "drop_limit", "stall",
             "slow", "flap", "deny_all", "broken_code", "echo_password", "garbage_greeting", "greeting",
             "hangup")


class _Gone(Exception):
    """The client went away."""


class _Link(object):
    """What one connection has done so far."""

    def __init__(self, admitted):
        self.admitted = admitted
        self.user = None
        self.password = None
        self.authenticated = False
        self.answers = 0


class FakeNews(object):
    host = "127.0.0.1"

    def __init__(self, have=(), username=USERNAME, password=PASSWORD, **switches):
        self.port = 0           # filled in by start(); kept after stop() so a stopped server is a closed port
        self.have = set(have)
        self.username = username
        self.password = password
        self.reject_login = False
        self.max_connections = 0
        self.limit_style = "greeting"
        self.drop_after_answers = None
        self.drop_limit = 1
        self.stall = False
        self.slow = 0.0
        self.flap = set()
        self.deny_all = False
        self.broken_code = 0
        self.echo_password = False
        self.garbage_greeting = False
        self.greeting = "200 FakeNews ready"
        self.hangup = False
        for name, value in switches.items():
            if name not in _SWITCHES:
                raise TypeError("FakeNews has no switch called %r" % name)
            setattr(self, name, value)
        self._lock = threading.Lock()
        self._stopping = None
        self._listener = None
        self._acceptor = None
        self._sockets = set()
        self._handlers = []
        self._open = 0
        self._flapped = set()
        self.reset()

    # ------------------------------------------------------------ life cycle

    def reset(self):
        """Zero the counters and forget which flapping ids were asked already."""
        with self._lock:
            self.connections_total = 0
            self.rejected_total = 0
            self.max_concurrent = 0
            self.logins_total = 0
            self.stats_total = 0
            self.stat_log = []
            self.drops = 0
            self.quits = 0
            self.max_burst = 0
            self.commands = []
            self.passwords_seen = []
            self.groups_joined = []
            self._flapped = set()

    def start(self):
        """Listen on 127.0.0.1. The port is chosen by the system the first time and reused after stop()."""
        if self._listener is not None:
            return self
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((self.host, self.port))
            listener.listen(64)
            listener.settimeout(0.1)
        except OSError:
            listener.close()
            raise
        self.port = listener.getsockname()[1]
        self._listener = listener
        self._stopping = threading.Event()
        self._acceptor = threading.Thread(target=self._accept_loop, args=(listener, self._stopping),
                                          name="fakenews-accept", daemon=True)
        self._acceptor.start()
        return self

    def stop(self):
        """Stop listening, hang up every connection and wait for the threads. Safe to call twice."""
        listener, self._listener = self._listener, None
        stopping = self._stopping
        if listener is None:
            return
        stopping.set()
        try:
            listener.close()
        except OSError:
            pass
        with self._lock:
            sockets = list(self._sockets)
            handlers = list(self._handlers)
            self._handlers = []
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self._acceptor.join(2.0)
        for handler in handlers:
            handler.join(2.0)

    def __enter__(self):
        return self.start()

    def __exit__(self, *_exc):
        self.stop()

    @property
    def open_connections(self):
        with self._lock:
            return self._open

    def env_entry(self, number, level=0, group=0, optional=False, retention=0, connections=10, name=None,
                  tls=False, join_group=False, username=USERNAME, password=PASSWORD, active=True, **extra):
        """The ``NZBOP_SERVER<number>_*`` variables that make NZBGet's configuration point at this server.

        Exactly the upper-case spelling that ``settings.load_servers`` reads, plus ``NZBOP_CertCheck``.
        ``extra`` replaces or adds single fields by their upper-case name, e.g. ``IPVERSION="ipv4"``.
        """
        if not self.port:
            raise RuntimeError("start() the server first: its port is not known yet")
        prefix = "NZBOP_SERVER%d_" % number
        fields = {
            "HOST": self.host,
            "PORT": str(self.port),
            "USERNAME": username,
            "PASSWORD": password,
            "ENCRYPTION": "yes" if tls else "no",
            "CONNECTIONS": str(connections),
            "LEVEL": str(level),
            "GROUP": str(group),
            "OPTIONAL": "yes" if optional else "no",
            "RETENTION": str(retention),
            "ACTIVE": "yes" if active else "no",
            "NAME": name if name is not None else "fake%d" % number,
            "JOINGROUP": "yes" if join_group else "no",
            "CERTVERIFICATION": "Strict",
            "IPVERSION": "auto",
        }
        fields.update(extra)
        entry = {prefix + field: value for field, value in fields.items()}
        entry["NZBOP_CertCheck"] = "yes"
        return entry

    # ------------------------------------------------------------ connections

    def _accept_loop(self, listener, stopping):
        while not stopping.is_set():
            try:
                conn, _address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with self._lock:
                self.connections_total += 1
                admitted = not (self.max_connections and self._open >= self.max_connections)
                if admitted:
                    self._open += 1
                    self.max_concurrent = max(self.max_concurrent, self._open)
                else:
                    self.rejected_total += 1
                self._sockets.add(conn)
                self._handlers = [handler for handler in self._handlers if handler.is_alive()]
                handler = threading.Thread(target=self._serve, args=(conn, stopping, _Link(admitted)),
                                           name="fakenews-link", daemon=True)
                self._handlers.append(handler)
            handler.start()

    def _serve(self, conn, stopping, link):
        try:
            conn.settimeout(0.25)
            try:
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            self._talk(conn, stopping, link)
        except (_Gone, OSError):
            pass
        finally:
            try:
                conn.shutdown(socket.SHUT_WR)  # a clean end: the answers already sent are read first
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
            with self._lock:
                self._sockets.discard(conn)
                if link.admitted:
                    self._open -= 1

    def _talk(self, conn, stopping, link):
        if self.hangup:
            return
        if self.stall is True:
            self._hold(conn, stopping)
            return
        if not link.admitted and (self.limit_style != "login" or self.username is None):
            if self.limit_style == "greeting-text":
                self._send(conn, "502 maximum connections exceeded", link)
            else:
                self._send(conn, "400 too many connections", link)
            return
        if self.garbage_greeting:
            self._send(conn, "<html><body>this is not a news server</body></html>")
            self._hold(conn, stopping)
            return
        greeting = self.greeting
        self._send(conn, greeting)
        if greeting[:3] not in ("200", "201"):
            return
        buffer = bytearray()
        while not stopping.is_set():
            try:
                data = conn.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                return
            buffer += data
            with self._lock:
                self.max_burst = max(self.max_burst, buffer.count(b"\n"))
            while True:
                end = buffer.find(b"\n")
                if end < 0:
                    break
                line = bytes(buffer[:end]).rstrip(b"\r").decode("latin-1")
                del buffer[:end + 1]
                if not self._command(conn, stopping, link, line):
                    return

    @staticmethod
    def _hold(conn, stopping):
        """Read and ignore everything until the client leaves."""
        while not stopping.is_set():
            try:
                if not conn.recv(4096):
                    return
            except socket.timeout:
                continue
            except OSError:
                return

    def _send(self, conn, text, link=None):
        if link is not None and self.echo_password:
            text += " " + str(link.password or self.password or "")
        try:
            conn.sendall(text.encode("latin-1", "replace") + b"\r\n")
        except OSError:
            raise _Gone()

    # ------------------------------------------------------------ commands

    def _command(self, conn, stopping, link, line):
        """Answer one command line. Returns False when the connection is to be closed."""
        verb, _space, rest = line.strip().partition(" ")
        verb = verb.upper()
        rest = rest.strip()
        if verb == "STAT":
            return self._stat(conn, stopping, link, rest)
        with self._lock:
            self.commands.append(line)
        if verb == "AUTHINFO":
            return self._login(conn, link, rest)
        if verb == "GROUP":
            if self.username is not None and not link.authenticated:
                self._send(conn, "480 authentication required", link)
            else:
                with self._lock:
                    self.groups_joined.append(rest)
                self._send(conn, "211 0 0 0 %s" % rest)
            return True
        if verb == "QUIT":
            with self._lock:
                self.quits += 1
            self._send(conn, "205 closing connection")
            return False
        if verb == "MODE":
            self._send(conn, "200 reader mode, posting prohibited")
            return True
        self._send(conn, "500 command not recognized", link)
        return True

    def _login(self, conn, link, rest):
        word, _space, argument = rest.partition(" ")
        word = word.upper()
        refuse = self.reject_login
        if word == "USER":
            if refuse and refuse is not True and refuse != 481:
                self._send(conn, "%d authentication refused" % int(refuse), link)
                return True
            link.user = argument
            self._send(conn, "381 password required")
        elif word == "PASS":
            link.password = argument
            with self._lock:
                self.passwords_seen.append(argument)
            if not link.admitted:
                self._send(conn, "481 maximum connections exceeded", link)
            elif refuse or (self.username is not None and
                            (link.user != self.username or argument != self.password)):
                self._send(conn, "481 authentication failed", link)
            else:
                link.authenticated = True
                with self._lock:
                    self.logins_total += 1
                self._send(conn, "281 authentication accepted")
        else:
            self._send(conn, "501 syntax error", link)
        return True

    def _stat(self, conn, stopping, link, argument):
        msgid = argument[1:-1] if len(argument) >= 2 and argument[0] == "<" and argument[-1] == ">" else None
        with self._lock:
            self.stats_total += 1
            self.stat_log.append(argument if msgid is None else msgid)
        if self.stall == "stat":
            return True  # the question is read and logged, and never answered
        limit = self.drop_after_answers
        if limit is not None and link.answers >= limit and self._claim_drop():
            return False
        if self.slow > 0 and stopping.wait(self.slow):
            return False
        code = self.broken_code
        if code:
            self._send(conn, "%s STAT does not work here" % code, link)
        elif self.username is not None and not link.authenticated:
            self._send(conn, "480 authentication required", link)
        elif msgid is None:
            self._send(conn, "501 syntax error", link)
        elif self.deny_all or not self._exists(msgid):
            self._send(conn, "430 no such article", link)
        else:
            self._send(conn, "223 0 <%s>" % msgid)
        link.answers += 1
        return True

    def _exists(self, msgid):
        if msgid in self.flap:
            with self._lock:
                first = msgid not in self._flapped
                self._flapped.add(msgid)
            return not first
        return msgid in self.have

    def _claim_drop(self):
        with self._lock:
            if self.drop_limit and self.drops >= self.drop_limit:
                return False
            self.drops += 1
            return True
