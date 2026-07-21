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

notify-send -u normal -i document-open "📰 Morning report ready" "$LEAD"
touch "$MARKER"
# keep only the last 14 markers
ls -1t "$MARKER_DIR" 2>/dev/null | tail -n +15 | while read -r f; do rm -f "$MARKER_DIR/$f"; done
exit 0
