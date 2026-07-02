# LEAP module

Already wired up and working — ported from the original `stock-tracker` app.

- `scanner.py` → `scan_leaps()` returns a scored, sorted list of LEAP setups.
- The app's `/api/leap` route and the **LEAP** tab already call it.
- Scoring (0–15) lives in `score_leap()`; the ticker list lives in
  `common/market_data.py` (`TICKERS`).

Nothing to do here unless you want to tweak the scoring thresholds or tickers.
