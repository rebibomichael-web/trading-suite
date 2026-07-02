"""Fidelity-CSV parsing + FIFO trade matching.

Lifted VERBATIM (via AST) from the user's desktop trade_journal.py — this is the
window-free data core only. No tkinter, no matplotlib, no sqlite, so it is safe
to import inside the web app.
"""
import re
from collections import defaultdict
from datetime import datetime
from io import StringIO

import pandas as pd


OPTION_SYMBOL_RE = re.compile(r"^-?[A-Z]{1,6}\d{6}[CP]\d")


def looks_like_option_symbol(sym):
    return bool(OPTION_SYMBOL_RE.match(str(sym).strip().upper()))


def default_method_for(leg):
    """Smart default for an UNTAGGED leg, so options/assignment byproducts never
    silently land in the Swing bucket (which previously skewed swing analytics):
      • real option symbol   → 'LEAP Strategy'  (it's an option, not a swing trade)
      • assignment-derived   → 'Excluded'       (wheel byproduct, not a directional bet)
      • otherwise            → 'Swing Trader'
    Saved tags always take precedence over this — it only fills the default.
    NOTE: a regular stock buy on an options-play ticker (e.g. a fractional BMNR
    remnant) is indistinguishable from a genuine swing trade by transaction data
    alone; that case is left as Swing and needs an explicit user tag."""
    if looks_like_option_symbol(leg.get("ticker", "")):
        return "LEAP Strategy"
    if leg.get("from_assignment"):
        return "Excluded"
    return "Swing Trader"


def _norm_col(c):
    """Normalize a column name: lowercase, collapse any non-alphanumeric run to _."""
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", c.strip().lower())).strip("_")


def _parse_num(val):
    """Parse a numeric cell that may contain $, commas, or parentheses for negatives."""
    if pd.isna(val) or str(val).strip() in ("", "--", "nan", "n/a", "N/A"):
        return 0.0
    s = str(val).replace("$", "").replace(",", "").strip()
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    try:
        return float(s)
    except ValueError:
        return 0.0


def _find_header_row(lines, *required_phrases, max_scan=20):
    """
    Return the index of the first line (within max_scan non-empty lines) whose
    lowercased text contains every phrase in required_phrases.
    Falls back to scanning the full file if not found in the first pass.
    """
    def matches(line):
        low = line.strip().lower()
        return all(ph in low for ph in required_phrases)

    checked = 0
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        if matches(line):
            return i
        checked += 1
        if checked >= max_scan:
            break

    # Full-file fallback
    for i, line in enumerate(lines):
        if matches(line):
            return i
    return None


def _load_df(lines, header_idx):
    """Build a DataFrame from lines starting at header_idx, skipping blank rows."""
    data_lines = [l for l in lines[header_idx:] if l.strip()]
    df = pd.read_csv(StringIO("\n".join(data_lines)), dtype=str, skipinitialspace=True)
    df.columns = [_norm_col(c) for c in df.columns]
    return df


def _parse_trade_history(lines, header_idx):
    """
    Parse a Fidelity trade history CSV (Run Date / Action / Symbol format).
    Returns (closed_legs, open_positions, orphan_sells) after FIFO matching.
    """
    df = _load_df(lines, header_idx)

    required = {"run_date", "action", "symbol"}
    if not required.issubset(set(df.columns)):
        raise ValueError(
            f"Trade history header found but missing columns.\n"
            f"Found: {list(df.columns)}\nNeed: {sorted(required)}"
        )

    skip_keywords = ["DIVIDEND", "REINVEST", "TRANSFER", "MARGIN INTEREST",
                     "JOURNALED", "ELECTRONIC FUNDS", "DIRECT DEBIT",
                     "SHORT TERM CAP GAIN", "LONG TERM CAP GAIN"]

    transactions = []
    for _, row in df.iterrows():
        action = str(row.get("action", "")).strip().upper()
        symbol = str(row.get("symbol", "")).strip().upper()

        if not symbol or symbol == "NAN" or len(symbol) > 20:
            continue
        if any(kw in action for kw in skip_keywords):
            continue

        is_buy  = "BOUGHT" in action or "BUY" in action
        is_sell = "SOLD"   in action or "SELL" in action
        if not is_buy and not is_sell:
            continue

        desc = str(row.get("description", "")).strip()
        is_option = (any(kw in action for kw in ("CALL", "PUT", "OPTION")) or
                     any(kw in desc.upper() for kw in ("CALL", "PUT")))

        # Assignment origin: shares acquired via option assignment (e.g.
        # "YOU BOUGHT ASSIGNED PUTS AS OF ...") are a wheel/options byproduct,
        # NOT a directional swing decision. Flag so they don't default to Swing.
        from_assignment = "ASSIGNED" in action

        qty        = abs(_parse_num(row.get("quantity",   0)))
        price      = abs(_parse_num(row.get("price",      0)))
        amount     =     _parse_num(row.get("amount",     0))
        commission = abs(_parse_num(row.get("commission", 0)))
        fees       = abs(_parse_num(row.get("fees",       0)))

        date_str = str(row.get("run_date", "")).strip()
        dt = None
        for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%d/%m/%Y"):
            try:
                dt = datetime.strptime(date_str, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            continue

        ticker_label = f"{symbol} ({desc[:40]})" if is_option and desc else symbol

        amount_per_unit = abs(amount) / qty if qty > 0 else price
        transactions.append({
            "date":            dt,
            "action":          "BUY" if is_buy else "SELL",
            "symbol":          symbol,
            "ticker_label":    ticker_label,
            "qty":             qty,
            "price":           price,
            "amount":          abs(amount),
            "amount_per_unit": amount_per_unit,
            "commission":      commission,
            "fees":            fees,
            "is_option":       is_option,
            "description":     desc,
            "from_assignment": from_assignment,
        })

    transactions.sort(key=lambda x: x["date"])
    return fifo_match(transactions)


def _parse_positions(lines, header_idx):
    """
    Parse a Fidelity portfolio positions CSV (Symbol / Quantity / Last Price format).
    Returns ([], open_positions) — no FIFO matching possible from a snapshot.
    """
    df = _load_df(lines, header_idx)

    def col(*keywords):
        """Return the first column name that contains every keyword."""
        for c in df.columns:
            if all(kw in c for kw in keywords):
                return c
        return None

    col_qty       = col("quantity")
    col_last      = col("last_price") or col("last", "price")
    col_cost_tot  = col("cost_basis_total") or col("cost", "basis", "total") or col("cost", "total")
    col_avg_cost  = col("average_cost_basis") or col("average", "cost") or col("avg", "cost")
    col_gl_dollar = col("total_gain_loss_dollar") or col("total", "gain") or col("total", "loss")
    col_gl_pct    = col("total_gain_loss_percent") or col("total", "gain", "percent")
    col_desc      = "description" if "description" in df.columns else None
    col_type      = "type"        if "type"        in df.columns else None

    # ── Validation gate (fail loud; do NOT fabricate) ───────────────
    # Detects column misalignment before any record is built. The known
    # trigger: Fidelity quotes the entire header as one string and appends a
    # trailing comma to every data row, so data rows carry one more field than
    # the header and every value lands one column to the left. Symptom: the
    # quantity column holds currency-formatted strings ($, %) instead of bare
    # numbers. We refuse rather than emit $0 fabricated positions.
    #
    # NOTE: this gate only detects. The dialect itself is fixed in a separate,
    # later change — keeping detection and parsing as independent variables.
    def _looks_like_quantity(series):
        """True if the column reads like real share/contract counts."""
        checked = parsed = 0
        for raw in series:
            s = str(raw).strip()
            if s in ("", "--", "nan", "n/a", "N/A"):
                continue
            checked += 1
            # A genuine quantity has no currency or percent markers.
            if "$" in s or "%" in s:
                continue
            try:
                float(s.replace(",", ""))
                parsed += 1
            except ValueError:
                pass
        if checked == 0:
            return True   # nothing to judge — let downstream emptiness handling deal with it
        return (parsed / checked) >= 0.8

    if col_qty is not None and not _looks_like_quantity(df[col_qty]):
        sample_vals = [str(v).strip() for v in df[col_qty].head(4)
                       if str(v).strip() not in ("", "nan")]
        raise ValueError(
            "Positions CSV column misalignment detected — refusing to parse.\n\n"
            f"The '{col_qty}' column should contain share/contract counts, but it holds "
            f"currency/percent values like: {sample_vals}\n\n"
            "Likely cause: this Fidelity export quotes the entire header row as a single "
            "string and adds a trailing comma to each data row, so every value is shifted "
            "one column. The app will not emit fabricated $0 positions from misaligned data.\n\n"
            "This is a known format issue; a dialect fix is planned. For now, re-export "
            "without the quoted header, or load a trade-history CSV instead."
        )

    open_positions = []

    for _, row in df.iterrows():
        symbol = str(row.get("symbol", "")).strip().upper()

        # Skip totals, blanks, pending-activity sentinel rows
        if (not symbol or symbol == "NAN" or
                symbol.startswith("**") or symbol.startswith("--") or
                "PENDING" in symbol or "TOTAL" in symbol):
            continue

        qty = abs(_parse_num(row.get(col_qty, 0))) if col_qty else 0.0
        if qty == 0.0:
            continue

        last_price = _parse_num(row.get(col_last,     0)) if col_last     else 0.0
        buy_cost   = _parse_num(row.get(col_cost_tot, 0)) if col_cost_tot else 0.0
        avg_cost   = _parse_num(row.get(col_avg_cost, 0)) if col_avg_cost else (buy_cost / qty if qty else 0.0)
        gl_dollar  = _parse_num(row.get(col_gl_dollar,0)) if col_gl_dollar else 0.0
        gl_pct     = _parse_num(row.get(col_gl_pct,  0)) if col_gl_pct   else 0.0

        desc       = str(row.get(col_desc, "")).strip() if col_desc else ""
        asset_type = str(row.get(col_type, "")).strip().upper() if col_type else ""

        is_option = (
            "OPTION" in asset_type or
            any(kw in desc.upper() for kw in ("CALL", "PUT")) or
            (len(symbol) > 10 and any(c.isdigit() for c in symbol))
        )

        if is_option:
            qty_str      = f"{int(qty)} contract{'s' if qty != 1 else ''}"
            ticker_label = f"{symbol} ({desc[:40]})" if desc else symbol
        elif qty == int(qty):
            qty_str      = f"{int(qty)} sh"
            ticker_label = symbol
        else:
            qty_str      = f"{qty:.4f} sh"
            ticker_label = symbol

        trade_key = f"{symbol}-POSITIONS-{qty:.4f}"

        open_positions.append({
            "ticker":       symbol,
            "ticker_label": ticker_label,
            "buy_date":     None,          # snapshot — no purchase date available
            "sell_date":    None,
            "qty":          qty,
            "qty_str":      qty_str,
            "buy_price":    avg_cost,
            "sell_price":   last_price,
            "buy_cost":     buy_cost,
            "sell_proceeds":0.0,
            "commission":   0.0,
            "pl_dollar":    gl_dollar,
            "pl_pct":       gl_pct,
            "hold_days":    0,
            "is_option":    is_option,
            "description":  desc,
            "trade_key":    trade_key,
            "is_open":      True,
        })

    return [], open_positions, []


def parse_fidelity_csv(filepath):
    """
    Auto-detect Fidelity CSV format and parse.
    Supports:
      • Trade history  (Run Date / Action / Symbol / Price / Amount …)
      • Positions snapshot  (Symbol / Quantity / Last Price / Cost Basis …)
    Returns (closed_legs, open_positions, orphan_sells).
    """
    with open(filepath, "r", encoding="utf-8-sig") as f:
        raw = f.read()
    lines = raw.splitlines()

    # ── Trade history format ──
    idx = _find_header_row(lines, "run date", "action", "symbol")
    if idx is not None:
        return _parse_trade_history(lines, idx)

    # ── Positions / portfolio format ──
    idx = _find_header_row(lines, "symbol", "quantity")
    if idx is not None:
        # Extra guard: must also have a price-like or cost-like column nearby
        header_low = lines[idx].lower()
        if any(kw in header_low for kw in ("last price", "cost basis", "current value")):
            return _parse_positions(lines, idx)

    sample = "\n".join(f"  [{i}] {l}" for i, l in enumerate(lines[:5]))
    raise ValueError(
        "Unrecognized Fidelity CSV format.\n"
        "Expected either:\n"
        "  • Trade history  — columns: Run Date, Action, Symbol, Price, Amount …\n"
        "  • Positions CSV  — columns: Symbol, Quantity, Last Price, Cost Basis …\n\n"
        f"First 5 rows of your file:\n{sample}"
    )


def fifo_match(transactions):
    """
    FIFO matching engine.
    Returns (closed_legs, open_positions, orphan_sells).
    Orphan sells = SELLs with no matching BUY in this CSV.
    """
    by_symbol = defaultdict(list)
    for tx in transactions:
        by_symbol[tx["symbol"]].append(tx)

    closed_legs = []
    open_positions = []
    orphan_sells = []   # SELLs with no prior BUY in this CSV — preserved instead of silently dropped

    for symbol, txs in by_symbol.items():
        buy_queue = []
        seen_keys = {}

        for tx in txs:
            if tx["action"] == "BUY":
                comm_per = tx["commission"] / tx["qty"] if tx["qty"] > 0 else 0
                fees_per = tx["fees"] / tx["qty"] if tx["qty"] > 0 else 0
                buy_queue.append({
                    "date": tx["date"],
                    "qty_remaining": tx["qty"],
                    "price": tx["price"],
                    "amount_per_unit": tx["amount_per_unit"],
                    "comm_per": comm_per,
                    "fees_per": fees_per,
                    "ticker_label": tx["ticker_label"],
                    "is_option": tx["is_option"],
                    "description": tx["description"],
                    "symbol": symbol,
                    "from_assignment": tx.get("from_assignment", False),
                })

            elif tx["action"] == "SELL":
                sell_qty = tx["qty"]
                sell_price = tx["price"]
                sell_comm = tx["commission"]
                sell_fees = tx["fees"]
                sell_comm_per = sell_comm / sell_qty if sell_qty > 0 else 0
                sell_fees_per = sell_fees / sell_qty if sell_qty > 0 else 0

                while sell_qty > 1e-8 and buy_queue:
                    lot = buy_queue[0]
                    match_qty = min(sell_qty, lot["qty_remaining"])

                    buy_cost = match_qty * lot["amount_per_unit"]
                    buy_comm = match_qty * lot["comm_per"]
                    buy_fees_total = match_qty * lot["fees_per"]
                    sell_proceeds = match_qty * tx["amount_per_unit"]
                    sell_comm_portion = match_qty * sell_comm_per
                    sell_fees_portion = match_qty * sell_fees_per

                    total_costs = buy_comm + buy_fees_total + sell_comm_portion + sell_fees_portion
                    pl_dollar = sell_proceeds - buy_cost - total_costs
                    pl_pct = (pl_dollar / buy_cost * 100) if buy_cost > 0 else 0.0
                    hold_days = (tx["date"] - lot["date"]).days

                    if lot["is_option"]:
                        qty_str = f"{int(match_qty)} contract{'s' if match_qty > 1 else ''}"
                    elif match_qty == int(match_qty):
                        qty_str = f"{int(match_qty)} sh"
                    else:
                        qty_str = f"{match_qty:.4f} sh"

                    base_key = f"{symbol}-{lot['date'].strftime('%Y%m%d')}-{tx['date'].strftime('%Y%m%d')}-{match_qty:.4f}"
                    seen_keys[base_key] = seen_keys.get(base_key, 0) + 1
                    trade_key = base_key if seen_keys[base_key] == 1 else f"{base_key}-{seen_keys[base_key]}"

                    closed_legs.append({
                        "ticker": symbol,
                        "ticker_label": lot["ticker_label"],
                        "buy_date": lot["date"],
                        "sell_date": tx["date"],
                        "qty": match_qty,
                        "qty_str": qty_str,
                        "buy_price": lot["price"],
                        "sell_price": sell_price,
                        "buy_cost": buy_cost,
                        "sell_proceeds": sell_proceeds,
                        "commission": total_costs,
                        "pl_dollar": round(pl_dollar, 2),
                        "pl_pct": round(pl_pct, 2),
                        "hold_days": hold_days,
                        "is_option": lot["is_option"],
                        "description": lot["description"],
                        "trade_key": trade_key,
                        "is_open": False,
                        "from_assignment": lot.get("from_assignment", False),
                    })

                    lot["qty_remaining"] -= match_qty
                    sell_qty -= match_qty
                    if lot["qty_remaining"] < 1e-8:
                        buy_queue.pop(0)

                # If sell quantity remains after the buy_queue was exhausted,
                # this SELL has no matching BUY in this CSV. Preserve as orphan
                # — do NOT silently drop. (Defect A fix.)
                if sell_qty > 1e-8:
                    if tx["is_option"]:
                        orphan_qty_str = f"{int(sell_qty)} contract{'s' if sell_qty > 1 else ''}"
                    elif sell_qty == int(sell_qty):
                        orphan_qty_str = f"{int(sell_qty)} sh"
                    else:
                        orphan_qty_str = f"{sell_qty:.4f} sh"

                    orphan_proceeds = sell_qty * tx["amount_per_unit"]
                    orphan_key = (f"{symbol}-ORPHAN-{tx['date'].strftime('%Y%m%d')}-"
                                  f"{sell_qty:.4f}")
                    seen_keys[orphan_key] = seen_keys.get(orphan_key, 0) + 1
                    if seen_keys[orphan_key] > 1:
                        orphan_key = f"{orphan_key}-{seen_keys[orphan_key]}"

                    orphan_sells.append({
                        "ticker":        symbol,
                        "ticker_label":  tx["ticker_label"],
                        "buy_date":      None,
                        "sell_date":     tx["date"],
                        "qty":           sell_qty,
                        "qty_str":       orphan_qty_str,
                        "buy_price":     0.0,
                        "sell_price":    sell_price,
                        "buy_cost":      0.0,
                        "sell_proceeds": orphan_proceeds,
                        "commission":    0.0,
                        "pl_dollar":     0.0,
                        "pl_pct":        0.0,
                        "hold_days":     0,
                        "is_option":     tx["is_option"],
                        "description":   tx["description"],
                        "trade_key":     orphan_key,
                        "is_open":       False,
                        "is_orphan":     True,
                    })

        # Remaining buy lots = open positions
        for lot in buy_queue:
            if lot["qty_remaining"] > 1e-8:
                qty = lot["qty_remaining"]
                if lot["is_option"]:
                    qty_str = f"{int(qty)} contract{'s' if qty > 1 else ''}"
                elif qty == int(qty):
                    qty_str = f"{int(qty)} sh"
                else:
                    qty_str = f"{qty:.4f} sh"

                buy_cost = qty * lot["amount_per_unit"]
                base_key = f"{lot['symbol']}-{lot['date'].strftime('%Y%m%d')}-OPEN-{qty:.4f}"
                seen_keys[base_key] = seen_keys.get(base_key, 0) + 1
                trade_key = base_key if seen_keys[base_key] == 1 else f"{base_key}-{seen_keys[base_key]}"

                open_positions.append({
                    "ticker": lot["symbol"],
                    "ticker_label": lot["ticker_label"],
                    "buy_date": lot["date"],
                    "sell_date": None,
                    "qty": qty,
                    "qty_str": qty_str,
                    "buy_price": lot["price"],
                    "sell_price": 0.0,
                    "buy_cost": buy_cost,
                    "sell_proceeds": 0.0,
                    "commission": 0.0,
                    "pl_dollar": 0.0,
                    "pl_pct": 0.0,
                    "hold_days": (datetime.now() - lot["date"]).days,
                    "is_option": lot["is_option"],
                    "description": lot["description"],
                    "trade_key": trade_key,
                    "is_open": True,
                    "from_assignment": lot.get("from_assignment", False),
                })

    closed_legs.sort(key=lambda x: x["sell_date"])
    open_positions.sort(key=lambda x: x["buy_date"])
    orphan_sells.sort(key=lambda x: x["sell_date"])
    return closed_legs, open_positions, orphan_sells


def monthly_equivalent(total_return_frac, window_days):
    """Geometric monthly-equivalent of a total fractional return over window_days.
    Scale-invariant: the same underlying performance gives the same monthly figure
    regardless of window length. Falls back to linear if capital is more than wiped
    out (1 + r <= 0) to avoid a math domain error."""
    if window_days <= 0:
        return 0.0
    if 1.0 + total_return_frac > 0.0:
        return ((1.0 + total_return_frac) ** (30.0 / window_days) - 1.0) * 100.0
    return total_return_frac * (30.0 / window_days) * 100.0


def compute_time_weighted_return(legs, window_start, window_end):
    """
    Return (avg_capital_deployed, monthly_return_pct, turnover_x) for a list of
    closed legs over a window [window_start, window_end].

    The honest answer to "what % am I earning?" for a strategy that recycles
    capital. Summed buy-cost double-counts the same dollars across consecutive
    trades — this metric does not.

    Method:
      • For each leg, clip [buy_date, sell_date] to [window_start, window_end].
      • capital_days = Σ (buy_cost × days_held_INSIDE_window).  Floor at 1 day
        per leg so same-day trades still count.
      • avg_capital   = capital_days / window_days
      • monthly_pct   = ((1 + total_pl/avg_capital) ** (30/window_days) - 1) × 100
                       (geometric — compounding-aware; scale-invariant across windows so
                        the same underlying performance yields the same monthly figure
                        whether you look at 30d, 90d, or YTD)
      • turnover      = Σ buy_cost / avg_capital   (how many times $1 of deployed
                         capital was "spent" on new positions over the window)

    Returns (0.0, 0.0, 0.0) when there's nothing to measure.
    """
    if not legs or window_start is None or window_end is None or window_end <= window_start:
        return 0.0, 0.0, 0.0

    window_days = (window_end - window_start).days
    if window_days <= 0:
        return 0.0, 0.0, 0.0

    capital_days = 0.0
    total_cost = 0.0
    total_pl = 0.0
    for leg in legs:
        buy  = leg.get("buy_date")
        sell = leg.get("sell_date")
        if buy is None or sell is None:
            continue
        eff_start = max(buy,  window_start)
        eff_end   = min(sell, window_end)
        days_in_window = max((eff_end - eff_start).days, 1)
        capital_days += leg["buy_cost"] * days_in_window
        total_cost   += leg["buy_cost"]
        total_pl     += leg["pl_dollar"]

    if capital_days <= 0:
        return 0.0, 0.0, 0.0

    avg_capital = capital_days / window_days
    period_return_frac = total_pl / avg_capital
    # Guard the geometric formula against (1 + r) ≤ 0  (i.e. capital wiped out
    # and then some). Fall back to linear in that pathological case; never let
    # the app crash on a math domain error.
    if 1.0 + period_return_frac > 0.0:
        monthly_pct = ((1.0 + period_return_frac) ** (30.0 / window_days) - 1.0) * 100.0
    else:
        monthly_pct = period_return_frac * (30.0 / window_days) * 100.0
    turnover    = total_cost / avg_capital if avg_capital > 0 else 0.0
    return avg_capital, monthly_pct, turnover


