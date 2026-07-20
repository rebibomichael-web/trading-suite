#!/usr/bin/env python3
"""Coverage check: how many videos did the followed channels publish, and how
many did the digest actually summarize?

For every channel in the digest's list, compares the channel's feed (live RSS,
falling back to the Dell's feeds/youtube_feed.json snapshot when YouTube blocks
the caller's IP) against the digest archives (summaries/youtube/*.md) and
state.json. Each published video is classified as:

  transcript   summarized from the real transcript
  fallback     summarized from title/description only (shallow)
  queued       not summarized yet but safe: in the pending backlog, or still
               inside the digest's window/grace so the next run handles it
  MISSING      never summarized, not queued, and too old for the digest to
               ever pick up — genuinely lost

Flags:
  --days N          how far back to check (default 7)
  --markdown        emit a GitHub-markdown report instead of the console table
  --fail-on-missing exit 2 when any video is MISSING (for CI)

Exit codes: 0 = checked (regardless of counts) · 2 = MISSING found and
--fail-on-missing set · 3 = could not check (no feed and no usable snapshot).
"""
import argparse
import datetime
import glob
import json
import os
import re
import sys

from youtube_digest import (
    CHANNELS, STATE_PATH, WINDOW_DAYS, GRACE_HOURS,
    fetch_feed, load_feed_snapshot, load_state,
)


def summarized_ids():
    """video id -> (digest date, 'transcript'|'fallback') from the archives."""
    out = {}
    for md in sorted(glob.glob("summaries/youtube/*.md")):
        day = os.path.basename(md)[:-3]
        text = open(md).read()
        for m in re.finditer(
            r"### \[[^\]]*\]\(https://www\.youtube\.com/watch\?v=([\w-]+)\)"
            r"(.*?)(?=### \[|\Z)", text, re.S,
        ):
            vid, body = m.group(1), m.group(2)
            kind = "fallback" if ("captions unavailable" in body
                                  or "No captions" in body) else "transcript"
            out[vid] = (day, kind)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--markdown", action="store_true")
    ap.add_argument("--fail-on-missing", action="store_true")
    args = ap.parse_args()

    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(days=args.days)

    state = load_state()
    seen = set(state["seen"])
    pending = {p.get("id") for p in state.get("pending", [])}
    digested = summarized_ids()

    snapshot = None
    snapshot_loaded = False
    lines = []
    totals = {"published": 0, "transcript": 0, "fallback": 0,
              "queued": 0, "missing": 0}
    missing_rows = []
    channels_unavailable = 0

    for channel, cid in CHANNELS.items():
        try:
            videos = fetch_feed(cid)
            source = "live"
        except Exception as e:
            if not snapshot_loaded:
                snapshot = load_feed_snapshot()
                snapshot_loaded = True
            if snapshot and channel in snapshot:
                videos = snapshot[channel]
                source = "snapshot"
            else:
                lines.append((channel, None, f"feed unavailable ({e!r})"))
                channels_unavailable += 1
                continue

        recent = [v for v in videos if v["published"] >= cutoff]
        counts = {"transcript": 0, "fallback": 0, "queued": 0, "missing": 0}
        for v in recent:
            vid = v["id"]
            age_h = (now - v["published"]).total_seconds() / 3600
            if vid in digested:
                counts[digested[vid][1]] += 1
            elif (vid in pending
                  or age_h < GRACE_HOURS
                  or (vid not in seen and age_h < WINDOW_DAYS * 24)):
                counts["queued"] += 1
            else:
                counts["missing"] += 1
                missing_rows.append(
                    f"{channel}: {v['title'][:70]} "
                    f"(https://www.youtube.com/watch?v={vid}, "
                    f"published {v['published']:%Y-%m-%d})")
        summarized = counts["transcript"] + counts["fallback"]
        totals["published"] += len(recent)
        for k in ("transcript", "fallback", "queued", "missing"):
            totals[k] += counts[k]
        lines.append((channel, (len(recent), summarized, counts, source), None))

    total_summarized = totals["transcript"] + totals["fallback"]
    if args.markdown:
        print(f"## YouTube coverage — last {args.days} days\n")
        print(f"**{totals['published']} published / {total_summarized} "
              f"summarized** ({totals['transcript']} transcript, "
              f"{totals['fallback']} description-only), "
              f"{totals['queued']} queued, {totals['missing']} MISSING\n")
        print("| Channel | Published | Summarized | Queued | Missing | Feed |")
        print("|---|---|---|---|---|---|")
        for channel, data, err in lines:
            if err:
                print(f"| {channel} | — | — | — | — | {err} |")
            else:
                n, s, c, src = data
                print(f"| {channel} | {n} | {s} ({c['transcript']}T/"
                      f"{c['fallback']}D) | {c['queued']} | "
                      f"{c['missing']} | {src} |")
        if missing_rows:
            print("\n### Missing videos")
            for row in missing_rows:
                print(f"- {row}")
    else:
        print(f"YouTube coverage — last {args.days} days "
              f"(as of {now:%Y-%m-%d %H:%M} UTC)")
        for channel, data, err in lines:
            if err:
                print(f"  {channel:38} {err}")
            else:
                n, s, c, src = data
                flag = "  <-- MISSING" if c["missing"] else ""
                print(f"  {channel:38} published={n:<3} summarized={s:<3} "
                      f"(transcript={c['transcript']} desc-only={c['fallback']}) "
                      f"queued={c['queued']} missing={c['missing']} "
                      f"[{src}]{flag}")
        print(f"  TOTAL: {totals['published']} published / {total_summarized} "
              f"summarized / {totals['queued']} queued / "
              f"{totals['missing']} missing")
        for row in missing_rows:
            print(f"    MISSING: {row}")

    if channels_unavailable == len(CHANNELS):
        print("ERROR: no channel could be checked (no live feed, no snapshot)",
              file=sys.stderr)
        sys.exit(3)
    if args.fail_on_missing and totals["missing"] > 0:
        sys.exit(2)


if __name__ == "__main__":
    main()
