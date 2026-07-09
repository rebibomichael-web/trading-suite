#!/usr/bin/env python3
"""Daily YouTube digest: simple summaries of new videos from followed channels.

For each channel, reads the channel's RSS feed, finds videos not yet seen,
pulls the auto-generated captions, and asks Claude for a short plain-language
summary (no per-speaker breakdown). Writes digest.md for the workflow to
deliver, and updates summaries/youtube/state.json so videos are only
summarized once.

To keep cost bounded, only MAX_PER_CHANNEL videos are summarized per channel
per run. Any surplus is remembered in state["pending"] and summarized on a
later run (oldest first), so a busy day or a skipped run never silently drops
videos.

Exit codes: 0 = digest written (or NO_VIDEOS marker if nothing new),
nonzero = real failure.
"""
import datetime
import html
import json
import os
import re
import urllib.request

from halftime_pipeline import ask_claude, preflight_auth

CHANNELS = {
    "Brighter with Herbert": "UC4DBLlq1x0AKmip1QJUcbXg",
    "Matt Pocius on Tesla Stock & Money": "UCF1iS7Bp9_hsQphNF6o8qwQ",
    "Fundstrat": "UCcBzKSM4A-pIHMJWSnxmi_g",
    "Fundstrat Capital": "UCQxFhbPxp6VtAMGEF8OWG5g",
    "Mr. FIRED Up Wealth": "UCqqHGGPbhISeKkpEx8676sw",
    "Kaspa Silver": "UCv8-2oyrfqDigJAKjZ_RCzQ",
}

STATE_PATH = "summaries/youtube/state.json"
MAX_PER_CHANNEL = 3          # cost guard: videos summarized per channel per run
FIRST_RUN_WINDOW_H = 36      # without state, only look this far back
WINDOW_DAYS = 4              # with state, ignore fresh feed items older than this
PENDING_CAP_PER_CHANNEL = 30  # bound the carry-over backlog so state can't grow forever

SUMMARY_INSTRUCTIONS = (
    "Summarize this YouTube video transcript in SIMPLE terms for a busy "
    "investor. Write 2-6 plain sentences: what the video is about and the "
    "main takeaways or calls. Be specific about tickers, price targets, and "
    "numbers. No per-speaker breakdown, no headers, no bullet lists — just "
    "the sentences. If the transcript is very short (a Short/clip), one or "
    "two sentences suffice."
)

FALLBACK_INSTRUCTIONS = (
    "Captions were unavailable for this YouTube video, so summarize what it "
    "is about in 1-3 plain sentences using ONLY its title and description "
    "below. Do not invent specifics that are not stated."
)


def load_state():
    """Load state, tolerating a missing or corrupt file (a truncated write
    must not brick every future run)."""
    data = {}
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH) as fh:
                data = json.load(fh)
        except (ValueError, OSError) as e:
            print(f"WARN: state.json unreadable ({e!r}); starting fresh")
            data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("seen", [])
    data.setdefault("pending", [])
    return data


def save_state(state):
    """Persist state atomically: write a temp file then rename, so a crash
    mid-write leaves the previous good state intact."""
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    state["seen"] = state["seen"][-500:]
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=0)
    os.replace(tmp, STATE_PATH)


def fetch_feed(channel_id):
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    xml = urllib.request.urlopen(url, timeout=30).read().decode("utf-8", "ignore")
    videos = []
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
            # one malformed timestamp shouldn't drop the whole channel
            continue
        videos.append({
            "id": vid.group(1),
            "title": html.unescape(title.group(1)),
            "published": published,
            "description": html.unescape(desc.group(1).strip()) if desc else "",
        })
    return videos


def fetch_transcript(video_id):
    """Fetch captions; uses a Webshare rotating residential proxy when the
    WEBSHARE_PROXY_USERNAME/PASSWORD secrets are set (YouTube blocks direct
    requests from cloud IPs like GitHub's runners)."""
    from youtube_transcript_api import YouTubeTranscriptApi

    user = os.environ.get("WEBSHARE_PROXY_USERNAME")
    password = os.environ.get("WEBSHARE_PROXY_PASSWORD")
    if user and password:
        from youtube_transcript_api.proxies import WebshareProxyConfig
        api = YouTubeTranscriptApi(
            proxy_config=WebshareProxyConfig(proxy_username=user,
                                             proxy_password=password))
    else:
        api = YouTubeTranscriptApi()
    snippets = api.fetch(video_id)
    return " ".join(s.text for s in snippets)


def summarize_video(channel, v):
    """Return a rendered digest section for one video, or None if the summary
    could not be produced this run because Claude was unreachable (so the
    caller keeps the video pending and retries next run).

    A missing transcript is NOT a transient failure: we fall back to the
    title/description summary and the video is considered done.
    """
    url = f"https://www.youtube.com/watch?v={v['id']}"
    date = v["published"].strftime("%b %d")

    transcript = None
    try:
        transcript = fetch_transcript(v["id"])
        os.makedirs("transcripts/youtube", exist_ok=True)
        with open(f"transcripts/youtube/{v['id']}.txt", "w") as tf:
            tf.write(f"{channel} — {v['title']} ({v['published']:%Y-%m-%d})\n\n")
            tf.write(transcript)
    except Exception as e:
        print(f"{channel} / {v['title']}: no transcript ({e!r}) — using description")
        transcript = None

    try:
        if transcript:
            summary = ask_claude(
                f"{SUMMARY_INSTRUCTIONS}\nChannel: {channel}\n"
                f"Video title: {v['title']}",
                transcript,
            )
        elif v.get("description"):
            gist = ask_claude(
                f"{FALLBACK_INSTRUCTIONS}\n"
                f"Channel: {channel}\nVideo title: {v['title']}",
                v["description"][:4000],
            )
            summary = f"{gist}\n\n*(based on the video description — captions unavailable)*"
        else:
            summary = ("*No captions or description available — "
                       "watch via the link above.*")
    except Exception as e:
        # Claude itself failed — transient. Leave the video pending to retry.
        print(f"{channel} / {v['title']}: summary generation failed, will retry: {e!r}")
        return None

    if not summary:
        return None
    return (
        f"### [{v['title']}]({url})\n"
        f"**{channel}** · {date}\n\n{summary}\n"
    )


def main():
    preflight_auth()
    state = load_state()
    seen = set(state["seen"])

    # Carry-over backlog from previous runs, grouped by channel. These are
    # summarized regardless of the freshness window (they were already in it).
    pending_by_channel = {}
    for item in state.get("pending", []):
        try:
            item = dict(item)
            item["published"] = datetime.datetime.fromisoformat(item["published"])
        except (KeyError, TypeError, ValueError):
            continue
        if item.get("id") in seen:
            continue
        pending_by_channel.setdefault(item.get("channel"), []).append(item)

    first_run = not seen and not pending_by_channel
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - (
        datetime.timedelta(hours=FIRST_RUN_WINDOW_H) if first_run
        else datetime.timedelta(days=WINDOW_DAYS)
    )

    sections = []
    new_pending = []
    for channel, cid in CHANNELS.items():
        carried = pending_by_channel.get(channel, [])
        try:
            videos = fetch_feed(cid)
        except Exception as e:
            print(f"{channel}: feed error {e!r}")
            new_pending.extend(carried)  # don't lose the backlog on a feed hiccup
            continue

        carried_ids = {x["id"] for x in carried}
        fresh = [v for v in videos
                 if v["id"] not in seen
                 and v["id"] not in carried_ids
                 and v["published"] >= cutoff]
        for v in fresh:
            v["channel"] = channel

        # Oldest first: summarize the videos closest to aging out before the
        # newer ones, so a per-run cap never permanently strands the old ones.
        candidates = carried + fresh
        candidates.sort(key=lambda v: v["published"])
        chosen = candidates[:MAX_PER_CHANNEL]
        leftover = candidates[MAX_PER_CHANNEL:]

        for v in chosen:
            section = summarize_video(channel, v)
            if section is None:
                leftover.append(v)          # transient failure — retry next run
                continue
            sections.append(section)
            seen.add(v["id"])
            state["seen"].append(v["id"])

        if len(leftover) > PENDING_CAP_PER_CHANNEL:
            leftover.sort(key=lambda v: v["published"])
            drop = len(leftover) - PENDING_CAP_PER_CHANNEL
            print(f"{channel}: backlog over cap, dropping {drop} oldest video(s)")
            leftover = leftover[drop:]
        elif leftover:
            print(f"{channel}: {len(leftover)} video(s) deferred to backlog")
        new_pending.extend(leftover)

    state["pending"] = [
        {
            "id": v["id"],
            "channel": v.get("channel", ""),
            "title": v["title"],
            "published": v["published"].isoformat(),
            "description": v.get("description", ""),
        }
        for v in new_pending
    ]

    day = now.strftime("%Y-%m-%d")
    if sections:
        digest = "Daily summaries of new videos from your followed channels.\n\n" + \
                 "\n---\n\n".join(sections)
        open("digest.md", "w").write(digest)
        print(f"Digest written: {len(sections)} videos for {day}")
    else:
        open("digest.md", "w").write("NO_VIDEOS")
        print("No new videos across all channels.")

    # Always persist: even on a NO_VIDEOS day the pending backlog may have changed.
    save_state(state)


if __name__ == "__main__":
    main()
