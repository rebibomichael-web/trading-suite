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


if __name__ == "__main__":
    unittest.main()
