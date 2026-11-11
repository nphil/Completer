"""Reads an nzb file the way NZBGet does, as far as availability is concerned.

An nzb lists files, and for every file the articles (message-ids) it is made
of. This module turns that into two lists, data articles and repair (par2)
articles, and counts what NZBGet counts: sizes in bytes, and articles the nzb
itself already leaves out (gaps in the segment numbering count as failed
before the download even starts).
"""

import gzip
import io
import random
import re
import xml.etree.ElementTree as ElementTree
import zlib

from .settings import NO_PAR_ALLOWANCE

MAX_BYTES = 256 * 1024 * 1024  # larger than any real nzb; protects memory

# A message-id that is safe to put into an NNTP command: printable ASCII
# without spaces and without angle brackets, so it can never smuggle a line break.
# \Z, not $: "$" would also accept one trailing newline.
_MESSAGE_ID = re.compile(r"^[\x21-\x3b\x3d\x3f-\x7e]{1,248}\Z")
_GROUP = re.compile(r"^[A-Za-z0-9.+_-]{1,200}\Z")
_GOLDEN = 0.6180339887498949


class NzbError(Exception):
    """The file is not a usable nzb."""


class Article(object):
    __slots__ = ("msgid", "group", "posted")

    def __init__(self, msgid, group, posted):
        self.msgid = msgid
        self.group = group
        self.posted = posted  # unix time of the post, 0 when the nzb does not say


class Nzb(object):
    def __init__(self):
        self.data = []        # articles of the content
        self.par = []         # articles of the par2 repair files
        self.data_gaps = 0    # content articles the nzb does not even list
        self.par_gaps = 0
        self.data_bytes = 0
        self.par_bytes = 0
        self.files = 0
        self.newest = 0       # unix time of the most recent post in the nzb
        self.skipped = 0      # message-ids that were unusable or listed twice

    @property
    def allowed_missing(self):
        """Share of the content that may be missing while a repair is still possible.

        This is what NZBGet's critical health says: the repair data can replace
        as many bytes as it is large. Without recognisable par2 files NZBGet
        assumes 15 percent, and so do we.
        """
        if self.data_bytes <= 0:
            return 1.0
        if self.par_bytes <= 0:
            return NO_PAR_ALLOWANCE
        return min(1.0, float(self.par_bytes) / self.data_bytes)

    def seed(self):
        """A number that is the same every time this nzb is read."""
        first = (self.data or self.par or [None])[0]
        return zlib.crc32(first.msgid.encode("ascii")) if first else 0


def _local(tag):
    return tag.rsplit("}", 1)[-1].lower()


def _read(path):
    with open(path, "rb") as handle:
        head = handle.read(2)
    if head == b"\x1f\x8b":
        try:
            with gzip.open(path, "rb") as handle:
                raw = handle.read(MAX_BYTES + 1)
        except (EOFError, zlib.error, gzip.BadGzipFile):
            raise NzbError("the nzb file is a damaged gzip file")
    else:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise NzbError("the nzb file is too large")
    # Wherever it stands, and in whatever encoding (UTF-16 has a zero byte between the letters).
    if b"<!ENTITY" in (raw.replace(b"\x00", b"") if b"\x00" in raw[:64] else raw):
        raise NzbError("the nzb file declares XML entities")
    return raw


def parse(path):
    """Read the nzb at ``path``. Raises NzbError or OSError when that is not possible."""
    raw = _read(path)
    nzb = Nzb()
    seen = set()
    current = None
    try:
        for event, element in ElementTree.iterparse(io.BytesIO(raw), events=("start", "end")):
            name = _local(element.tag)
            if event == "start":
                if name == "file":
                    current = {"subject": element.get("subject") or "", "date": element.get("date") or "",
                               "groups": [], "segments": []}
                continue
            if current is None:
                continue
            if name == "group":
                group = (element.text or "").strip()
                if _GROUP.match(group):
                    current["groups"].append(group)
            elif name == "segment":
                current["segments"].append((element.get("number"), element.get("bytes"), element.text))
            elif name == "file":
                _add_file(nzb, current, seen)
                current = None
                element.clear()
    except (ElementTree.ParseError, ValueError, LookupError):  # the last two: an encoding that cannot be read
        raise NzbError("the nzb file is not valid XML")
    return nzb


def _integer(text):
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return 0


def _add_file(nzb, entry, seen):
    posted = max(0, _integer(entry["date"]))
    group = entry["groups"][0] if entry["groups"] else ""
    articles = []
    numbers = set()
    total_bytes = 0
    first_bytes = 0
    for number, size, text in entry["segments"]:
        number = _integer(number)
        if number > 0:
            numbers.add(number)  # listed by the nzb, even when we cannot or need not ask about it: no gap
        msgid = (text or "").strip()
        if msgid.startswith("<") and msgid.endswith(">"):
            msgid = msgid[1:-1]
        if not _MESSAGE_ID.match(msgid) or msgid in seen:
            nzb.skipped += 1
            continue
        seen.add(msgid)
        size = max(0, _integer(size))
        total_bytes += size
        if not first_bytes:
            first_bytes = size
        articles.append(Article(msgid, group, posted))
    if not articles:
        return  # NZBGet ignores a file without articles as well

    # Like NZBGet: the highest segment number tells how many articles the file
    # should have; numbers that are absent are articles lost before the start.
    declared = max(numbers) if numbers else len(articles)
    gaps = declared - len(numbers) if numbers and declared <= 10 * len(articles) else 0
    gaps = max(0, gaps)
    total_bytes += gaps * first_bytes

    nzb.files += 1
    nzb.newest = max(nzb.newest, posted)
    if ".par2" in entry["subject"].lower():
        nzb.par.extend(articles)
        nzb.par_gaps += gaps
        nzb.par_bytes += total_bytes
    else:
        nzb.data.extend(articles)
        nzb.data_gaps += gaps
        nzb.data_bytes += total_bytes


def spread_order(count, seed=0):
    """All indexes below ``count``, ordered so that every beginning of the list is spread evenly.

    Taking the first hundred entries samples the whole download from start to
    end, taking the next three hundred fills the spaces in between. The order
    depends on ``seed`` only, so the same nzb is always sampled the same way,
    but it follows no regular step that a pattern of missing articles could
    hide behind.
    """
    if count <= 0:
        return []
    generator = random.Random(seed)
    position = generator.random()
    taken = bytearray(count)
    order = []
    for _ in range(count):
        position += _GOLDEN
        position -= int(position)
        index = min(count - 1, int(position * count))
        if not taken[index]:
            taken[index] = 1
            order.append(index)
    rest = [index for index in range(count) if not taken[index]]
    generator.shuffle(rest)
    order.extend(rest)
    return order
