#!/usr/bin/env bash
cd "$(dirname "$0")"
source venv/bin/activate
fuser -k 10000/tcp 2>/dev/null
python app.py
