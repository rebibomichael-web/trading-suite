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

Exit code is 0 even if some individual videos have no captions — a missing
transcript is normal (Shorts, music, brand-new uploads) and the digest degrades
to the description summary for those.
"""
import argparse
import datetime
import html
import os
import re
import subprocess
import sys
import urllib.request

# Keep this list in sync with CHANNELS in scripts/youtube_digest.py.
CHANNELS = {
    "Brighter with Herbert": "UC4DBLlq1x0AKmip1QJUcbXg",
    "Matt Pocius on Tesla Stock & Money": "UCF1iS7Bp9_hsQphNF6o8qwQ",
    "Fundstrat": "UCcBzKSM4A-pIHMJWSnxmi_g",
    "Fundstrat Capital": "UCQxFhbPxp6VtAMGEF8OWG5g",
    "Mr. FIRED Up Wealth": "UCqqHGGPbhISeKkpEx8676sw",
    "Kaspa Silver": "UCv8-2oyrfqDigJAKjZ_RCzQ",
}

TRANSCRIPT_DIR = os.path.join("transcripts", "youtube")


def fetch_feed(channel_id):
    """Return [(video_id, published_datetime), ...] from a channel's RSS feed."""
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    xml = urllib.request.urlopen(url, timeout=30).read().decode("utf-8", "ignore")
    out = []
    for entry in re.findall(r"<entry>.*?</entry>", xml, re.S):
        vid = re.search(r"<yt:videoId>([^<]+)</yt:videoId>", entry)
        pub = re.search(r"<published>([^<]+)</published>", entry)
        if not (vid and pub):
            continue
        try:
            published = datetime.datetime.fromisoformat(pub.group(1))
        except ValueError:
            continue
        out.append((vid.group(1), published))
    return out


def fetch_transcript(video_id):
    """Fetch the transcript text for one video (residential IP, no proxy)."""
    from youtube_transcript_api import YouTubeTranscriptApi

    api = YouTubeTranscriptApi()
    snippets = api.fetch(video_id)
    return " ".join(s.text for s in snippets)


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
    args = ap.parse_args()

    repo = os.path.abspath(args.repo)
    out_dir = os.path.join(repo, TRANSCRIPT_DIR)
    os.makedirs(out_dir, exist_ok=True)

    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(days=args.days)

    written = []
    skipped = 0
    no_caps = 0
    for channel, cid in CHANNELS.items():
        try:
            videos = fetch_feed(cid)
        except Exception as e:
            print(f"{channel}: feed error {e!r}", file=sys.stderr)
            continue
        for vid, published in videos:
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

    print(f"Done: {len(written)} new, {skipped} already present, "
          f"{no_caps} without captions.")

    if args.no_git or not written:
        if not written:
            print("Nothing new to commit.")
        return

    # commit + push only the transcript files we added
    day = now.strftime("%Y-%m-%d")
    git(["add", *written], repo)
    # nothing staged (e.g. identical content) → done
    if git(["diff", "--cached", "--quiet"], repo, check=False).returncode == 0:
        print("No staged changes.")
        return
    git(["commit", "-m", f"YouTube transcripts {day} ({len(written)} videos)"], repo)
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
