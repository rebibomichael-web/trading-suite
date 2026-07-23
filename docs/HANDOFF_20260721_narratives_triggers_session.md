# Session handoff — 2026-07-21 (remote claude.ai session)

Complete record of a remote Claude Code session (July 20–21, 2026) for any
other session to load as context. Written by the session itself; everything
below was verified against live data/runs at the time of writing.
Session ref: `claude.ai/code/session_01Qf9a9M7H1bcwXhvMWDVzQV`.

---

## 1. SNPS analysis (the session's origin)

Owner observed "both swing and leap like SNPS, high for a few weeks in LEAP."
Findings from trading-data + web:

- **LEAP:** score 8–9 (MONITOR) since June 8, jumped 10→12/15 ("S2 ALERT")
  on 7/15–7/19 as price fell $477→$384 (−19%). 12 was #2 on the whole 7/19
  board (ISRG 13 was #1).
- **Swing:** "⚡ ARB BUY" fired 7/17 ($378.20, score 88, arb_z −3.24) and
  7/19 ($384.28, score 83, vol 2.4×). Setup: deeply_oversold_+_arb_dislocation.
- **Cautions from the owner's own data:**
  - 11 swing signals on SNPS 5/29–6/29 — every one negative at 7/14/21d
    (falling knife). One actual trade won anyway (+5.8%, entered 6/16 exited 6/23).
  - ARB-dislocation setup class overall: mean −1.7% @7d, 49% win (n=114) vs
    +3.5% / 65% for all other setups. Weakest setup class.
  - LEAP band stats: strong(≥10) mean +5.9% @30d, 61% win (n=390) — best band.
- **The 7/17 drop was a thesis event, not technicals:** Moonshot AI's Kimi K3
  demoed an autonomous full chip-design flow on open-source EDA tools,
  challenging the EDA moat (CDNS −9.6% same day); plus a July 7 Reuters story
  on Synopsys discontinuing manufacturing process-control products.
- **Position:** owner holds 0.795 sh @ $376.96 avg (~$300, entered 7/17);
  suggested stop $338.61, TP1 $415.30, TP2 $434.48.
- **Quirk found:** `event_penalty` in swing signal sinks is misnamed — in
  `swing_core.py` it's `evt_pen`, a **+20 bonus** when extended-hours change
  < −3%; real earnings penalties are negative (−30/−15).
- Owner's 7/13 SIGNAL_CHRONICITY study: no evidence chronic signaling predicts
  worse outcomes (swing: underpowered null; LEAP: not yet testable).

## 2. LOW analysis

- LEAP 7–10 every night since June 8; six of last nine scans at 10/15 STRONG.
  Score is **structural**: Prem Efficiency 2/2 + Leverage 2/2 (Jan-2028 $290C
  ≈ $10 = 4.8% of spot, ~21× leverage, IV 34) + ATH drawdown 2/3 = 6 standing
  points; the 9↔10 flicker is just RSI/S2 noise. Unlike SNPS, no event.
- **$208 shelf, third test:** only swing signal ever (6/2 @ $207.70) won
  +4.7%/7d +9.5%/21d; 6/8 LEAP STRONG @ $209.11 → +5.3%/7d; July closes
  $207.71–208.73 four of five scans. July STRONGs at $213–215 are −2.2/−2.4%
  @7d so far; mid-June 30d outcomes all negative (slow bleed, bounces only
  from the shelf).
- Owner's own band finding [SUSPECTED]: score 10–11 is the WEAKEST strong
  band at 60d (sweet spot 12–13). LOW never printed above 10.
- Market: macro grind not thesis shock — mortgage rates, housing turnover,
  flat-to-+2% comps guidance, Zacks #4, **earnings Aug 19**. Option is
  illiquid (bid 9.40/ask 12.50, vol 3, OI 165) — limit orders only.
- Suggested (not added): manual triggers below ~$205 (shelf break) and above
  ~$216 (range reclaim).

## 3. Built this session — trading-suite (all pushed to main)

Commits: `82e1fb0`, `35cd8f0`, `9304c1e`, `2a4bbb9`, `d35875c` (workflow state),
`9e2fec3`. All follow the halftime/youtube pipeline pattern.

### Market narratives pipeline (push-button)
- `scripts/market_narratives.py` + `.github/workflows/market-narratives.yml`
  (workflow_dispatch = the button; no cron yet by design).
- Reads trading-data checkout (LEAP score_history + recommendations, swing
  signals + state), picks targets by scope — `signals` (LEAP ≥10 or S2/S3
  alerts + swing fires ≤4d + held positions; ~25 names), `monitor`/`all`
  (~76, NARRATIVE_MAX 80), or explicit tickers input.
- Per ticker: builds a stats context block (score trajectory, band stats,
  setup stats, per-name signal history, position), asks Claude **with web
  search** for sections: What happened / Thesis or technicals? / What the
  scanners see / Your own data says / Bottom line + Sources.
- Delivery: GitHub issue (assigned to owner → email+push) + archive
  `summaries/narratives/<date>.md`.
- **Free-run auth:** prefers CLAUDE_CODE_OAUTH_TOKEN (subscription CLI,
  `--allowedTools WebSearch`) over ANTHROPIC_API_KEY; CLI subprocess strips
  the API key from env so the CLI can't silently bill; `NARRATIVE_AUTH=api`
  forces the metered path. Verified the OAuth secret exists (halftime run
  installed the CLI). Preflight logs which path ran.
- Offline `--selftest` covers selection/stats/extraction/auth-routing.

### Trigger watch (price-level notifications)
- `scripts/trigger_watch.py` + `.github/workflows/trigger-watch.yml`
  (cron `*/30 13-20 * * 1-5` + dispatch). Free: yfinance, no Claude calls.
- Three trigger sources merged (priority manual > position > narrative),
  deduped by `ticker|when|level`:
  1. `triggers.json` `manual` list (owner's switches; `"enabled": false` pauses),
  2. every active swing trade's ATR stop / TP1 / TP2 from trading-data
     `swing/state.json` (30 levels across the 10 held names at build time),
  3. `narrative` list — rewritten by each narratives run (the model emits a
     fenced ```triggers``` JSON block per ticker; script parses it into
     triggers.json and renders a "🔔 Watching:" line in the report).
- First cross → `alert.md` → GitHub issue **assigned to owner** (assignment
  guarantees email + GitHub-mobile push regardless of watch settings).
  `fired` map in triggers.json prevents re-alerts; delete a key to re-arm;
  stale fired keys pruned after 30d.
- **End-to-end verified live:** test trigger fired through a real workflow
  run → issue #46 created with real price (SNPS $383.72), then cleaned up
  (trigger removed, fired key cleared, issue closed). yfinance confirmed
  working on Actions runners.

### Open setup items (owner-side)
- [ ] **TRADING_DATA_TOKEN secret** in trading-suite (fine-grained PAT,
  Contents:Read on trading-data). Until set: narratives workflow fails at
  preflight with instructions; trigger watch runs but sees only
  manual+narrative triggers (no auto position exits).
- [ ] First narratives run (Actions → Market narratives → Run workflow).
- [ ] Optional: make narratives daily — add the 3-line `schedule:` block
  commented at the top of market-narratives.yml.
- [x] GitHub mobile app installed + notifications allowed (owner confirmed).

## 4. Dell-side SYMBOL build — pushed + independently reviewed

The parallel Dell session built the 🔎 SYMBOL tab in swing_trader.py:
C1 `09580c5` (build) → C2 `58128f6` (retarget) → C3 `2a1d48f` (retire;
tabs now SIGNALS·SYMBOL·TRADES·PERFORMANCE), pushed to trading-src main.

**This session's independent audit of the pushed diff (7e44b51..2a1d48f) — PASSED:**
- Only swing_trader.py changed (+414/−32); swing_core untouched; linear history.
- Retire completeness verified by enclosing-def + caller analysis: all refs
  to retired widgets (`_tc _tb _tc_hdr _bc _tres _tdiag _abtn`) live only in
  the dead set {_analyze, _analyze_bg, _render_az, _build_research,
  _build_diagnose, _run_diagnose} or comments; dead set has zero live callers.
  (`_abtn` checked specifically — widget, not frame, could have been missed.)
- `_sym_analyze_bg` thread contract clean (all Tk via `_ui_call`).
- All `_sym*`/`_tsym` attrs read are assigned (no typo'd StringVars).
- All live `_nb.select` sites target surviving frames; the one
  `_nb.select(self._tc)` landmine is inside dead `_render_az`.
- **Recommendation:** do the planned dead-code sweep soon — the dead bodies
  contain real-looking calls (`_nb.select(self._tc)`, `_abtn.configure`) that
  crash if any future edit re-wires a caller; also update `_ui_call`'s
  docstring (still cites _analyze/_analyze_bg).
- Still pending on the Dell for sign-off: R4 perf reproducer, HELD/NOT-HELD
  screenshots, render_chart-only stall confirmation (that session is driving
  them).

## 5. Standing facts / conventions learned

- LEAP scoring: `leap_scoring.py` pillars /15, STRONG ≥10, MONITOR ≥7
  (deployed stock-tracker app.py has an older /15 scorer without RSI pillar —
  data comes from the Dell scanner, not the Render app).
- trading-src is a one-way Dell→GitHub mirror (hourly auto-sync DOES push);
  never ship code changes to it from GitHub. trading-suite is GitHub-native.
- Notification pattern: `gh issue create --assignee "$GITHUB_REPOSITORY_OWNER"`
  = guaranteed email + mobile push; bare issue creation only reaches watchers.
- The screenshotted "Flips to a BUY if…" plain-lingo text comes from
  `trading-src/swing/diagnose.py` (now surfaced in the SYMBOL tab's holding
  checkup); its flip levels are the natural feed for triggers.json manual
  entries.
- 10 active swing positions at time of writing: AA, GM, ORCL, PFE, CSCO,
  NVDA, RIOT, CDNS, ISRG, SNPS.
