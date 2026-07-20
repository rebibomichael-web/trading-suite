#!/usr/bin/env python3
"""Trigger watch: alert when a watched price level is crossed.

Reads trigger levels from three sources and checks them against live prices
(yfinance). When a level is crossed for the first time, writes alert.md — the
workflow turns that into a GitHub issue, which lands in the owner's email
(and phone, via the GitHub mobile app).

Sources, in priority order when levels collide:
  manual    — the "manual" list in triggers.json; the owner's own switches
  position  — ATR stop / TP1 / TP2 of every active swing trade, read from a
              trading-data checkout when one is present (auto-refreshed as
              positions change on the Dell)
  narrative — flip levels emitted by the market-narratives pipeline

State: the "fired" map in triggers.json records alerts already sent, so a
crossed level alerts once, not every 30 minutes. Delete a key to re-arm.
Stale fired keys (trigger gone for >30 days) are pruned.

Env:
  TRIGGERS_FILE      path to triggers.json (default: triggers.json)
  TRADING_DATA_DIR   optional trading-data checkout for position triggers

Exit 0 whether or not anything fired; alert.md exists only when something did.
Run with --selftest for offline tests (no network).
"""
import datetime
import json
import os
import sys

FIRED_PRUNE_DAYS = 30


def trigger_key(t):
    return f"{t['ticker']}|{t['when']}|{round(float(t['level']), 2)}"


def load_triggers_file(path):
    data = {"manual": [], "narrative": [], "fired": {}}
    if os.path.exists(path):
        try:
            data.update(json.load(open(path)))
        except ValueError:
            print(f"Warning: {path} is not valid JSON; starting fresh",
                  file=sys.stderr)
    return data


def position_triggers(swing_state):
    out = []
    for t in swing_state.get("active_trades") or []:
        sym = t.get("stock")
        if not sym:
            continue
        exits = t.get("suggested_exits", {})
        entry = t.get("avg_entry_price")
        for field, when, label in (("stop_loss_atr", "below", "ATR stop"),
                                   ("take_profit_1r", "above", "TP1 (1R)"),
                                   ("take_profit_2r", "above", "TP2 (2R)")):
            level = exits.get(field, {}).get("price")
            if level:
                out.append({"ticker": sym, "when": when, "level": level,
                            "note": f"{label}, entry {entry}",
                            "source": "position"})
    return out


def collect(data, positions):
    """Merged active triggers, deduped by (ticker, direction, level)."""
    merged, seen = [], set()
    manual = [dict(t, source="manual") for t in data.get("manual", [])
              if t.get("enabled", True)]
    for t in manual + positions + data.get("narrative", []):
        try:
            key = trigger_key(t)
            assert t["when"] in ("above", "below") and float(t["level"]) > 0
        except (KeyError, AssertionError, TypeError, ValueError):
            print(f"Skipping malformed trigger: {t}", file=sys.stderr)
            continue
        if key not in seen:
            seen.add(key)
            merged.append(t)
    return merged


def is_crossed(t, price):
    return price >= t["level"] if t["when"] == "above" else price <= t["level"]


def evaluate(triggers, prices, fired, today):
    """Pure core: returns (newly_fired_triggers, updated_fired_map)."""
    fired = dict(fired)
    newly = []
    for t in triggers:
        key = trigger_key(t)
        price = prices.get(t["ticker"])
        if price is None or key in fired:
            continue
        if is_crossed(t, price):
            newly.append(dict(t, price=price))
            fired[key] = today
    return newly, fired


def prune_fired(fired, triggers, today):
    """Drop fired entries whose trigger vanished more than N days ago."""
    live = {trigger_key(t) for t in triggers}
    cutoff = (datetime.date.fromisoformat(today)
              - datetime.timedelta(days=FIRED_PRUNE_DAYS)).isoformat()
    return {k: d for k, d in fired.items() if k in live or d >= cutoff}


def fetch_prices(tickers):
    import yfinance as yf

    prices = {}
    for sym in tickers:
        try:
            hist = yf.Ticker(sym).history(period="1d")
            if len(hist):
                prices[sym] = round(float(hist["Close"].iloc[-1]), 2)
        except Exception as e:
            print(f"Price fetch failed for {sym}: {e}", file=sys.stderr)
    return prices


def render_alert(newly, now_utc):
    lines = [f"# Trigger alert — {now_utc}", ""]
    for t in newly:
        arrow = "🔺" if t["when"] == "above" else "🔻"
        lines.append(f"- {arrow} **{t['ticker']}** at ${t['price']:g} crossed "
                     f"**{t['when']} ${t['level']:g}** — {t.get('note', '')} "
                     f"_({t['source']})_")
    lines += ["", "_To re-arm a trigger, delete its key from the 'fired' map "
              "in triggers.json. Levels come from your manual list, open "
              "position exits, and the latest narratives run._"]
    return "\n".join(lines)


def main():
    path = os.environ.get("TRIGGERS_FILE", "triggers.json")
    data_dir = os.environ.get("TRADING_DATA_DIR", "trading-data")
    today = datetime.date.today().isoformat()

    data = load_triggers_file(path)
    positions = []
    state_path = os.path.join(data_dir, "swing", "state.json")
    if os.path.exists(state_path):
        positions = position_triggers(json.load(open(state_path)))
    triggers = collect(data, positions)
    if not triggers:
        print("No triggers to watch — add some to triggers.json 'manual' "
              "or run the Market narratives workflow")
        return

    prices = fetch_prices({t["ticker"] for t in triggers})
    newly, fired = evaluate(triggers, prices, data.get("fired", {}), today)
    for t in triggers:
        key = trigger_key(t)
        status = ("FIRED-NOW" if any(trigger_key(n) == key for n in newly)
                  else "already-fired" if key in fired else "armed")
        print(f"{t['ticker']:6} {t['when']:5} {t['level']:>9} "
              f"(now {prices.get(t['ticker'], '?'):>9}) {status:14} "
              f"{t['source']}: {t.get('note', '')}")

    data["fired"] = prune_fired(fired, triggers, today)
    json.dump(data, open(path, "w"), indent=1)
    if newly:
        now = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC")
        open("alert.md", "w").write(render_alert(newly, now))
        print(f"{len(newly)} trigger(s) fired — alert.md written")
    else:
        print("Nothing newly crossed")


# ── selftest (offline) ─────────────────────────────────────────────────────

def selftest():
    state = {"active_trades": [
        {"stock": "SNPS", "avg_entry_price": 376.96, "suggested_exits": {
            "stop_loss_atr": {"price": 338.61},
            "take_profit_1r": {"price": 415.30},
            "take_profit_2r": {"price": 434.48}}},
        {"stock": None}]}
    pos = position_triggers(state)
    assert [(t["when"], t["level"]) for t in pos] == \
        [("below", 338.61), ("above", 415.30), ("above", 434.48)]

    data = {"manual": [{"ticker": "AAA", "when": "above", "level": 79.2,
                        "note": "flip to buy"},
                       {"ticker": "OFF", "when": "above", "level": 1,
                        "enabled": False},
                       {"ticker": "BAD", "when": "sideways", "level": 5}],
            "narrative": [{"ticker": "AAA", "when": "above", "level": 79.2,
                           "note": "dupe of manual", "source": "narrative"},
                          {"ticker": "BBB", "when": "below", "level": 55.43,
                           "source": "narrative"}],
            "fired": {}}
    trigs = collect(data, pos)
    keys = [trigger_key(t) for t in trigs]
    assert "OFF|above|1" not in keys and len(keys) == len(set(keys))
    assert keys.count("AAA|above|79.2") == 1
    assert next(t for t in trigs if t["ticker"] == "AAA")["source"] == "manual"

    prices = {"AAA": 80.0, "BBB": 60.0, "SNPS": 330.0}
    newly, fired = evaluate(trigs, prices, {}, "2026-07-20")
    got = {trigger_key(t) for t in newly}
    assert got == {"AAA|above|79.2", "SNPS|below|338.61"}, got
    # second run: nothing re-fires
    again, fired2 = evaluate(trigs, prices, fired, "2026-07-21")
    assert again == [] and fired2 == fired
    # missing price -> no fire, no crash
    newly3, _ = evaluate(trigs, {}, {}, "2026-07-20")
    assert newly3 == []

    # prune: stale fired for vanished trigger goes, recent + live stay
    fired = {"GONE|above|1": "2026-01-01", "GONE2|above|1": "2026-07-15",
             "AAA|above|79.2": "2026-01-01"}
    pruned = prune_fired(fired, trigs, "2026-07-20")
    assert set(pruned) == {"GONE2|above|1", "AAA|above|79.2"}, pruned

    alert = render_alert([dict(trigs[0], price=80.0)], "2026-07-20 14:30 UTC")
    assert "AAA" in alert and "$79.2" in alert and "re-arm" in alert
    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main()
