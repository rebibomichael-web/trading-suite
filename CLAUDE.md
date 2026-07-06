# Trading Suite — context for Claude

Consolidation of three trading projects into one Flask app with tabbed modules.
Read this first in any session on this repo.

## Modules
- `leap/nightly.py` — **primary LEAP data source**: reads the nightly Dell
  scan's records (real 6-pillar scores, premiums, IV) from the private
  trading-data repo. Requires `TRADING_DATA_TOKEN` env var (fine-grained
  GitHub token, read-only Contents on trading-data). Why: Yahoo blocks
  option-chain fetches from cloud IPs (verified 2026-07-06), so the suite
  must not fetch chains itself.
- `leap/scanner.py` — legacy live scanner + old 5-factor scoring; the
  automatic **fallback when no token is configured** (option-dependent
  factors will be missing on Render). Entry point: `scan_leaps()`.
- `tracker/module.py` — the original "Stock Tracker" tab: price/day-change,
  daily S3..R3 pivots with nearest-level markers, Barchart opinion columns.
  **Working** (added 2026-07-06; home decision resolved: its own tab).
  `get_data(on_row=None)` — on_row receives rows-so-far for partial display.
- `swing/module.py` — swing-trader. **Stub**; expose `get_data() -> list[dict]`.
- `journal/module.py` — trade journal + P&L. **Stub**; same `get_data()` contract.
- `common/market_data.py` — shared: daily/weekly pivots, ATH/52-week, Barchart
  opinion scraper, and the `TICKERS` universe.

## App shell (`app.py`)
- Serves `templates/index.html` (3 tabs) and a JSON API: `/api/leap`,
  `/api/refresh/leap` (POST), `/api/swing`, `/api/journal`.
- A background thread refreshes all modules every 30 min. It starts on the
  **first web request**, never at import time (so the app imports cleanly and
  boots instantly).
- The Swing/Journal tabs render any list-of-dicts `get_data()` returns via a
  generic table; until then they show a placeholder.

## Deploy
- Render web service, `gunicorn app:app`, Python 3.11 — see `render.yaml`.

## History / provenance
- LEAP + shared helpers came from `rebibomichael-web/stock-tracker/app.py`.
- The original also had a "Stock Tracker" tab (`refresh_tracker` + Barchart
  opinions). That logic is preserved in `common/market_data.py`
  (`calc_pivots_correct`, `get_barchart`) and can be wired into the **Swing**
  tab if that's what it maps to — confirm with the user before assuming.

## Open threads
- [ ] Migrate real Swing code into `swing/module.py` (was local-only; the
      headless scan engine is the natural donor).
- [ ] Migrate real Journal code into `journal/module.py` (was local-only;
      stock-tracker's `journal_dashboard/build_journal_data.py` is prior art).
- [ ] Wire `common/market_data.py` helpers into Swing/Journal as they land.
- [ ] Verify the ported LEAP tab against live data, then retire old
      stock-tracker (which now also hosts the Journal Dashboard — that needs a
      new home first).
- [x] ~~Decide whether the old "Stock Tracker" tab becomes part of Swing~~ —
      resolved 2026-07-06: it's its own Tracker tab (`tracker/module.py`).
- [ ] Watch the first scheduled Halftime run (Mon Jul 6, ~1:20pm ET) and
      verify @claude Q&A on a report issue.

The full-system board (suite + automations + swing/LEAP research threads) is
`ROADMAP.html`, refreshed 2026-07-06. Deep research/status detail lives in
`trading-src/docs/planning/` (MASTER_STATUS_BOARD.md, ROADMAP_swing_trader.md,
LEAP_ROADMAP.md) — trading-src is an hourly-synced mirror of the Dell; edit
those docs on the machine, not on GitHub.

## Storage rule
GitHub is the source of truth; the local machine is a disposable working copy.
Push often. `handoff.sh` snapshots repo state for a clean handoff between sessions.

## Related repositories — add at session start

The owner wants every session started from this repo to also have access to
the sibling repositories. At the start of a session, add them with the
`add_repo` tool (this note is standing authorization from the owner to do so):

- `rebibomichael-web/stock-tracker`
- `rebibomichael-web/trading-src`
- `rebibomichael-web/trading-data`

If an add fails with an authorization error, tell the owner to grant the
Claude GitHub App access to that repo at https://github.com/apps/claude
(Configure → All repositories).

## Report archives (for Q&A)
Automated daily reports live in this repo — when answering questions about
"the reports," read these first:
- `summaries/` — CNBC Halftime Report daily summaries (one .md per trading day)
- `transcripts/` — full Halftime episode transcripts (same dates, more detail)
- `summaries/youtube/` — daily YouTube digest across followed channels
- `transcripts/youtube/` — full video transcripts when captions were available
Pipelines: `scripts/halftime_pipeline.py`, `scripts/youtube_digest.py`,
scheduled via `.github/workflows/`.
