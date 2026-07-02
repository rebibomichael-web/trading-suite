"""Trading Suite — one Flask app, three tabs: LEAP, Swing, Journal.

Each area is a self-contained module under leap/, swing/, journal/. This file
is only the shell: it serves the page, exposes a small JSON API per module, and
runs a background refresher. Data loading starts on the first web request (never
at import time), so the app boots instantly and imports cleanly.
"""
import os
import threading
import time
from datetime import datetime

from flask import Flask, jsonify, render_template

from leap import scanner as leap_scanner
from swing import module as swing_module
from journal import module as journal_module

app = Flask(__name__)

cache = {
    'leap': {'data': [], 'updated': None, 'loading': False},
    'swing': {'data': [], 'updated': None},
    'journal': {'data': [], 'updated': None},
}


def _stamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def refresh_leap():
    if cache['leap']['loading']:
        return
    cache['leap']['loading'] = True
    try:
        cache['leap']['data'] = leap_scanner.scan_leaps()
        cache['leap']['updated'] = _stamp()
    except Exception as e:
        print(f"LEAP refresh error: {e}")
    finally:
        cache['leap']['loading'] = False


def refresh_swing():
    try:
        cache['swing']['data'] = swing_module.get_data()
        cache['swing']['updated'] = _stamp()
    except Exception as e:
        print(f"Swing refresh error: {e}")


def refresh_journal():
    try:
        cache['journal']['data'] = journal_module.get_data()
        cache['journal']['updated'] = _stamp()
    except Exception as e:
        print(f"Journal refresh error: {e}")


def background_refresh():
    while True:
        refresh_leap()
        refresh_swing()
        refresh_journal()
        time.sleep(1800)  # every 30 minutes


# ─── Start the background thread on the first request (not at import time) ───
_bg_started = False
_bg_lock = threading.Lock()


@app.before_request
def _ensure_background():
    global _bg_started
    if not _bg_started:
        with _bg_lock:
            if not _bg_started:
                _bg_started = True
                threading.Thread(target=background_refresh, daemon=True).start()


# ─── Routes ─────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/leap')
def api_leap():
    c = cache['leap']
    return jsonify({'data': c['data'], 'updated': c['updated'], 'loading': c['loading']})


@app.route('/api/refresh/leap', methods=['POST'])
def api_refresh_leap():
    threading.Thread(target=refresh_leap, daemon=True).start()
    return jsonify({'status': 'refreshing'})


@app.route('/api/swing')
def api_swing():
    c = cache['swing']
    if not c['data']:
        return jsonify({'status': 'not_yet_implemented', 'data': [], 'updated': c['updated']})
    return jsonify({'data': c['data'], 'updated': c['updated']})


@app.route('/api/journal')
def api_journal():
    c = cache['journal']
    if not c['data']:
        return jsonify({'status': 'not_yet_implemented', 'data': [], 'updated': c['updated']})
    return jsonify({'data': c['data'], 'updated': c['updated']})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
