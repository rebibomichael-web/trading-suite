"""Trade Journal web module.

Reuses your real Fidelity parser (journal/fidelity.py, lifted from your desktop
trade_journal.py). get_data() finds the most-recent Fidelity CSV — the same one
your desktop journal remembers — matches trades FIFO, and returns closed trades
as rows for the Journal tab.

It reads files on THIS machine, so it fills in when you run the app LOCALLY
(where your CSV lives) and simply shows the placeholder on the cloud/Render copy
(which can't see your computer). Never raises — returns [] if anything is off.
"""
import json
import os

try:
    from journal.fidelity import parse_fidelity_csv, default_method_for
except Exception:
    parse_fidelity_csv = None

# Same config file your desktop journal writes its "last opened CSV" into.
_CONFIG_PATH = os.path.expanduser("~/.trade_journal_config.json")


def _find_csv():
    # 1) whatever CSV your desktop journal last loaded
    try:
        with open(_CONFIG_PATH) as f:
            last = json.load(f).get("last_csv_path")
        if last and os.path.isfile(last):
            return last
    except Exception:
        pass
    # 2) optional fallback: a CSV dropped next to the app
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("fidelity.csv", "trades.csv", "journal.csv"):
        p = os.path.join(here, name)
        if os.path.isfile(p):
            return p
    return None


def _fmt_date(d):
    try:
        return d.strftime("%Y-%m-%d") if d else ""
    except Exception:
        return str(d) if d else ""


def get_data():
    """Closed trades as a list of dicts (most recent first). [] if no CSV found."""
    if parse_fidelity_csv is None:
        return []
    path = _find_csv()
    if not path:
        return []
    try:
        closed, opens, orphans = parse_fidelity_csv(path)
    except Exception:
        return []

    rows = []
    for leg in closed:
        rows.append({
            "ticker": leg.get("ticker"),
            "strategy": default_method_for(leg),
            "buy_date": _fmt_date(leg.get("buy_date")),
            "sell_date": _fmt_date(leg.get("sell_date")),
            "qty": leg.get("qty_str") or leg.get("qty"),
            "$ P/L": round(float(leg.get("pl_dollar", 0.0)), 2),
            "% P/L": round(float(leg.get("pl_pct", 0.0)), 2),
            "hold days": leg.get("hold_days"),
        })
    rows.sort(key=lambda r: r["sell_date"], reverse=True)
    return rows
