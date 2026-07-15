#!/usr/bin/env python3
"""One-off backfill: regenerate proper summaries for videos whose transcripts
arrived AFTER the digest already summarized them from descriptions (or missed
them during the 07-14/07-15 feed outage), using transcript files already
committed under transcripts/youtube/.

Reads a spec JSON (list of {id, channel, title, published}) and writes
backfill.md for the workflow to deliver as a GitHub issue. Deliberately does
NOT touch summaries/youtube/state.json — the videos stay "seen"; this only
produces the better summaries.

Usage: python scripts/backfill_youtube_digest.py [spec.json]
"""
import datetime
import json
import sys

from halftime_pipeline import ask_claude, preflight_auth
from youtube_digest import SUMMARY_INSTRUCTIONS, transcript_path


def main():
    spec_path = sys.argv[1] if len(sys.argv) > 1 else "scripts/backfill_2026-07-15.json"
    with open(spec_path) as fh:
        videos = json.load(fh)
    preflight_auth()

    sections = []
    for v in videos:
        try:
            with open(transcript_path(v["id"])) as fh:
                text = fh.read().strip()
        except OSError:
            print(f"{v['id']}: no transcript file — skipped")
            continue
        if not text:
            print(f"{v['id']}: empty transcript — skipped")
            continue
        summary = ask_claude(
            f"{SUMMARY_INSTRUCTIONS}\nChannel: {v['channel']}\n"
            f"Video title: {v['title']}",
            text,
        )
        date = datetime.datetime.fromisoformat(v["published"]).strftime("%b %d")
        sections.append(
            f"### [{v['title']}](https://www.youtube.com/watch?v={v['id']})\n"
            f"**{v['channel']}** · {date}\n\n{summary}\n"
        )
        print(f"{v['id']}: summarized")

    if not sections:
        open("backfill.md", "w").write("NOTHING")
        print("Nothing to backfill.")
        return
    open("backfill.md", "w").write(
        "Upgraded summaries for videos whose full transcripts arrived after "
        "the original digest (which had only their descriptions).\n\n"
        + "\n---\n\n".join(sections)
    )
    print(f"backfill.md written: {len(sections)} videos")


if __name__ == "__main__":
    main()
