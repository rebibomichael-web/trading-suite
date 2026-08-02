#!/usr/bin/env python3
"""Guru call ledger: build the audited track record no YouTube trader publishes.

Extracts explicit market calls from each new video transcript in
transcripts/youtube/ into ledger/guru_calls.json, then scores every call at
7/14/21 trading days using prices/daily_closes.json (fetched daily by the Dell
from a residential IP — see scripts/fetch_youtube_transcripts.py).

Rules:
- BMNR: EVERY mention is logged, however brief (kind="mention") — and any
  BMNR item from a Fundstrat channel is flagged affiliated=True, because Tom
  Lee (Fundstrat) chairs BitMine: that is issuer commentary, not analysis.
- Other tickers: only explicit actionable calls (entry/stop/target or a clear
  buy/sell-now) and strong directional opinions.
- Outcomes are direction-adjusted: long wins if price rose, short/exit wins if
  it fell; neutral mentions just record the move.

Usage:
  python scripts/guru_ledger.py            # extract new + update outcomes
  python scripts/guru_ledger.py --report   # print the markdown scoreboard
"""
import argparse
import datetime
import json
import os
import re
import sys

LEDGER_PATH = os.path.join("ledger", "guru_calls.json")
PRICES_PATH = os.path.join("prices", "daily_closes.json")
FEED_SNAPSHOT_PATH = os.path.join("feeds", "youtube_feed.json")
TRANSCRIPT_DIR = os.path.join("transcripts", "youtube")
CHECKPOINTS = (7, 14, 21)          # calendar days after the video
MAX_EXTRACT_ATTEMPTS = 3
AFFILIATED_CHANNELS = {"Fundstrat", "Fundstrat Capital"}
AFFILIATED_TICKER = "BMNR"         # Tom Lee chairs BitMine Immersion

EXTRACT_INSTRUCTIONS = (
    "You are building an auditable ledger of market calls from a YouTube "
    "transcript. Output a STRICT JSON array ONLY — no prose, no code fences. "
    "Each element: {\"ticker\": \"UPPERCASE\", \"direction\": one of "
    "\"long\"|\"short\"|\"exit\"|\"neutral\", \"kind\": "
    "\"call\"|\"opinion\"|\"mention\", \"entry\": number or null, "
    "\"stop\": number or null, \"target\": number or null, "
    "\"horizon_days\": number or null, \"quote\": \"verbatim supporting "
    "snippet, max 200 chars\"}. RULES: (1) Log an element for EVERY mention "
    "of BMNR / BitMine, however brief — use kind=\"mention\" and "
    "direction=\"neutral\" when it is not actionable. (2) For every other "
    "ticker, log ONLY explicit actionable calls (a stated entry, stop, "
    "target, level, or a clear buy/sell/trim-now) as kind=\"call\", and "
    "strong unambiguous directional opinions as kind=\"opinion\" — never "
    "passing references. (3) Never invent numbers; use null when unstated. "
    "(4) Real tradable US tickers only (no crypto pairs, no indexes without "
    "tickers). (5) If nothing qualifies, output []."
)


def load_json(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save_ledger(ledger):
    os.makedirs(os.path.dirname(LEDGER_PATH), exist_ok=True)
    tmp = LEDGER_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(ledger, fh, indent=1)
    os.replace(tmp, LEDGER_PATH)


def video_metadata():
    """video_id -> (channel, title, published-iso) from the Dell feed snapshot."""
    snap = load_json(FEED_SNAPSHOT_PATH, {})
    meta = {}
    for channel, vids in snap.get("channels", {}).items():
        for v in vids:
            meta[v.get("id")] = (channel, v.get("title", ""), v.get("published", ""))
    return meta


def parse_calls(raw):
    """Tolerant parse of the model's JSON array."""
    text = raw.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError("no JSON array in output")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, list):
        raise ValueError("not a list")
    out = []
    for c in data:
        if not isinstance(c, dict) or not c.get("ticker"):
            continue
        out.append({
            "ticker": str(c["ticker"]).upper().strip(),
            "direction": c.get("direction") or "neutral",
            "kind": c.get("kind") or "mention",
            "entry": c.get("entry"),
            "stop": c.get("stop"),
            "target": c.get("target"),
            "horizon_days": c.get("horizon_days"),
            "quote": str(c.get("quote", ""))[:220],
        })
    return out


def extract_new(ledger):
    from halftime_pipeline import ask_claude, preflight_auth
    preflight_auth()
    meta = video_metadata()
    processed = set(ledger["processed"])
    attempts = ledger.setdefault("attempts", {})
    new_calls = 0
    for fname in sorted(os.listdir(TRANSCRIPT_DIR)):
        if not fname.endswith(".txt"):
            continue
        vid = fname[:-4]
        if vid in processed or attempts.get(vid, 0) >= MAX_EXTRACT_ATTEMPTS:
            continue
        channel, title, published = meta.get(vid, ("(unknown)", "", ""))
        if not published:
            published = datetime.datetime.fromtimestamp(
                os.path.getmtime(os.path.join(TRANSCRIPT_DIR, fname)),
                datetime.timezone.utc).isoformat()
        transcript = open(os.path.join(TRANSCRIPT_DIR, fname)).read()
        try:
            raw = ask_claude(
                f"{EXTRACT_INSTRUCTIONS}\nChannel: {channel}\nVideo title: {title}",
                transcript)
            calls = parse_calls(raw)
        except Exception as e:
            attempts[vid] = attempts.get(vid, 0) + 1
            print(f"{vid} ({channel}): extraction failed "
                  f"(attempt {attempts[vid]}): {type(e).__name__}")
            continue
        for c in calls:
            c.update({
                "video_id": vid,
                "channel": channel,
                "video_title": title,
                "date": published[:10],
                "affiliated": (channel in AFFILIATED_CHANNELS
                               and c["ticker"] == AFFILIATED_TICKER),
                "outcomes": {},
            })
            ledger["calls"].append(c)
            new_calls += 1
        processed.add(vid)
        ledger["processed"] = sorted(processed)
        print(f"{vid} ({channel}): {len(calls)} call(s) logged")
    return new_calls


def close_on_or_after(closes, day, limit_days=6):
    d = datetime.date.fromisoformat(day)
    for i in range(limit_days):
        key = (d + datetime.timedelta(days=i)).isoformat()
        if key in closes:
            return closes[key]
    return None


def update_outcomes(ledger):
    prices = load_json(PRICES_PATH, {}).get("closes", {})
    if not prices:
        print("no prices file yet — outcomes skipped (the Dell writes it daily)")
        return 0
    today = datetime.date.today()
    updated = 0
    for c in ledger["calls"]:
        closes = prices.get(c["ticker"])
        if not closes:
            continue
        base = close_on_or_after(closes, c["date"])
        if base is None:
            continue
        for n in CHECKPOINTS:
            key = f"{n}d"
            if key in c["outcomes"]:
                continue
            due = datetime.date.fromisoformat(c["date"]) + datetime.timedelta(days=n)
            if today < due:
                continue
            cp = close_on_or_after(closes, due.isoformat())
            if cp is None:
                continue
            pct = round((cp - base) / base * 100, 2)
            win = None
            if c["direction"] == "long":
                win = pct > 0
            elif c["direction"] in ("short", "exit"):
                win = pct < 0
            c["outcomes"][key] = {"pct": pct, "win": win}
            updated += 1
    return updated


def report(ledger):
    calls = ledger["calls"]
    day = datetime.date.today().isoformat()
    print(f"## Guru scoreboard — {day}\n")
    print(f"{len(calls)} logged items across "
          f"{len({c['channel'] for c in calls})} channels. Win = direction-"
          f"adjusted move at the checkpoint (longs up / shorts-exits down); "
          f"mentions are tracked but not scored.\n")
    print("| Channel | Items | Scored @7d | Wins | Win% | Avg move @7d | @21d win% |")
    print("|---|---|---|---|---|---|---|")
    by_channel = {}
    for c in calls:
        by_channel.setdefault(c["channel"], []).append(c)
    for channel in sorted(by_channel):
        cs = by_channel[channel]
        s7 = [c for c in cs if c["outcomes"].get("7d", {}).get("win") is not None]
        w7 = [c for c in s7 if c["outcomes"]["7d"]["win"]]
        moves = [c["outcomes"]["7d"]["pct"] for c in cs if "7d" in c["outcomes"]]
        s21 = [c for c in cs if c["outcomes"].get("21d", {}).get("win") is not None]
        w21 = [c for c in s21 if c["outcomes"]["21d"]["win"]]
        pct7 = f"{100 * len(w7) / len(s7):.0f}%" if s7 else "—"
        pct21 = f"{100 * len(w21) / len(s21):.0f}%" if s21 else "—"
        avg = f"{sum(moves) / len(moves):+.1f}%" if moves else "—"
        print(f"| {channel} | {len(cs)} | {len(s7)} | {len(w7)} | {pct7} "
              f"| {avg} | {pct21} |")
    bmnr = [c for c in calls if c["ticker"] == AFFILIATED_TICKER]
    if bmnr:
        print(f"\n### BMNR — every mention ({len(bmnr)})\n")
        print("| Date | Channel | Kind | Direction | @7d | @21d | Affiliated | Quote |")
        print("|---|---|---|---|---|---|---|---|")
        for c in sorted(bmnr, key=lambda x: x["date"]):
            o7 = c["outcomes"].get("7d", {}).get("pct")
            o21 = c["outcomes"].get("21d", {}).get("pct")
            flag = "⚠️ Tom Lee chairs BitMine" if c["affiliated"] else ""
            q = c["quote"].replace("|", "/")[:80]
            print(f"| {c['date']} | {c['channel']} | {c['kind']} "
                  f"| {c['direction']} | "
                  f"{'—' if o7 is None else f'{o7:+.1f}%'} | "
                  f"{'—' if o21 is None else f'{o21:+.1f}%'} | {flag} | {q} |")
    print("\n*Win rates without R-multiples are directional only; a high win "
          "rate with bad risk:reward still loses money. Compare against the "
          "SPY column in prices before crediting anyone with edge.*")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    ledger = load_json(LEDGER_PATH, {"processed": [], "attempts": {}, "calls": []})
    if args.report:
        report(ledger)
        return
    new = extract_new(ledger)
    upd = update_outcomes(ledger)
    save_ledger(ledger)
    print(f"ledger: +{new} call(s), {upd} outcome(s) updated, "
          f"{len(ledger['calls'])} total")


if __name__ == "__main__":
    main()
