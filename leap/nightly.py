"""LEAP tab data from the nightly Dell scan, via the private trading-data repo.

Yahoo blocks option-chain fetches from cloud IPs (verified 2026-07-06), so the
suite doesn't fetch chains itself. The real LEAP program (leap_headless_scan.py
on the Dell, nightly cron) logs every signal-carrying scan record — score,
signal, premium, DTE — to its recommendations JSON, and backup-data.sh pushes
that to trading-data daily. This module reads the latest record per symbol
from that file, so the tab shows the actual program's numbers (6-pillar
scoring, MAX 15) instead of a degraded re-scan.

Config (Render env vars):
  TRADING_DATA_TOKEN  fine-grained GitHub token, read-only Contents access to
                      trading-data (required — without it the app falls back
                      to the legacy live scan)
  TRADING_DATA_REPO   owner/repo override (default rebibomichael-web/trading-data)
"""
from datetime import datetime, timedelta

from common import trading_data
from common.trading_data import NotConfigured  # re-export for app.py  # noqa: F401

FILE_PATH = "leap/recommendations.json"
MAX_AGE_DAYS = 14  # hide symbols whose newest signal is older than this


def _fetch_records():
    return trading_data.fetch_json(FILE_PATH)


def shape_rows(records, now=None):
    """Latest record per symbol (within MAX_AGE_DAYS), shaped for the LEAP tab.

    Only signal-carrying scans are ever logged by the nightly program, so this
    is 'every ticker with a live signal', not the full universe.
    """
    now = now or datetime.now()
    latest = {}
    for rec in records:
        sym = rec.get("symbol")
        if not sym or not rec.get("date"):
            continue
        if sym not in latest or rec["date"] > latest[sym]["date"]:
            latest[sym] = rec
    cutoff = (now - timedelta(days=MAX_AGE_DAYS)).isoformat()
    rows = []
    for rec in latest.values():
        if rec["date"] < cutoff:
            continue
        leap = rec.get("leap") or {}
        price = rec.get("price")
        premium = leap.get("premium")
        prem_pct = round(premium / price * 100, 1) if premium and price else None
        leverage = round(price / premium, 1) if premium and price else None
        rows.append({
            "ticker": rec["symbol"], "price": price,
            "score": rec.get("score"), "signal": rec.get("signal", "—"),
            "strike": leap.get("strike"), "exp": leap.get("exp"),
            "dte": leap.get("dte"), "premium": premium,
            "prem_pct": prem_pct, "leverage": leverage,
            "iv": leap.get("iv"),
            "premium_stale": rec.get("premium_stale"),
            "rev_confirmed": rec.get("rev_confirmed"),
            "scan_date": rec["date"][:10],
            "breakdown": rec.get("breakdown") or {},
        })
    rows.sort(key=lambda r: -(r["score"] or 0))
    return rows


def get_data():
    """Returns (rows, meta). Raises NotConfigured if no token is set."""
    records = _fetch_records()
    rows = shape_rows(records)
    meta = {
        "records_total": len(records),
        "latest_scan": max((r["scan_date"] for r in rows), default=None),
        "window_days": MAX_AGE_DAYS,
    }
    return rows, meta
