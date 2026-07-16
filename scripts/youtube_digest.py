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
import sys
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
FEED_SNAPSHOT_PATH = os.path.join("feeds", "youtube_feed.json")
MAX_PER_CHANNEL = 3          # cost guard: videos summarized per channel per run
FIRST_RUN_WINDOW_H = 36      # without state, only look this far back
WINDOW_DAYS = 4              # with state, ignore fresh feed items older than this
PENDING_CAP_PER_CHANNEL = 30  # bound the carry-over backlog so state can't grow forever
SNAPSHOT_MAX_AGE_H = 72      # ignore a Dell feed snapshot older than this
GRACE_HOURS = 40             # wait this long for captions before settling for the description

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

OVERVIEW_INSTRUCTIONS = (
    "Below are today's per-video summaries from the investor YouTube channels "
    "I follow. Write ONE tight TL;DR paragraph (3-6 sentences; two paragraphs "
    "only if the videos split into genuinely unrelated threads) that "
    "synthesizes ACROSS them the way a sharp market-brief editor would: find "
    "the through-line or tension of the day, play theses against their "
    "counterweights, keep the concrete numbers (price targets, unit counts, "
    "odds, dates, levels), and attribute claims briefly to their channel or "
    "video. Opinionated connective framing is welcome ('the skeptical "
    "counterweight', 'the palate cleanser') but NEVER invent facts that are "
    "not in the summaries. No headers, no bullet lists — flowing prose only."
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


def load_feed_snapshot():
    """Feed snapshot committed daily by the Dell fetcher
    (scripts/fetch_youtube_transcripts.py). Lets the digest keep working when
    YouTube blocks RSS requests from GitHub's runner IPs (observed 2026-07-14:
    404 on every channel feed, two days running)."""
    try:
        with open(FEED_SNAPSHOT_PATH) as fh:
            snap = json.load(fh)
        fetched = datetime.datetime.fromisoformat(snap["fetched_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    age = datetime.datetime.now(datetime.timezone.utc) - fetched
    if age > datetime.timedelta(hours=SNAPSHOT_MAX_AGE_H):
        print(f"WARN: feed snapshot is {age} old — ignoring")
        return None
    channels = {}
    for name, vids in snap.get("channels", {}).items():
        parsed = []
        for v in vids:
            try:
                parsed.append({
                    "id": v["id"],
                    "title": v["title"],
                    "published": datetime.datetime.fromisoformat(v["published"]),
                    "description": v.get("description", ""),
                })
            except (KeyError, TypeError, ValueError):
                continue
        channels[name] = parsed
    return channels


def transcript_path(video_id):
    return os.path.join("transcripts", "youtube", f"{video_id}.txt")


def fetch_transcript(video_id):
    """Return the transcript text for a video.

    Prefers a transcript pre-fetched on a residential IP (e.g. the Dell, via
    scripts/fetch_youtube_transcripts.py) and committed to
    transcripts/youtube/<id>.txt — this sidesteps YouTube's block on caption
    requests from cloud IPs like GitHub's runners. Only if no pre-fetched file
    exists does it fetch directly (optionally through a Webshare residential
    proxy when WEBSHARE_PROXY_USERNAME/PASSWORD are set), caching the result in
    the same raw-text file for reuse and audit."""
    cached = transcript_path(video_id)
    if os.path.exists(cached):
        with open(cached) as fh:
            text = fh.read().strip()
        if text:
            return text

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
    text = " ".join(s.text for s in snippets)

    os.makedirs(os.path.dirname(cached), exist_ok=True)
    with open(cached, "w") as fh:
        fh.write(text)
    return text


def summarize_video(channel, v, now):
    """Summarize one video. Returns (status, section):
      ("ok", section)  — summary produced; caller marks the video seen.
      ("defer", None)  — no transcript yet and the video is younger than
                          GRACE_HOURS; caller keeps it pending so a later run
                          (after the next Dell fetch) can use the real
                          transcript instead of burning a shallow fallback.
      ("retry", None)  — Claude was unreachable; caller keeps it pending.

    Only once the grace period has passed does a missing transcript fall back
    to the title/description summary and count as done.
    """
    url = f"https://www.youtube.com/watch?v={v['id']}"
    date = v["published"].strftime("%b %d")

    transcript = None
    try:
        transcript = fetch_transcript(v["id"])
    except Exception as e:
        print(f"{channel} / {v['title']}: no transcript ({e!r})")
        transcript = None

    if transcript is None:
        age = now - v["published"]
        if age < datetime.timedelta(hours=GRACE_HOURS):
            print(f"{channel} / {v['title']}: no transcript yet "
                  f"({age.total_seconds() / 3600:.0f}h old) — deferring until "
                  f"the next transcript fetch")
            return "defer", None

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
        return "retry", None

    if not summary:
        return "retry", None
    return "ok", (
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
    snapshot = None
    snapshot_loaded = False
    channels_unavailable = 0
    for channel, cid in CHANNELS.items():
        carried = pending_by_channel.get(channel, [])
        try:
            videos = fetch_feed(cid)
        except Exception as e:
            # YouTube blocks RSS from some cloud IPs — fall back to the feed
            # snapshot the Dell commits daily from a residential IP.
            if not snapshot_loaded:
                snapshot = load_feed_snapshot()
                snapshot_loaded = True
            if snapshot and channel in snapshot:
                videos = snapshot[channel]
                print(f"{channel}: live feed failed ({e!r}) — using the Dell feed snapshot")
            else:
                print(f"{channel}: feed error {e!r} (no usable snapshot)")
                channels_unavailable += 1
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
            status, section = summarize_video(channel, v, now)
            if status != "ok":
                leftover.append(v)          # deferred/transient — retry next run
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
        # Editorial lead: one synthesized cross-video paragraph. Best-effort —
        # a failure here must never cost the digest itself.
        overview = None
        if len(sections) >= 2:
            try:
                overview = ask_claude(OVERVIEW_INSTRUCTIONS,
                                      "\n\n---\n\n".join(sections)).strip()
            except Exception as e:
                print(f"WARN: overview generation failed: {e!r}")
        digest = "Daily summaries of new videos from your followed channels.\n\n"
        if overview:
            digest += f"**Today's read:** {overview}\n\n---\n\n"
        digest += "\n---\n\n".join(sections)
        open("digest.md", "w").write(digest)
        print(f"Digest written: {len(sections)} videos for {day}"
              + (" (with editorial lead)" if overview else ""))
    else:
        open("digest.md", "w").write("NO_VIDEOS")
        print("No new videos across all channels.")

    # Always persist: even on a NO_VIDEOS day the pending backlog may have changed.
    save_state(state)

    # A run that couldn't see ANY channel (live or snapshot) must not look like
    # a quiet success — fail it so the outage is visible.
    if channels_unavailable == len(CHANNELS):
        print("ERROR: every channel feed failed and no usable snapshot exists — "
              "failing the run instead of reporting a silent empty digest")
        sys.exit(1)


if __name__ == "__main__":
    main()
