"""PROV-YTDATE-01 (ruled 2026-10-09): the digest's Asia/Jerusalem day key and
its never-overwrite archive/audio writes.

    python -m pytest -q tests/test_digest_day.py
"""
import datetime
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import digest_day as DD           # noqa: E402
import make_audio as MA           # noqa: E402

UTC = datetime.timezone.utc


def at(s):
    return datetime.datetime.fromisoformat(s).replace(tzinfo=UTC)


class DayKey(unittest.TestCase):
    def test_the_10_06_collision_now_splits(self):
        # the two runs that shared UTC 10-06 (074b351, 6f6498c)
        self.assertEqual(DD.digest_day(at("2026-10-06T01:17:11")), "2026-10-06")
        self.assertEqual(DD.digest_day(at("2026-10-06T23:46:55")), "2026-10-07")

    def test_boundary_summer_and_winter(self):
        self.assertEqual(DD.digest_day(at("2026-10-08T20:59:59")), "2026-10-08")  # IDT +3
        self.assertEqual(DD.digest_day(at("2026-10-08T21:00:00")), "2026-10-09")
        self.assertEqual(DD.digest_day(at("2026-12-01T21:59:59")), "2026-12-01")  # IST +2
        self.assertEqual(DD.digest_day(at("2026-12-01T22:00:00")), "2026-12-02")

    def test_job_env_wins_over_the_clock(self):
        with mock.patch.dict(os.environ, {"DIGEST_DAY": "2026-01-02"}):
            self.assertEqual(DD.digest_day(), "2026-01-02")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertRegex(DD.digest_day(), r"^\d{4}-\d{2}-\d{2}$")


class NeverOverwrite(unittest.TestCase):
    def test_archive_creates_then_appends(self):
        with tempfile.TemporaryDirectory() as d:
            src, dest = os.path.join(d, "digest.md"), os.path.join(d, "y", "2026-10-07.md")
            open(src, "w").write("FIRST RUN\n### [a](https://www.youtube.com/watch?v=AAA)\n")
            self.assertEqual(DD.archive(src, dest), "created")
            open(src, "w").write("SECOND RUN\n### [b](https://www.youtube.com/watch?v=BBB)\n")
            self.assertEqual(DD.archive(src, dest, at("2026-10-07T21:30:00")), "appended")
            text = open(dest).read()
            self.assertTrue(text.startswith("FIRST RUN"))
            self.assertIn("watch?v=AAA", text)
            self.assertIn("watch?v=BBB", text)
            self.assertIn("## Later run — 2026-10-08 00:30 IDT", text)

    def test_audio_appends_only_when_asked(self):
        with tempfile.TemporaryDirectory() as d:
            day = datetime.date.today().isoformat()     # inside make_audio's prune window
            mp3 = f"audio/youtube/{day}.mp3"
            inp = os.path.join(d, "digest.md")
            open(inp, "w").write("Some digest text.\n")
            fake = lambda text, dest: open(dest, "wb").write(b"NEW")  # noqa: E731
            cwd = os.getcwd()
            os.chdir(d)
            try:
                os.makedirs("audio/youtube")
                open(mp3, "wb").write(b"OLD")
                argv = ["make_audio.py", "--input", inp, "--kind", "youtube",
                        "--date", day]
                with mock.patch.object(MA, "synthesize", fake), \
                        mock.patch.object(sys, "argv", argv + ["--append-if-exists"]):
                    MA.main()
                self.assertEqual(open(mp3, "rb").read(), b"OLDNEW")
                self.assertFalse(os.path.exists(mp3 + ".part"))
                with mock.patch.object(MA, "synthesize", fake), \
                        mock.patch.object(sys, "argv", argv):           # halftime path: unchanged
                    MA.main()
                self.assertEqual(open(mp3, "rb").read(), b"NEW")
            finally:
                os.chdir(cwd)


class IssueBodyAndComment(unittest.TestCase):
    """Ruled 2026-10-09: a later run on an existing day COMMENTS that day's
    issue; the run marker makes a re-run of the same run post nothing."""

    def test_first_run_body_and_later_comment_carry_the_marker(self):
        with tempfile.TemporaryDirectory() as d:
            md = os.path.join(d, "digest.md")
            open(md, "w").write("### [b](https://www.youtube.com/watch?v=BBB)\n")
            body = DD.issue_body(md, "2026-10-09", "111", "o/r")
            self.assertTrue(body.startswith("<!-- digest-run: 111 -->"))
            self.assertNotIn("Later run", body)
            self.assertIn("watch?v=BBB", body)
            c = DD.issue_body(md, "2026-10-09", "222", "o/r", later=True,
                              now=at("2026-10-09T20:20:00"))
            self.assertTrue(c.startswith(DD.run_marker("222")))
            self.assertIn("**Later run — 2026-10-09 23:20 IDT** (run 222)", c)
            self.assertIn("watch?v=BBB", c)
            self.assertNotIn(DD.run_marker("111"), c)

    def test_listen_link_only_when_the_day_has_audio(self):
        with tempfile.TemporaryDirectory() as d:
            cwd = os.getcwd()
            os.chdir(d)
            try:
                open("digest.md", "w").write("x\n")
                self.assertNotIn("Listen", DD.issue_body("digest.md", "2026-10-09", "1", "o/r"))
                os.makedirs("audio/youtube")
                open("audio/youtube/2026-10-09.mp3", "wb").write(b"x")
                self.assertIn("main/audio/youtube/2026-10-09.mp3",
                              DD.issue_body("digest.md", "2026-10-09", "1", "o/r"))
            finally:
                os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
