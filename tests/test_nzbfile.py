"""Tests for completer.nzbfile: reading an nzb, the repair allowance and the sampling order."""

import gzip
import os
import sys
import tempfile
import unittest
import zlib
from unittest import mock
from xml.sax.saxutils import escape

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Completer"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import nzbtools  # noqa: E402
from completer import nzbfile  # noqa: E402
from completer.nzbfile import Nzb, NzbError, parse, spread_order  # noqa: E402
from completer.settings import NO_PAR_ALLOWANCE  # noqa: E402

NOW = 1700000000.0
NAMESPACE = ' xmlns="http://www.newzbin.com/DTD/2003/nzb"'


def segment(number, size, text):
    """One <segment>; ``text`` goes in as it is, so character references can be used."""
    return '<segment bytes="%s" number="%s">%s</segment>' % (size, number, text)


def file_entry(segments, subject='"data.rar" yEnc', date="1700000000", groups=("alt.binaries.test",)):
    return ('<file poster="x@example.invalid" date="%s" subject="%s"><groups>%s</groups><segments>%s</segments></file>'
            % (date, escape(subject, {'"': "&quot;"}), "".join("<group>%s</group>" % g for g in groups),
               "".join(segments)))


def nzb_text(*files, **options):
    head = options.get("head", '<?xml version="1.0" encoding="UTF-8"?>\n')
    namespace = options.get("namespace", NAMESPACE)
    return "%s<nzb%s>\n%s\n</nzb>\n" % (head, namespace, "\n".join(files))


class NzbCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def path(self, name="x.nzb"):
        return os.path.join(self.dir, name)

    def write(self, data, name="x.nzb"):
        path = self.path(name)
        if not isinstance(data, bytes):
            data = data.encode("utf-8")
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def parse_text(self, text, name="x.nzb"):
        return parse(self.write(text, name))


class ParsePostTest(NzbCase):
    def test_counts_bytes_and_order(self):
        post = nzbtools.make_post("cnt", data_files=3, segments=100, par_files=2, par_segments=10,
                                  size=700000, par_size=500000, now=NOW, age_hours=24)
        nzb = parse(post.write(self.path()))
        self.assertEqual(len(nzb.data), 300)
        self.assertEqual(len(nzb.par), 20)
        self.assertEqual(nzb.files, 5)
        self.assertEqual(nzb.data_bytes, 300 * 700000)
        self.assertEqual(nzb.par_bytes, 20 * 500000)
        self.assertEqual((nzb.data_gaps, nzb.par_gaps, nzb.skipped), (0, 0, 0))
        self.assertEqual([a.msgid for a in nzb.data], post.data_ids)
        self.assertEqual([a.msgid for a in nzb.par], post.par_ids)
        self.assertAlmostEqual(nzb.allowed_missing, (20 * 500000) / float(300 * 700000))

    def test_every_article_carries_the_group_and_the_post_time_of_its_file(self):
        post = nzbtools.make_post("grp", data_files=1, segments=5, par_files=1, par_segments=2,
                                  group="alt.binaries.example", now=NOW, age_hours=10)
        nzb = parse(post.write(self.path()))
        posted = int(NOW - 10 * 3600)
        for article in nzb.data + nzb.par:
            self.assertEqual(article.group, "alt.binaries.example")
            self.assertEqual(article.posted, posted)
        self.assertEqual(nzb.newest, posted)

    def test_no_par2_files_means_the_default_allowance(self):
        post = nzbtools.make_post("nopar", data_files=2, segments=30, par_files=0, now=NOW)
        nzb = parse(post.write(self.path()))
        self.assertEqual(nzb.par, [])
        self.assertEqual(nzb.par_bytes, 0)
        self.assertEqual(nzb.allowed_missing, NO_PAR_ALLOWANCE)
        self.assertEqual(NO_PAR_ALLOWANCE, 0.15)

    def test_no_content_bytes_means_everything_may_be_missing(self):
        text = nzb_text(file_entry([segment(1, 0, "a@x"), segment(2, 0, "b@x")]),
                        file_entry([segment(1, 1000, "p@x")], subject='"data.par2" yEnc'))
        nzb = self.parse_text(text)
        self.assertEqual(nzb.data_bytes, 0)
        self.assertEqual(nzb.allowed_missing, 1.0)

    def test_a_post_with_only_repair_files_has_no_content(self):
        nzb = self.parse_text(nzb_text(file_entry([segment(1, 100, "p@x")], subject='"a.par2" yEnc')))
        self.assertEqual((len(nzb.data), len(nzb.par)), (0, 1))
        self.assertEqual(nzb.allowed_missing, 1.0)

    def test_par2_is_recognised_by_the_subject_in_any_case(self):
        text = nzb_text(file_entry([segment(1, 10, "d@x")], subject='"movie.mkv" yEnc'),
                        file_entry([segment(1, 10, "p1@x")], subject='"movie.PAR2" yEnc (1/1)'),
                        file_entry([segment(1, 10, "p2@x")], subject='[1/9] - "movie.vol000+01.par2" yEnc'),
                        file_entry([segment(1, 10, "d2@x")], subject='"movie.par" yEnc'))
        nzb = self.parse_text(text)
        self.assertEqual([a.msgid for a in nzb.data], ["d@x", "d2@x"])
        self.assertEqual([a.msgid for a in nzb.par], ["p1@x", "p2@x"])

    def test_allowance_is_capped_at_one(self):
        text = nzb_text(file_entry([segment(1, 100, "d@x")]),
                        file_entry([segment(1, 900, "p@x")], subject='"d.par2" yEnc'))
        self.assertEqual(self.parse_text(text).allowed_missing, 1.0)

    def test_newest_is_the_most_recent_post_time(self):
        text = nzb_text(file_entry([segment(1, 1, "a@x")], date="1000"),
                        file_entry([segment(1, 1, "b@x")], date="5000"),
                        file_entry([segment(1, 1, "c@x")], date="3000"))
        self.assertEqual(self.parse_text(text).newest, 5000)

    def test_missing_or_broken_dates_count_as_zero(self):
        text = nzb_text(file_entry([segment(1, 1, "a@x")], date=""),
                        file_entry([segment(1, 1, "b@x")], date="yesterday"),
                        file_entry([segment(1, 1, "c@x")], date="-50"))
        nzb = self.parse_text(text)
        self.assertEqual(nzb.newest, 0)
        self.assertEqual([a.posted for a in nzb.data], [0, 0, 0])

    def test_a_file_without_a_usable_article_is_ignored(self):
        text = nzb_text(file_entry([segment(1, 1, "a@x")], date="1000"),
                        file_entry([segment(1, 1, "has space@x"), segment(2, 1, "")], date="9000"),
                        file_entry([], date="9500"))
        nzb = self.parse_text(text)
        self.assertEqual(nzb.files, 1)
        self.assertEqual(nzb.newest, 1000)
        self.assertEqual(nzb.skipped, 2)

    def test_nzb_without_namespace_or_with_another_one_is_read_too(self):
        body = file_entry([segment(1, 10, "a@x"), segment(2, 10, "b@x")])
        for namespace in ("", ' xmlns="http://example.invalid/other"'):
            nzb = self.parse_text(nzb_text(body, namespace=namespace))
            self.assertEqual([a.msgid for a in nzb.data], ["a@x", "b@x"])

    def test_nzb_without_any_file_is_empty(self):
        nzb = self.parse_text(nzb_text())
        self.assertEqual((nzb.data, nzb.par, nzb.files, nzb.newest), ([], [], 0, 0))
        self.assertEqual(nzb.allowed_missing, 1.0)
        self.assertEqual(nzb.seed(), 0)

    def test_a_large_nzb_is_read_completely(self):
        post = nzbtools.make_post("big", data_files=6, segments=2000, par_files=1, par_segments=100, now=NOW)
        nzb = parse(post.write(self.path()))
        self.assertEqual((len(nzb.data), len(nzb.par)), (12000, 100))
        self.assertEqual(len({a.msgid for a in nzb.data + nzb.par}), 12100)

    def test_other_encodings_are_read(self):
        post = nzbtools.make_post("enc", data_files=1, segments=3, par_files=0, now=NOW)
        for label, codec in (("UTF-16", "utf-16"), ("ISO-8859-1", "latin-1")):
            text = post.xml.replace("UTF-8", label).replace("</nzb>", "<!-- \xe9 --></nzb>")
            nzb = parse(self.write(text.encode(codec)))
            self.assertEqual([a.msgid for a in nzb.data], post.data_ids, label)

    def test_a_missing_file_raises_oserror(self):
        with self.assertRaises(OSError):
            parse(self.path("does-not-exist.nzb"))
        with self.assertRaises(OSError):
            parse(self.dir)


class GroupTest(NzbCase):
    def groups_of(self, *groups):
        nzb = self.parse_text(nzb_text(file_entry([segment(1, 1, "a@x")], groups=groups)))
        return [a.group for a in nzb.data]

    def test_first_usable_group_is_taken(self):
        self.assertEqual(self.groups_of("alt.binaries.one", "alt.binaries.two"), ["alt.binaries.one"])

    def test_unusable_groups_are_passed_over(self):
        self.assertEqual(self.groups_of("bad group", "alt.binaries.ok"), ["alt.binaries.ok"])
        self.assertEqual(self.groups_of("bad group", "x&amp;y;z"), [""])
        self.assertEqual(self.groups_of(), [""])

    def test_group_text_is_trimmed_but_never_carries_a_line_break(self):
        self.assertEqual(self.groups_of("  alt.test  "), ["alt.test"])
        self.assertEqual(self.groups_of("alt.test&#10;x", "alt.fine"), ["alt.fine"])

    def test_group_names_are_matched_to_the_end_not_up_to_a_trailing_newline(self):
        # The pattern must end with \Z: with $ the name "alt.test\n" would match, and could reach a GROUP command.
        self.assertIsNone(nzbfile._GROUP.match("alt.test\n"))
        self.assertIsNotNone(nzbfile._GROUP.match("alt.test"))
        self.assertIsNone(nzbfile._GROUP.match("a" * 201))


class MessageIdTest(NzbCase):
    def accepted(self, *texts):
        segments = [segment(number, 10, text) for number, text in enumerate(texts, 1)]
        nzb = self.parse_text(nzb_text(file_entry(segments)))
        return [a.msgid for a in nzb.data], nzb

    def test_brackets_are_stripped(self):
        ids, nzb = self.accepted("&lt;one@x&gt;", "  &lt;two@x&gt;\n  ", "plain@x", "&#10;  three@x &#10;")
        self.assertEqual(ids, ["one@x", "two@x", "plain@x", "three@x"])
        self.assertEqual(nzb.skipped, 0)

    def test_only_one_pair_of_brackets_is_removed(self):
        ids, nzb = self.accepted("&lt;&lt;one@x&gt;&gt;", "&lt;two@x", "three@x&gt;")
        self.assertEqual(ids, [])
        self.assertEqual(nzb.skipped, 3)

    def test_unusable_ids_are_skipped(self):
        texts = [
            "has space@x",
            "in&lt;ner@x",
            "in&gt;ner@x",
            "&lt;&gt;",
            "",
            "tab&#9;id@x",
            "new&#10;line@x",
            "carriage&#13;return@x",
            "del&#127;@x",
            "caf\u00e9@x",
            "x" * 249,
        ]
        ids, nzb = self.accepted(*texts)
        self.assertEqual(ids, [])
        self.assertEqual(nzb.skipped, len(texts))
        self.assertEqual((nzb.data_bytes, nzb.files), (0, 0))

    def test_a_line_break_inside_the_brackets_is_not_let_through(self):
        # "<abc@x\n>" passes the "starts with < and ends with >" test; what stands between must still be one token.
        ids, nzb = self.accepted("&lt;abc@x&#10;&gt;", "&lt;abc@x&#13;&#10;QUIT&gt;", "&lt;&#10;abc@x&gt;")
        self.assertEqual(ids, [])
        self.assertEqual(nzb.skipped, 3)

    def test_the_pattern_is_matched_to_the_very_end(self):
        pattern = nzbfile._MESSAGE_ID
        self.assertIsNone(pattern.match("abc@x\n"))
        self.assertIsNone(pattern.match("abc@x\r\n"))
        self.assertIsNotNone(pattern.match("abc@x"))

    def test_limits_of_what_is_allowed(self):
        longest = "y" * 248
        odd = "part1of2.a1b2$c%d~e=f?g;h!i#j'k\"l&amp;m@news.example.invalid"
        ids, nzb = self.accepted(longest, odd)
        self.assertEqual(ids, [longest, odd.replace("&amp;", "&")])
        self.assertEqual(nzb.skipped, 0)

    def test_duplicates_are_skipped_inside_a_file_and_across_files(self):
        text = nzb_text(file_entry([segment(1, 10, "a@x"), segment(2, 10, "b@x"), segment(3, 10, "a@x")]),
                        file_entry([segment(1, 10, "&lt;b@x&gt;"), segment(2, 10, "c@x")]),
                        file_entry([segment(1, 10, "c@x")], subject='"d.par2" yEnc'))
        nzb = self.parse_text(text)
        self.assertEqual([a.msgid for a in nzb.data], ["a@x", "b@x", "c@x"])
        self.assertEqual(nzb.par, [])
        self.assertEqual(nzb.skipped, 3)
        self.assertEqual(nzb.files, 2)  # the par2 file had nothing left
        self.assertEqual(nzb.data_bytes, 30)  # what is listed twice is counted once
        self.assertEqual(nzb.data_gaps, 0)

    def test_skipped_ids_are_listed_articles_and_create_no_gaps(self):
        # The nzb does list segments 2 and 4; that we cannot (or need not) ask about them does not make them lost.
        text = nzb_text(file_entry([segment(1, 10, "a@x"), segment(2, 10, "has space@x"), segment(3, 10, "c@x"),
                                    segment(4, 10, "a@x"), segment(5, 10, "e@x")]))
        nzb = self.parse_text(text)
        self.assertEqual([a.msgid for a in nzb.data], ["a@x", "c@x", "e@x"])
        self.assertEqual((nzb.skipped, nzb.data_gaps), (2, 0))
        self.assertEqual(nzb.data_bytes, 30)

    def test_a_skipped_id_with_an_absurd_number_creates_no_gaps_either(self):
        text = nzb_text(file_entry([segment(1, 10, "a@x"), segment(2, 10, "b@x"), segment(900000, 10, "bad id")]))
        self.assertEqual(self.parse_text(text).data_gaps, 0)

    def test_the_same_nzb_gives_the_same_articles(self):
        post = nzbtools.make_post("same", now=NOW)
        path = post.write(self.path())
        first, second = parse(path), parse(path)
        self.assertEqual([a.msgid for a in first.data], [a.msgid for a in second.data])
        self.assertEqual(first.seed(), second.seed())


class GapTest(NzbCase):
    def test_skipped_segment_numbers_are_gaps_counted_at_the_size_of_the_first_segment(self):
        post = nzbtools.make_post("gap", data_files=2, segments=50, par_files=0, size=1000, skip=(5, 6, 7), now=NOW)
        nzb = parse(post.write(self.path()))
        self.assertEqual(len(nzb.data), 97)
        self.assertEqual(nzb.data_gaps, 3)
        self.assertEqual(nzb.par_gaps, 0)
        self.assertEqual(nzb.data_bytes, 100 * 1000)

    def test_a_missing_last_segment_is_not_noticed(self):
        # The highest number in the file is the one that says how many there should be, like in NZBGet.
        post = nzbtools.make_post("last", data_files=1, segments=20, par_files=0, size=500, skip=(19, 20), now=NOW)
        nzb = parse(post.write(self.path()))
        self.assertEqual((len(nzb.data), nzb.data_gaps, nzb.data_bytes), (18, 0, 18 * 500))

    def test_gaps_of_repair_files_are_counted_separately(self):
        text = nzb_text(file_entry([segment(n, 100, "d%d@x" % n) for n in (1, 2, 3)]),
                        file_entry([segment(1, 80, "p1@x"), segment(2, 80, "p2@x"), segment(5, 80, "p5@x")],
                                   subject='"d.par2" yEnc'))
        nzb = self.parse_text(text)
        self.assertEqual((nzb.data_gaps, nzb.par_gaps), (0, 2))
        self.assertEqual(nzb.par_bytes, 5 * 80)
        self.assertEqual(len(nzb.par), 3)

    def test_absurd_numbering_creates_no_gaps(self):
        text = nzb_text(file_entry([segment(1, 100, "a@x"), segment(5000, 100, "b@x")]))
        nzb = self.parse_text(text)
        self.assertEqual((len(nzb.data), nzb.data_gaps, nzb.data_bytes), (2, 0, 200))

    def test_unnumbered_segments_create_no_gaps(self):
        text = nzb_text(file_entry([segment("", 100, "a@x"), segment("x", 100, "b@x"), segment(0, 100, "c@x")]))
        nzb = self.parse_text(text)
        self.assertEqual((len(nzb.data), nzb.data_gaps), (3, 0))

    def test_repeated_numbers_create_no_gaps(self):
        text = nzb_text(file_entry([segment(1, 100, "a@x"), segment(2, 100, "b@x"), segment(2, 100, "c@x")]))
        self.assertEqual(self.parse_text(text).data_gaps, 0)

    def test_gaps_lower_the_allowance_through_the_bytes_they_add(self):
        post = nzbtools.make_post("gapallow", data_files=1, segments=100, par_files=1, par_segments=10,
                                  size=1000, par_size=1000, skip=(10, 11, 12, 13, 14), now=NOW)
        nzb = parse(post.write(self.path()))
        self.assertEqual(nzb.data_bytes, 100 * 1000)
        self.assertAlmostEqual(nzb.allowed_missing, 0.1)


class AllowedMissingTest(unittest.TestCase):
    def nzb(self, data_bytes, par_bytes):
        nzb = Nzb()
        nzb.data_bytes, nzb.par_bytes = data_bytes, par_bytes
        return nzb

    def test_share_of_repair_bytes_in_content_bytes(self):
        self.assertAlmostEqual(self.nzb(1000, 100).allowed_missing, 0.1)
        self.assertAlmostEqual(self.nzb(3, 1).allowed_missing, 1 / 3.0)

    def test_without_repair_data_it_is_fifteen_percent(self):
        self.assertEqual(self.nzb(1000, 0).allowed_missing, 0.15)

    def test_without_content_everything_is_allowed(self):
        self.assertEqual(self.nzb(0, 0).allowed_missing, 1.0)
        self.assertEqual(self.nzb(0, 500).allowed_missing, 1.0)

    def test_it_never_exceeds_one(self):
        self.assertEqual(self.nzb(100, 100).allowed_missing, 1.0)
        self.assertEqual(self.nzb(100, 5000).allowed_missing, 1.0)


class SeedTest(NzbCase):
    def test_seed_is_the_checksum_of_the_first_article(self):
        post = nzbtools.make_post("seed", now=NOW)
        nzb = parse(post.write(self.path()))
        self.assertEqual(nzb.seed(), zlib.crc32(post.data_ids[0].encode("ascii")))
        self.assertTrue(0 <= nzb.seed() < 2 ** 32)

    def test_seed_differs_between_posts_and_is_stable(self):
        seeds = set()
        for tag in ("a", "b", "c", "d"):
            path = nzbtools.make_post("seed-" + tag, now=NOW).write(self.path(tag + ".nzb"))
            self.assertEqual(parse(path).seed(), parse(path).seed())
            seeds.add(parse(path).seed())
        self.assertEqual(len(seeds), 4)

    def test_a_post_of_repair_files_only_uses_those(self):
        nzb = self.parse_par_only()
        self.assertEqual(nzb.seed(), zlib.crc32(b"p1@x"))

    def parse_par_only(self):
        return parse(self.write(nzb_text(file_entry([segment(1, 10, "p1@x")], subject='"a.par2" yEnc'))))


class DamagedFileTest(NzbCase):
    def good_xml(self):
        return nzbtools.make_post("dmg", data_files=2, segments=50, now=NOW).xml.encode("utf-8")

    def test_gzip_is_read_like_plain_text(self):
        post = nzbtools.make_post("gz", now=NOW)
        plain = parse(post.write(self.path("plain.nzb")))
        packed_path = self.path("packed.nzb")
        with gzip.open(packed_path, "wb") as handle:
            handle.write(post.xml.encode("utf-8"))
        packed = parse(packed_path)
        self.assertEqual([a.msgid for a in packed.data], [a.msgid for a in plain.data])
        self.assertEqual([a.msgid for a in packed.par], [a.msgid for a in plain.par])
        self.assertEqual((packed.data_bytes, packed.par_bytes), (plain.data_bytes, plain.par_bytes))

    def test_truncated_gzip_is_an_nzb_error(self):
        packed = gzip.compress(self.good_xml())
        with self.assertRaises(NzbError):
            parse(self.write(packed[:len(packed) // 2]))
        with self.assertRaises(NzbError):
            parse(self.write(packed[:12]))

    def test_corrupt_gzip_is_an_nzb_error(self):
        packed = bytearray(gzip.compress(self.good_xml()))
        for offset in (len(packed) // 2, len(packed) - 6):
            damaged = bytearray(packed)
            damaged[offset] ^= 0xFF
            with self.assertRaises(NzbError):
                parse(self.write(bytes(damaged)))

    def test_something_that_only_starts_like_gzip_is_an_nzb_error(self):
        for junk in (b"\x1f\x8b", b"\x1f\x8b" + b"junk" * 20, b"\x1f\x8b\x08\x00" + b"\xff" * 30):
            with self.assertRaises(NzbError):
                parse(self.write(junk))

    def test_text_that_is_not_xml_is_refused(self):
        for content in (b"", b"hello", b"<nzb", b"<nzb><file></nzb>", b"\x00\x01\x02\x03", b"{\"a\": 1}",
                        b"<?xml version='1.0'?>", b"\xff\xfe\xfd\xfc" * 10):
            with self.assertRaises(NzbError, msg=repr(content)):
                parse(self.write(content))

    def test_an_encoding_that_cannot_be_read_is_an_nzb_error(self):
        for label in ("utf-32", "no-such-encoding"):
            text = '<?xml version="1.0" encoding="%s"?>\n<nzb></nzb>' % label
            with self.assertRaises(NzbError, msg=label):
                parse(self.write(text.encode("ascii")))

    def test_too_large_a_file_is_refused(self):
        xml = self.good_xml()
        path = self.write(xml)
        with mock.patch.object(nzbfile, "MAX_BYTES", len(xml)):
            self.assertEqual(len(parse(path).data), 100)  # exactly as large as allowed is fine
        with mock.patch.object(nzbfile, "MAX_BYTES", len(xml) - 1):
            with self.assertRaises(NzbError):
                parse(path)

    def test_a_gzip_that_unpacks_to_too_much_is_refused_without_unpacking_it_all(self):
        path = self.path("bomb.nzb")
        with gzip.open(path, "wb") as handle:
            handle.write(b" " * (5 * 1024 * 1024))
        self.assertLess(os.path.getsize(path), 100 * 1024)
        with mock.patch.object(nzbfile, "MAX_BYTES", 1024 * 1024):
            with self.assertRaises(NzbError):
                parse(path)


class EntityTest(NzbCase):
    DOCTYPE = '<!DOCTYPE nzb [<!ENTITY big "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa">]>\n'

    def document(self, prolog=""):
        return ('<?xml version="1.0" encoding="UTF-8"?>\n' + prolog + self.DOCTYPE +
                "<nzb><file subject='x' date='1'><segments><segment number='1' bytes='1'>&big;</segment>"
                "</segments></file></nzb>")

    def test_entity_declaration_is_refused(self):
        with self.assertRaises(NzbError):
            parse(self.write(self.document()))

    def test_entity_bomb_is_refused(self):
        bomb = ('<?xml version="1.0"?>\n<!DOCTYPE nzb [<!ENTITY a "aaaaaaaaaa">'
                '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;"><!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">]>\n'
                "<nzb>&c;</nzb>")
        with self.assertRaises(NzbError):
            parse(self.write(bomb))

    def test_declaration_far_from_the_start_is_refused(self):
        prolog = "<!-- " + "x" * (2 * 1024 * 1024) + " -->\n"
        with self.assertRaises(NzbError):
            parse(self.write(self.document(prolog)))

    def test_declaration_in_a_utf16_document_is_refused(self):
        text = self.document().replace("UTF-8", "UTF-16")
        for codec in ("utf-16", "utf-16-le", "utf-16-be"):
            data = text.encode(codec)
            if codec != "utf-16":
                data = (b"\xff\xfe" if codec.endswith("le") else b"\xfe\xff") + data
            with self.assertRaises(NzbError, msg=codec):
                parse(self.write(data))

    def test_a_doctype_without_entities_is_fine(self):
        text = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE nzb PUBLIC "-//newzBin//DTD NZB 1.1//EN" '
                '"http://www.newzbin.com/DTD/nzb/nzb-1.1.dtd">\n'
                + nzb_text(file_entry([segment(1, 5, "a@x")]), head=""))
        self.assertEqual([a.msgid for a in self.parse_text(text).data], ["a@x"])


class SpreadOrderTest(unittest.TestCase):
    def test_it_is_a_permutation(self):
        for count in (1, 2, 3, 4, 7, 10, 97, 200, 1000, 3000):
            for seed in (0, 1, 12345, 2 ** 32 - 1):
                self.assertEqual(sorted(spread_order(count, seed)), list(range(count)), (count, seed))

    def test_nothing_to_order(self):
        self.assertEqual(spread_order(0), [])
        self.assertEqual(spread_order(0, 99), [])
        self.assertEqual(spread_order(-5), [])
        self.assertEqual(spread_order(1), [0])
        self.assertEqual(spread_order(1, 77), [0])

    def test_the_same_seed_gives_the_same_order(self):
        self.assertEqual(spread_order(500, 42), spread_order(500, 42))
        self.assertEqual(spread_order(500), spread_order(500, 0))

    def test_different_seeds_give_different_orders(self):
        orders = {tuple(spread_order(300, seed)) for seed in range(20)}
        self.assertEqual(len(orders), 20)
        self.assertNotEqual(spread_order(300, 1)[:20], spread_order(300, 2)[:20])

    def test_the_first_entries_cover_the_whole_range_evenly(self):
        for count, firsts in ((100, (10, 25)), (300, (10, 25, 50)), (1000, (10, 50, 120)), (3000, (25, 120, 400))):
            for seed in range(100):
                order = spread_order(count, seed)
                for k in firsts:
                    picked = sorted(order[:k])
                    gaps = [picked[0] + count - picked[-1]] + [b - a for a, b in zip(picked, picked[1:])]
                    self.assertLessEqual(max(gaps), 2.0 * count / k + 1, (count, k, seed))
                    self.assertGreaterEqual(min(gaps), 1)

    def test_the_first_entries_do_not_follow_a_step(self):
        # A pattern in which every p-th article is missing must be seen at about the right rate.
        count, sample = 3000, 120
        for step in (2, 3, 5, 7, 10):
            for seed in range(50):
                hits = sum(1 for index in spread_order(count, seed)[:sample] if index % step == 0)
                self.assertAlmostEqual(hits / float(sample), 1.0 / step, delta=0.06, msg=(step, seed))

    def test_a_missing_block_is_seen_at_about_the_right_rate(self):
        count, sample = 1000, 120
        for seed in range(50):
            hits = sum(1 for index in spread_order(count, seed)[:sample] if 300 <= index < 400)
            self.assertAlmostEqual(hits / float(sample), 0.1, delta=0.04, msg=seed)

    def test_the_next_entries_fill_the_spaces_in_between(self):
        count = 600
        order = spread_order(count, 3)
        for k in (100, 300):
            picked = sorted(order[:k])
            gaps = [b - a for a, b in zip(picked, picked[1:])]
            self.assertLessEqual(max(gaps), 2.0 * count / k + 1)
        self.assertEqual(len(set(order[:300])), 300)


if __name__ == "__main__":
    unittest.main()
