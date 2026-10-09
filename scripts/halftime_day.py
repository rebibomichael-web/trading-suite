"""The Halftime summary's day key and its never-overwrite archive write.

PROV-HTDATE-01 (ruled by Mike 2026-10-09; class precedent PROV-YTDATE-01,
26a3fce + 78347f5). Before this, the halftime job computed its day TWICE from
the UTC clock (the pipeline's episode target and the deliver step's `date -u`),
`cp`-overwrote summaries/DAY.md + transcripts/DAY.txt, overwrote the day's mp3,
and opened a second issue for a second run on one day. Now:

  * ONE day key per job, computed once (the workflow exports it as HALFTIME_DAY)
    and used at every site: the pipeline's episode target, the audio file, the
    issue title + ask link, both archive files and the commit message;
  * the key is the show's own calendar, America/New_York — NOT Asia/Jerusalem
    as the YT digest uses. Scheduled runs start 3.5–6h late (21:00–23:11 UTC,
    Sep 28–Oct 8), when the Jerusalem date is already the NEXT day: a Jerusalem
    key would poll for tomorrow's episode and quietly write NO_EPISODE. The UTC
    key already lost the 08-27 and 08-28 episodes that way (runs fired 01:30Z
    and 00:58Z, each timed out its 80-min poll). New York midnight is 04:00/05:00
    UTC, ~11h after the 17:20 UTC cron. One constant (TZ) if Mike rules otherwise;
  * a run that lands on an existing day APPENDS, loudly — never overwrites.

    python scripts/halftime_day.py day                  # prints YYYY-MM-DD
    python scripts/halftime_day.py archive SRC DEST     # create or append
    python scripts/halftime_day.py body MD DAY RUN_ID [--later]
                                                        # issue body / later-run comment
"""
import datetime
import os
import sys
from zoneinfo import ZoneInfo

TZ = "America/New_York"


def halftime_day(now=None):
    """YYYY-MM-DD in the show's timezone. HALFTIME_DAY (set once per job) wins."""
    env = os.environ.get("HALFTIME_DAY")
    if env and now is None:
        return env
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return now.astimezone(ZoneInfo(TZ)).strftime("%Y-%m-%d")


def _stamp(now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return now.astimezone(ZoneInfo(TZ)).strftime("%Y-%m-%d %H:%M %Z")


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
    with open(dest, "a") as fh:
        if not open(dest).read().endswith("\n"):
            fh.write("\n")
        fh.write(f"\n---\n\n## Later run — {_stamp(now)}\n\n{text}")
    print(f"::warning::archive: {dest} ALREADY EXISTS — a second run landed on "
          f"day {os.path.basename(dest).rsplit('.', 1)[0]}; APPENDED (never overwrite).")
    return "appended"


def run_marker(run_id):
    """Idempotency marker: a re-run keeps GITHUB_RUN_ID, so one run posts once."""
    return f"<!-- halftime-run: {run_id} -->"


def issue_body(summary_md, day, run_id, later=False, now=None):
    """The issue body (first run of the day) or the COMMENT on that day's
    existing issue (a later run, so it still reaches email). Both carry the
    run marker; the workflow checks it before posting."""
    head = [run_marker(run_id)]
    if later:
        head.append(f"**Later run — {_stamp(now)}** (run {run_id}). This run landed "
                    f"on a day that already has a summary; it is appended to "
                    f"`summaries/{day}.md`.")
    return "\n\n".join(head) + "\n\n" + open(summary_md).read()


def main(argv):
    if argv[:1] == ["day"]:
        print(halftime_day())
        return 0
    if len(argv) == 3 and argv[0] == "archive":
        archive(argv[1], argv[2])
        return 0
    if len(argv) in (4, 5) and argv[0] == "body":      # body MD DAY RUN_ID [--later]
        sys.stdout.write(issue_body(argv[1], argv[2], argv[3],
                                    later=argv[4:] == ["--later"]))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
