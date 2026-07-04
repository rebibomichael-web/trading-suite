#!/usr/bin/env python3
"""Daily CNBC Halftime Report summarizer.

Polls the show's podcast RSS feed for today's episode, transcribes it with
faster-whisper, summarizes with the Claude API, and writes the summary to
summary.md for the GitHub Actions workflow to deliver.

Exit codes: 0 = summary written (or no episode today — writes NO_EPISODE
marker instead), nonzero = real failure.
"""
import datetime
import os
import re
import subprocess
import sys
import time
import urllib.request

FEED_URL = "https://feeds.simplecast.com/qltQrd_8"
POLL_MINUTES = 80          # show can post 1:07-1:50pm ET; covers EST/EDT drift
POLL_INTERVAL_SEC = 300
ANTHROPIC_MODEL = "claude-sonnet-5"

SUMMARY_PROMPT = """You are summarizing today's episode of CNBC's Halftime Report
(host Scott Wapner and the Investment Committee) from an auto-generated
transcript. Regular panelists include Josh Brown, Stephanie Link, Jim Lebenthal,
Joe Terranova, Jenny Harrington, Malcolm Etheridge, Kevin Simpson, Bill Baruch,
Shannon Saccocia, Jason Snipe, Rob Sechan, and Steve Weiss — the transcript may
misspell names (e.g. "Wopner" = Wapner); silently correct them.

Format the summary EXACTLY like this:
1. Title line: episode title and date.
2. "Overview:" — 2-3 plain, simple sentences on the episode's main themes.
3. A bullet list of who said what: one bullet per panelist/guest with their key
   calls, trades, price targets, and notable quotes. Include a bullet for any
   featured segments (Calls of the Day, Best Stocks in the Market, guest
   interviews) and always end with a Final Trades bullet listing each
   panelist's pick.
Be specific about tickers, numbers, and targets. Keep it tight and readable.

TRANSCRIPT:
"""


def fetch(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "halftime-summary/1.0"})
    return urllib.request.urlopen(req, timeout=timeout).read()


def find_todays_episode(target_date):
    """Return (title, mp3_url) for the episode matching target_date, else None."""
    xml = fetch(FEED_URL).decode("utf-8", "ignore")
    m_d_yy = f"{target_date.month}/{target_date.day}/{target_date.strftime('%y')}"
    for item in re.findall(r"<item>.*?</item>", xml, re.S)[:5]:
        title_m = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", item)
        url_m = re.search(r'<enclosure[^>]*url="([^"]+)"', item)
        pub_m = re.search(r"<pubDate>(.*?)</pubDate>", item)
        if not (title_m and url_m and pub_m):
            continue
        title = title_m.group(1).strip()
        pub = datetime.datetime.strptime(
            pub_m.group(1).strip(), "%a, %d %b %Y %H:%M:%S %z"
        ).date()
        if m_d_yy in title or pub == target_date:
            return title, url_m.group(1).replace("&amp;", "&")
    return None


def transcribe(mp3_path):
    from faster_whisper import WhisperModel

    model = WhisperModel("small", device="cpu", compute_type="int8", cpu_threads=4)
    segments, _ = model.transcribe(mp3_path, language="en", vad_filter=True)
    return "\n".join(seg.text.strip() for seg in segments)


def ask_claude(instructions, content):
    """Run a summarization prompt via whichever credential is configured.

    ANTHROPIC_API_KEY        -> Claude API (pay-as-you-go)
    CLAUDE_CODE_OAUTH_TOKEN  -> Claude Code CLI using the user's Claude
                                subscription (no extra cost)
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        return _ask_api(instructions, content)
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return _ask_cli(instructions, content)
    raise RuntimeError(
        "Set the ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN repo secret"
    )


def _ask_api(instructions, content):
    import json

    body = json.dumps({
        "model": ANTHROPIC_MODEL,
        "max_tokens": 3000,
        "messages": [{"role": "user", "content": f"{instructions}\n\n{content}"}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "content-type": "application/json",
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
        },
    )
    resp = json.loads(urllib.request.urlopen(req, timeout=300).read())
    return "".join(block["text"] for block in resp["content"] if block["type"] == "text")


def _ask_cli(instructions, content):
    result = subprocess.run(
        ["claude", "-p", f"{instructions}\n\nThe transcript follows on stdin."],
        input=content,
        capture_output=True,
        text=True,
        timeout=900,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI failed: {result.stderr[:500]}")
    return result.stdout.strip()


def summarize(transcript, title):
    return ask_claude(f"{SUMMARY_PROMPT}\nEpisode title: {title}", transcript)


def main():
    # Optional override for back-catalog runs: HALFTIME_DATE=YYYY-MM-DD
    override = os.environ.get("HALFTIME_DATE", "").strip()
    if override:
        target = datetime.date.fromisoformat(override)
        episode = find_todays_episode(target)
        if not episode:
            print(f"No episode found for {target}", file=sys.stderr)
            sys.exit(1)
    else:
        target = datetime.datetime.now(datetime.timezone.utc).date()
        deadline = time.time() + POLL_MINUTES * 60
        episode = find_todays_episode(target)
        while not episode and time.time() < deadline:
            print(f"Episode for {target} not posted yet; waiting...")
            time.sleep(POLL_INTERVAL_SEC)
            episode = find_todays_episode(target)
        if not episode:
            # Market holiday or preempted show — succeed quietly.
            open("summary.md", "w").write("NO_EPISODE")
            print(f"No episode for {target} after {POLL_MINUTES} min — assuming holiday.")
            return

    title, mp3_url = episode
    print(f"Episode: {title}")
    open("episode.mp3", "wb").write(fetch(mp3_url, timeout=300))
    print("Downloaded; transcribing (~20 min)...")
    transcript = transcribe("episode.mp3")
    print(f"Transcript: {len(transcript)} chars; summarizing...")
    summary = summarize(transcript, title)
    open("summary.md", "w").write(summary)
    open("transcript.txt", "w").write(transcript)
    print("Summary written to summary.md")


if __name__ == "__main__":
    main()
