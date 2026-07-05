#!/usr/bin/env python3
"""TEMPORARY experiment: which caption-fetch methods work from GitHub runners?

Tests several approaches against a video known to have captions
(vOuhRx-KHnM, verified 18902 chars of captions on 2026-07-04).
"""
import glob
import json
import os
import subprocess
import sys

VIDEO = "vOuhRx-KHnM"
URL = f"https://www.youtube.com/watch?v={VIDEO}"


def report(name, ok, detail):
    print(f"{'PASS' if ok else 'FAIL'} | {name} | {detail}", flush=True)


# (a) control: youtube-transcript-api (expected: RequestBlocked)
try:
    from youtube_transcript_api import YouTubeTranscriptApi
    snippets = list(YouTubeTranscriptApi().fetch(VIDEO))
    report("transcript-api", True, f"{sum(len(s.text) for s in snippets)} chars")
except Exception as e:
    report("transcript-api", False, type(e).__name__)

# (b-d) yt-dlp auto-subs with different player clients
for name, extra in [
    ("yt-dlp default", []),
    ("yt-dlp tv client", ["--extractor-args", "youtube:player_client=tv"]),
    ("yt-dlp web_embedded", ["--extractor-args", "youtube:player_client=web_embedded"]),
    ("yt-dlp android", ["--extractor-args", "youtube:player_client=android"]),
]:
    for f in glob.glob("sub*"):
        os.remove(f)
    r = subprocess.run(
        ["yt-dlp", "--skip-download", "--write-auto-subs", "--write-subs",
         "--sub-langs", "en.*", "--sub-format", "json3", "-o", "sub", URL] + extra,
        capture_output=True, text=True, timeout=120,
    )
    files = glob.glob("sub*")
    if files:
        try:
            data = json.load(open(files[0]))
            n = sum(len(seg.get("utf8", "")) for ev in data.get("events", [])
                    for seg in ev.get("segs", []))
            report(name, n > 1000, f"{files[0]}: {n} chars")
        except Exception as e:
            report(name, False, f"parse error {e}")
    else:
        tail = (r.stderr or r.stdout).strip().splitlines()[-1:] or ["no output"]
        report(name, False, tail[0][:160])

# (e) can we at least get the audio URL (for a whisper fallback)?
r = subprocess.run(["yt-dlp", "-g", "-f", "bestaudio", URL],
                   capture_output=True, text=True, timeout=120)
report("yt-dlp audio url", r.returncode == 0 and r.stdout.startswith("http"),
       (r.stdout[:60] if r.returncode == 0 else (r.stderr.strip().splitlines()[-1:] or [""])[0][:160]))

print("EXPERIMENT COMPLETE")
sys.exit(0)
