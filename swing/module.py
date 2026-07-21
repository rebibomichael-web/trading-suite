"""Swing-trader web module.

Surfaces the signal log the desktop swing platform writes — it does NOT
re-run the scan (ADR-1: the Dell scans, displays read its JSON).

Data sources, in order:
  1. The local signals file (~/.michael_swing_signals.json) — fills in when
     the app runs on the Dell itself.
  2. The private trading-data repo (swing/signals.json, pushed by the daily
     backup) via common.trading_data — fills in on Render. Requires the
     TRADING_DATA_TOKEN env var; without it this returns [] (placeholder tab).

A transient bridge error raises, so the app shell keeps the previous cache
(stale beats blank). Shows each symbol's latest signal from the last
WINDOW_DAYS days, best score first, with the 7d outcome where it has been
checked.
"""
import json
import os
from datetime import datetime, timedelta

from common import trading_data

# Same file the desktop swing platform writes its signals to (CFG.tracker_path).
_SIGNALS_PATH = os.path.expanduser("~/.michael_swing_signals.json")
_BRIDGE_PATH = "swing/signals.json"
WINDOW_DAYS = 21  # hide symbols whose newest signal is older than this


def _fmt_date(s):
    if not s:
        return ""
    return str(s).replace("T", " ")[:16]  # YYYY-MM-DD HH:MM


def _num(v, nd=2):
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return v


def _shape(signals, now=None):
    """Latest signal per symbol within WINDOW_DAYS, best score first."""
    now = now or datetime.now()
    latest = {}
    for sig in signals:
        if not isinstance(sig, dict):
            continue
        sym = sig.get("symbol")
        if not sym:
            continue
        prev = latest.get(sym)
        if prev is None or str(sig.get("date", "")) >= str(prev.get("date", "")):
            latest[sym] = sig

    cutoff = (now - timedelta(days=WINDOW_DAYS)).isoformat()
    rows = []
    for sig in latest.values():
        if str(sig.get("date", "")) < cutoff:
            continue
        conds = sig.get("conditions", [])
        o7 = (sig.get("outcomes") or {}).get("7d") or {}
        rows.append({
            "symbol": sig.get("symbol"),
            "price": _num(sig.get("price")),
            "score": sig.get("score"),
            "setup": sig.get("setup_type"),
            "confidence": f"{sig.get('confidence_score', '?')}/5",
            "7d %": f"{o7['change_pct']:+.1f}%" if o7.get("checked") else "",
            "regime": sig.get("regime"),
            "conditions": ", ".join(conds) if isinstance(conds, list) else conds,
            "vol x": _num(sig.get("volume_ratio")),
            "atr %": sig.get("atr_swing"),
            "last flagged": _fmt_date(sig.get("date")),
        })
    rows.sort(key=lambda r: r["score"] if isinstance(r["score"], (int, float)) else -1,
              reverse=True)
    return rows


def get_data(path=None):
    """Latest swing signal per symbol (recent window), best score first."""
    path = path or _SIGNALS_PATH
    signals = None
    try:
        with open(path) as f:
            signals = json.load(f)
    except Exception:
        signals = None

    if signals is None:
        try:
            signals = trading_data.fetch_json(_BRIDGE_PATH)
        except trading_data.NotConfigured:
            return []
        # any other bridge error propagates — the shell keeps the old cache

    if not isinstance(signals, list):
        return []
    return _shape(signals)
