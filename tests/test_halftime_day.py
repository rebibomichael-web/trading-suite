"""PROV-HTDATE-01 (ruled 2026-10-09; class precedent PROV-YTDATE-01): the
Halftime job's one America/New_York day key, its never-overwrite archive/audio
writes, and the run-marked issue body / later-run comment.

    python -m pytest -q tests/test_halftime_day.py
"""
import datetime
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import halftime_day as HD         # noqa: E402
import make_audio as MA           # noqa: E402

UTC = datetime.timezone.utc


def at(s):
    return datetime.datetime.fromisoformat(s).replace(tzinfo=UTC)


class DayKey(unittest.TestCase):
    def test_lagged_runs_key_the_air_date(self):
        # the two late-August runs whose UTC key rolled to the next day and
        # timed out polling for a non-existent episode (08-27, 08-28 lost)
        self.assertEqual(HD.halftime_day(at("2026-08-28T01:30:38")), "2026-08-27")
        self.assertEqual(HD.halftime_day(at("2026-08-29T00:58:41")), "2026-08-28")
        # the latest-starting recent run (Sep 28–Oct 8): still its air date
        self.assertEqual(HD.halftime_day(at("2026-10-05T23:11:14")), "2026-10-05")

    def test_boundary_summer_and_winter(self):
        self.assertEqual(HD.halftime_day(at("2026-10-09T03:59:59")), "2026-10-08")  # EDT -4
        self.assertEqual(HD.halftime_day(at("2026-10-09T04:00:00")), "2026-10-09")
        self.assertEqual(HD.halftime_day(at("2026-12-01T04:59:59")), "2026-11-30")  # EST -5
        self.assertEqual(HD.halftime_day(at("2026-12-01T05:00:00")), "2026-12-01")

    def test_job_env_wins_over_the_clock(self):
        with mock.patch.dict(os.environ, {"HALFTIME_DAY": "2026-01-02"}):
            self.assertEqual(HD.halftime_day(), "2026-01-02")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertRegex(HD.halftime_day(), r"^\d{4}-\d{2}-\d{2}$")

    def test_pipeline_targets_the_job_key(self):
        import halftime_pipeline as HP
        seen = []

        def fake_find(target):
            seen.append(target)
            return None

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"HALFTIME_DAY": "2026-10-09",
                                             "HALFTIME_DATE": ""}), \
                mock.patch.object(HP, "preflight_auth", lambda: None), \
                mock.patch.object(HP, "find_todays_episode", fake_find), \
                mock.patch.object(HP, "POLL_MINUTES", 0):
            cwd = os.getcwd()
            os.chdir(d)
            try:
                HP.main()
                self.assertEqual(open("summary.md").read(), "NO_EPISODE")
            finally:
                os.chdir(cwd)
        self.assertEqual(seen, [datetime.date(2026, 10, 9)])


class NeverOverwrite(unittest.TestCase):
    def test_summary_and_transcript_create_then_append(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "summary.md")
            for dest, first, second in (
                    (os.path.join(d, "summaries", "2026-10-09.md"), "FIRST SUMMARY", "SECOND SUMMARY"),
                    (os.path.join(d, "transcripts", "2026-10-09.txt"), "first words", "second words")):
                open(src, "w").write(first + "\n")
                self.assertEqual(HD.archive(src, dest), "created")
                open(src, "w").write(second + "\n")
                self.assertEqual(HD.archive(src, dest, at("2026-10-09T22:30:00")), "appended")
                text = open(dest).read()
                self.assertTrue(text.startswith(first))
                self.assertIn(second, text)
                self.assertIn("## Later run — 2026-10-09 18:30 EDT", text)

    def test_halftime_audio_appends_with_the_flag(self):
        with tempfile.TemporaryDirectory() as d:
            day = datetime.date.today().isoformat()     # inside make_audio's prune window
            mp3 = f"audio/halftime/{day}.mp3"
            inp = os.path.join(d, "summary.md")
            open(inp, "w").write("Some summary text.\n")
            fake = lambda text, dest: open(dest, "wb").write(b"NEW")  # noqa: E731
            cwd = os.getcwd()
            os.chdir(d)
            try:
                os.makedirs("audio/halftime")
                open(mp3, "wb").write(b"OLD")
                argv = ["make_audio.py", "--input", inp, "--kind", "halftime",
                        "--date", day, "--append-if-exists"]
                with mock.patch.object(MA, "synthesize", fake), \
                        mock.patch.object(sys, "argv", argv):
                    MA.main()
                self.assertEqual(open(mp3, "rb").read(), b"OLDNEW")
                self.assertFalse(os.path.exists(mp3 + ".part"))
            finally:
                os.chdir(cwd)


class IssueBodyAndComment(unittest.TestCase):
    def test_first_run_body_and_later_comment_carry_the_marker(self):
        with tempfile.TemporaryDirectory() as d:
            md = os.path.join(d, "summary.md")
            open(md, "w").write("Halftime Report — Oct 9\nOverview: x\n")
            body = HD.issue_body(md, "2026-10-09", "111")
            self.assertTrue(body.startswith("<!-- halftime-run: 111 -->"))
            self.assertNotIn("Later run", body)
            self.assertIn("Overview: x", body)
            c = HD.issue_body(md, "2026-10-09", "222", later=True,
                              now=at("2026-10-09T23:40:00"))
            self.assertTrue(c.startswith(HD.run_marker("222")))
            self.assertIn("**Later run — 2026-10-09 19:40 EDT** (run 222)", c)
            self.assertIn("`summaries/2026-10-09.md`", c)
            self.assertNotIn(HD.run_marker("111"), c)

    def test_marker_does_not_collide_with_the_youtube_digest(self):
        import digest_day as DD
        self.assertNotEqual(HD.run_marker("1"), DD.run_marker("1"))


if __name__ == "__main__":
    unittest.main()
