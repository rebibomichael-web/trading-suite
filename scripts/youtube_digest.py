#!/usr/bin/env python3
"""Daily YouTube digest: simple summaries of new videos from followed channels.

For each channel, reads the channel's RSS feed, finds videos not yet seen,
pulls the auto-generated captions, and asks Claude for a short plain-language
summary (no per-speaker breakdown). Writes digest.md for the workflow to
deliver, and updates summaries/youtube/state.json so videos are only
summarized once.

Exit codes: 0 = digest written (or NO_VIDEOS marker if nothing new),
nonzero = real failure.
"""
import datetime
import json
import os
import re
import urllib.request

from halftime_pipeline import ask_claude

CHANNELS = {
    "Brighter with Herbert": "UC4DBLlq1x0AKmip1QJUcbXg",
    "Matt Pocius on Tesla Stock & Money": "UCF1iS7Bp9_hsQphNF6o8qwQ",
    "Fundstrat": "UCcBzKSM4A-pIHMJWSnxmi_g",
    "Fundstrat Capital": "UCQxFhbPxp6VtAMGEF8OWG5g",
    "Mr. FIRED Up Wealth": "UCqqHGGPbhISeKkpEx8676sw",
    "Kaspa Silver": "UCv8-2oyrfqDigJAKjZ_RCzQ",
}

STATE_PATH = "summaries/youtube/state.json"
MAX_PER_CHANNEL = 3          # cost guard per run
FIRST_RUN_WINDOW_H = 36      # without state, only look this far back
WINDOW_DAYS = 4              # with state, ignore anything older than this

SUMMARY_INSTRUCTIONS = (
    "Summarize this YouTube video transcript in SIMPLE terms for a busy "
    "investor. Write 2-6 plain sentences: what the video is about and the "
    "main takeaways or calls. Be specific about tickers, price targets, and "
    "numbers. No per-speaker breakdown, no headers, no bullet lists — just "
    "the sentences. If the transcript is very short (a Short/clip), one or "
    "two sentences suffice."
)


def load_state():
    if os.path.exists(STATE_PATH):
        return json.load(open(STATE_PATH))
    return {"seen": []}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    state["seen"] = state["seen"][-500:]
    json.dump(state, open(STATE_PATH, "w"), indent=0)


def fetch_feed(channel_id):
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    xml = urllib.request.urlopen(url, timeout=30).read().decode("utf-8", "ignore")
    videos = []
    for entry in re.findall(r"<entry>.*?</entry>", xml, re.S):
        vid = re.search(r"<yt:videoId>([^<]+)</yt:videoId>", entry)
        title = re.search(r"<title>([^<]+)</title>", entry)
        pub = re.search(r"<published>([^<]+)</published>", entry)
        if vid and title and pub:
            videos.append({
                "id": vid.group(1),
                "title": title.group(1),
                "published": datetime.datetime.fromisoformat(pub.group(1)),
            })
    return videos


def fetch_transcript(video_id):
    from youtube_transcript_api import YouTubeTranscriptApi

    snippets = YouTubeTranscriptApi().fetch(video_id)
    return " ".join(s.text for s in snippets)


def main():
    state = load_state()
    seen = set(state["seen"])
    first_run = not seen
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - (
        datetime.timedelta(hours=FIRST_RUN_WINDOW_H) if first_run
        else datetime.timedelta(days=WINDOW_DAYS)
    )

    sections = []
    for channel, cid in CHANNELS.items():
        try:
            videos = fetch_feed(cid)
        except Exception as e:
            print(f"{channel}: feed error {e}")
            continue
        fresh = [v for v in videos if v["id"] not in seen and v["published"] >= cutoff]
        fresh.sort(key=lambda v: v["published"], reverse=True)
        skipped = len(fresh) - MAX_PER_CHANNEL
        if skipped > 0:
            print(f"{channel}: capping at {MAX_PER_CHANNEL}, skipping {skipped}")
        for v in fresh[:MAX_PER_CHANNEL]:
            url = f"https://www.youtube.com/watch?v={v['id']}"
            date = v["published"].strftime("%b %d")
            try:
                transcript = fetch_transcript(v["id"])
                summary = ask_claude(
                    f"{SUMMARY_INSTRUCTIONS}\nChannel: {channel}\n"
                    f"Video title: {v['title']}",
                    transcript,
                )
            except Exception as e:
                print(f"{channel} / {v['title']}: {type(e).__name__}: {e}")
                summary = ("*No captions available (possibly a live stream) — "
                           "watch directly via the link above.*")
            sections.append(
                f"### [{v['title']}]({url})\n"
                f"**{channel}** · {date}\n\n{summary}\n"
            )
            seen.add(v["id"])
            state["seen"].append(v["id"])

    if not sections:
        open("digest.md", "w").write("NO_VIDEOS")
        print("No new videos across all channels.")
        return

    day = now.strftime("%Y-%m-%d")
    digest = f"Daily summaries of new videos from your followed channels.\n\n" + \
             "\n---\n\n".join(sections)
    open("digest.md", "w").write(digest)
    save_state(state)
    print(f"Digest written: {len(sections)} videos for {day}")


if __name__ == "__main__":
    main()
