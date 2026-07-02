"""Swing-trader web module.

Reads the signal log your desktop swing platform writes
(~/.michael_swing_signals.json) and returns the latest setup per symbol for the
Swing tab, best score first. Dashboard-style: it surfaces your existing signals,
it does not re-run the scan. Reads a file on THIS machine, so it fills in when
you run the app locally. Never raises — returns [] if the file is missing.
"""
import json
import os

# Same file your desktop swing platform writes its signals to (CFG.tracker_path).
_SIGNALS_PATH = os.path.expanduser("~/.michael_swing_signals.json")


def _fmt_date(s):
    if not s:
        return ""
    return str(s).replace("T", " ")[:16]  # YYYY-MM-DD HH:MM


def _num(v, nd=2):
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return v


def get_data(path=None):
    """Latest swing signal per symbol, best score first. [] if no signals file."""
    path = path or _SIGNALS_PATH
    try:
        with open(path) as f:
            signals = json.load(f)
    except Exception:
        return []
    if not isinstance(signals, list):
        return []

    # File is an append-log: keep each symbol's most-recent entry.
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

    rows = []
    for sig in latest.values():
        conds = sig.get("conditions", [])
        rows.append({
            "symbol": sig.get("symbol"),
            "price": _num(sig.get("price")),
            "score": sig.get("score"),
            "setup": sig.get("setup_type"),
            "regime": sig.get("regime"),
            "confidence": f"{sig.get('confidence_score', '?')}/5",
            "conditions": ", ".join(conds) if isinstance(conds, list) else conds,
            "vol x": _num(sig.get("volume_ratio")),
            "atr %": sig.get("atr_swing"),
            "last flagged": _fmt_date(sig.get("date")),
        })
    rows.sort(key=lambda r: r["score"] if isinstance(r["score"], (int, float)) else -1,
              reverse=True)
    return rows
