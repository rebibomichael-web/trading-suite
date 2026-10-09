"""The YouTube digest's day key and its never-overwrite archive write.

Ruled by Mike 2026-10-09 (PROV-YTDATE-01): GitHub's schedule lag put two runs on
UTC 2026-10-06, and the second `cp` clobbered the first run's archive file and
audio (7 summaries lost until restored). So:

  * the day key is the Asia/Jerusalem date, computed ONCE per job (the workflow
    exports it as DIGEST_DAY) and used by all four sites: the archive file, the
    audio file, the issue title, and youtube_digest.py's `day`;
  * a run that lands on an existing day-file APPENDS to it, loudly — it never
    overwrites. A calendar key can still collide when the fire time jitters
    across its day boundary; appending makes that collision harmless.

    python scripts/digest_day.py day                  # prints YYYY-MM-DD
    python scripts/digest_day.py archive SRC DEST     # create or append
"""
import datetime
import os
import sys
from zoneinfo import ZoneInfo

TZ = "Asia/Jerusalem"


def digest_day(now=None):
    """YYYY-MM-DD in Asia/Jerusalem. DIGEST_DAY (set once per job) wins."""
    env = os.environ.get("DIGEST_DAY")
    if env and now is None:
        return env
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return now.astimezone(ZoneInfo(TZ)).strftime("%Y-%m-%d")


def archive(src, dest, now=None):
    """Write SRC's text to DEST; if DEST exists, append under a dated rule.
    Returns "created" or "appended"."""
    text = open(src).read()
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    if not os.path.exists(dest):
        with open(dest, "w") as fh:
            fh.write(text)
        print(f"archive: created {dest}")
        return "created"
    now = now or datetime.datetime.now(datetime.timezone.utc)
    stamp = now.astimezone(ZoneInfo(TZ)).strftime("%Y-%m-%d %H:%M %Z")
    with open(dest, "a") as fh:
        if not open(dest).read().endswith("\n"):
            fh.write("\n")
        fh.write(f"\n---\n\n## Later run — {stamp}\n\n{text}")
    print(f"::warning::archive: {dest} ALREADY EXISTS — a second run landed on "
          f"day {os.path.basename(dest)[:-3]}; APPENDED its digest (never overwrite).")
    return "appended"


def main(argv):
    if argv[:1] == ["day"]:
        print(digest_day())
        return 0
    if len(argv) == 3 and argv[0] == "archive":
        archive(argv[1], argv[2])
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
