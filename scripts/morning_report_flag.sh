#!/usr/bin/env bash
# Desktop flag on the Dell: "morning report ready".
#
# Polls the repo for today's YouTube digest (summaries/youtube/<today>.md,
# pushed by the digest workflow ~09:30-11:00 Israel time) and raises ONE
# desktop notification the first time it appears, with the first line of
# "Today's read" as the preview.
#
# Cron (times are system-local; this box's cron ignores CRON_TZ):
#   */10 9-13 * * * $HOME/trading-suite/scripts/morning_report_flag.sh >> $HOME/morning_report_flag.log 2>&1
#
# Exit 0 always — a quiet no-op until the digest lands, and once flagged the
# per-day marker in ~/.cache/morning-report/ prevents repeats.
set -u

REPO="${1:-$HOME/trading-suite}"
MARKER_DIR="$HOME/.cache/morning-report"
mkdir -p "$MARKER_DIR"
TODAY=$(date +%F)
MARKER="$MARKER_DIR/$TODAY"
[ -f "$MARKER" ] && exit 0

cd "$REPO" || exit 0
git pull -q 2>/dev/null || true

DIGEST="summaries/youtube/$TODAY.md"
[ -f "$DIGEST" ] || exit 0

# notify-send under cron needs the desktop session env (same fix as the LEAP
# watchdog notifications).
export DISPLAY="${DISPLAY:-:0}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$(id -u)/bus}"

LEAD=$(sed -n "s/^\*\*Today's read:\*\* //p" "$DIGEST" | head -1 | cut -c1-220)
[ -n "$LEAD" ] || LEAD="YouTube digest for $TODAY is in — check your email or the repo issues."

# Resolve the exact issue URL for one-click open (gh is authed on this box);
# fall back to a filtered issues search if gh is unavailable.
REPO_SLUG="rebibomichael-web/trading-suite"
URL=""
if command -v gh >/dev/null 2>&1; then
  URL=$(gh issue list --repo "$REPO_SLUG" --state all \
        --search "YouTube digest — $TODAY in:title" \
        --json url --jq '.[0].url' 2>/dev/null)
fi
[ -n "$URL" ] || URL="https://github.com/$REPO_SLUG/issues?q=is%3Aissue+%22YouTube+digest%22+$TODAY"

# Mark BEFORE showing: action-mode notify-send blocks until click/dismiss,
# and later cron ticks must not raise a second banner meanwhile.
touch "$MARKER"

if notify-send --help 2>&1 | grep -q -- '--action'; then
  # Clickable banner: clicking the body fires the "default" action -> open
  # the report in the browser. Critical urgency = stays until acted on;
  # timeout is a safety net if it is never clicked or dismissed.
  ACTION=$(timeout 4h notify-send -u critical -i document-open \
           -A default="Open report" \
           "📰 Morning report ready" "$LEAD" || true)
  if [ "$ACTION" = "default" ]; then
    xdg-open "$URL" >/dev/null 2>&1 &
  fi
else
  notify-send -u critical -t 0 -i document-open \
    "📰 Morning report ready" "$LEAD
$URL"
fi
# keep only the last 14 markers
ls -1t "$MARKER_DIR" 2>/dev/null | tail -n +15 | while read -r f; do rm -f "$MARKER_DIR/$f"; done
exit 0
