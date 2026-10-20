"""X posts folded into the daily YouTube digest.

since_id is the window (not the calendar day). A missing token, HTTP 401/429,
or a timeout is a one-line "X posts unavailable" note. No videos and no X
posts writes NOTHING, which replaced the NO_VIDEOS sentinel. An X-only day
still produces a digest.

    python -m unittest tests.test_x_posts -v
"""
import datetime
import io
import json
import os
import socket
import sys
import tempfile
import unittest
import urllib.error
from email.message import Message
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import make_audio as A          # noqa: E402
import x_posts as X              # noqa: E402
import youtube_coverage as C     # noqa: E402
import youtube_digest as D       # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
TOKEN = "super-secret-token-xyz"
ACCOUNT = {
    "handle": "pdicarlotrader",
    "name": "Peter DiCarlo",
    "label": "Smart Money Trader",
    "user_id": "42",
}


class Resp:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def http_error(code, body=b""):
    return urllib.error.HTTPError(
        "https://api.x.com/2/users/42/tweets", code, "nope", Message(),
        io.BytesIO(body),
    )


def posts_payload(*ids):
    data = []
    for pid in ids:
        data.append({
            "id": str(pid),
            "text": f"$TSLA support 250 target 280 ({pid})",
            "created_at": "2026-10-19T15:00:00.000Z",
        })
    newest = str(max(ids))
    return {
        "data": data,
        "meta": {"newest_id": newest, "oldest_id": str(min(ids)),
                 "result_count": len(data)},
    }


class InTemp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        os.makedirs("summaries/youtube")

    def tearDown(self):
        os.chdir(self.cwd)
        self.tmp.cleanup()


class SinceIdWindow(unittest.TestCase):
    def test_since_id_is_sent_and_advanced_to_newest(self):
        calls = []

        def opener(req, timeout=None):
            calls.append(req.full_url)
            self.assertTrue(req.get_header("Authorization").endswith(TOKEN))
            self.assertNotIn(TOKEN, req.full_url)
            return Resp(posts_payload(140, 150))

        state = {"x": {"pdicarlotrader": {"user_id": "42", "since_id": "100"}}}
        with mock.patch.object(X, "X_ACCOUNTS", (ACCOUNT,)), \
             mock.patch.dict(os.environ, {"X_BEARER_TOKEN": TOKEN,
                                          "DIGEST_DAY": "1999-01-01"}):
            out = io.StringIO()
            with mock.patch("sys.stdout", out):
                result = X.collect_x_posts(state, lambda i, c: "Levels: 250 and 280.",
                                           opener)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(calls), 1)                 # one timeline call, no user lookup
        self.assertIn("/users/42/tweets", calls[0])
        self.assertIn("since_id=100", calls[0])
        self.assertIn("max_results=20", calls[0])
        self.assertNotIn("start_time", calls[0])
        self.assertNotIn("1999-01-01", calls[0])
        self.assertNotIn("/users/by/username/", calls[0])
        self.assertEqual(state["x"]["pdicarlotrader"]["since_id"], "150")
        self.assertIn("https://x.com/pdicarlotrader/status/150", result["section"])
        self.assertIn("https://x.com/pdicarlotrader/status/140", result["section"])
        self.assertIn("250", result["section"])
        self.assertIn("Levels: 250 and 280.", result["section"])
        self.assertNotIn(TOKEN, result["section"])
        self.assertNotIn(TOKEN, out.getvalue())

    def test_second_run_requests_only_posts_after_the_saved_id(self):
        pages = [posts_payload(5, 9), posts_payload(12)]
        calls = []

        def opener(req, timeout=None):
            calls.append(req.full_url)
            return Resp(pages[len(calls) - 1])

        state = {"x": {"pdicarlotrader": {"since_id": "3"}}}
        with mock.patch.object(X, "X_ACCOUNTS", (ACCOUNT,)), \
             mock.patch.dict(os.environ, {"X_BEARER_TOKEN": TOKEN}):
            X.collect_x_posts(state, lambda i, c: "ok", opener)
            self.assertEqual(state["x"]["pdicarlotrader"]["since_id"], "9")
            X.collect_x_posts(state, lambda i, c: "ok", opener)
        self.assertIn("since_id=3", calls[0])
        self.assertIn("since_id=9", calls[1])
        self.assertNotIn("since_id=3", calls[1])

    def test_empty_page_does_not_move_since_id(self):
        def opener(req, timeout=None):
            return Resp({"meta": {"result_count": 0}})

        state = {"x": {"pdicarlotrader": {"since_id": "100"}}}
        with mock.patch.object(X, "X_ACCOUNTS", (ACCOUNT,)), \
             mock.patch.dict(os.environ, {"X_BEARER_TOKEN": TOKEN}):
            result = X.collect_x_posts(state, lambda i, c: "nope", opener)
        self.assertEqual(result["status"], "empty")
        self.assertEqual(result["section"], "")
        self.assertEqual(state["x"]["pdicarlotrader"]["since_id"], "100")

    def test_cap_still_advances_to_newest_so_the_page_is_not_reread(self):
        payload = posts_payload(10, 11)
        payload["meta"]["next_token"] = "more"

        def opener(req, timeout=None):
            return Resp(payload)

        state = {"x": {"pdicarlotrader": {"since_id": "1"}}}
        with mock.patch.object(X, "X_ACCOUNTS", (ACCOUNT,)), \
             mock.patch.dict(os.environ, {"X_BEARER_TOKEN": TOKEN}):
            result = X.collect_x_posts(state, lambda i, c: "ok", opener)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(state["x"]["pdicarlotrader"]["since_id"], "11")

    def test_user_id_is_resolved_once_then_cached(self):
        calls = []

        def opener(req, timeout=None):
            calls.append(req.full_url)
            if "/users/by/username/" in req.full_url:
                return Resp({"data": {"id": "42", "username": "pdicarlotrader"}})
            return Resp({"meta": {"result_count": 0}})

        blank = dict(ACCOUNT, user_id="")
        state = {}
        with mock.patch.object(X, "X_ACCOUNTS", (blank,)), \
             mock.patch.dict(os.environ, {"X_BEARER_TOKEN": TOKEN}):
            X.collect_x_posts(state, lambda i, c: "ok", opener)
            self.assertEqual(state["x"]["pdicarlotrader"]["user_id"], "42")
            n = len(calls)
            X.collect_x_posts(state, lambda i, c: "ok", opener)
        self.assertEqual(n, 2)                          # lookup + timeline, first run only
        self.assertIn("/users/by/username/pdicarlotrader", calls[0])
        self.assertTrue(all("/users/by/username/" not in u for u in calls[n:]))
        self.assertEqual(len(calls) - n, 1)             # second run is the timeline only

    def test_note_tweet_text_is_what_gets_summarized(self):
        seen = {}

        def opener(req, timeout=None):
            return Resp({"data": [{
                "id": "7",
                "text": "short",
                "note_tweet": {"text": "$HIMS hold 27 target 35"},
                "created_at": "2026-10-19T15:00:00.000Z",
            }], "meta": {"newest_id": "7", "result_count": 1}})

        def ask(instructions, content):
            seen["content"] = content
            return "HIMS 27 to 35."

        state = {}
        with mock.patch.object(X, "X_ACCOUNTS", (ACCOUNT,)), \
             mock.patch.dict(os.environ, {"X_BEARER_TOKEN": TOKEN}):
            result = X.collect_x_posts(state, ask, opener)
        self.assertIn("$HIMS hold 27 target 35", seen["content"])
        self.assertNotIn("short", seen["content"])
        self.assertIn("HIMS 27 to 35.", result["section"])


class Unavailable(unittest.TestCase):
    def _run(self, opener, token=TOKEN):
        state = {"x": {"pdicarlotrader": {"since_id": "100"}}}
        out = io.StringIO()
        env = {"X_BEARER_TOKEN": token}
        with mock.patch.object(X, "X_ACCOUNTS", (ACCOUNT,)), \
             mock.patch.dict(os.environ, env), \
             mock.patch("sys.stdout", out):
            result = X.collect_x_posts(state, lambda i, c: "should not run", opener)
        return result, state, out.getvalue()

    def test_missing_token_does_not_call_the_api(self):
        def opener(req, timeout=None):
            raise AssertionError("API was called without a token")

        # patch.dict cannot delete; set it empty and also pop via a wrapper
        state = {"x": {"pdicarlotrader": {"since_id": "100"}}}
        out = io.StringIO()
        with mock.patch.object(X, "X_ACCOUNTS", (ACCOUNT,)), \
             mock.patch.dict(os.environ, {"X_BEARER_TOKEN": ""}, clear=False), \
             mock.patch("sys.stdout", out):
            result = X.collect_x_posts(state, lambda i, c: "no", opener)
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("\nX posts unavailable\n", "\n" + result["section"])
        self.assertEqual(state["x"]["pdicarlotrader"]["since_id"], "100")
        self.assertIn("X posts unavailable", out.getvalue())
        self.assertNotIn(TOKEN, out.getvalue())

    def test_http_401_and_429_keep_since_id_and_hide_the_token(self):
        for code in (401, 429):
            def opener(req, timeout=None, code=code):
                raise http_error(code, body=TOKEN.encode())

            result, state, log = self._run(opener)
            self.assertEqual(result["status"], "unavailable", code)
            self.assertIn(X.UNAVAILABLE_NOTE, result["section"])
            self.assertNotIn(TOKEN, result["section"])
            self.assertNotIn(TOKEN, log)
            self.assertIn(f"HTTP {code}", log)
            self.assertEqual(state["x"]["pdicarlotrader"]["since_id"], "100")

    def test_timeout_is_unavailable(self):
        def opener(req, timeout=None):
            raise urllib.error.URLError(socket.timeout("timed out"))

        result, state, log = self._run(opener)
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("timeout", log)
        self.assertEqual(state["x"]["pdicarlotrader"]["since_id"], "100")
        self.assertNotIn(TOKEN, log)

    def test_summary_failure_still_lists_posts_and_advances(self):
        def opener(req, timeout=None):
            return Resp(posts_payload(150))

        def ask(instructions, content):
            raise RuntimeError("claude down " + TOKEN)

        state = {"x": {"pdicarlotrader": {"since_id": "100"}}}
        out = io.StringIO()
        with mock.patch.object(X, "X_ACCOUNTS", (ACCOUNT,)), \
             mock.patch.dict(os.environ, {"X_BEARER_TOKEN": TOKEN}), \
             mock.patch("sys.stdout", out):
            result = X.collect_x_posts(state, ask, opener)
        self.assertEqual(result["status"], "ok")
        self.assertIn("/status/150", result["section"])
        self.assertIn("$TSLA support 250", result["section"])
        self.assertEqual(state["x"]["pdicarlotrader"]["since_id"], "150")
        self.assertNotIn(TOKEN, out.getvalue())
        self.assertIn("summary failed", out.getvalue())


class DigestSentinel(InTemp):
    def _video(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        return {"id": "vid1", "title": "Roadster levels",
                "published": now - datetime.timedelta(hours=2),
                "description": "desc"}

    def test_nothing_when_no_videos_and_no_posts(self):
        def opener(req, timeout=None):
            return Resp({"meta": {"result_count": 0}})

        with mock.patch.object(X, "X_ACCOUNTS", (ACCOUNT,)), \
             mock.patch.dict(os.environ, {"X_BEARER_TOKEN": TOKEN, "DIGEST_DRY_RUN": ""}), \
             mock.patch.object(D, "preflight_auth"), \
             mock.patch.object(D, "fetch_feed", return_value=[]), \
             mock.patch.object(D, "ask_claude", return_value="A summary."), \
             mock.patch("x_posts.urllib.request.urlopen", side_effect=opener):
            D.main()
        self.assertEqual(open("digest.md").read(), "NOTHING")
        state = json.load(open(D.STATE_PATH))
        self.assertIsNone(state["x"]["pdicarlotrader"].get("since_id"))

    def test_x_only_day_is_a_digest_not_the_old_sentinel(self):
        def opener(req, timeout=None):
            return Resp(posts_payload(150))

        with mock.patch.object(X, "X_ACCOUNTS", (ACCOUNT,)), \
             mock.patch.dict(os.environ, {"X_BEARER_TOKEN": TOKEN, "DIGEST_DRY_RUN": ""}), \
             mock.patch.object(D, "preflight_auth"), \
             mock.patch.object(D, "fetch_feed", return_value=[]), \
             mock.patch.object(D, "fetch_transcript", return_value="words " * 40), \
             mock.patch.object(D, "ask_claude", return_value="TSLA support 250, target 280."), \
             mock.patch("x_posts.urllib.request.urlopen", side_effect=opener):
            D.main()
        md = open("digest.md").read()
        self.assertNotEqual(md, "NOTHING")
        self.assertNotEqual(md, "NO_VIDEOS")
        self.assertNotIn("NO_VIDEOS", md)
        self.assertIn("<!-- x-posts -->", md)
        self.assertIn("https://x.com/pdicarlotrader/status/150", md)
        self.assertIn("TSLA support 250, target 280.", md)
        self.assertIn("$TSLA support 250", md)
        state = json.load(open(D.STATE_PATH))
        self.assertEqual(state["x"]["pdicarlotrader"]["since_id"], "150")
        # videos were not invented for the coverage counter
        self.assertNotIn("youtube.com/watch", md)

    def test_videos_ship_when_x_is_unavailable(self):
        def opener(req, timeout=None):
            raise http_error(401, body=TOKEN.encode())

        with mock.patch.object(X, "X_ACCOUNTS", (ACCOUNT,)), \
             mock.patch.dict(os.environ, {"X_BEARER_TOKEN": TOKEN, "DIGEST_DRY_RUN": ""}), \
             mock.patch.object(D, "preflight_auth"), \
             mock.patch.object(D, "fetch_feed", return_value=[self._video()]), \
             mock.patch.object(D, "fetch_transcript", return_value="words " * 40), \
             mock.patch.object(D, "ask_claude", return_value="A summary."), \
             mock.patch("x_posts.urllib.request.urlopen", side_effect=opener):
            D.main()
        md = open("digest.md").read()
        self.assertIn("Roadster levels", md)
        self.assertIn("A summary.", md)
        self.assertIn(X.UNAVAILABLE_NOTE, md)
        self.assertNotIn(TOKEN, md)
        self.assertNotEqual(md.strip(), "NOTHING")
        state = json.load(open(D.STATE_PATH))
        self.assertIsNone(state.get("x", {}).get("pdicarlotrader", {}).get("since_id"))

    def test_overview_sees_the_x_section_when_videos_run(self):
        seen = []

        def ask(instructions, content):
            seen.append((instructions, content))
            if "TL;DR" in instructions:
                return "DiCarlo's 250 level is the new number."
            return "A summary."

        def opener(req, timeout=None):
            return Resp(posts_payload(150))

        now = datetime.datetime.now(datetime.timezone.utc)
        videos = [
            {"id": "v1", "title": "One", "published": now - datetime.timedelta(hours=3),
             "description": "d"},
            {"id": "v2", "title": "Two", "published": now - datetime.timedelta(hours=2),
             "description": "d"},
        ]
        with mock.patch.object(X, "X_ACCOUNTS", (ACCOUNT,)), \
             mock.patch.dict(os.environ, {"X_BEARER_TOKEN": TOKEN, "DIGEST_DRY_RUN": ""}), \
             mock.patch.object(D, "preflight_auth"), \
             mock.patch.object(D, "fetch_feed", return_value=videos), \
             mock.patch.object(D, "fetch_transcript", return_value="words " * 40), \
             mock.patch.object(D, "ask_claude", side_effect=ask), \
             mock.patch("x_posts.urllib.request.urlopen", side_effect=opener):
            D.main()
        md = open("digest.md").read()
        overview = [c for i, c in seen if "TL;DR" in i]
        self.assertEqual(len(overview), 1)
        self.assertIn("status/150", overview[0])
        self.assertIn("**Today's read:**", md)
        self.assertIn("DiCarlo's 250 level", md)
        self.assertIn("<!-- x-posts -->", md.split(D.ROSTER_START)[0])

    def test_dry_run_soft_fails_without_calling_x_or_claude(self):
        def boom(*args, **kwargs):
            raise AssertionError("should not be called")

        with mock.patch.dict(os.environ, {"DIGEST_DRY_RUN": "true", "X_BEARER_TOKEN": ""}), \
             mock.patch.object(D, "preflight_auth", side_effect=boom), \
             mock.patch.object(D, "fetch_feed", side_effect=boom), \
             mock.patch.object(D, "ask_claude", side_effect=boom), \
             mock.patch("x_posts.urllib.request.urlopen", side_effect=boom):
            D.main()
        md = open("digest.md").read()
        self.assertIn(X.UNAVAILABLE_NOTE, md)
        self.assertNotIn(TOKEN, md)
        self.assertFalse(os.path.exists(D.STATE_PATH))


class AudioAndCoverage(InTemp):
    def test_narration_keeps_x_and_strips_urls_and_handles(self):
        md = (
            "### [Video](https://www.youtube.com/watch?v=abc)\n"
            "**Brighter with Herbert** · Oct 19\n\n"
            "Tesla target 400.\n\n"
            "<!-- x-posts -->\n"
            "## X posts\n\n"
            "**Peter DiCarlo** · Smart Money Trader · "
            "[@pdicarlotrader](https://x.com/pdicarlotrader)\n\n"
            "Watch $TSLA support at 250, target 280. "
            "https://x.com/pdicarlotrader/status/150\n\n"
            "- [Oct 19](https://x.com/pdicarlotrader/status/150) — "
            "$TSLA 250 to 280 @pdicarlotrader\n"
            "<!-- /x-posts -->\n"
            f"{D.ROSTER_START}\n| Channel | x |\n|---|---|\n| Kaspa Silver | 1 |\n"
            f"{D.ROSTER_END}\n"
        )
        speech = A.md_to_speech(md, "youtube")
        self.assertIn("Tesla target 400", speech)
        self.assertIn("X posts", speech)
        self.assertIn("Peter DiCarlo", speech)
        self.assertIn("250", speech)
        self.assertIn("TSLA", speech)
        self.assertNotIn("http", speech)
        self.assertNotIn("@pdicarlotrader", speech)
        self.assertNotIn("x-posts", speech)
        self.assertNotIn("Kaspa", speech)
        self.assertNotIn("|", speech)

    def test_coverage_does_not_count_x_posts(self):
        with open("summaries/youtube/2026-10-20.md", "w") as fh:
            fh.write(
                "### [Vid](https://www.youtube.com/watch?v=abc123)\n\ntranscript\n\n"
                "<!-- x-posts -->\n"
                "- [Oct 19](https://x.com/pdicarlotrader/status/999) — $TSLA 250\n"
                "<!-- /x-posts -->\n"
            )
        ids = C.summarized_ids()
        self.assertIn("abc123", ids)
        self.assertNotIn("999", ids)


class WorkflowContract(unittest.TestCase):
    def test_workflow_keeps_timeout_concurrency_and_soft_x(self):
        path = os.path.join(ROOT, ".github", "workflows", "youtube-digest.yml")
        text = open(path).read()
        self.assertIn("timeout-minutes: 30", text)
        self.assertIn("group: youtube-digest", text)
        self.assertIn("cancel-in-progress: false", text)
        self.assertIn("X_BEARER_TOKEN: ${{ secrets.X_BEARER_TOKEN }}", text)
        self.assertIn("NOTHING", text)
        self.assertIn("NO_VIDEOS", text)          # still treated as empty
        self.assertLess(text.index("git push"), text.index("gh issue create"))
        self.assertNotRegex(text, r"echo\s+.*X_BEARER_TOKEN")
        self.assertIn("dry_run:", text)


if __name__ == "__main__":
    unittest.main()
