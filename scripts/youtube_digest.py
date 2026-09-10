#!/usr/bin/env python3
"""Daily YouTube digest: simple summaries of new videos from followed channels.

For each channel, reads the channel's RSS feed, finds videos not yet seen,
pulls the auto-generated captions, and asks Claude for a short plain-language
summary (no per-speaker breakdown). Writes digest.md for the workflow to
deliver, and updates summaries/youtube/state.json so videos are only
summarized once.

Every run's digest.md starts with a channel roster listing ALL keys in
CHANNELS as Covered / Quiet / Missing, so a quiet channel never disappears
from the GitHub issue (and the notification email). Days with no new videos
still write that roster instead of a bare NO_VIDEOS marker, so the issue
path can fire.

To keep cost bounded, only MAX_PER_CHANNEL videos are summarized per channel
per run. Any surplus is remembered in state["pending"] and summarized on a
later run (oldest first), so a busy day or a skipped run never silently drops
videos.

`python scripts/youtube_digest.py --self-check` builds a roster from fake
per-channel results and checks that all 8 channels are listed.

Exit codes: 0 = digest written (including an all-Quiet roster-only day),
nonzero = real failure.
"""
import datetime
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request

from halftime_pipeline import ask_claude, preflight_auth

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

# Roster statuses. Quiet is never Missing: a reachable feed with nothing new
# (or nothing due yet) is Quiet even if the channel is absent from the body.
ROSTER_COVERED = "Covered"
ROSTER_QUIET = "Quiet"
ROSTER_MISSING = "Missing"


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


def ask_claude_link(label, title, body):
    """A tappable "ask a question" shortcut. GitHub issues can't host real
    buttons, but a pre-filled new-issue link is one tap away: the @claude
    mention, video context, and links are pre-filled — the reader only types
    the question. The Q&A workflow (claude.yml) triggers on owner-opened
    issues containing @claude, with the full transcript archive available."""
    repo = os.environ.get("GITHUB_REPOSITORY", "rebibomichael-web/trading-suite")
    return (f"*[{label}](https://github.com/{repo}/issues/new"
            f"?title={urllib.parse.quote(title)}"
            f"&body={urllib.parse.quote(body)})*")


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
    ask = ask_claude_link(
        "💬 Ask Claude about this video",
        f"Q: {v['title'][:70]}",
        f"@claude Regarding \"{v['title']}\" by {channel} ({url}):\n\n",
    )
    return "ok", (
        f"### [{v['title']}]({url})\n"
        f"**{channel}** · {date}\n\n{summary}\n\n{ask}\n"
    )


def classify_channel(*, summarized, feed_ok, dropped=0):
    """One channel's Covered / Quiet / Missing status for today's roster.

    Covered — at least one video from this channel is in today's digest body.
    Quiet   — feed reachable; zero new videos in the digest window, or all
              already seen / deferred to pending / still inside GRACE_HOURS.
              A deferred ("not due") video is Quiet, never Missing.
    Missing — only a real gap: the feed was unreachable (live RSS and the
              Dell snapshot both failed), or in-window video(s) were dropped
              from the pending cap and will never be summarized. That second
              case is the same "genuinely lost" idea as youtube_coverage.py's
              MISSING; feed-unreachable is listed here because Quiet
              requires a reachable feed and the roster must still name the
              channel.
    """
    if summarized > 0:
        return ROSTER_COVERED
    if not feed_ok or dropped > 0:
        return ROSTER_MISSING
    return ROSTER_QUIET


def format_roster(results, window_days=WINDOW_DAYS):
    """Markdown listing every CHANNELS key. Quiet channels are not omitted.

    `results` is a dict of channel name -> {
        "status": "Covered"|"Quiet"|"Missing",
        "n": int,       # videos summarized this run (Covered)
        "note": str,    # optional; shown for Missing
    }
    Channels missing from `results` still appear as Quiet so a partial
    dict cannot hide anyone from the issue/email.
    """
    counts = {ROSTER_COVERED: 0, ROSTER_QUIET: 0, ROSTER_MISSING: 0}
    lines = []
    for name in CHANNELS:
        info = results.get(name) or {}
        status = info.get("status") or ROSTER_QUIET
        if status not in counts:
            status = ROSTER_QUIET
        counts[status] += 1
        extra = ""
        if status == ROSTER_COVERED:
            n = int(info.get("n") or 0)
            if n:
                extra = f" ({n} video{'s' if n != 1 else ''})"
        elif status == ROSTER_MISSING and info.get("note"):
            extra = f" — {info['note']}"
        lines.append(f"- **{name}** — {status}{extra}")
    header = (
        f"## Channel roster\n\n"
        f"{counts[ROSTER_COVERED]} Covered · {counts[ROSTER_QUIET]} Quiet · "
        f"{counts[ROSTER_MISSING]} Missing — last {window_days} days. "
        f"Quiet = feed reachable, nothing new (or already seen / not due). "
        f"Missing = in-window video not summarized and not queued, or the "
        f"feed was unreachable. Quiet is never Missing.\n"
    )
    return header + "\n" + "\n".join(lines) + "\n"


def compose_digest(*, roster_md, sections, overview=None, ask_all=None):
    """Assemble digest.md: intro, roster (near the top), optional overview,
    then the existing per-video sections. Empty `sections` still produces a
    short digest with the full roster — never the bare NO_VIDEOS marker —
    so the workflow can open an issue and the email still lists all 8.
    """
    chunks = [
        "Daily summaries of new videos from your followed channels.\n",
        roster_md.strip(),
        "",
    ]
    if overview:
        chunks.append(f"**Today's read:** {overview}")
        if ask_all:
            chunks.append("")
            chunks.append(ask_all)
        chunks.append("")
    if sections:
        chunks.append("---")
        chunks.append("")
        chunks.append("\n---\n\n".join(sections))
    else:
        chunks.append("No new videos to summarize today.")
    text = "\n".join(chunks)
    if not text.endswith("\n"):
        text += "\n"
    return text


def self_check():
    """Build roster markdown from fake per-channel results; require all 8."""
    errors = []
    cases = [
        (dict(summarized=2, feed_ok=True), ROSTER_COVERED),
        (dict(summarized=1, feed_ok=False), ROSTER_COVERED),
        (dict(summarized=0, feed_ok=True, dropped=0), ROSTER_QUIET),
        (dict(summarized=0, feed_ok=True, dropped=1), ROSTER_MISSING),
        (dict(summarized=0, feed_ok=False), ROSTER_MISSING),
        (dict(summarized=3, feed_ok=True, dropped=2), ROSTER_COVERED),
    ]
    for kwargs, expected in cases:
        got = classify_channel(**kwargs)
        if got != expected:
            errors.append(f"classify_channel({kwargs}) -> {got!r}, want {expected!r}")
    if classify_channel(summarized=0, feed_ok=True) != ROSTER_QUIET:
        errors.append("reachable feed with nothing due must be Quiet, not Missing")

    if len(CHANNELS) != 8:
        errors.append(f"expected 8 CHANNELS, got {len(CHANNELS)}")

    fake = {
        "Brighter with Herbert": {"status": ROSTER_COVERED, "n": 2},
        "Matt Pocius on Tesla Stock & Money": {"status": ROSTER_QUIET},
        "Fundstrat": {"status": ROSTER_COVERED, "n": 1},
        "Fundstrat Capital": {"status": ROSTER_QUIET},
        "Mr. FIRED Up Wealth": {
            "status": ROSTER_MISSING, "note": "feed unavailable",
        },
        "Kaspa Silver": {"status": ROSTER_QUIET},
        # Wicked Stocks omitted on purpose — must still appear as Quiet.
        "Traders Helping Traders": {"status": ROSTER_COVERED, "n": 3},
    }
    md = format_roster(fake)
    for name in CHANNELS:
        if f"**{name}**" not in md:
            errors.append(f"roster omitted {name!r}")
    if "**Wicked Stocks** — Quiet" not in md:
        errors.append("omitted channel must still list as Quiet")
    if "feed unavailable" not in md:
        errors.append("Missing note not shown")
    if not md.lstrip().startswith("## Channel roster"):
        errors.append("roster markdown should start with the Channel roster heading")

    empty = format_roster({})
    quiet_hits = sum(1 for name in CHANNELS
                     if f"**{name}** — Quiet" in empty)
    if quiet_hits != 8:
        errors.append(f"empty results should list 8 Quiet, got {quiet_hits}")

    video = (
        "### [Fake Video](https://www.youtube.com/watch?v=abc)\n"
        "**Brighter with Herbert** · Sep 01\n\nHello.\n"
    )
    digest = compose_digest(
        roster_md=md,
        sections=[video],
        overview="Tesla tape.",
        ask_all="*ask*",
    )
    if digest.strip() == "NO_VIDEOS":
        errors.append("digest with sections must not be the NO_VIDEOS marker")
    roster_at = digest.find("## Channel roster")
    overview_at = digest.find("**Today's read:**")
    video_at = digest.find("### [Fake Video]")
    if roster_at < 0 or roster_at > 120:
        errors.append("roster should appear near the top of the digest")
    if not (0 <= roster_at < overview_at < video_at):
        errors.append("expected order: roster, then overview, then video sections")

    quiet_digest = compose_digest(roster_md=empty, sections=[])
    if quiet_digest.strip() == "NO_VIDEOS":
        errors.append("zero-video digest must not be the NO_VIDEOS marker")
    for name in CHANNELS:
        if f"**{name}**" not in quiet_digest:
            errors.append(f"zero-video digest omitted {name!r}")
    if "No new videos to summarize today." not in quiet_digest:
        errors.append("zero-video digest should say nothing was summarized")

    if errors:
        print("self-check FAILED:")
        for err in errors:
            print(f"  - {err}")
        return 1
    print("self-check ok")
    print("--- sample mixed roster ---")
    print(md)
    print("--- sample zero-video digest ---")
    print(quiet_digest)
    return 0


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
    roster_results = {}
    for channel, cid in CHANNELS.items():
        carried = pending_by_channel.get(channel, [])
        covered_n = 0
        dropped_n = 0
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
                roster_results[channel] = {
                    "status": classify_channel(
                        summarized=0, feed_ok=False, dropped=0),
                    "note": "feed unavailable",
                }
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
            covered_n += 1
            seen.add(v["id"])
            state["seen"].append(v["id"])

        if len(leftover) > PENDING_CAP_PER_CHANNEL:
            leftover.sort(key=lambda v: v["published"])
            drop = len(leftover) - PENDING_CAP_PER_CHANNEL
            print(f"{channel}: backlog over cap, dropping {drop} oldest video(s)")
            leftover = leftover[drop:]
            dropped_n = drop
        elif leftover:
            print(f"{channel}: {len(leftover)} video(s) deferred to backlog")
        new_pending.extend(leftover)
        roster_status = classify_channel(
            summarized=covered_n, feed_ok=True, dropped=dropped_n)
        entry = {"status": roster_status, "n": covered_n}
        if roster_status == ROSTER_MISSING and dropped_n:
            entry["note"] = f"dropped {dropped_n} over pending cap"
        roster_results[channel] = entry

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
    roster_md = format_roster(roster_results, window_days=WINDOW_DAYS)

    overview = None
    ask_all = None
    if len(sections) >= 2:
        # Editorial lead: one synthesized cross-video paragraph. Best-effort —
        # a failure here must never cost the digest itself.
        try:
            overview = ask_claude(OVERVIEW_INSTRUCTIONS,
                                  "\n\n---\n\n".join(sections)).strip()
        except Exception as e:
            print(f"WARN: overview generation failed: {e!r}")
        if overview:
            ask_all = ask_claude_link(
                "💬 Ask Claude about today's digest",
                f"Q: YouTube digest {day}",
                f"@claude Regarding the {day} YouTube digest:\n\n",
            )

    digest = compose_digest(
        roster_md=roster_md,
        sections=sections,
        overview=overview,
        ask_all=ask_all,
    )
    open("digest.md", "w").write(digest)
    print(f"Digest written: {len(sections)} videos for {day}"
          + (" (with editorial lead)" if overview else "")
          + ("" if sections else " (roster only — no new videos)"))

    # Always persist: even on a roster-only day the pending backlog may have changed.
    save_state(state)

    # A run that couldn't see ANY channel (live or snapshot) must not look like
    # a quiet success — fail it so the outage is visible. The roster was still
    # written (all Missing) so a human inspecting digest.md can see why.
    if channels_unavailable == len(CHANNELS):
        print("ERROR: every channel feed failed and no usable snapshot exists — "
              "failing the run instead of reporting a silent empty digest")
        sys.exit(1)


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        sys.exit(self_check())
    main()
