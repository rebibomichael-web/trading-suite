"""Per-channel snapshot staleness (ruled 2026-09-17) and the Y-6 roster
footer in scripts/youtube_digest.py, driven end-to-end through main() with
every network/Claude call mocked.

    .venv/bin/python -m unittest tests.test_youtube_digest_roster -v
"""
import datetime
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import youtube_digest as D        # noqa: E402
import youtube_coverage as C      # noqa: E402

NOW = datetime.datetime.now(datetime.timezone.utc)
CH = list(D.CHANNELS)
H = datetime.timedelta(hours=1)


def iso(dt):
    return dt.isoformat()


def entry(vid, age_h=5):
    return {"id": vid, "title": f"Title {vid}", "published": iso(NOW - age_h * H),
            "description": "desc"}


def write_snapshot(channels, meta=None, fetched_at=NOW - 2 * H):
    os.makedirs("feeds", exist_ok=True)
    snap = {"fetched_at": iso(fetched_at) if fetched_at else None,
            "channels": channels}
    if meta is not None:
        snap["meta"] = meta
    with open(D.FEED_SNAPSHOT_PATH, "w") as fh:
        json.dump(snap, fh)


def run_digest(live):
    """live: {channel_id: entries | Exception}. Returns (digest.md, stdout)."""
    def fake_feed(cid):
        r = live.get(cid, RuntimeError("blocked"))
        if isinstance(r, Exception):
            raise r
        return [dict(v, published=datetime.datetime.fromisoformat(v["published"]))
                for v in r]
    out = io.StringIO()
    with mock.patch.object(D, "fetch_feed", side_effect=fake_feed), \
         mock.patch.object(D, "preflight_auth"), \
         mock.patch.object(D, "ask_claude", return_value="A summary."), \
         mock.patch.object(D, "fetch_transcript", return_value="words " * 50), \
         redirect_stdout(out):
        D.main()
    with open("digest.md") as fh:
        return fh.read(), out.getvalue()


class InTempRepo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        os.makedirs("summaries/youtube")

    def tearDown(self):
        os.chdir(self.cwd)
        self.tmp.cleanup()


class LoadSnapshotTests(InTempRepo):
    def test_missing_file(self):
        self.assertEqual(D.load_feed_snapshot(NOW), ({}, {}))

    def test_legacy_file_uses_fetched_at_per_channel(self):
        write_snapshot({CH[0]: [entry("a")]}, fetched_at=NOW - 100 * H)
        chans, meta = D.load_feed_snapshot(NOW)
        self.assertIn(CH[0], chans)                           # NOT dropped at >72h
        self.assertEqual(meta[CH[0]]["stale_note"], "(feed stale 4d)")
        self.assertEqual(meta[CH[0]]["consecutive_errors"], 0)

    def test_legacy_file_fresh_has_no_note(self):
        write_snapshot({CH[0]: [entry("a")]}, fetched_at=NOW - 10 * H)
        _, meta = D.load_feed_snapshot(NOW)
        self.assertEqual(meta[CH[0]]["stale_note"], "")

    def test_meta_gates_per_channel_not_fetched_at(self):
        write_snapshot(
            {CH[0]: [entry("a")], CH[1]: [entry("b")], CH[2]: [entry("c")]},
            meta={CH[0]: {"last_ok": iso(NOW - 1 * H), "consecutive_errors": 0, "last_error": None},
                  CH[1]: {"last_ok": iso(NOW - 5 * 24 * H), "consecutive_errors": 4, "last_error": "HTTP 404"},
                  CH[2]: {"last_ok": None, "consecutive_errors": 1, "last_error": "HTTP 500"},
                  CH[3]: {"last_ok": None, "consecutive_errors": 3, "last_error": "HTTP 404"}},
            fetched_at=NOW - 1 * H)   # top-level says fresh; per-channel must win
        chans, meta = D.load_feed_snapshot(NOW)
        self.assertEqual(set(chans), {CH[0], CH[1], CH[2]})
        self.assertEqual(meta[CH[0]]["stale_note"], "")
        self.assertEqual(meta[CH[1]]["stale_note"], "(feed stale 5d)")
        self.assertEqual(meta[CH[1]]["consecutive_errors"], 4)
        self.assertEqual(meta[CH[2]]["stale_note"], "(feed stale, last ok unknown)")
        self.assertNotIn(CH[3], chans)                       # meta-only: no entries
        self.assertEqual(meta[CH[3]]["last_error"], "HTTP 404")

    def test_uses_default_now(self):
        write_snapshot({CH[0]: [entry("a")]})
        chans, meta = D.load_feed_snapshot()
        self.assertEqual(meta[CH[0]]["stale_note"], "")


class DigestRosterTests(InTempRepo):
    """Live feed works for CH[0] only; CH[1] fresh in snapshot; CH[2] stale
    5d in snapshot; CH[3] reseeded (last_ok null) in snapshot; CH[4] meta-only
    with 3 errors; CH[5..7] absent everywhere."""

    def setUp(self):
        super().setUp()
        write_snapshot(
            {CH[1]: [entry("b1")], CH[2]: [entry("c1", age_h=30)],
             CH[3]: [entry("d1", age_h=200)]},
            meta={CH[1]: {"last_ok": iso(NOW - 1 * H), "consecutive_errors": 0, "last_error": None},
                  CH[2]: {"last_ok": iso(NOW - 5 * 24 * H), "consecutive_errors": 5, "last_error": "HTTP 500"},
                  CH[3]: {"last_ok": None, "consecutive_errors": 1, "last_error": "HTTP 404"},
                  CH[4]: {"last_ok": None, "consecutive_errors": 3, "last_error": "HTTP 404"}})
        self.live = {D.CHANNELS[CH[0]]: [entry("a1")]}

    def test_every_channel_stated(self):
        md, log = run_digest(self.live)
        self.assertIn(D.ROSTER_START, md)
        self.assertIn(D.ROSTER_END, md)
        footer = md[md.index(D.ROSTER_START):]
        for c in CH:
            self.assertIn(f"| {c} |", footer, c)
        self.assertEqual(footer.count("\n| "), 1 + len(CH))   # header + 8 rows

    def test_stale_channel_included_and_labelled(self):
        md, log = run_digest(self.live)
        # c1 (30h old, in window) was summarized, header carries the label
        self.assertIn(f"**{CH[2]}** · ", md)
        self.assertIn("*(feed stale 5d)*", md)
        self.assertIn(f"{CH[2]}: live feed failed", log)
        self.assertIn("(feed stale 5d)", log)
        footer = md[md.index(D.ROSTER_START):]
        row = next(l for l in footer.splitlines() if l.startswith(f"| {CH[2]} |"))
        self.assertIn("| snapshot (feed stale 5d) |", row)
        self.assertIn("| 5 (HTTP 500) |", row)

    def test_reseeded_channel_unknown_last_ok(self):
        md, _ = run_digest(self.live)
        footer = md[md.index(D.ROSTER_START):]
        row = next(l for l in footer.splitlines() if l.startswith(f"| {CH[3]} |"))
        self.assertIn("| snapshot (feed stale, last ok unknown) |", row)
        self.assertIn("| unknown |", row)
        self.assertIn("| 1 (HTTP 404) |", row)

    def test_absent_channels_are_stated(self):
        md, _ = run_digest(self.live)
        footer = md[md.index(D.ROSTER_START):]
        rows = {l.split(" | ")[0][2:]: l for l in footer.splitlines() if l.startswith("| ")}
        self.assertIn("| unavailable | unknown | 3 (HTTP 404) |", rows[CH[4]])
        for c in CH[5:]:
            self.assertIn("| unavailable | — (not in snapshot) | — |", rows[c])

    def test_live_and_fresh_snapshot_rows(self):
        md, _ = run_digest(self.live)
        footer = md[md.index(D.ROSTER_START):]
        rows = {l.split(" | ")[0][2:]: l for l in footer.splitlines() if l.startswith("| ")}
        self.assertIn("| live |", rows[CH[0]])
        self.assertIn("| — (not in snapshot) | — |", rows[CH[0]])   # live-only, no Dell row
        self.assertIn("| snapshot |", rows[CH[1]])
        # summarized THIS run counts as summarized (not missing/queued)
        self.assertRegex(rows[CH[1]], r"\| 1 \| 1 \(1T/0D\) \| 0 \| 0 \|")
        self.assertNotIn("*(feed", md.split(D.ROSTER_START)[0].split(CH[1])[1][:60])

    def test_footer_uses_coverage_classifier(self):
        with mock.patch.object(C, "classify_channel", wraps=C.classify_channel) as cc:
            run_digest(self.live)
        self.assertGreaterEqual(cc.call_count, 4)   # a1, b1, c1, d1 channels

    def test_no_videos_day_keeps_sentinel(self):
        write_snapshot({}, meta={})
        md, _ = run_digest({D.CHANNELS[CH[0]]: [entry("old", age_h=30 * 24)]})
        self.assertEqual(md, "NO_VIDEOS")   # workflow sentinel contract kept: no footer

    def test_all_unavailable_still_fails_run(self):
        os.remove(D.FEED_SNAPSHOT_PATH)
        with self.assertRaises(SystemExit):
            run_digest({})


class CoverageTests(InTempRepo):
    def test_coverage_reports_stale_source(self):
        write_snapshot({c: [entry(f"{i}", age_h=30)] for i, c in enumerate(CH)},
                       meta={c: {"last_ok": iso(NOW - (100 if c == CH[2] else 1) * H),
                                 "consecutive_errors": 0, "last_error": None} for c in CH})
        out = io.StringIO()
        with mock.patch.object(C, "fetch_feed", side_effect=RuntimeError("blocked")), \
             mock.patch.object(sys, "argv", ["x", "--markdown"]), redirect_stdout(out):
            C.main()
        text = out.getvalue()
        self.assertIn(f"| {CH[2]} | 1 | 0 (0T/0D) | 1 | 0 | snapshot (feed stale 4d) |", text)
        self.assertIn(f"| {CH[0]} | 1 | 0 (0T/0D) | 1 | 0 | snapshot |", text)

    def test_classify_last_upload_over_all_entries(self):
        vids = [dict(entry("x", age_h=500), published=NOW - 500 * H),
                dict(entry("y", age_h=1), published=NOW - 1 * H)]
        c = C.classify_channel(vids, NOW - 7 * 24 * H, NOW, {}, set(), set())
        self.assertEqual(c["published"], 1)
        self.assertEqual(c["last_upload"], NOW - 1 * H)
        self.assertEqual(c["queued"], 1)


class AudioStripTests(unittest.TestCase):
    def test_roster_not_narrated(self):
        import make_audio as A
        md = ("### [T](https://youtu.be/x)\n**Ch** · Sep 17\n\nBody.\n\n"
              f"{D.ROSTER_START}\n| Channel | x |\n|---|---|\n| Kaspa Silver | 1 |\n{D.ROSTER_END}\n")
        speech = A.md_to_speech(md, "youtube")
        self.assertIn("Body.", speech)
        self.assertNotIn("Kaspa", speech)
        self.assertNotIn("|", speech)


if __name__ == "__main__":
    unittest.main()
