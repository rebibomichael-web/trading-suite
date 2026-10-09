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
    python scripts/halftime_day.py late DAY RUN_ID      # ::warning:: if the run
                                                        # started after its show day

Missed-episode warning (ruled by Mike 2026-10-09, PROV-HTDATE-01 follow-up):
a scheduled run's SHOW DAY is the latest weekday whose 17:20 UTC cron slot has
passed. If GitHub's lag pushes the start past that day's New York midnight,
the key has already rolled to a day with no episode yet — the 80-min poll
finds nothing and NO_EPISODE exits green. `late` makes that loud instead.
"""
import datetime
import os
import sys
from zoneinfo import ZoneInfo

TZ = "America/New_York"
CRON_UTC = (17, 20)        # halftime-summary.yml schedule '20 17 * * 1-5'


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


def show_day(now=None):
    """The weekday whose scheduled slot this run belongs to: the latest
    Mon–Fri date D with D CRON_UTC <= now (UTC)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    d = now.astimezone(datetime.timezone.utc).date()
    while True:
        slot = datetime.datetime(d.year, d.month, d.day, *CRON_UTC,
                                 tzinfo=datetime.timezone.utc)
        if d.weekday() < 5 and slot <= now:
            return d.isoformat()
        d -= datetime.timedelta(days=1)


def late_warning(day, run_id, now=None):
    """None if the job key DAY is the run's show day; else the warning text
    naming the show day, the UTC start and the run id."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    show = show_day(now)
    if day == show:
        return None
    return (f"HALFTIME RUN STARTED AFTER ITS SHOW DAY ENDED — show day {show} "
            f"(America/New_York) ended before this run started at UTC "
            f"{now.astimezone(datetime.timezone.utc):%Y-%m-%dT%H:%M:%SZ} "
            f"(run {run_id}); the job key is {day}, so the poll targets a day "
            f"with no episode yet. Episode {show} is likely MISSED — backfill "
            f"it with a workflow_dispatch date={show}.")


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
    if len(argv) == 3 and argv[0] == "late":            # late DAY RUN_ID
        msg = late_warning(argv[1], argv[2])
        if msg:
            print(f"::warning::{msg}")
            summ = os.environ.get("GITHUB_STEP_SUMMARY")
            if summ:
                with open(summ, "a") as fh:
                    fh.write(msg + "\n")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
