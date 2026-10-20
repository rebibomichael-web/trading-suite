#!/usr/bin/env python3
"""X posts for the daily YouTube digest. Self-contained: a later newscast can
import this module without the video pipeline.

One official user-timeline call per configured account per run (no scrape, no
Dell snapshot, no pagination):

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
import socket
import urllib.error
import urllib.parse
import urllib.request

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
    """Post timestamp for the link label. Not the digest day — that stays
    DIGEST_DAY, computed once in the workflow."""
    try:
        dt = datetime.datetime.fromisoformat(created.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return "post"
    return dt.strftime("%b %d")


def _excerpt(text, limit=180):
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def _prompt_body(account, posts):
    chunks = []
    for p in posts:
        text = p["text"].strip()
        if len(text) > 1200:
            text = text[:1200].rstrip() + "…"
        chunks.append(f"@{account['handle']} · {_when(p['created_at'])}\n{text}")
    return "\n\n---\n\n".join(chunks)


def _render_account(account, summary, posts):
    handle = account["handle"]
    label = account.get("label") or ""
    head = f"**{account['name']}**"
    if label:
        head += f" · {label}"
    head += f" · [@{handle}](https://x.com/{handle})"
    lines = [head, ""]
    if summary:
        lines += [summary.strip(), ""]
    for p in posts:
        url = f"https://x.com/{handle}/status/{p['id']}"
        lines.append(f"- [{_when(p['created_at'])}]({url}) — {_excerpt(p['text'])}")
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


def _timeline_url(user_id, since_id):
    params = {
        "max_results": str(max(5, min(100, int(X_MAX_RESULTS)))),
        "exclude": "retweets,replies",
        "tweet.fields": "created_at,note_tweet",
    }
    if since_id and str(since_id).isdigit():
        params["since_id"] = str(since_id)
    query = urllib.parse.urlencode(params)
    return f"{X_API}/users/{urllib.parse.quote(str(user_id))}/tweets?{query}"


def _fetch_account(account, state, token, ask_claude, opener):
    """One timeline call. Returns a markdown block, or '' when nothing is new.
    Advances since_id only after the posts are in hand, so a failed call
    retries next run instead of skipping them."""
    rec = _account_record(state, account["handle"])
    user_id = _resolve_user_id(account, rec, token, opener)
    since_id = str(rec.get("since_id") or "")
    payload = _get_json(_timeline_url(user_id, since_id), token, opener)
    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    if meta.get("next_token"):
        print(f"X @{account['handle']}: capped at {X_MAX_RESULTS} "
              f"(older posts in this window were not fetched)")
    posts = _parse_posts(payload)
    if not posts:
        return ""
    summary = ""
    try:
        summary = (ask_claude(
            f"{X_SUMMARY_INSTRUCTIONS}\nAuthor: {account['name']} "
            f"(@{account['handle']})",
            _prompt_body(account, posts),
        ) or "").strip()
    except Exception as e:
        print(f"X @{account['handle']}: summary failed ({type(e).__name__}); "
              f"listing the posts without a summary")
        summary = ""
    newest = str(meta.get("newest_id") or "")
    if not newest.isdigit():
        newest = posts[-1]["id"]
    rec["since_id"] = newest
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
