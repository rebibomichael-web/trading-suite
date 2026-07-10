# SYSTEM_MAP — the four-repo trading platform

One personal trading platform, spread across four GitHub repos plus the
owner's Dell PC. This file is the **canonical map**; every repo's CLAUDE.md
carries a compact version of it and points here. Update this file (and only
this file) when the system's shape changes.

## Data flow

```
Dell PC (where everything actually runs; cron-driven)
 │  live code: ~, ~/Desktop/swing_project, ~/Downloads
 │  live state: ~/.michael_leap_*, ~/.michael_swing_*, ~/.trade_journal_*
 │
 ├─ hourly  ~/sync-trading-src.sh ──────►  trading-src   (code + docs MIRROR)
 ├─ daily   ~/backup-data.sh ───────────►  trading-data  (state/data backup)
 │
 ▼
trading-suite (Flask app on Render) ──reads──► trading-data
        via GitHub Contents API (TRADING_DATA_TOKEN, read-only)

stock-tracker (legacy Flask app on Render + journal dashboard)
        — being retired into trading-suite (roadmap item)
```

## The repos

| Repo | Visibility | Role | Edit on GitHub? |
|---|---|---|---|
| `trading-src` | private | Canonical trading code (leap/, swing/, journal/) + all planning/findings docs. **Hourly-synced mirror of the Dell** — the Dell's live copies win. | Docs like CLAUDE.md / PROJECT_MAP.md: yes. Mirrored code (leap/, swing/, journal/, tools/*.sh): **no** — the next sync overwrites it. |
| `trading-data` | private | Machine-generated data backups: nightly LEAP scans, swing signals/state, trade-journal tags.db and raw Fidelity CSVs. | **Never hand-edit.** Written only by `backup-data.sh` on the Dell. |
| `trading-suite` | public | The consolidated tabbed Flask app (Render: trading-suite-yar3.onrender.com) and the declared migration target. Also hosts CI report pipelines (Halftime, YouTube digests). | Yes — GitHub is the source of truth here. |
| `stock-tracker` | public | Legacy deployed dashboard (`app.py` on Render) + `journal_dashboard/` subproject. Slated for retirement into trading-suite. | Yes, but check whether the change belongs in trading-suite instead. |

## Session bootstrap

1. Whichever repo the session starts in, `add_repo` the other three —
   every CLAUDE.md carries standing authorization from the owner.
2. Clone siblings to `/workspace/<repo>`. trading-src's test suite hardcodes
   `/workspace/trading-data` for its real-corpus tests.
3. Read the repo's own CLAUDE.md before touching files — trading-src's
   mirror semantics in particular have already caused one incident
   (root-cause writeup in its CLAUDE.md).

## Known duplication / drift — never edit one copy blind

- `build_journal_data.py` — canonical: `stock-tracker/journal_dashboard/`.
  `trading-src/inbox/build_journal_data.py` is a stale fork (500+ line diff).
- `classify()` swing-flag rules — canonical: `trading-src/journal/swing_flag.py`;
  an embedded fallback copy lives inside stock-tracker's
  `build_journal_data.py`, kept in sync by comment only.
- `holdings.py` — `trading-src/swing/holdings.py` says its canonical is
  `stock-tracker/journal_dashboard/holdings.py`, but that file no longer
  exists at stock-tracker HEAD. The pointer has drifted; resolve before edits.
- LEAP scanner / tracker / market helpers — ported from `stock-tracker/app.py`
  into trading-suite (`leap/scanner.py`, `tracker/module.py`,
  `common/market_data.py`). The trading-suite copies are the live ones.
- The 16-ticker watchlist is hardcoded in at least three places:
  `stock-tracker/app.py` (`TICKERS`), stock-tracker's `build_journal_data.py`
  (`ORIG_WATCHLIST`), and `trading-suite/common/market_data.py` (`TICKERS`).

## Secrets and environment

- `TRADING_DATA_TOKEN` — fine-grained GitHub token, read-only Contents on
  trading-data. Powers trading-suite's LEAP and Swing tabs; without it the
  suite silently falls back to a degraded live scan.
- `ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN`,
  `WEBSHARE_PROXY_USERNAME/PASSWORD` — trading-suite CI report pipelines.
- Yahoo blocks option-chain fetches from cloud IPs (verified 2026-07-06):
  live scans behave differently in cloud sessions than on the Dell.

## Sensitive data

`trading-data/journal/fidelity/*.csv` are raw Fidelity brokerage exports —
account numbers and full transaction history. Do not paste their contents
into PRs, issues, logs, or summaries. The journal's migration into the public
suite is deliberately blocked on an encryption design for this reason.

## Quick run/test reference

| Repo | Run | Test |
|---|---|---|
| trading-suite | `pip install -r requirements.txt && python app.py` (port 10000) | none — verify via the JSON API endpoints |
| stock-tracker | `pip install -r requirements.txt && python app.py` | `python3 journal_dashboard/build_journal_data.py --selftest` |
| trading-src | code runs on the Dell; headless variants exist (`leap/leap_headless_scan.py`, `swing/swing_headless_scan.py`) | `python3 swing/test_scoring_golden.py`, `python3 swing/test_ohlcv_cache.py`, `python3 journal/test_trade_journal.py` (no manifest — `pip install yfinance pandas numpy matplotlib requests beautifulsoup4`) |
| trading-data | nothing runs here | n/a |

## Traps

- **trading-src greps are dominated by dead copies**: `archive/`, `_attic/`,
  and duplicated `docs/` files hold ~2/3 of the repo by size. Confirm you are
  citing/editing the live copy (`leap/`, `swing/`, `journal/`), and route
  files per `PROJECT_MAP.md`.
- **trading-data JSONs are huge** (1.0–2.6 MB, ~300k lines in swing/ alone):
  sample with `jq`/`head`/Python — never read whole files into context.
  Filenames like `Accounts_History (31).csv` need quoting in shell.
- **Auto-commit history carries no signal** in trading-src ("Auto-sync …")
  and trading-data ("History backup …"). The change narrative lives in
  `trading-src/docs/planning/` (MASTER_LOG.md, MASTER_STATUS_BOARD.md).
- **trading-suite gets daily CI commits** (summaries/, transcripts/) — local
  clones go stale within a day; rebase before pushing.
