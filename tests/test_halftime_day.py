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


class MissedEpisodeWarning(unittest.TestCase):
    """Ruled 2026-10-09: a run that starts after its show day ended is LOUD."""

    def test_normal_lagged_runs_are_quiet(self):
        for t in ("2026-10-09T17:20:00", "2026-10-05T23:11:14",
                  "2026-08-28T01:30:38",            # the lost 08-27 run, under the NY key
                  "2026-10-10T03:59:59"):           # Fri slot, last second of NY Friday
            now = at(t)
            self.assertIsNone(HD.late_warning(HD.halftime_day(now), "1", now), t)

    def test_start_past_ny_midnight_warns_with_day_utc_and_run(self):
        now = at("2026-10-10T04:00:00")             # Sat 00:00 EDT: Friday's show day is over
        day = HD.halftime_day(now)
        self.assertEqual(day, "2026-10-10")
        self.assertEqual(HD.show_day(now), "2026-10-09")
        msg = HD.late_warning(day, "37999", now)
        for part in ("show day 2026-10-09", "UTC 2026-10-10T04:00:00Z", "run 37999",
                     "date=2026-10-09"):
            self.assertIn(part, msg)
        # Mon slot run that starts Tue 05:30Z (Tue 01:30 EDT)
        now = at("2026-10-13T05:30:00")
        self.assertEqual(HD.show_day(now), "2026-10-12")
        self.assertIsNotNone(HD.late_warning(HD.halftime_day(now), "2", now))

    def test_old_utc_key_would_have_warned(self):
        # what the 08-28 01:30Z run did: keyed 08-28, its show day was 08-27
        msg = HD.late_warning("2026-08-28", "33133074271", at("2026-08-28T01:30:38"))
        self.assertIn("show day 2026-08-27", msg)

    def test_before_the_monday_slot_the_show_day_is_friday(self):
        self.assertEqual(HD.show_day(at("2026-10-12T17:19:59")), "2026-10-09")
        self.assertEqual(HD.show_day(at("2026-10-12T17:20:00")), "2026-10-12")

    def test_cli_writes_warning_and_step_summary_line(self):
        import io
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as d:
            summ = os.path.join(d, "summary")
            out = io.StringIO()
            with mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": summ}), \
                    mock.patch.object(HD, "late_warning",
                                      lambda day, rid: f"LATE {day} {rid}"), \
                    redirect_stdout(out):
                self.assertEqual(HD.main(["late", "2026-10-10", "9"]), 0)
            self.assertEqual(out.getvalue(), "::warning::LATE 2026-10-10 9\n")
            self.assertEqual(open(summ).read(), "LATE 2026-10-10 9\n")
            out = io.StringIO()
            with mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": summ}), \
                    mock.patch.object(HD, "late_warning", lambda day, rid: None), \
                    redirect_stdout(out):
                HD.main(["late", "2026-10-09", "9"])
            self.assertEqual(out.getvalue(), "")
            self.assertEqual(open(summ).read(), "LATE 2026-10-10 9\n")

    def test_cron_constant_matches_the_workflow(self):
        wf = open(os.path.join(os.path.dirname(__file__), "..", ".github",
                               "workflows", "halftime-summary.yml")).read()
        self.assertIn("cron: '%d %d * * 1-5'" % (HD.CRON_UTC[1], HD.CRON_UTC[0]), wf)
        self.assertIn('halftime_day.py late "$DAY" "$GITHUB_RUN_ID"', wf)
        self.assertIn('if [ "$GITHUB_EVENT_NAME" = "schedule" ]; then', wf)


def _item(title, pub, url):
    return (f"<item><title><![CDATA[{title}]]></title><pubDate>{pub}</pubDate>"
            f'<enclosure url="{url}" length="1" type="audio/mpeg"/></item>')


class BackCatalogDispatch(unittest.TestCase):
    """Dispatch dates scan the whole feed (the 08-27/08-28 recovery sat at
    items 28/29); the scheduled path keeps the newest 5."""

    FEED = "<rss>" + "".join(
        [_item(f"Show {i} 10/{9 - i}/26", f"Thu, {9 - i:02d} Oct 2026 17:20:00 +0000",
               f"https://x/{i}.mp3") for i in range(6)]
        + [_item("Nov show 11/2/25", "Sun, 02 Nov 2025 17:20:00 +0000", "https://x/nov.mp3"),
           _item("Trading Nvidia 8/27/26", "Thu, 27 Aug 2026 17:18:11 +0000", "https://x/a.mp3"),
           _item("Jan show 1/2/25", "Thu, 02 Jan 2025 17:20:00 +0000", "https://x/jan.mp3")]
    ) + "</rss>"

    def setUp(self):
        import halftime_pipeline as HP
        self.HP = HP
        p = mock.patch.object(HP, "fetch", lambda url, timeout=60: self.FEED.encode())
        p.start()
        self.addCleanup(p.stop)

    def test_full_scan_finds_deep_episode_top5_does_not(self):
        d = datetime.date(2026, 8, 27)
        self.assertIsNone(self.HP.find_todays_episode(d))
        self.assertEqual(self.HP.find_todays_episode(d, limit=None),
                         ("Trading Nvidia 8/27/26", "https://x/a.mp3"))

    def test_title_match_is_digit_bounded(self):
        # 1/2/25 is a substring of 11/2/25; the full scan must not take it
        self.assertEqual(self.HP.find_todays_episode(datetime.date(2025, 1, 2), limit=None)[1],
                         "https://x/jan.mp3")

    def test_dispatch_override_uses_the_full_scan(self):
        seen = []

        def fake_find(target, limit=5):
            seen.append(limit)
            return None

        with mock.patch.dict(os.environ, {"HALFTIME_DATE": "2026-08-27"}), \
                mock.patch.object(self.HP, "preflight_auth", lambda: None), \
                mock.patch.object(self.HP, "find_todays_episode", fake_find):
            with self.assertRaises(SystemExit):
                self.HP.main()
        self.assertEqual(seen, [None])


if __name__ == "__main__":
    unittest.main()
