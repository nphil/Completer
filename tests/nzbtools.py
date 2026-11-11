"""Makes up nzb files for the tests: a post is a few data files plus a few par2 files.

Every message-id is derived from a tag, so two posts with different tags never
share an article, and the tests can say exactly which articles a fake news
server has.
"""

import time
from xml.sax.saxutils import escape

DAY = 86400


class Post(object):
    """One made-up post: its nzb text and the message-ids it names."""

    def __init__(self, tag):
        self.tag = tag
        self.files = []  # dicts: subject, posted, group, segments [(number, bytes, msgid)], par

    def add_file(self, subject, segments, size, posted, group, par, skip=()):
        entry = {"subject": subject, "posted": posted, "group": group, "par": par, "segments": []}
        index = len(self.files)
        for number in range(1, segments + 1):
            if number in skip:
                continue
            entry["segments"].append((number, size, "%s.%d.%d@test.invalid" % (self.tag, index, number)))
        self.files.append(entry)

    @property
    def data_ids(self):
        return [msgid for entry in self.files if not entry["par"] for _n, _s, msgid in entry["segments"]]

    @property
    def par_ids(self):
        return [msgid for entry in self.files if entry["par"] for _n, _s, msgid in entry["segments"]]

    @property
    def all_ids(self):
        return self.data_ids + self.par_ids

    @property
    def xml(self):
        out = ['<?xml version="1.0" encoding="UTF-8"?>',
               '<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">']
        for entry in self.files:
            out.append('<file poster="x@test.invalid" date="%d" subject="%s">' % (entry["posted"], escape(entry["subject"], {'"': "&quot;"})))
            out.append("<groups><group>%s</group></groups><segments>" % entry["group"])
            for number, size, msgid in entry["segments"]:
                out.append('<segment bytes="%d" number="%d">%s</segment>' % (size, number, escape(msgid)))
            out.append("</segments></file>")
        out.append("</nzb>")
        return "\n".join(out) + "\n"

    def write(self, path):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(self.xml)
        return path


def make_post(tag="a", data_files=3, segments=100, par_files=1, par_segments=10, age_hours=48.0,
              size=700000, par_size=700000, group="alt.binaries.test", now=None, skip=()):
    """A post whose files were posted ``age_hours`` before ``now``.

    ``skip`` leaves out the segment numbers of the first data file: the nzb
    names the file's last segment but not these, like a post that lost articles
    before it was ever indexed.
    """
    now = time.time() if now is None else now
    posted = int(now - age_hours * 3600)
    post = Post(tag)
    for number in range(data_files):
        post.add_file('[%s] - "data.part%02d.rar" yEnc (1/%d)' % (tag, number + 1, segments),
                      segments, size, posted, group, False, skip=skip if number == 0 else ())
    for number in range(par_files):
        name = "data.par2" if number == 0 else "data.vol%03d+%02d.par2" % (number, number)
        post.add_file('[%s] - "%s" yEnc (1/%d)' % (tag, name, par_segments), par_segments, par_size, posted, group, True)
    return post
