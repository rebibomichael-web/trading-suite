"""Tracker module — the original stock-tracker "Stock Tracker" tab.

Per-ticker: price + day change, daily floor-trader pivots (S3..R3) with
nearest-level markers, and Barchart's opinion widget (opinion, strength,
direction, yesterday / last week / last month snapshots).

Ported faithfully from stock-tracker/app.py refresh_tracker(); the fetch
helpers live in common/market_data.py. A full pass over the 16-ticker
universe takes ~60-90s (a 1s politeness pause per Barchart request), so
get_data() accepts an optional on_row callback that receives the
rows-so-far after each ticker — the app shell uses it to publish partial
results while the scan is still running.
"""
import time
from datetime import datetime

import yfinance as yf

from common.market_data import TICKERS, calc_pivots_correct, get_barchart


def get_data(on_row=None):
    rows = []
    for ticker in TICKERS:
        try:
            hist = yf.Ticker(ticker).history(period="2d")
            if len(hist) == 0:
                continue
            price = round(hist['Close'].iloc[-1], 2)
            prev = round(hist['Close'].iloc[-2], 2) if len(hist) > 1 else price
            chg_pct = round(((price - prev) / prev) * 100, 2)
            s3, s2, s1, r1, r2, r3 = calc_pivots_correct(ticker)
            levels = [(s3, 's3'), (s2, 's2'), (s1, 's1'),
                      (r1, 'r1'), (r2, 'r2'), (r3, 'r3')]
            valid = [(l, n) for l, n in levels if l is not None]
            below = [(l, n) for l, n in valid if l < price]
            above = [(l, n) for l, n in valid if l > price]
            below_lvl = (max(below, key=lambda x: x[0])[1] if below
                         else (min(valid, key=lambda x: x[0])[1] if valid else None))
            above_lvl = (min(above, key=lambda x: x[0])[1] if above
                         else (max(valid, key=lambda x: x[0])[1] if valid else None))
            try:
                (opinion, strength, direction,
                 yesterday, last_week, last_month) = get_barchart(ticker)
                time.sleep(1)
            except Exception:
                opinion = strength = direction = 'N/A'
                yesterday = last_week = last_month = 'N/A'
            rows.append({
                'ticker': ticker, 'price': price, 'chg_pct': chg_pct,
                's3': s3, 's2': s2, 's1': s1, 'r1': r1, 'r2': r2, 'r3': r3,
                'below': below_lvl, 'above': above_lvl,
                'opinion': opinion, 'strength': strength, 'direction': direction,
                'yesterday': yesterday, 'last_week': last_week, 'last_month': last_month,
                'updated': datetime.now().strftime("%H:%M:%S"),
            })
            if on_row:
                on_row(rows.copy())
        except Exception as e:
            print(f"Tracker error {ticker}: {e}")
    return rows
