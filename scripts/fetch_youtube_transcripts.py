#!/usr/bin/env python3
"""Fetch YouTube transcripts from a residential IP and commit them to the repo.

WHY THIS EXISTS
---------------
YouTube blocks caption/transcript requests coming from cloud IP ranges
(GitHub Actions runs on Azure), so the daily digest (scripts/youtube_digest.py)
running on GitHub can't pull transcripts and falls back to shallow
title/description summaries.

This script runs on a machine with an ordinary residential IP — Michael's Dell,
which already runs the swing/LEAP crons — where YouTube does NOT block the
requests. It fetches transcripts for recent videos of the followed channels and
writes each to transcripts/youtube/<id>.txt (raw text). It then commits and
pushes them. When the digest workflow later checks out the repo, fetch_transcript()
finds the pre-fetched file and uses it instead of hitting YouTube from the cloud.

So the split is: transcripts fetched here (residential IP), summaries generated
on GitHub Actions (which reads these files). No paid proxy required.

SETUP (one time, on the Dell)
-----------------------------
    cd ~/trading-suite            # a clone of rebibomichael-web/trading-suite
    python3 -m venv .venv && . .venv/bin/activate
    pip install youtube-transcript-api

CRON (run a bit BEFORE the digest's 04:40 UTC so transcripts are ready)
-----------------------------------------------------------------------
    # 04:10 UTC daily
    10 4 * * *  cd ~/trading-suite && git pull --quiet && \
                .venv/bin/python scripts/fetch_youtube_transcripts.py \
                >> ~/youtube_transcripts.log 2>&1

FLAGS
-----
    --days N     how far back to fetch (default 7; covers the digest's 4-day
                 window plus its carry-over backlog)
    --repo DIR   repo working copy (default: the repo this script lives in)
    --no-push    fetch/commit but don't push (for testing)
    --no-git     just write files, no commit/push at all
    --backfill-days N --channels "A,B"
                 ONE-OFF deep backfill (Y-5, ruled 2026-09-15): enumerate the
                 named channels' uploads playlists via yt-dlp (RSS stops at ~15)
                 and fetch transcripts for uploads newer than N days. Transcript
                 files only — the feed snapshot and prices are NOT touched, so
                 the digest/coverage window is unaffected. Default off.

Exit code is 0 even if some individual videos have no captions — a missing
transcript is normal (Shorts, music, brand-new uploads) and the digest degrades
to the description summary for those.
"""
import argparse
import datetime
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

# Keep this list in sync with CHANNELS in scripts/youtube_digest.py.
CHANNELS = {
    "Brighter with Herbert": "UC4DBLlq1x0AKmip1QJUcbXg",
    "Matt Pocius on Tesla Stock & Money": "UCF1iS7Bp9_hsQphNF6o8qwQ",
    "Fundstrat": "UCcBzKSM4A-pIHMJWSnxmi_g",
    "Fundstrat Capital": "UCQxFhbPxp6VtAMGEF8OWG5g",
    "Mr. FIRED Up Wealth": "UCqqHGGPbhISeKkpEx8676sw",
    "Kaspa Silver": "UCv8-2oyrfqDigJAKjZ_RCzQ",
    "Wicked Stocks": "UCQSiWKh7o9oRtApc4YjRssg",
    "Traders Helping Traders": "UCMno7bbQKigk6RxiO0uv78g",
}

TRANSCRIPT_DIR = os.path.join("transcripts", "youtube")
FEED_SNAPSHOT = os.path.join("feeds", "youtube_feed.json")
PRICES_PATH = os.path.join("prices", "daily_closes.json")
LEDGER_PATH = os.path.join("ledger", "guru_calls.json")
# always priced, on top of whatever tickers appear in the guru ledger
DEFAULT_TICKERS = ["BMNR", "PLTR", "TSLA", "NVDA", "SPY"]

# Feed fetch retry: YouTube's RSS endpoint returned 500 then 404 for the SAME
# channel id across consecutive runs (2026-09-17, 6/8 feeds failed one run,
# 8/8 an earlier one), so neither a 5xx nor a 404 is authoritative on first
# sight. One retry, short backoff.
FEED_RETRY_STATUSES = {404, 500, 502, 503, 504}
FEED_RETRY_BACKOFF_S = 5
# consecutive feed errors at which a channel is escalated in the log —
# persistent 404 on one id while the others succeed = renamed/deleted channel,
# which must not ride on last-good entries forever
FEED_ESCALATE_ERRORS = 3


def load_snapshot(path):
    """Previous feed snapshot, or an empty one. A corrupt/missing file is an
    empty prior — the merge then behaves like a first-ever run."""
    try:
        with open(path) as fh:
            snap = json.load(fh)
        if not isinstance(snap, dict) or not isinstance(snap.get("channels"), dict):
            return {"fetched_at": None, "channels": {}, "meta": {}}
        snap.setdefault("fetched_at", None)
        if not isinstance(snap.get("meta"), dict):
            snap["meta"] = {}
        return snap
    except (OSError, ValueError):
        return {"fetched_at": None, "channels": {}, "meta": {}}


def merge_snapshot(prev, results, now):
    """MERGE this run's feed results into the previous snapshot (never
    overwrite — 2026-09-17 a 6/8-feed-failure run rewrote the file with the
    2 survivors and dropped last-good entries for the rest).

    prev:    dict from load_snapshot()
    results: {channel: (entries_list | None, error_str | None)} — entries on
             success, error string on failure
    Returns the new snapshot dict:
      channels[name] -> list of entry dicts (UNCHANGED shape: every reader —
                        youtube_digest / youtube_coverage / guru_ledger —
                        iterates it as a list)
      meta[name]     -> {"last_ok", "consecutive_errors", "last_error"}
      fetched_at     -> now if ANY channel succeeded, else carried forward, so
                        the digest's 72h staleness gate still ages out a
                        snapshot that has had no success at all
      written_at     -> now, always
    A channel key present in prev is never dropped; on failure its entries
    and last_ok carry forward and consecutive_errors increments."""
    channels = dict(prev.get("channels", {}))
    meta = {k: dict(v) for k, v in prev.get("meta", {}).items()}
    now_iso = now.isoformat()
    any_ok = False
    for name, (entries, err) in results.items():
        m = meta.get(name, {"last_ok": None, "consecutive_errors": 0,
                            "last_error": None})
        if err is None:
            channels[name] = entries
            m = {"last_ok": now_iso, "consecutive_errors": 0, "last_error": None}
            any_ok = True
        else:
            # entries (if any) and last_ok carry forward untouched
            m = {"last_ok": m.get("last_ok"),
                 "consecutive_errors": int(m.get("consecutive_errors", 0)) + 1,
                 "last_error": err}
        meta[name] = m
    return {
        "fetched_at": now_iso if any_ok else prev.get("fetched_at"),
        "written_at": now_iso,
        "channels": channels,
        "meta": meta,
    }


def write_snapshot(path, snap):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(snap, fh, indent=0)
    os.replace(tmp, path)


def describe_error(e):
    """Short provenance string for meta.last_error: 'HTTP 404' / 'URLError'."""
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    return type(e).__name__


def fetch_daily_closes(ticker):
    """Six months of daily closes from Yahoo's chart API (works from a
    residential IP; GitHub runners are hit-or-miss, which is why the Dell does
    this). Returns {\"YYYY-MM-DD\": close}."""
    req = urllib.request.Request(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        f"?range=6mo&interval=1d",
        headers={"User-Agent": "Mozilla/5.0"})
    data = json.load(urllib.request.urlopen(req, timeout=30))
    result = data["chart"]["result"][0]
    stamps = result["timestamp"]
    closes = result["indicators"]["quote"][0]["close"]
    out = {}
    for ts, c in zip(stamps, closes):
        if c is None:
            continue
        day = datetime.datetime.fromtimestamp(
            ts, datetime.timezone.utc).strftime("%Y-%m-%d")
        out[day] = round(float(c), 4)
    return out


def update_prices(repo):
    """Fetch daily closes for the guru-ledger tickers + defaults and write
    prices/daily_closes.json for the GitHub-side outcome checker."""
    tickers = set(DEFAULT_TICKERS)
    ledger_file = os.path.join(repo, LEDGER_PATH)
    try:
        ledger = json.load(open(ledger_file))
        tickers.update(c["ticker"] for c in ledger.get("calls", [])
                       if c.get("ticker"))
    except (OSError, ValueError):
        pass
    prices = {}
    for t in sorted(tickers):
        try:
            prices[t] = fetch_daily_closes(t)
            print(f"prices: {t} ({len(prices[t])} closes)")
        except Exception as e:
            print(f"prices: {t} FAILED ({type(e).__name__})", file=sys.stderr)
    if not prices:
        return None
    dest = os.path.join(repo, PRICES_PATH)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w") as fh:
        json.dump({"fetched_at": datetime.datetime.now(
            datetime.timezone.utc).isoformat(), "closes": prices}, fh, indent=0)
    return PRICES_PATH


def fetch_feed(channel_id):
    """Return video dicts (id/title/published/description) from a channel's
    RSS feed. Full metadata, because this feed also becomes the snapshot the
    GitHub-hosted digest falls back to when YouTube blocks RSS from the
    runner's cloud IP (observed 2026-07-14)."""
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    xml = urllib.request.urlopen(url, timeout=30).read().decode("utf-8", "ignore")
    out = []
    for entry in re.findall(r"<entry>.*?</entry>", xml, re.S):
        vid = re.search(r"<yt:videoId>([^<]+)</yt:videoId>", entry)
        title = re.search(r"<title>([^<]+)</title>", entry)
        pub = re.search(r"<published>([^<]+)</published>", entry)
        desc = re.search(r"<media:description>(.*?)</media:description>", entry, re.S)
        if not (vid and title and pub):
            continue
        try:
            published = datetime.datetime.fromisoformat(pub.group(1))
        except ValueError:
            continue
        out.append({
            "id": vid.group(1),
            "title": html.unescape(title.group(1)),
            "published": published,
            "description": html.unescape(desc.group(1).strip()) if desc else "",
        })
    return out


def fetch_feed_with_retry(channel_id, sleep=None):
    """fetch_feed with ONE retry after FEED_RETRY_BACKOFF_S on a retryable
    HTTP status (5xx and 404 — see FEED_RETRY_STATUSES). Any other error, or a
    second failure, propagates."""
    try:
        return fetch_feed(channel_id)
    except urllib.error.HTTPError as e:
        if e.code not in FEED_RETRY_STATUSES:
            raise
        (sleep or time.sleep)(FEED_RETRY_BACKOFF_S)
        return fetch_feed(channel_id)


def fetch_transcript(video_id):
    """Fetch the transcript text for one video (residential IP, no proxy)."""
    from youtube_transcript_api import YouTubeTranscriptApi

    api = YouTubeTranscriptApi()
    snippets = api.fetch(video_id)
    return " ".join(s.text for s in snippets)


def enumerate_uploads(channel_id, cutoff, page=200, stop_after=5):
    """Every upload newer than `cutoff` from the channel's uploads playlist
    (UU + channel id), newest first. yt-dlp's flat listing carries no dates, so
    each id costs one metadata fetch (~2 s); stop after `stop_after`
    consecutive videos older than the cutoff. No API key."""
    import yt_dlp   # imported here only: normal mode never needs it
    flat = {"quiet": True, "no_warnings": True, "skip_download": True,
            "extract_flat": True}
    meta = {"quiet": True, "no_warnings": True, "skip_download": True,
            "extractor_args": {"youtube": {"player_skip": ["js", "configs"],
                                           "player_client": ["web"]}}}
    out, older, start = [], 0, 1
    with yt_dlp.YoutubeDL(flat) as y_flat, yt_dlp.YoutubeDL(meta) as y_meta:
        while True:
            y_flat.params.update({"playliststart": start, "playlistend": start + page - 1})
            info = y_flat.extract_info(
                f"https://www.youtube.com/playlist?list=UU{channel_id[2:]}", download=False)
            entries = [e for e in (info.get("entries") or []) if e and e.get("id")]
            if not entries:
                break
            for e in entries:
                try:
                    m = y_meta.extract_info(f"https://www.youtube.com/watch?v={e['id']}",
                                            download=False, process=False)
                    ts = m.get("timestamp")
                except Exception as err:
                    print(f"  {e['id']}: metadata failed ({type(err).__name__})", file=sys.stderr)
                    continue
                if not ts:
                    continue
                published = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
                if published < cutoff:
                    older += 1
                    if older >= stop_after:
                        return out
                    continue
                older = 0
                out.append({"id": e["id"], "title": e.get("title") or "", "published": published})
            if len(entries) < page:
                break
            start += page
    return out


def backfill(channels, days, out_dir, now):
    """Y-5 one-off: transcripts for the named channels' uploads of the last
    `days` days. Same file identity (transcripts/youtube/<id>.txt), same skip
    rule (non-empty file present), same no-captions tolerance as the nightly
    path. Returns the list of written repo-relative paths."""
    cutoff = now - datetime.timedelta(days=days)
    written = []
    for channel in channels:
        cid = CHANNELS.get(channel)
        if not cid:
            print(f"backfill: unknown channel {channel!r} — must be a CHANNELS key", file=sys.stderr)
            continue
        try:
            videos = enumerate_uploads(cid, cutoff)
        except Exception as e:
            print(f"{channel}: backfill enumeration error {e!r}", file=sys.stderr)
            continue
        n_new = n_skip = n_nocap = 0
        for v in videos:
            dest = os.path.join(out_dir, f"{v['id']}.txt")
            if os.path.exists(dest) and os.path.getsize(dest) > 0:
                n_skip += 1
                continue
            try:
                text = fetch_transcript(v["id"])
            except Exception as e:
                n_nocap += 1
                print(f"{channel}: {v['id']} no captions ({type(e).__name__})")
                continue
            if not text.strip():
                n_nocap += 1
                continue
            with open(dest, "w") as fh:
                fh.write(text)
            written.append(f"{TRANSCRIPT_DIR}/{v['id']}.txt")
            n_new += 1
        print(f"backfill {channel}: {len(videos)} uploads in the last {days}d — "
              f"{n_new} new, {n_skip} already present, {n_nocap} without captions")
    return written


def git(args, repo, check=True):
    return subprocess.run(["git", "-C", repo, *args], check=check,
                          capture_output=True, text=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--repo", default=os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--no-git", action="store_true")
    ap.add_argument("--backfill-days", type=int, default=0,
                    help="one-off deep backfill of --channels; 0 = off (default)")
    ap.add_argument("--channels", default="",
                    help="comma-separated CHANNELS keys for --backfill-days")
    args = ap.parse_args()

    repo = os.path.abspath(args.repo)
    out_dir = os.path.join(repo, TRANSCRIPT_DIR)
    os.makedirs(out_dir, exist_ok=True)

    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(days=args.days)

    if args.backfill_days > 0:
        # Backfill mode: transcript files only. No feed snapshot, no prices —
        # nothing the digest/coverage window reads is touched.
        chans = [c.strip() for c in args.channels.split(",") if c.strip()]
        if not chans:
            sys.exit("--backfill-days needs --channels")
        written = backfill(chans, args.backfill_days, out_dir, now)
        commit_and_push(repo, list(written), written, now, args,
                        msg=f"YouTube transcript backfill {now.strftime('%Y-%m-%d')} "
                            f"({len(written)} videos, {args.backfill_days}d, {', '.join(chans)})")
        return

    written = []
    skipped = 0
    no_caps = 0
    feed_results = {}   # channel -> (entries | None, error | None)
    for channel, cid in CHANNELS.items():
        try:
            videos = fetch_feed_with_retry(cid)
        except Exception as e:
            print(f"{channel}: feed error {e!r}", file=sys.stderr)
            feed_results[channel] = (None, describe_error(e))
            continue
        feed_results[channel] = ([
            {
                "id": v["id"],
                "title": v["title"],
                "published": v["published"].isoformat(),
                "description": v["description"],
            }
            for v in videos
        ], None)
        for v in videos:
            vid, published = v["id"], v["published"]
            if published < cutoff:
                continue
            dest = os.path.join(out_dir, f"{vid}.txt")
            if os.path.exists(dest) and os.path.getsize(dest) > 0:
                skipped += 1
                continue
            try:
                text = fetch_transcript(vid)
            except Exception as e:
                # normal for Shorts / music / captionless uploads
                no_caps += 1
                print(f"{channel}: {vid} no captions ({type(e).__name__})")
                continue
            if not text.strip():
                no_caps += 1
                continue
            with open(dest, "w") as fh:
                fh.write(text)
            written.append(f"{TRANSCRIPT_DIR}/{vid}.txt")
            print(f"{channel}: {vid} transcript saved ({len(text)} chars)")

    # Feed snapshot for the digest's runner-IP fallback: MERGE into the
    # previous file (failed channels keep their last-good entries, with
    # per-channel provenance in "meta"), then escalate persistent failures.
    to_add = []
    snap_path = os.path.join(repo, FEED_SNAPSHOT)
    snap = merge_snapshot(load_snapshot(snap_path), feed_results, now)
    write_snapshot(snap_path, snap)
    to_add.append(FEED_SNAPSHOT)
    n_ok = sum(1 for v in feed_results.values() if v[1] is None)
    n_carried = sum(1 for c, v in feed_results.items()
                    if v[1] is not None and c in snap["channels"])
    print(f"Feed snapshot written (ok {n_ok}/{len(feed_results)}, "
          f"carried {n_carried}, keys {len(snap['channels'])}).")
    for channel in feed_results:
        m = snap["meta"][channel]
        if m["consecutive_errors"] >= FEED_ESCALATE_ERRORS:
            print(f"WARN: {channel}: {m['consecutive_errors']} consecutive feed "
                  f"errors (last {m['last_error']}, last_ok {m['last_ok']}) — "
                  f"renamed/deleted channel id? riding on last-good entries",
                  file=sys.stderr)

    print(f"Done: {len(written)} new, {skipped} already present, "
          f"{no_caps} without captions.")

    # Daily closes for the guru-ledger outcome checker (best effort).
    try:
        prices_rel = update_prices(repo)
        if prices_rel:
            to_add.append(prices_rel)
    except Exception as e:
        print(f"prices update failed (non-fatal): {e!r}", file=sys.stderr)

    to_add.extend(written)
    commit_and_push(repo, to_add, written, now, args)


def commit_and_push(repo, to_add, written, now, args, msg=None):
    """Shared commit/push tail — the nightly path and the backfill path both
    ride it. Body unchanged from the inline original except the optional msg."""
    if args.no_git or not to_add:
        if not to_add:
            print("Nothing new to commit.")
        return

    # commit + push the transcript files and the feed snapshot
    day = now.strftime("%Y-%m-%d")
    git(["add", *to_add], repo)
    # nothing staged (e.g. identical content) → done
    if git(["diff", "--cached", "--quiet"], repo, check=False).returncode == 0:
        print("No staged changes.")
        return
    if msg is None:
        msg = (f"YouTube transcripts {day} ({len(written)} videos)" if written
               else f"YouTube feed snapshot {day}")
    git(["commit", "-m", msg], repo)
    if args.no_push:
        print("Committed locally (--no-push).")
        return

    branch = git(["rev-parse", "--abbrev-ref", "HEAD"], repo).stdout.strip()
    for attempt in range(4):
        push = git(["push", "origin", f"HEAD:{branch}"], repo, check=False)
        if push.returncode == 0:
            print(f"Pushed {len(written)} transcript(s) to {branch}.")
            return
        # someone else pushed (the digest workflow commits state) — rebase & retry
        git(["pull", "--rebase", "origin", branch], repo, check=False)
    print("Push failed after retries — transcripts are committed locally; "
          "next run will retry.", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
