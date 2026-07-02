"""LEAP option scanner.

Finds long-dated (>= ~18 month) call options near each ticker's all-time high,
scores each setup 0-15, and returns the results as plain data. No Flask, no web
globals — call scan_leaps() to get a sorted list of dicts. Ported faithfully
from the original stock-tracker app.
"""
from datetime import datetime

import yfinance as yf

from common.market_data import TICKERS, calc_weekly_pivots, get_ath_and_52w


def get_leaps(ticker, ath):
    """Find the best (nearest-ATH strike) and furthest-dated LEAP calls.

    Returns (best, furthest); each is a dict or None.
    """
    try:
        stock = yf.Ticker(ticker)
        exps = stock.options
        today = datetime.today()
        best = furthest = None
        furthest_dte = 0
        for exp_str in exps:
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d")
            dte = (exp_date - today).days
            if dte < 540:
                continue
            chain = stock.option_chain(exp_str).calls
            if chain.empty:
                continue
            chain = chain[chain['strike'] > 0].copy()
            chain['dist'] = abs(chain['strike'] - ath)
            row = chain.loc[chain['dist'].idxmin()]
            strike = row['strike']
            prem = row['lastPrice'] if row['lastPrice'] > 0 else row['ask']
            iv = round(row['impliedVolatility'] * 100, 1) if row['impliedVolatility'] else None
            if prem and prem > 0:
                if best is None or abs(strike - ath) < abs(best['strike'] - ath):
                    best = {'strike': strike, 'premium': round(prem, 2), 'dte': dte, 'exp': exp_str, 'iv': iv}
                if dte > furthest_dte:
                    furthest_dte = dte
                    furthest = {'strike': strike, 'premium': round(prem, 2), 'dte': dte, 'exp': exp_str, 'iv': iv}
        return best, furthest
    except Exception:
        return None, None


def score_leap(price, ath, prem_pct, leverage, vs_s2, vs_s3, dte):
    """Score a LEAP setup 0-15 across five factors. Returns (score, breakdown)."""
    score = 0
    bd = {}
    # Drawdown from ATH
    if ath and price:
        d = ((ath - price) / ath) * 100
        p1 = 3 if d >= 30 else (2 if d >= 15 else 1)
    else:
        p1 = 0
    score += p1; bd['ATH Drawdown'] = p1
    # Premium efficiency
    if prem_pct is not None:
        p2 = 3 if prem_pct < 10 else (2 if prem_pct < 15 else 1)
    else:
        p2 = 0
    score += p2; bd['Prem Efficiency'] = p2
    # Leverage
    if leverage is not None:
        p3 = 3 if leverage >= 8 else (2 if leverage >= 4 else 1)
    else:
        p3 = 0
    score += p3; bd['Leverage'] = p3
    # Time horizon (DTE)
    if dte:
        p4 = 3 if dte >= 720 else (2 if dte >= 540 else 1)
    else:
        p4 = 0
    score += p4; bd['Time Horizon'] = p4
    # Proximity to weekly S2 / S3
    if vs_s3 is not None and abs(vs_s3) <= 5:
        p5 = 3
    elif vs_s2 is not None and abs(vs_s2) <= 5:
        p5 = 2
    elif vs_s2 is not None and abs(vs_s2) <= 15:
        p5 = 1
    else:
        p5 = 0
    score += p5; bd['S2/S3 Level'] = p5
    return score, bd


def scan_leaps(tickers=None):
    """Scan every ticker for LEAP setups; return a scored, sorted list of dicts.

    Makes live network calls (yfinance) and can take 1-2 minutes.
    """
    tickers = tickers or TICKERS
    rows = []
    for ticker in tickers:
        try:
            stock = yf.Ticker(ticker)
            hist = stock.history(period="1d")
            if len(hist) == 0:
                continue
            price = round(hist['Close'].iloc[-1], 2)
            ath, w52h = get_ath_and_52w(ticker)
            ws2, ws3 = calc_weekly_pivots(ticker)
            leap, furthest = get_leaps(ticker, ath) if ath else (None, None)
            prem_pct = leverage = None
            if leap and price and leap['premium']:
                prem_pct = round((leap['premium'] / price) * 100, 1)
                leverage = round(price / leap['premium'], 1)
            vs_s2 = round(((price - ws2) / ws2) * 100, 1) if ws2 else None
            vs_s3 = round(((price - ws3) / ws3) * 100, 1) if ws3 else None
            pct_52w = round(((w52h - price) / w52h) * 100, 1) if w52h else None
            closest = None
            if vs_s2 is not None and vs_s3 is not None:
                closest = 's2' if abs(vs_s2) < abs(vs_s3) else 's3'
            elif vs_s2 is not None:
                closest = 's2'
            elif vs_s3 is not None:
                closest = 's3'
            sc, bd = score_leap(price, ath, prem_pct, leverage, vs_s2, vs_s3,
                                leap['dte'] if leap else None)
            if vs_s3 is not None and abs(vs_s3) <= 5:
                signal = 'S3 ALERT'
            elif vs_s2 is not None and abs(vs_s2) <= 5:
                signal = 'S2 ALERT'
            elif sc >= 12:
                signal = 'STRONG SETUP'
            elif sc >= 8:
                signal = 'MONITOR'
            else:
                signal = '—'
            rows.append({
                'ticker': ticker, 'price': price, 'w52h': w52h,
                'pct_52w': pct_52w, 'prem_pct': prem_pct, 'leverage': leverage,
                'ws2': ws2, 'ws3': ws3, 'vs_s2': vs_s2, 'vs_s3': vs_s3,
                'closest': closest, 'score': sc, 'breakdown': bd,
                'signal': signal, 'leap': leap, 'furthest': furthest,
            })
        except Exception as e:
            print(f"LEAP error {ticker}: {e}")
    rows.sort(key=lambda r: -r['score'])
    return rows
