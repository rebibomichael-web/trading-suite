"""Feed-snapshot MERGE semantics for scripts/fetch_youtube_transcripts.py.

Runs main() end-to-end against a temp repo with every network call mocked:
all-fail run, partial-fail run, recovery run, first-ever run (no prior
snapshot), plus the 404/5xx retry and the >=3-consecutive-errors WARN.

    .venv/bin/python -m unittest tests.test_fetch_youtube_snapshot -v
"""
import datetime
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import fetch_youtube_transcripts as F  # noqa: E402

NOW = datetime.datetime(2026, 9, 17, 1, 10, tzinfo=datetime.timezone.utc)
CH = list(F.CHANNELS)   # 8 names


def http_error(code):
    return urllib.error.HTTPError("u", code, "x", {}, None)


def entry(vid, when=NOW):
    return {"id": vid, "title": f"t-{vid}", "published": when, "description": ""}


class SnapshotRun:
    """Drive main() once with a scripted per-channel feed outcome."""

    def __init__(self, repo):
        self.repo = repo
        self.path = os.path.join(repo, F.FEED_SNAPSHOT)

    def run(self, outcomes):
        """outcomes: {channel_id: list-of-entries | Exception}. Missing ids
        raise a generic error. Returns (snapshot dict, stdout, stderr)."""
        def fake_feed(cid):
            r = outcomes.get(cid, RuntimeError("unscripted"))
            if isinstance(r, Exception):
                raise r
            return r
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(F, "fetch_feed", side_effect=fake_feed), \
             mock.patch.object(F, "fetch_transcript", return_value="words"), \
             mock.patch.object(F, "update_prices", return_value=None), \
             mock.patch.object(F, "commit_and_push"), \
             mock.patch.object(F.time, "sleep"), \
             mock.patch.object(sys, "argv", ["x", "--repo", self.repo, "--no-git"]), \
             redirect_stdout(out), redirect_stderr(err):
            F.main()
        with open(self.path) as fh:
            snap = json.load(fh)
        return snap, out.getvalue(), err.getvalue()


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.r = SnapshotRun(self.tmp.name)
        self.all_ok = {cid: [entry(f"{cid[-4:]}-v1")] for cid in F.CHANNELS.values()}

    def tearDown(self):
        self.tmp.cleanup()

    # --- scenario 4: first-ever run, no prior snapshot -------------------
    def test_first_run_all_ok(self):
        snap, out, err = self.r.run(self.all_ok)
        self.assertEqual(set(snap["channels"]), set(CH))
        for c in CH:
            self.assertIsInstance(snap["channels"][c], list)     # back-compat shape
            self.assertEqual(snap["meta"][c]["consecutive_errors"], 0)
            self.assertIsNotNone(snap["meta"][c]["last_ok"])
            self.assertIsNone(snap["meta"][c]["last_error"])
        self.assertEqual(snap["fetched_at"], snap["written_at"])
        self.assertIn("ok 8/8, carried 0, keys 8", out)

    def test_first_run_all_fail(self):
        snap, out, err = self.r.run({})
        self.assertEqual(snap["channels"], {})          # nothing ever succeeded
        self.assertIsNone(snap["fetched_at"])           # digest gate: unusable
        self.assertIsNotNone(snap["written_at"])
        for c in CH:
            self.assertEqual(snap["meta"][c]["consecutive_errors"], 1)
            self.assertIsNone(snap["meta"][c]["last_ok"])
        self.assertIn("ok 0/8, carried 0, keys 0", out)

    # --- scenario 2: partial fail keeps last-good --------------------------
    def test_partial_fail_carries_forward(self):
        snap0, _, _ = self.r.run(self.all_ok)
        t0 = snap0["fetched_at"]
        ok_ids = {F.CHANNELS[CH[0]], F.CHANNELS[CH[3]]}
        outcomes = {cid: ([entry(f"{cid[-4:]}-v2")] if cid in ok_ids else http_error(404))
                    for cid in F.CHANNELS.values()}
        snap, out, err = self.r.run(outcomes)
        self.assertEqual(set(snap["channels"]), set(CH))            # no key dropped
        for c in CH:
            m = snap["meta"][c]
            if F.CHANNELS[c] in ok_ids:
                self.assertTrue(snap["channels"][c][0]["id"].endswith("-v2"))
                self.assertEqual(m["consecutive_errors"], 0)
                self.assertIsNone(m["last_error"])
                self.assertNotEqual(m["last_ok"], t0)
            else:
                self.assertTrue(snap["channels"][c][0]["id"].endswith("-v1"))  # last-good
                self.assertEqual(m["consecutive_errors"], 1)
                self.assertEqual(m["last_error"], "HTTP 404")
                self.assertEqual(m["last_ok"], t0)                              # stale, stamped
        self.assertIn("ok 2/8, carried 6, keys 8", out)
        self.assertNotIn("WARN", err)

    # --- scenario 1: all fail after a good snapshot -----------------------
    def test_all_fail_after_good(self):
        snap0, _, _ = self.r.run(self.all_ok)
        outcomes = {cid: http_error(500) for cid in F.CHANNELS.values()}
        snap, out, err = self.r.run(outcomes)
        self.assertEqual(snap["channels"], snap0["channels"])       # byte-for-byte last-good
        self.assertEqual(snap["fetched_at"], snap0["fetched_at"])   # NOT refreshed
        self.assertNotEqual(snap["written_at"], snap0["written_at"])
        for c in CH:
            self.assertEqual(snap["meta"][c]["consecutive_errors"], 1)
            self.assertEqual(snap["meta"][c]["last_error"], "HTTP 500")
        self.assertIn("ok 0/8, carried 8, keys 8", out)

    # --- scenario 3: recovery resets provenance ---------------------------
    def test_recovery_resets(self):
        self.r.run(self.all_ok)
        fail = {cid: http_error(404) for cid in F.CHANNELS.values()}
        for _ in range(3):
            snap, out, err = self.r.run(fail)
        self.assertEqual(snap["meta"][CH[0]]["consecutive_errors"], 3)
        self.assertIn("WARN", err)                                   # escalation fires at 3
        self.assertIn(CH[0], err)
        self.assertIn("3 consecutive feed errors", err)
        snap, out, err = self.r.run(self.all_ok)
        for c in CH:
            m = snap["meta"][c]
            self.assertEqual(m["consecutive_errors"], 0)
            self.assertIsNone(m["last_error"])
        self.assertNotIn("WARN", err)

    def test_warn_only_below_threshold(self):
        self.r.run(self.all_ok)
        fail = {cid: http_error(404) for cid in F.CHANNELS.values()}
        _, _, err1 = self.r.run(fail)
        _, _, err2 = self.r.run(fail)
        self.assertNotIn("WARN", err1)
        self.assertNotIn("WARN", err2)

    def test_non_http_error_is_named(self):
        outcomes = dict(self.all_ok)
        outcomes[F.CHANNELS[CH[1]]] = TimeoutError("slow")
        snap, _, _ = self.r.run(outcomes)
        self.assertEqual(snap["meta"][CH[1]]["last_error"], "TimeoutError")

    def test_corrupt_prior_is_empty_prior(self):
        os.makedirs(os.path.dirname(self.r.path))
        with open(self.r.path, "w") as fh:
            fh.write("{not json")
        snap, _, _ = self.r.run(self.all_ok)
        self.assertEqual(set(snap["channels"]), set(CH))

    def test_legacy_prior_without_meta(self):
        """A pre-merge snapshot (no 'meta') is a valid prior."""
        os.makedirs(os.path.dirname(self.r.path))
        with open(self.r.path, "w") as fh:
            json.dump({"fetched_at": "2026-09-16T01:10:00+00:00",
                   "channels": {CH[0]: [{"id": "old", "title": "t",
                                         "published": "2026-09-15T00:00:00+00:00"}]}},
                      fh)
        snap, _, _ = self.r.run({})
        self.assertEqual(snap["channels"][CH[0]][0]["id"], "old")
        self.assertEqual(snap["fetched_at"], "2026-09-16T01:10:00+00:00")
        self.assertEqual(snap["meta"][CH[0]]["consecutive_errors"], 1)
        self.assertIsNone(snap["meta"][CH[0]]["last_ok"])   # unknown, not faked


class RetryTests(unittest.TestCase):
    def _run(self, errors_then):
        calls = []
        seq = list(errors_then)
        def fake(cid):
            calls.append(cid)
            r = seq.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        sleeps = []
        with mock.patch.object(F, "fetch_feed", side_effect=fake):
            try:
                res = F.fetch_feed_with_retry("UCx", sleep=sleeps.append)
            except Exception as e:
                res = e
        return res, len(calls), sleeps

    def test_404_then_ok(self):
        res, n, sleeps = self._run([http_error(404), ["ok"]])
        self.assertEqual(res, ["ok"]); self.assertEqual(n, 2)
        self.assertEqual(sleeps, [F.FEED_RETRY_BACKOFF_S])

    def test_500_then_ok(self):
        res, n, _ = self._run([http_error(500), ["ok"]])
        self.assertEqual(res, ["ok"]); self.assertEqual(n, 2)

    def test_500_then_404_propagates_second(self):
        res, n, _ = self._run([http_error(500), http_error(404)])
        self.assertIsInstance(res, urllib.error.HTTPError)
        self.assertEqual(res.code, 404); self.assertEqual(n, 2)

    def test_403_not_retried(self):
        res, n, sleeps = self._run([http_error(403), ["never"]])
        self.assertEqual(res.code, 403); self.assertEqual(n, 1); self.assertEqual(sleeps, [])

    def test_urlerror_not_retried(self):
        res, n, _ = self._run([urllib.error.URLError("dns"), ["never"]])
        self.assertIsInstance(res, urllib.error.URLError); self.assertEqual(n, 1)


class ReaderCompatTests(unittest.TestCase):
    """The three live readers must parse a merged snapshot unchanged."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        r = SnapshotRun(self.tmp.name)
        ok = {cid: [entry(f"{cid[-4:]}-v1")] for cid in F.CHANNELS.values()}
        r.run(ok)
        fail = {cid: http_error(404) for cid in F.CHANNELS.values()}
        fail[F.CHANNELS[CH[0]]] = [entry("fresh")]
        r.run(fail)
        self.cwd = os.getcwd(); os.chdir(self.tmp.name)

    def tearDown(self):
        os.chdir(self.cwd); self.tmp.cleanup()

    def test_digest_and_coverage_loader(self):
        import youtube_digest as D
        snap, meta = D.load_feed_snapshot()
        self.assertEqual(set(snap), set(CH))
        self.assertEqual(meta[CH[0]]["consecutive_errors"], 0)
        self.assertEqual(meta[CH[1]]["consecutive_errors"], 1)
        self.assertEqual(meta[CH[1]]["last_error"], "HTTP 404")
        self.assertEqual(meta[CH[1]]["stale_note"], "")   # last_ok is seconds old
        self.assertEqual(snap[CH[0]][0]["id"], "fresh")
        self.assertTrue(snap[CH[1]][0]["id"].endswith("-v1"))
        self.assertIsInstance(snap[CH[1]][0]["published"], datetime.datetime)

    def test_guru_ledger_metadata(self):
        import guru_ledger as G
        meta = G.video_metadata()
        self.assertIn("fresh", meta)
        self.assertEqual(meta["fresh"][0], CH[0])
        self.assertEqual(len(meta), 8)


if __name__ == "__main__":
    unittest.main()
