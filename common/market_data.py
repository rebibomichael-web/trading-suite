"""Shared market-data helpers used across the trading-suite modules.

Pure data functions — no Flask, no globals tied to web routes, and no network
calls at import time (every fetch lives inside a function). Ported faithfully
from the original single-file stock-tracker app.
"""
import re
from datetime import datetime  # noqa: F401  (handy for callers)

import requests
import yfinance as yf
from bs4 import BeautifulSoup

# The ticker universe the app scans. Edit this list to track different symbols.
TICKERS = ['CRWD', 'ORCL', 'SNOW', 'SSYS', 'LMND', 'PLTR', 'BMNR', 'TSLA',
           'NVDA', 'GRNY', 'DE', 'MU', 'NVMI', 'SOFI', 'HOOD', 'NOW']


def calc_pivots_correct(ticker):
    """Classic daily floor-trader pivots from the prior completed session.

    Returns (s3, s2, s1, r1, r2, r3); any element may be None on failure.
    """
    try:
        hist = yf.Ticker(ticker).history(period="5d")
        if len(hist) < 2:
            return None, None, None, None, None, None
        h = hist['High'].iloc[-2]
        l = hist['Low'].iloc[-2]
        c = hist['Close'].iloc[-2]
        p = (h + l + c) / 3
        r1 = round(2 * p - l, 2)
        r2 = round(p + (h - l), 2)
        r3 = round(p + 2 * (h - l), 2)
        s1 = round(2 * p - h, 2)
        s2 = round(p - (h - l), 2)
        s3 = round(p - 2 * (h - l), 2)
        return s3, s2, s1, r1, r2, r3
    except Exception:
        return None, None, None, None, None, None


def calc_weekly_pivots(ticker):
    """Weekly S2 / S3 support levels. Returns (ws2, ws3); either may be None."""
    try:
        hist = yf.Ticker(ticker).history(period="1mo", interval="1wk")
        if len(hist) < 2:
            return None, None
        h = hist['High'].iloc[-2]
        l = hist['Low'].iloc[-2]
        c = hist['Close'].iloc[-2]
        p = (h + l + c) / 3
        return round(p - (h - l), 2), round(p - 2 * (h - l), 2)
    except Exception:
        return None, None


def get_ath_and_52w(ticker):
    """Return (all_time_high, 52_week_high); either may be None."""
    try:
        stock = yf.Ticker(ticker)
        hmax = stock.history(period="max")
        h1y = stock.history(period="1y")
        ath = round(hmax['High'].max(), 2) if len(hmax) else None
        w52h = round(h1y['High'].max(), 2) if len(h1y) else None
        return ath, w52h
    except Exception:
        return None, None


def get_barchart(ticker):
    """Scrape Barchart's opinion widget.

    Returns (opinion, strength, direction, yesterday, last_week, last_month);
    all 'N/A' on failure. Used by the (optional) tracker/swing view.
    """
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36'}
        url = f'https://www.barchart.com/stocks/quotes/{ticker}/opinion'
        r = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(r.text, 'html.parser')
        pct = (soup.find('span', class_='opinion-percent buy') or
               soup.find('span', class_='opinion-percent sell') or
               soup.find('span', class_='opinion-percent hold'))
        sig = (soup.find('span', class_='opinion-signal buy') or
               soup.find('span', class_='opinion-signal sell') or
               soup.find('span', class_='opinion-signal hold'))
        graphs = soup.find('div', class_='opinion-graphs')
        opinion = f"{pct.get_text(strip=True)} {sig.get_text(strip=True)}" if pct else 'N/A'
        strength = direction = 'N/A'
        if graphs:
            txt = graphs.get_text(strip=True)
            if 'Strength:' in txt and 'Direction:' in txt:
                parts = txt.replace('Strength:', '').replace('Direction:', '|').split('|')
                strength = parts[0].strip() if parts else 'N/A'
                direction = parts[1].strip() if len(parts) > 1 else 'N/A'
        yesterday = last_week = last_month = 'N/A'
        snap = soup.find('h3', string='Snapshot Opinion')
        if snap:
            txt = snap.find_parent().get_text(strip=True)
            ym = re.search(r'Yesterday(\d+%\s+\w+?)Last', txt)
            wm = re.search(r'Last Week(\d+%\s+\w+?)Last', txt)
            mm = re.search(r'Last Month(\d+%\s+\w+?)Snapshot', txt)
            yesterday = ym.group(1).strip() if ym else 'N/A'
            last_week = wm.group(1).strip() if wm else 'N/A'
            last_month = mm.group(1).strip() if mm else 'N/A'
        return opinion, strength, direction, yesterday, last_week, last_month
    except Exception:
        return 'N/A', 'N/A', 'N/A', 'N/A', 'N/A', 'N/A'
