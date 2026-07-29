#!/usr/bin/env python3
"""Narrate a digest/summary markdown file to MP3 and maintain the podcast feed.

Turns digest.md (YouTube digest) or summary.md (Halftime) into spoken audio
via Microsoft Edge neural TTS (pip install edge-tts — free, no key), writes
audio/<kind>/<date>.mp3, prunes episodes older than RETAIN_DAYS, and
regenerates audio/feed.xml — a podcast RSS feed served straight from
raw.githubusercontent.com, subscribable in any podcast app:

    https://raw.githubusercontent.com/rebibomichael-web/trading-suite/main/audio/feed.xml

Usage: python scripts/make_audio.py --input digest.md --kind youtube [--date YYYY-MM-DD]
Exit 0 on success, nonzero on failure — callers should treat failure as
non-fatal (audio is a nice-to-have; the text digest must never be blocked).
"""
import argparse
import asyncio
import datetime
import email.utils
import glob
import os
import re
import sys
from xml.sax.saxutils import escape

VOICE = "en-US-AndrewNeural"
RETAIN_DAYS = 30
AUDIO_DIR = "audio"
REPO_SLUG = os.environ.get("GITHUB_REPOSITORY", "rebibomichael-web/trading-suite")
RAW_BASE = f"https://raw.githubusercontent.com/{REPO_SLUG}/main"

KIND_TITLE = {
    "youtube": "YouTube digest",
    "halftime": "Halftime Report",
}


def md_to_speech(md, kind):
    """Markdown digest -> text that reads well aloud."""
    text = md
    # drop UI-only lines: "Ask Claude" pills and any Listen links
    text = re.sub(r"^\*\[💬[^\n]*$", "", text, flags=re.M)
    text = re.sub(r"^🔊[^\n]*$", "", text, flags=re.M)
    # video/section headers: "### [Title](url)" -> "Title."
    text = re.sub(r"### \[([^\]]*)\]\([^)]*\)", r"\1.", text)
    text = re.sub(r"^#+\s*", "", text, flags=re.M)          # other headers
    # "**Channel** · Jul 14" byline -> "From Channel, July 14."
    text = re.sub(r"\*\*([^*]+)\*\*\s*·\s*(\w+ \d+)", r"From \1, \2.", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)     # remaining links
    text = text.replace("**Today's read:**", "Today's read.")
    text = text.replace("**", "").replace("*(", "(").replace(")*", ")")
    text = text.replace("---", "\n")
    text = re.sub(r"\(https?://[^)]*\)", "", text)
    text = re.sub(r"\n{2,}", "\n\n", text)
    intro = {"youtube": "Your YouTube channel digest.",
             "halftime": "Your Halftime Report summary."}[kind]
    return f"{intro}\n\n{text.strip()}"


def synthesize(text, dest):
    import edge_tts

    async def go():
        await edge_tts.Communicate(text, VOICE).save(dest)

    asyncio.run(go())


def rebuild_feed():
    """Regenerate audio/feed.xml from the mp3 files on disk."""
    items = []
    for path in glob.glob(f"{AUDIO_DIR}/*/*.mp3"):
        kind = os.path.basename(os.path.dirname(path))
        day = os.path.basename(path)[:-4]
        try:
            pub = datetime.datetime.strptime(day, "%Y-%m-%d").replace(
                hour=7, tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
        title = f"{KIND_TITLE.get(kind, kind)} — {day}"
        url = f"{RAW_BASE}/{AUDIO_DIR}/{kind}/{day}.mp3"
        items.append((pub, title, url, os.path.getsize(path)))
    items.sort(key=lambda x: x[0], reverse=True)

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">',
        "<channel>",
        "<title>Trading Briefings</title>",
        f"<link>https://github.com/{REPO_SLUG}</link>",
        "<language>en-us</language>",
        "<description>Narrated daily YouTube channel digests and CNBC Halftime "
        "Report summaries, generated automatically.</description>",
        "<itunes:author>trading-suite automations</itunes:author>",
    ]
    for pub, title, url, size in items:
        parts += [
            "<item>",
            f"<title>{escape(title)}</title>",
            f"<enclosure url=\"{escape(url)}\" length=\"{size}\" type=\"audio/mpeg\"/>",
            f"<guid isPermaLink=\"false\">{escape(url)}</guid>",
            f"<pubDate>{email.utils.format_datetime(pub)}</pubDate>",
            "</item>",
        ]
    parts += ["</channel>", "</rss>"]
    with open(f"{AUDIO_DIR}/feed.xml", "w") as fh:
        fh.write("\n".join(parts) + "\n")
    return len(items)


def prune():
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=RETAIN_DAYS)
    removed = 0
    for path in glob.glob(f"{AUDIO_DIR}/*/*.mp3"):
        day = os.path.basename(path)[:-4]
        try:
            when = datetime.datetime.strptime(day, "%Y-%m-%d").replace(
                tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
        if when < cutoff:
            os.remove(path)
            removed += 1
    return removed


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True)
    ap.add_argument("--kind", choices=sorted(KIND_TITLE), required=True)
    ap.add_argument("--date", default=datetime.datetime.now(
        datetime.timezone.utc).strftime("%Y-%m-%d"))
    args = ap.parse_args()

    md = open(args.input).read()
    if md.strip() in ("NO_VIDEOS", "NO_EPISODE", "NOTHING", ""):
        print("nothing to narrate")
        return
    text = md_to_speech(md, args.kind)
    dest_dir = os.path.join(AUDIO_DIR, args.kind)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, f"{args.date}.mp3")
    synthesize(text, dest)
    print(f"narrated {args.input} -> {dest} ({os.path.getsize(dest)} bytes, "
          f"{len(text)} chars)")
    removed = prune()
    count = rebuild_feed()
    print(f"feed rebuilt: {count} episode(s); pruned {removed} old file(s)")


if __name__ == "__main__":
    main()
