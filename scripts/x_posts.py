#!/usr/bin/env python3
"""X posts for the daily YouTube digest. Self-contained: a later newscast can
import this module without the video pipeline.

One official user-timeline read per configured account per run, plus a single
extra page when that window is longer than max_results (no scrape, no Dell
snapshot):

    GET https://api.x.com/2/users/{id}/tweets?since_id=...&max_results=20
    Authorization: Bearer $X_BEARER_TOKEN

Pay-per-use, not a monthly tier (Basic and Pro were retired in 2026). Rates
published at developer.x.com, billed per resource returned, prepaid credits:

    Posts read   $0.005 each
    User read    $0.010 each

The numeric user id is cached in summaries/youtube/state.json under "x" after
one lookup, so later runs do not pay the user read. since_id in that same
object is the window — not the calendar day — and the workflow commits
state.json before it opens the issue.

A missing token, HTTP 401/429, timeout, or any other X failure returns the
one-line note "X posts unavailable" and never raises. The bearer token is
never logged.

Owner setup (once), in the X Developer Console at https://developer.x.com :

    1. Create a Project and an App. Generate an app-only Bearer Token.
    2. Buy prepaid credits (pay-per-use; there is no Basic/Pro subscription).
    3. Set a spending limit for the billing cycle. This job caps each account
       at 20 returned posts ($0.10) per run, one run a day.
    4. Add the token as the GitHub Actions secret X_BEARER_TOKEN.
       Confirm the current per-resource rates in the console before buying;
       they can change.
"""
import datetime
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

# Same zone as digest_day.TZ / DIGEST_DAY. Post labels use it; the digest's
# day key is still computed once in the workflow.
JERUSALEM = ZoneInfo("Asia/Jerusalem")
_ISSUE_REF = re.compile(r"#(\d+)")
# Scheme URLs and scheme-less host/path tokens. '@' and '#123' inside them
# must stay literal or the link breaks (medium.com/@bob, ...#123).
_URL = re.compile(
    r"https?://[^\s<>]+"
    r"|(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}(?::\d+)?(?:/[^\s<>]*)?",
    re.IGNORECASE,
)

X_API = "https://api.x.com/2"
# One call. The API allows 5–100. Replies and reposts are excluded from the
# summary but still count toward this cap, so 20 leaves room for a reply-heavy
# day. Only posts actually returned are billed ($0.005 each).
X_MAX_RESULTS = 20
X_TIMEOUT_S = 20

# Add an account by appending a dict. user_id may be left blank: the first
# successful run resolves it once ($0.010) and caches it in state.
X_ACCOUNTS = (
    {
        "handle": "pdicarlotrader",
        "name": "Peter DiCarlo",
        "label": "Smart Money Trader",
        "user_id": "",
    },
)

X_START, X_END = "<!-- x-posts -->", "<!-- /x-posts -->"
UNAVAILABLE_NOTE = "X posts unavailable"

X_SUMMARY_INSTRUCTIONS = (
    "Summarize these X posts for a busy investor. Write 2-5 plain sentences: "
    "the key points, the tickers, and any price levels or targets. Attribute "
    "the calls to the author. NEVER invent tickers, prices, or levels that "
    "are not in the posts. No headers, no bullet lists — flowing prose only."
)


class XUnavailable(Exception):
    """A soft failure. Callers turn this into UNAVAILABLE_NOTE."""


def unavailable_section():
    return f"{X_START}\n\n## X posts\n\n{UNAVAILABLE_NOTE}\n\n{X_END}\n"


def _account_record(state, handle):
    x = state.get("x")
    if not isinstance(x, dict):
        x = {}
        state["x"] = x
    rec = x.get(handle)
    if not isinstance(rec, dict):
        rec = {}
        x[handle] = rec
    return rec


def _scrub(text, token):
    if token and text and token in text:
        return text.replace(token, "[redacted]")
    return text


def _get_json(url, token, opener):
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "trading-suite-digest",
        },
    )
    try:
        with opener(req, timeout=X_TIMEOUT_S) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        # 401 = bad/missing token, 429 = rate or credit limit. Both are soft.
        raise XUnavailable(f"HTTP {e.code}") from None
    except (TimeoutError, socket.timeout):
        raise XUnavailable("timeout") from None
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        timed_out = isinstance(reason, (TimeoutError, socket.timeout)) or (
            "timed out" in str(reason).lower()
        )
        raise XUnavailable("timeout" if timed_out else "network") from None
    except Exception:
        raise XUnavailable("network") from None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, AttributeError):
        raise XUnavailable("bad response") from None
    if not isinstance(payload, dict):
        raise XUnavailable("bad response")
    return payload


def _post_text(item):
    note = item.get("note_tweet")
    if isinstance(note, dict) and note.get("text"):
        return str(note["text"])
    return str(item.get("text") or "")


def _parse_posts(payload):
    if payload.get("errors") and not payload.get("data"):
        raise XUnavailable("API error")
    posts = []
    for item in payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        pid = str(item.get("id") or "")
        if not pid.isdigit():
            continue
        text = _post_text(item).strip()
        if not text:
            continue
        posts.append({
            "id": pid,
            "text": text,
            "created_at": str(item.get("created_at") or ""),
        })
    posts.sort(key=lambda p: int(p["id"]))
    return posts


def _when(created):
    """Post timestamp in Asia/Jerusalem. Not the digest day — that stays
    DIGEST_DAY, computed once in the workflow."""
    try:
        dt = datetime.datetime.fromisoformat(created.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return "post"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(JERUSALEM).strftime("%b %d")


def _escape_issue_chunk(text):
    text = text.replace("<!--", "<\u200b!--")
    text = _ISSUE_REF.sub(lambda m: "#\u200b" + m.group(1), text)
    text = text.replace("@", "@\u200b")
    return text


def escape_for_issue(text):
    """Break GitHub mentions, issue autolinks, and HTML comments in text that
    came from a post or from Claude. Structural markers (<!-- x-posts -->)
    are added after this and must not be passed through it.

    URL spans are copied through unchanged. A zero-width space inside
    medium.com/@bob or a fragment such as #123 would break the link.
    """
    if not text:
        return text
    parts = []
    last = 0
    for match in _URL.finditer(text):
        parts.append(_escape_issue_chunk(text[last:match.start()]))
        parts.append(match.group(0))
        last = match.end()
    parts.append(_escape_issue_chunk(text[last:]))
    return "".join(parts)


def _excerpt(text, limit=180):
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def _prompt_body(account, posts):
    # No leading @handle: Claude echoes it, and that string would become a
    # GitHub mention once the summary is posted on the issue.
    chunks = []
    for p in posts:
        text = p["text"].strip()
        if len(text) > 1200:
            text = text[:1200].rstrip() + "…"
        chunks.append(f"{account['name']} · {_when(p['created_at'])}\n{text}")
    return "\n\n---\n\n".join(chunks)


def _render_account(account, summary, posts):
    handle = account["handle"]
    label = account.get("label") or ""
    head = f"**{account['name']}**"
    if label:
        head += f" · {label}"
    head += f" · [{handle}](https://x.com/{handle})"
    lines = [head, ""]
    if summary:
        lines += [escape_for_issue(summary.strip()), ""]
    for p in posts:
        url = f"https://x.com/{handle}/status/{p['id']}"
        lines.append(
            f"- [{_when(p['created_at'])}]({url}) — "
            f"{escape_for_issue(_excerpt(p['text']))}"
        )
    return "\n".join(lines)


def _render_ok(parts):
    body = "\n\n".join(parts)
    return f"{X_START}\n\n## X posts\n\n{body}\n\n{X_END}\n"


def _resolve_user_id(account, rec, token, opener):
    configured = str(account.get("user_id") or "").strip()
    if configured.isdigit():
        return configured
    cached = str(rec.get("user_id") or "").strip()
    if cached.isdigit():
        return cached
    handle = urllib.parse.quote(account["handle"])
    payload = _get_json(f"{X_API}/users/by/username/{handle}", token, opener)
    uid = str((payload.get("data") or {}).get("id") or "")
    if not uid.isdigit():
        raise XUnavailable("user lookup failed")
    rec["user_id"] = uid
    print(f"X @{account['handle']}: cached user id")
    return uid


def _timeline_url(user_id, since_id, until_id=None):
    params = {
        "max_results": str(max(5, min(100, int(X_MAX_RESULTS)))),
        "exclude": "retweets,replies",
        "tweet.fields": "created_at,note_tweet",
    }
    if since_id and str(since_id).isdigit():
        params["since_id"] = str(since_id)
    if until_id and str(until_id).isdigit():
        params["until_id"] = str(until_id)
    query = urllib.parse.urlencode(params)
    return f"{X_API}/users/{urllib.parse.quote(str(user_id))}/tweets?{query}"


def _page_meta(payload):
    meta = payload.get("meta")
    return meta if isinstance(meta, dict) else {}


def _raw_ids(payload):
    ids = []
    for item in payload.get("data") or []:
        if isinstance(item, dict) and str(item.get("id") or "").isdigit():
            ids.append(str(item["id"]))
    return ids


def _load_pages(user_id, since_id, until_id, token, opener):
    """The newest page, and one older page when X says the window continues.
    A second page is the most we will pay for in a single run."""
    url = _timeline_url(user_id, since_id, until_id)
    first = _get_json(url, token, opener)
    pages = [first]
    nxt = _page_meta(first).get("next_token")
    if nxt:
        more = url + "&" + urllib.parse.urlencode({"pagination_token": str(nxt)})
        pages.append(_get_json(more, token, opener))
    return pages


def _merge_pages(pages):
    posts = []
    seen = set()
    raw = []
    newest = []
    truncated = False
    for i, payload in enumerate(pages):
        meta = _page_meta(payload)
        if str(meta.get("newest_id") or "").isdigit():
            newest.append(str(meta["newest_id"]))
        raw.extend(_raw_ids(payload))
        for post in _parse_posts(payload):
            if post["id"] in seen:
                continue
            seen.add(post["id"])
            posts.append(post)
        if i == len(pages) - 1 and meta.get("next_token"):
            truncated = True
    posts.sort(key=lambda p: int(p["id"]))
    candidates = [n for n in newest + raw + [p["id"] for p in posts] if str(n).isdigit()]
    top = max(candidates, key=int) if candidates else ""
    return posts, top, truncated


def _advance_window(rec, handle, since_id, top, oldest, truncated):
    """Move the cursor past posts we have summarized.

    A window that still continues past the two pages we read must not jump
    to the newest id: that would skip the older unread posts. Keep since_id
    and set until_id to the oldest post we did read (X treats until_id as
    exclusive). high_water remembers the newest id already summarized so the
    run that finishes the backlog can step past it.
    """
    if truncated and since_id and oldest:
        prev = rec.get("high_water")
        best = int(top)
        if str(prev or "").isdigit():
            best = max(best, int(prev))
        rec["until_id"] = str(oldest)
        rec["high_water"] = str(best)
        print(f"X @{handle}: more posts remain below {oldest}; "
              f"since_id stays {since_id}")
        return
    best = int(top)
    if str(rec.get("high_water") or "").isdigit():
        best = max(best, int(rec["high_water"]))
    rec["since_id"] = str(best)
    rec.pop("until_id", None)
    rec.pop("high_water", None)


def _fetch_account(account, state, token, ask_claude, opener):
    """Timeline read for one account. Returns a markdown block, or '' when
    nothing new is worth showing.

    since_id advances only after a non-empty summary, or when every returned
    post had no text (those were billed and cannot be summarized). A failed
    or empty summary leaves the cursor where it was so the next run retries.
    """
    handle = account["handle"]
    rec = _account_record(state, handle)
    user_id = _resolve_user_id(account, rec, token, opener)
    since_id = str(rec.get("since_id") or "")
    until_id = str(rec.get("until_id") or "")
    pages = _load_pages(user_id, since_id, until_id, token, opener)
    posts, top, truncated = _merge_pages(pages)
    if not posts:
        # Empty bodies were still returned (and billed). Step past them.
        if top:
            rec["since_id"] = str(top)
            rec.pop("until_id", None)
            rec.pop("high_water", None)
            print(f"X @{handle}: posts had no text; advanced since_id to {top}")
        return ""
    summary = ""
    try:
        summary = (ask_claude(
            f"{X_SUMMARY_INSTRUCTIONS}\nAuthor: {account['name']} "
            f"({handle})",
            _prompt_body(account, posts),
        ) or "").strip()
    except Exception as e:
        print(f"X @{handle}: summary failed ({type(e).__name__}); "
              f"listing the posts without advancing since_id")
        summary = ""
    if not summary:
        return _render_account(account, "", posts)
    _advance_window(rec, handle, since_id, top, posts[0]["id"], truncated)
    return _render_account(account, summary, posts)


def collect_x_posts(state, ask_claude, opener=None):
    """Return {"status": "ok"|"empty"|"unavailable", "section": str}.

    Never raises. Does not print the bearer token.
    """
    opener = opener or urllib.request.urlopen
    if not isinstance(state, dict):
        state = {}
    token = os.environ.get("X_BEARER_TOKEN", "").strip()
    if not token:
        print("X posts unavailable (no X_BEARER_TOKEN)")
        return {"status": "unavailable", "section": unavailable_section()}
    blocks = []
    failed = False
    try:
        accounts = list(X_ACCOUNTS)
    except Exception as e:
        print(f"X posts unavailable ({type(e).__name__})")
        return {"status": "unavailable", "section": unavailable_section()}
    for account in accounts:
        handle = account.get("handle") or "?"
        try:
            block = _fetch_account(account, state, token, ask_claude, opener)
        except XUnavailable as e:
            reason = _scrub(str(e), token)
            print(f"X posts unavailable (@{handle}: {reason})")
            failed = True
            continue
        except Exception as e:
            print(f"X posts unavailable (@{handle}: {type(e).__name__})")
            failed = True
            continue
        if block:
            blocks.append(block)
    if blocks:
        return {"status": "ok", "section": _render_ok(blocks)}
    if failed:
        return {"status": "unavailable", "section": unavailable_section()}
    return {"status": "empty", "section": ""}
