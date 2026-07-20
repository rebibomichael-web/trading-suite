#!/usr/bin/env python3
"""Market narratives: web-grounded "why is it moving" write-ups for scanner picks.

Reads the nightly LEAP board and swing signal log from a trading-data checkout,
picks the names the scanners currently like, and asks Claude (with web search)
to explain each one in plain English: what actually happened in the news,
whether the cheapness is a thesis event or just technicals, what the scanners
see, and what this system's own outcome data says about similar setups.

Outputs:
  narratives.md        full report (archived to summaries/narratives/)
  narratives_issue.md  same content, truncated if needed to fit an issue body

Env:
  TRADING_DATA_DIR   path to a trading-data checkout (default: trading-data)
  NARRATIVE_SCOPE    signals (default) | monitor | all
  NARRATIVE_TICKERS  comma-separated tickers — overrides scope selection
  NARRATIVE_MAX      max narratives per run (default 25; extras listed, not run)
  NARRATIVE_MODEL    Claude model for the API path (default claude-sonnet-5)
  ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN — same dual auth as
                     halftime_pipeline.py (API key preferred, else Claude CLI)

Exit codes: 0 = report written, nonzero = real failure.
Run with --selftest for offline tests on synthetic data (no network, no creds).
"""
import datetime
import json
import os
import subprocess
import sys
import time

MODEL = os.environ.get("NARRATIVE_MODEL", "claude-sonnet-5")
MAX_WEB_SEARCHES = 4          # per ticker; each search costs ~$0.01 on the API path
ISSUE_BODY_LIMIT = 60000      # GitHub issue bodies cap at 65536 chars

LEAP_STRONG = 10              # score >= this (of 15) counts as a signal
LEAP_MONITOR = 7
SWING_RECENT_DAYS = 4         # swing signals within this many days of the board date

INSTRUCTIONS = """You are writing a morning brief for the owner of a personal
stock-scanning system. Their scanners are quantitative dip-buyers: a LEAP
scanner scores drawdown, option cheapness, and support levels 0-15 nightly,
and a swing scanner fires on oversold technicals. The scanners only see price
action — they cannot see news. Your job is the missing narrative layer.

For the single ticker described in the scanner context, first use web search
to find what actually drove the recent price action (last 2-3 weeks: news,
earnings, guidance, sector moves, analyst actions). Then write EXACTLY these
sections, in markdown, tight and plain-English (whole write-up under 350 words):

#### What happened
2-4 sentences: the recent price action and the news behind it, from your search.

#### Thesis or technicals?
Is this move a thesis event (news that challenges or changes the bull case) or
mechanical — sector rotation, market-wide risk-off, profit taking? Be explicit
about which, and why. The scanners cannot tell the difference; this is the
most important section.

#### What the scanners see
Translate the provided scores, breakdowns, and conditions into plain English a
non-quant would follow. No jargon without a gloss.

#### Your own data says
The track-record caveats from the provided stats: how signals on this specific
name have worked out, and how this setup type performs overall. If the data is
thin or unflattering, say so plainly.

#### Bottom line
2-3 blunt sentences. A data-grounded read, not investment advice. If the owner
holds a position (noted in context), speak to it.

End with a "Sources:" line of markdown links to what you used. Do not invent
data — everything quantitative must come from the context block or a source.
The scanner context follows."""


# ── data loading ───────────────────────────────────────────────────────────

def load_data(data_dir):
    def j(rel):
        with open(os.path.join(data_dir, rel)) as f:
            return json.load(f)
    return {
        "score_history": j("leap/score_history.json"),
        "recommendations": j("leap/recommendations.json"),
        "swing_signals": j("swing/signals.json"),
        "swing_state": j("swing/state.json"),
    }


def latest_board(recommendations):
    """Latest LEAP scan: (date_str, {symbol: most recent row on that date})."""
    if not recommendations:
        return None, {}
    board_date = max(r["date"][:10] for r in recommendations)
    board = {}
    for r in recommendations:
        if r["date"][:10] == board_date:
            board[r["symbol"]] = r  # later rows win — same-day rescans
    return board_date, board


def leap_band_stats(recommendations):
    """30d outcome stats by LEAP score band, across the whole log."""
    bands = {}
    for r in recommendations:
        o = r.get("outcomes", {}).get("30d", {})
        if not o.get("checked"):
            continue
        sc = r.get("score", 0)
        band = "strong(>=10)" if sc >= LEAP_STRONG else (
            "monitor(7-9)" if sc >= LEAP_MONITOR else "low(<7)")
        bands.setdefault(band, []).append(o["change_pct"])
    return {b: {"n": len(v), "mean_30d": round(sum(v) / len(v), 2),
                "win_rate": round(100 * sum(1 for x in v if x > 0) / len(v))}
            for b, v in bands.items()}


def swing_setup_stats(signals):
    """7d outcome stats for arb-dislocation vs other swing setups."""
    out = {}
    for e in signals:
        o = e.get("outcomes", {}).get("7d", {})
        if not o.get("checked"):
            continue
        key = "arb_dislocation" if e.get("setup_arb") else "other_setups"
        out.setdefault(key, []).append(o["change_pct"])
    return {k: {"n": len(v), "mean_7d": round(sum(v) / len(v), 2),
                "win_rate": round(100 * sum(1 for x in v if x > 0) / len(v))}
            for k, v in out.items()}


def swing_history(signals, sym):
    """This ticker's past swing signals with whatever outcomes are checked."""
    rows = []
    for e in signals:
        if e.get("symbol") != sym:
            continue
        o = e.get("outcomes", {})
        rows.append({
            "date": e["date"][:10], "price": round(e.get("price", 0), 2),
            "score": e.get("score"), "setup": e.get("setup_type"),
            "outcome_7d": o.get("7d", {}).get("change_pct"),
            "outcome_14d": o.get("14d", {}).get("change_pct"),
            "outcome_21d": o.get("21d", {}).get("change_pct"),
        })
    return rows


def active_trades(swing_state):
    out = {}
    for t in swing_state.get("active_trades") or []:
        sym = t.get("stock")
        if not sym:
            continue
        exits = t.get("suggested_exits", {})
        out[sym] = {
            "avg_entry": t.get("avg_entry_price"),
            "shares": t.get("total_shares"),
            "usd": t.get("total_amount_usd"),
            "entered": (t.get("entries") or [{}])[0].get("timestamp", "")[:10],
            "stop": exits.get("stop_loss_atr", {}).get("price"),
            "tp1": exits.get("take_profit_1r", {}).get("price"),
            "tp2": exits.get("take_profit_2r", {}).get("price"),
            "unrealized_pct": t.get("excursion", {}).get("mfe_pct"),
        }
    return out


# ── target selection ───────────────────────────────────────────────────────

def recent_swing_symbols(signals, board_date, days=SWING_RECENT_DAYS):
    if not board_date:
        return {}
    cutoff = (datetime.date.fromisoformat(board_date)
              - datetime.timedelta(days=days)).isoformat()
    out = {}
    for e in signals:
        d = e["date"][:10]
        if d >= cutoff:
            prev = out.get(e["symbol"])
            if prev is None or e["date"] > prev["date"]:
                out[e["symbol"]] = e
    return out


def pick_targets(board, recent_swing, held, scope):
    """Ordered ticker list: held first, then by LEAP score descending."""
    if scope == "all":
        leap_picks = set(board)
    else:
        floor = LEAP_STRONG if scope == "signals" else LEAP_MONITOR
        leap_picks = {s for s, r in board.items()
                      if r.get("score", 0) >= floor or "ALERT" in (r.get("signal") or "")}
    picks = leap_picks | set(recent_swing) | set(held)

    def order(sym):
        return (0 if sym in held else 1, -(board.get(sym, {}).get("score", 0)))
    return sorted(picks, key=order)


# ── context building ───────────────────────────────────────────────────────

def build_context(sym, board, score_history, swing_signals, held,
                  band_stats, setup_stats, recent_swing):
    ctx = {"ticker": sym}
    rec = board.get(sym)
    if rec:
        ctx["leap_board"] = {
            "scan_date": rec["date"][:10], "price": rec.get("price"),
            "score_of_15": rec.get("score"), "signal": rec.get("signal"),
            "score_breakdown": rec.get("breakdown"),
            "leap_contract": rec.get("leap"),
        }
    hist = score_history.get(sym) or []
    ctx["leap_score_trajectory_recent"] = [
        {"date": e["date"], "score": e["score"], "price": e["price"]}
        for e in hist[-21:]]
    ctx["leap_score_band_stats_30d_outcomes"] = band_stats
    sw = recent_swing.get(sym)
    if sw:
        ta = sw.get("ta_snapshot", {})
        ctx["latest_swing_signal"] = {
            "date": sw["date"][:10], "price": round(sw.get("price", 0), 2),
            "score": sw.get("score"), "signal": sw.get("signal"),
            "setup_type": sw.get("setup_type"), "is_arb": sw.get("setup_arb"),
            "conditions": sw.get("conditions"),
            "confidence_checks": sw.get("confidence_checks"),
            "arb_z": sw.get("arb_z"), "rsi": ta.get("RSI"),
            "volume_ratio": sw.get("volume_ratio"),
        }
    ctx["swing_setup_type_stats_7d_outcomes"] = setup_stats
    ctx["past_swing_signals_this_ticker"] = swing_history(swing_signals, sym)
    if sym in held:
        ctx["owner_position"] = held[sym]
    return json.dumps(ctx, indent=1)


# ── Claude (dual auth, web search enabled) ─────────────────────────────────

def _clean_credentials():
    """Strip whitespace/newlines that sneak into pasted secrets."""
    for var in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
        value = os.environ.get(var)
        if value:
            cleaned = "".join(value.split())
            if cleaned != value:
                print(f"Note: removed whitespace from {var}")
                os.environ[var] = cleaned


def ask_claude(instructions, content, web_search=True):
    """ANTHROPIC_API_KEY -> Claude API; CLAUDE_CODE_OAUTH_TOKEN -> Claude CLI."""
    _clean_credentials()
    if os.environ.get("ANTHROPIC_API_KEY"):
        return _ask_api(instructions, content, web_search)
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return _ask_cli(instructions, content, web_search)
    raise RuntimeError(
        "Set the ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN repo secret")


def _ask_api(instructions, content, web_search):
    import anthropic

    client = anthropic.Anthropic()
    tools = []
    if web_search:
        tools = [{"type": "web_search_20260209", "name": "web_search",
                  "max_uses": MAX_WEB_SEARCHES}]
    messages = [{"role": "user", "content": f"{instructions}\n\n{content}"}]
    for _ in range(4):  # server-side search loop can pause; resume up to 3x
        response = client.messages.create(
            model=MODEL, max_tokens=16000, tools=tools, messages=messages)
        if response.stop_reason != "pause_turn":
            break
        messages = [messages[0],
                    {"role": "assistant", "content": response.content}]
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude refused this request")
    return "\n".join(b.text for b in response.content if b.type == "text").strip()


def _ask_cli(instructions, content, web_search):
    cmd = ["claude", "-p", f"{instructions}"]
    if web_search:
        cmd += ["--allowedTools", "WebSearch"]
    result = subprocess.run(cmd, input=content, capture_output=True,
                            text=True, timeout=900)
    if result.returncode != 0:
        # The CLI prints auth/API errors to stdout, not stderr.
        raise RuntimeError(
            f"claude CLI failed: {result.stdout[:500]} {result.stderr[:500]}".strip())
    return result.stdout.strip()


def preflight_auth():
    reply = ask_claude("Reply with exactly: OK", "ping", web_search=False)
    print(f"Auth preflight passed ({reply[:20]!r})")


# ── report assembly ────────────────────────────────────────────────────────

def summary_table(targets, board, recent_swing, held):
    lines = ["| Ticker | Price | LEAP score | LEAP signal | Swing | Held |",
             "|---|---|---|---|---|---|"]
    for sym in targets:
        rec = board.get(sym, {})
        sw = recent_swing.get(sym)
        pos = held.get(sym)
        lines.append("| {} | {} | {} | {} | {} | {} |".format(
            sym,
            rec.get("price", "—"),
            f"{rec['score']}/15" if "score" in rec else "—",
            (rec.get("signal") or "—").strip(),
            (sw.get("signal") or sw.get("setup_type") or "signal") if sw else "—",
            f"{pos['shares']} @ {pos['avg_entry']}" if pos else "—"))
    return "\n".join(lines)


def render_report(day, scope, targets, skipped, table, sections, errors):
    parts = [f"# Market narratives — {day}",
             f"_Scope: {scope} · {len(sections)} narratives_", "", table, ""]
    for sym, text in sections:
        parts += [f"## {sym}", "", text, ""]
    if errors:
        parts += ["## Errors", ""]
        parts += [f"- **{sym}**: {err}" for sym, err in errors]
        parts.append("")
    if skipped:
        parts += [f"_Skipped (over NARRATIVE_MAX): {', '.join(skipped)} — "
                  "re-run with tickers= to cover them._", ""]
    return "\n".join(parts)


def issue_body(report):
    if len(report) <= ISSUE_BODY_LIMIT:
        return report
    cut = report[:ISSUE_BODY_LIMIT]
    cut = cut[:cut.rfind("\n## ")]  # end at a section boundary
    return cut + "\n\n_Truncated — full report in summaries/narratives/._\n"


# ── main ───────────────────────────────────────────────────────────────────

def main():
    data_dir = os.environ.get("TRADING_DATA_DIR", "trading-data")
    scope = os.environ.get("NARRATIVE_SCOPE", "signals").strip() or "signals"
    override = [t.strip().upper() for t in
                os.environ.get("NARRATIVE_TICKERS", "").split(",") if t.strip()]
    max_n = int(os.environ.get("NARRATIVE_MAX", "25"))

    data = load_data(data_dir)
    board_date, board = latest_board(data["recommendations"])
    if not board:
        print("No LEAP board data found", file=sys.stderr)
        sys.exit(1)
    held = active_trades(data["swing_state"])
    recent_swing = recent_swing_symbols(data["swing_signals"], board_date)
    band_stats = leap_band_stats(data["recommendations"])
    setup_stats = swing_setup_stats(data["swing_signals"])

    if override:
        targets, scope = override, f"tickers={','.join(override)}"
    else:
        targets = pick_targets(board, recent_swing, held, scope)
    skipped = targets[max_n:]
    targets = targets[:max_n]
    print(f"Board {board_date}; scope {scope}; narrating {len(targets)}: "
          f"{', '.join(targets)}" + (f" (skipping {len(skipped)})" if skipped else ""))

    preflight_auth()
    sections, errors = [], []
    for i, sym in enumerate(targets):
        ctx = build_context(sym, board, data["score_history"],
                            data["swing_signals"], held, band_stats,
                            setup_stats, recent_swing)
        try:
            text = ask_claude(INSTRUCTIONS, ctx)
            sections.append((sym, text))
            print(f"[{i + 1}/{len(targets)}] {sym} ok ({len(text)} chars)")
        except Exception as e:  # keep going — one bad ticker shouldn't kill the run
            errors.append((sym, str(e)[:300]))
            print(f"[{i + 1}/{len(targets)}] {sym} FAILED: {e}", file=sys.stderr)
        time.sleep(2)

    table = summary_table(targets, board, recent_swing, held)
    report = render_report(board_date, scope, targets, skipped, table,
                           sections, errors)
    open("narratives.md", "w").write(report)
    open("narratives_issue.md", "w").write(issue_body(report))
    print(f"Wrote narratives.md ({len(report)} chars, "
          f"{len(sections)} ok / {len(errors)} failed)")
    if sections == []:
        sys.exit(1)


# ── selftest (offline, synthetic data) ─────────────────────────────────────

def selftest():
    day = "2026-07-19"
    recs = [
        {"date": f"{day}T10:00:00", "symbol": "AAA", "price": 100.0, "score": 12,
         "signal": "S2 ALERT", "breakdown": {"RSI": 4}, "leap": {"strike": 120},
         "outcomes": {"30d": {"change_pct": 5.0, "checked": True}}},
        {"date": f"{day}T10:00:00", "symbol": "BBB", "price": 50.0, "score": 8,
         "signal": "MONITOR", "breakdown": {}, "leap": None,
         "outcomes": {"30d": {"change_pct": -2.0, "checked": True}}},
        {"date": "2026-07-01T10:00:00", "symbol": "AAA", "price": 110.0, "score": 9,
         "signal": "MONITOR", "breakdown": {},
         "outcomes": {"30d": {"change_pct": 3.0, "checked": True}}},
    ]
    swing = [
        {"date": f"{day}T09:00:00", "symbol": "CCC", "price": 20.0, "score": 80,
         "setup_type": "oversold", "setup_arb": True, "conditions": ["at_lower_bb"],
         "signal": "ARB BUY", "ta_snapshot": {"RSI": 25},
         "outcomes": {"7d": {"change_pct": -1.0, "checked": True}}},
        {"date": "2026-06-01T09:00:00", "symbol": "AAA", "price": 105.0, "score": 70,
         "setup_type": "bounce", "setup_arb": False, "conditions": [],
         "outcomes": {"7d": {"change_pct": 2.0, "checked": True}}},
    ]
    state = {"active_trades": [
        {"stock": "DDD", "avg_entry_price": 10.0, "total_shares": 1.0,
         "total_amount_usd": 10.0, "entries": [{"timestamp": f"{day}T12:00:00Z"}],
         "suggested_exits": {"stop_loss_atr": {"price": 9.0}}, "excursion": {}},
        {"stock": None}]}

    bd, board = latest_board(recs)
    assert bd == day and set(board) == {"AAA", "BBB"}, (bd, board.keys())
    held = active_trades(state)
    assert set(held) == {"DDD"} and held["DDD"]["stop"] == 9.0
    rs = recent_swing_symbols(swing, bd)
    assert set(rs) == {"CCC"}, rs

    t = pick_targets(board, rs, held, "signals")
    assert t == ["DDD", "AAA", "CCC"], t          # held first, then by score
    assert pick_targets(board, rs, held, "monitor") == ["DDD", "AAA", "BBB", "CCC"]
    assert set(pick_targets(board, rs, held, "all")) == {"AAA", "BBB", "CCC", "DDD"}

    bands = leap_band_stats(recs)
    assert bands["strong(>=10)"]["n"] == 1 and bands["monitor(7-9)"]["n"] == 2
    setups = swing_setup_stats(swing)
    assert setups["arb_dislocation"]["win_rate"] == 0
    assert setups["other_setups"]["win_rate"] == 100

    ctx = json.loads(build_context("AAA", board, {"AAA": recs}, swing, held,
                                   bands, setups, rs))
    assert ctx["leap_board"]["score_of_15"] == 12
    assert ctx["past_swing_signals_this_ticker"][0]["outcome_7d"] == 2.0
    assert "owner_position" not in ctx
    ctx_d = json.loads(build_context("DDD", board, {}, swing, held,
                                     bands, setups, rs))
    assert ctx_d["owner_position"]["stop"] == 9.0

    table = summary_table(t, board, rs, held)
    report = render_report(bd, "signals", t, ["EEE"], table,
                           [("AAA", "narrative text")], [("CCC", "boom")])
    assert "12/15" in table and "## AAA" in report and "boom" in report
    assert "EEE" in report
    big = report + "x" * ISSUE_BODY_LIMIT
    assert len(issue_body(big)) <= ISSUE_BODY_LIMIT + 100
    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        main()
