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
import re
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

    def test_two_pages_then_advance_to_the_newest(self):
        calls = []

        def opener(req, timeout=None):
            calls.append(req.full_url)
            if len(calls) == 1:
                payload = posts_payload(40, 41)
                payload["meta"]["next_token"] = "page-2"
                return Resp(payload)
            payload = posts_payload(20, 21)
            return Resp(payload)

        state = {"x": {"pdicarlotrader": {"since_id": "1"}}}
        seen = {}

        def ask(instructions, content):
            seen["content"] = content
            return "Both pages."

        with mock.patch.object(X, "X_ACCOUNTS", (ACCOUNT,)), \
             mock.patch.dict(os.environ, {"X_BEARER_TOKEN": TOKEN}):
            result = X.collect_x_posts(state, ask, opener)
        self.assertEqual(len(calls), 2)
        self.assertIn("pagination_token=page-2", calls[1])
        self.assertIn("(20)", seen["content"])
        self.assertIn("(41)", seen["content"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(state["x"]["pdicarlotrader"]["since_id"], "41")
        self.assertNotIn("until_id", state["x"]["pdicarlotrader"])

    def test_still_truncated_keeps_the_oldest_unread(self):
        calls = []

        def opener(req, timeout=None):
            calls.append(req.full_url)
            if "pagination_token" not in req.full_url and "until_id" not in req.full_url:
                payload = posts_payload(40, 50)
                payload["meta"]["next_token"] = "page-2"
                return Resp(payload)
            if "pagination_token" in req.full_url:
                payload = posts_payload(10, 11)
                payload["meta"]["next_token"] = "page-3"
                return Resp(payload)
            # drain: posts still above the original since_id and below until_id
            return Resp(posts_payload(5, 6))

        state = {"x": {"pdicarlotrader": {"since_id": "1"}}}
        with mock.patch.object(X, "X_ACCOUNTS", (ACCOUNT,)), \
             mock.patch.dict(os.environ, {"X_BEARER_TOKEN": TOKEN}):
            X.collect_x_posts(state, lambda i, c: "page", opener)
            rec = state["x"]["pdicarlotrader"]
            self.assertEqual(rec["since_id"], "1")
            self.assertEqual(rec["until_id"], "10")
            self.assertEqual(rec["high_water"], "50")
            X.collect_x_posts(state, lambda i, c: "rest", opener)
        self.assertIn("until_id=10", calls[-1])
        self.assertIn("since_id=1", calls[-1])
        self.assertEqual(state["x"]["pdicarlotrader"]["since_id"], "50")
        self.assertNotIn("until_id", state["x"]["pdicarlotrader"])

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
        # Name kept from the review: the cursor must NOT advance.
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
        self.assertEqual(state["x"]["pdicarlotrader"]["since_id"], "100")
        self.assertNotIn("until_id", state["x"]["pdicarlotrader"])
        self.assertNotIn(TOKEN, out.getvalue())
        self.assertIn("summary failed", out.getvalue())

    def test_empty_summary_does_not_advance(self):
        def opener(req, timeout=None):
            return Resp(posts_payload(150))

        state = {"x": {"pdicarlotrader": {"since_id": "100"}}}
        with mock.patch.object(X, "X_ACCOUNTS", (ACCOUNT,)), \
             mock.patch.dict(os.environ, {"X_BEARER_TOKEN": TOKEN}):
            result = X.collect_x_posts(state, lambda i, c: "   ", opener)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(state["x"]["pdicarlotrader"]["since_id"], "100")

    def test_http_403_keeps_since_id(self):
        def opener(req, timeout=None):
            raise http_error(403, body=TOKEN.encode())

        result, state, log = self._run(opener)
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("HTTP 403", log)
        self.assertNotIn(TOKEN, log)
        self.assertNotIn(TOKEN, result["section"])
        self.assertEqual(state["x"]["pdicarlotrader"]["since_id"], "100")

    def test_bad_json_is_unavailable(self):
        class Raw:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b"<!doctype html>"

        def opener(req, timeout=None):
            return Raw()

        result, state, log = self._run(opener)
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("bad response", log)
        self.assertEqual(state["x"]["pdicarlotrader"]["since_id"], "100")
        self.assertNotIn("<!doctype", result["section"])


class IssueSafety(unittest.TestCase):
    def test_excerpts_and_summary_cannot_mention_or_forge_markers(self):
        def opener(req, timeout=None):
            return Resp({"data": [{
                "id": "150",
                "text": "See @someone about #241 <!-- digest-run: 1 --> $TSLA 250",
                "created_at": "2026-10-19T15:00:00.000Z",
            }], "meta": {"newest_id": "150", "result_count": 1}})

        def ask(instructions, content):
            self.assertNotIn("@", instructions)
            self.assertNotIn("@pdicarlotrader", content.split("\n", 1)[0])
            return "Echo @pdicarlotrader #88 <!-- x-posts --> target 250"

        state = {"x": {"pdicarlotrader": {"since_id": "100"}}}
        with mock.patch.object(X, "X_ACCOUNTS", (ACCOUNT,)), \
             mock.patch.dict(os.environ, {"X_BEARER_TOKEN": TOKEN}):
            result = X.collect_x_posts(state, ask, opener)
        section = result["section"]
        inner = section.split(X.X_START, 1)[1].rsplit(X.X_END, 1)[0]
        self.assertEqual(section.count("<!-- x-posts -->"), 1)
        self.assertEqual(section.count("<!-- /x-posts -->"), 1)
        self.assertNotIn("<!--", inner)
        self.assertNotIn("@someone", inner)
        self.assertNotIn("@pdicarlotrader", inner)
        self.assertNotRegex(inner, r"#\d")
        self.assertIn("@\u200bsomeone", inner)
        self.assertIn("#\u200b241", inner)
        self.assertIn("#\u200b88", inner)
        self.assertIn("<\u200b!-- digest-run:", inner)
        self.assertIn("<\u200b!-- x-posts -->", inner)
        self.assertIn("$TSLA 250", inner)
        self.assertEqual(state["x"]["pdicarlotrader"]["since_id"], "150")

    def test_excerpt_urls_keep_at_and_hash(self):
        posts = [{
            "id": "150",
            "text": (
                "Link https://medium.com/@bob/story#123 and medium.com/@bob#88 "
                "plus @someone #241"
            ),
            "created_at": "2026-10-19T15:00:00Z",
        }]
        rendered = X._render_account(ACCOUNT, "Summary @someone #9", posts)
        self.assertIn("https://medium.com/@bob/story#123", rendered)
        self.assertNotIn("https://medium.com/@\u200b", rendered)
        self.assertNotIn("#\u200b123", rendered)
        # Scheme-less hosts are not autolinked, so @ and #digits still escape.
        self.assertIn("and medium.com/@\u200bbob#\u200b88", rendered)
        self.assertNotIn("and medium.com/@bob#88", rendered)
        self.assertIn("@\u200bsomeone", rendered)
        self.assertIn("#\u200b241", rendered)
        self.assertIn("#\u200b9", rendered)
        self.assertNotIn("@someone", rendered)
        self.assertNotIn("#241", rendered)
        self.assertNotIn("#9", rendered)

    def test_schemeless_hosts_escape_mentions_and_issue_refs(self):
        posts = [{
            "id": "150",
            "text": "see x.com/@user now and a.co/@torvalds and a.co/#5",
            "created_at": "2026-10-19T15:00:00Z",
        }]
        rendered = X._render_account(ACCOUNT, "x.com/@user and a.co/#5", posts)
        self.assertIn("x.com/@\u200buser", rendered)
        self.assertIn("a.co/@\u200btorvalds", rendered)
        self.assertIn("a.co/#\u200b5", rendered)
        self.assertNotIn("x.com/@user", rendered)
        self.assertNotIn("@torvalds", rendered)
        self.assertNotIn("a.co/#5", rendered)

    def test_post_label_is_jerusalem_not_utc(self):
        # 22:30 UTC on 2026-10-19 is 01:30 the next morning in Israel (IDT, UTC+3).
        def opener(req, timeout=None):
            return Resp({"data": [{
                "id": "8",
                "text": "$SPY 570",
                "created_at": "2026-10-19T22:30:00.000Z",
            }], "meta": {"newest_id": "8", "result_count": 1}})

        with mock.patch.object(X, "X_ACCOUNTS", (ACCOUNT,)), \
             mock.patch.dict(os.environ, {"X_BEARER_TOKEN": TOKEN}):
            result = X.collect_x_posts({}, lambda i, c: "spy", opener)
        self.assertIn("Oct 20", result["section"])
        self.assertNotIn("Oct 19", result["section"])

    def test_empty_text_advances_so_it_is_not_reread(self):
        def opener(req, timeout=None):
            return Resp({"data": [{
                "id": "150",
                "text": "   ",
                "created_at": "2026-10-19T15:00:00.000Z",
            }], "meta": {"newest_id": "150", "result_count": 1}})

        def ask(instructions, content):
            raise AssertionError("empty posts must not be summarized")

        state = {"x": {"pdicarlotrader": {"since_id": "100"}}}
        with mock.patch.object(X, "X_ACCOUNTS", (ACCOUNT,)), \
             mock.patch.dict(os.environ, {"X_BEARER_TOKEN": TOKEN}):
            result = X.collect_x_posts(state, ask, opener)
        self.assertEqual(result["status"], "empty")
        self.assertEqual(result["section"], "")
        self.assertEqual(state["x"]["pdicarlotrader"]["since_id"], "150")

    def test_claude_cli_does_not_receive_the_x_token(self):
        import halftime_pipeline as H
        captured = {}

        def fake_run(cmd, input=None, capture_output=None, text=None,
                     timeout=None, env=None):
            captured["env"] = env
            return mock.Mock(returncode=0, stdout="ok\n", stderr="")

        with mock.patch.dict(os.environ, {
            "X_BEARER_TOKEN": TOKEN,
            "CLAUDE_CODE_OAUTH_TOKEN": "oauth-token",
        }, clear=True), mock.patch.object(H.subprocess, "run", side_effect=fake_run):
            self.assertEqual(H.ask_claude("Summarize", "body"), "ok")
        self.assertIsNotNone(captured["env"])
        self.assertNotIn("X_BEARER_TOKEN", captured["env"])
        self.assertEqual(captured["env"]["CLAUDE_CODE_OAUTH_TOKEN"], "oauth-token")


# The two promo texts that repeated in digest issue #245 (2026-10-22).
PROMO_A = ("\U0001F6A8 Free Smart Money Zones Training New 7-lesson course where I "
           "show the exact rules I use to find institutional zones")
PROMO_B = ("\U0001F6A8 Want to find these discount zones yourself? My FREE "
           "7-lesson Smart Money Zones course walks you through it")


def item(pid, text):
    return {"id": str(pid), "text": text,
            "created_at": "2026-10-22T15:00:00.000Z"}


class Dedupe(unittest.TestCase):
    def _run(self, pages, state=None):
        seen = {}
        calls = []

        def opener(req, timeout=None):
            calls.append(req.full_url)
            return Resp(pages[len(calls) - 1])

        def ask(instructions, content):
            seen["content"] = content
            return "Promo plus one call."

        state = state if state is not None else {
            "x": {"pdicarlotrader": {"since_id": "1"}}}
        with mock.patch.object(X, "X_ACCOUNTS", (ACCOUNT,)), \
             mock.patch.dict(os.environ, {"X_BEARER_TOKEN": TOKEN}):
            result = X.collect_x_posts(state, ask, opener)
        return result, state, seen

    def test_issue_245_promo_repeats_are_listed_once(self):
        data = [
            item(2112343208646644104, PROMO_A + " https://t.co/aaa111"),
            item(2112343545470181792, PROMO_A + " https://t.co/bbb222"),
            item(2112540526029471901, PROMO_B + " https://t.co/ccc333"),
            item(2112628098277761483,
                 PROMO_B.replace("7-lesson", "7‑lesson").upper()
                 + "\n\n https://t.co/ddd444"),
            item(2112638895691088103,
                 PROMO_A.replace(" ", "  ").replace("7-lesson", "7‑lesson")
                 + " https://t.co/eee555"),
            item(2112640000000000000, "$NVDA reclaiming 180, target 195"),
        ]
        payload = {"data": list(reversed(data)),   # X returns newest first
                   "meta": {"newest_id": "2112640000000000000",
                            "result_count": len(data)}}
        result, state, seen = self._run([payload])
        section = result["section"]
        self.assertEqual(section.count("Free Smart Money Zones Training"), 1)
        self.assertEqual(section.count("discount zones yourself"), 1)
        self.assertEqual(section.count("/status/"), 3)
        # The earliest id of each text is kept.
        self.assertIn("/status/2112343208646644104", section)
        self.assertIn("/status/2112540526029471901", section)
        self.assertIn("/status/2112640000000000000", section)
        for dup in ("2112343545470181792", "2112628098277761483",
                    "2112638895691088103"):
            self.assertNotIn(dup, section)
        # Claude sees each promo once.
        self.assertEqual(seen["content"].count("Free Smart Money Zones Training"), 1)
        self.assertEqual(seen["content"].count("discount zones yourself"), 1)
        self.assertIn("$NVDA", seen["content"])
        # The cursor still advances past the skipped duplicates.
        self.assertEqual(state["x"]["pdicarlotrader"]["since_id"],
                         "2112640000000000000")

    def test_cursor_steps_past_a_skipped_newest_duplicate(self):
        payload = {"data": [item(30, PROMO_A + " https://t.co/new"),
                            item(10, PROMO_A + " https://t.co/old")],
                   "meta": {"result_count": 2}}       # no newest_id in meta
        result, state, _ = self._run([payload])
        self.assertEqual(result["section"].count("/status/"), 1)
        self.assertIn("/status/10", result["section"])
        self.assertEqual(state["x"]["pdicarlotrader"]["since_id"], "30")

    def test_duplicates_across_two_pages_and_repeat_ids(self):
        first = {"data": [item(41, PROMO_B), item(40, "$AMD 150")],
                 "meta": {"newest_id": "41", "next_token": "p2",
                          "result_count": 2}}
        second = {"data": [item(40, "$AMD 150"), item(20, PROMO_B)],
                  "meta": {"result_count": 2}}
        result, state, seen = self._run([first, second])
        section = result["section"]
        self.assertEqual(section.count("/status/"), 2)
        self.assertIn("/status/20", section)
        self.assertIn("/status/40", section)
        self.assertNotIn("/status/41", section)
        self.assertEqual(seen["content"].count("$AMD 150"), 1)
        self.assertEqual(state["x"]["pdicarlotrader"]["since_id"], "41")

    def test_truncated_window_keeps_until_id_at_oldest_read(self):
        first = {"data": [item(50, PROMO_A), item(40, PROMO_A + " t.co/x")],
                 "meta": {"newest_id": "50", "next_token": "p2"}}
        second = {"data": [item(11, PROMO_A), item(10, "$SPY 570")],
                  "meta": {"next_token": "p3"}}
        _, state, _ = self._run([first, second])
        rec = state["x"]["pdicarlotrader"]
        self.assertEqual(rec["since_id"], "1")
        self.assertEqual(rec["until_id"], "10")
        self.assertEqual(rec["high_water"], "50")

    def test_text_key_normalization(self):
        k = X._text_key
        self.assertEqual(k("7-lesson  Course"), k("7‑LESSON course"))
        self.assertEqual(k("Join now https://t.co/abc"), k("join now"))
        self.assertEqual(k("a b​c"), k("a bc"))
        self.assertNotEqual(k("$TSLA 250"), k("$TSLA 260"))


class XLinks(unittest.TestCase):
    def test_clean_handle(self):
        self.assertEqual(X.clean_handle("@pdicarlotrader"), "pdicarlotrader")
        self.assertEqual(X.clean_handle(" Peter_D1 "), "Peter_D1")
        for bad in ("", None, "@", "@@", "has space", "x.com", "a/b",
                    "sixteen_chars_xx", "héllo"):
            self.assertEqual(X.clean_handle(bad), "", bad)

    def test_post_url(self):
        self.assertEqual(X.post_url("@pdicarlotrader", "123"),
                         "https://x.com/pdicarlotrader/status/123")
        self.assertEqual(X.post_url("", "123"), "https://x.com/i/status/123")
        self.assertEqual(X.post_url("@", "123"), "https://x.com/i/status/123")
        for bad_id in ("", None, "12a", "abc", " ", "-5"):
            self.assertEqual(X.post_url("pdicarlotrader", bad_id), "")

    def test_profile_url(self):
        self.assertEqual(X.profile_url("@x"), "https://x.com/x")
        self.assertEqual(X.profile_url(""), "")
        self.assertEqual(X.profile_url("bad handle"), "")

    def _assert_no_broken_links(self, rendered):
        self.assertNotIn("https://@", rendered)
        self.assertNotIn("x.com//", rendered)
        self.assertNotIn("x.com/@", rendered)
        self.assertNotIn("]()", rendered)
        for url in re.findall(r"\]\((https?://[^)]*)\)", rendered):
            self.assertRegex(
                url, r"^https://x\.com/[A-Za-z0-9_]{1,15}(/status/\d+)?$")

    def test_render_with_malformed_handles_and_ids(self):
        posts = [{"id": "150", "text": "$TSLA 250",
                  "created_at": "2026-10-19T15:00:00Z"},
                 {"id": "not-a-number", "text": "$NVDA 180",
                  "created_at": "2026-10-19T15:00:00Z"}]
        for handle in ("@x", "", "@", None, "bad handle", "x.com/y"):
            account = dict(ACCOUNT, handle=handle)
            rendered = X._render_account(account, "", posts)
            self._assert_no_broken_links(rendered)
            self.assertIn("$NVDA 180", rendered)        # label shown, no link
            self.assertIn("- Oct 19 — $NVDA 180", rendered)
        # "@x" is a valid handle once the @ is stripped.
        rendered = X._render_account(dict(ACCOUNT, handle="@x"), "", posts)
        self.assertIn("[x](https://x.com/x)", rendered)
        self.assertIn("(https://x.com/x/status/150)", rendered)
        # Invalid handle: no header link, post link falls back to /i/status.
        rendered = X._render_account(dict(ACCOUNT, handle=""), "", posts)
        self.assertIn("(https://x.com/i/status/150)", rendered)
        self.assertNotIn("](https://x.com/)", rendered)

    def test_bad_handle_without_user_id_is_unavailable_not_a_bad_lookup(self):
        calls = []

        def opener(req, timeout=None):
            calls.append(req.full_url)
            return Resp({"meta": {"result_count": 0}})

        with mock.patch.object(X, "X_ACCOUNTS",
                               (dict(ACCOUNT, handle="@", user_id=""),)), \
             mock.patch.dict(os.environ, {"X_BEARER_TOKEN": TOKEN}), \
             mock.patch("sys.stdout", io.StringIO()):
            result = X.collect_x_posts({}, lambda i, c: "ok", opener)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(calls, [])


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
            "DiCarlo (@pdicarlotrader) "
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
        self.assertIn("DiCarlo", speech)
        self.assertNotIn("()", speech)
        self.assertIn("250", speech)
        self.assertIn("TSLA", speech)
        self.assertNotIn("http", speech)
        self.assertNotIn("@pdicarlotrader", speech)
        self.assertNotIn("x-posts", speech)
        self.assertNotIn("Kaspa", speech)
        self.assertNotIn("|", speech)

    def test_rendered_section_narration_drops_handles_urls_and_comments(self):
        summary = (
            "Watch @pdicarlotrader and @elonmusk. $TSLA support 250. "
            "https://example.com/secret-level <!-- roster -->"
        )
        posts = [{
            "id": "150",
            "text": "Echo @elonmusk https://x.com/elonmusk/status/9 <!-- roster --> $TSLA 250",
            "created_at": "2026-10-19T22:30:00Z",
        }]
        section = X._render_ok([X._render_account(ACCOUNT, summary, posts)])
        self.assertIn("@\u200bpdicarlotrader", section)
        self.assertIn("@\u200belonmusk", section)
        self.assertIn("<\u200b!-- roster -->", section)
        speech = A.md_to_speech(section, "youtube")
        self.assertIn("Peter DiCarlo", speech)
        self.assertIn("TSLA", speech)
        self.assertIn("250", speech)
        self.assertNotIn("@", speech)
        self.assertNotIn("http", speech)
        self.assertNotIn("<!--", speech)
        self.assertNotIn("\u200b", speech)
        self.assertNotIn("elonmusk", speech)
        self.assertNotIn("roster", speech)
        self.assertNotIn("secret-level", speech)
        self.assertNotIn("x-posts", speech)

    def test_forged_roster_comment_does_not_cut_x_narration(self):
        posts = [{
            "id": "150",
            "text": "Before <!-- roster --> after the marker, target 280 holds.",
            "created_at": "2026-10-19T22:30:00Z",
        }]
        section = X._render_ok([
            X._render_account(ACCOUNT, "Levels first. $TSLA 250.", posts),
        ])
        self.assertIn("<\u200b!-- roster -->", section)
        md = (
            section
            + f"\n{D.ROSTER_START}\n| Channel | x |\n|---|---|\n| Kaspa Silver | 1 |\n"
            + f"{D.ROSTER_END}\n"
        )
        speech = A.md_to_speech(md, "youtube")
        self.assertIn("Before", speech)
        self.assertIn("target 280 holds", speech)
        self.assertIn("TSLA", speech)
        self.assertIn("250", speech)
        self.assertNotIn("Kaspa", speech)
        self.assertNotIn("roster", speech)
        self.assertNotIn("<!--", speech)
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
