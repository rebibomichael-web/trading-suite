# Trading Suite — context for Claude

Consolidation of three trading projects into one Flask app with tabbed modules.
Read this first in any session on this repo.

## Modules
- `leap/scanner.py` — LEAP option scanner + 0–15 scoring. **Working**; ported
  verbatim from the original `stock-tracker` app. Entry point: `scan_leaps()`.
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
- [ ] Migrate real Swing code into `swing/module.py` (was local-only).
- [ ] Migrate real Journal code into `journal/module.py` (was local-only).
- [ ] Verify the ported LEAP tab against live data, then retire old stock-tracker.
- [ ] Decide whether the old "Stock Tracker" tab becomes part of Swing.

## Storage rule
GitHub is the source of truth; the local machine is a disposable working copy.
Push often. `handoff.sh` snapshots repo state for a clean handoff between sessions.
