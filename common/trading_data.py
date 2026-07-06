"""Read files from the private trading-data repo (the Dell's daily backups).

The Dell does the heavy fetching; deployed surfaces read its JSON — the same
ADR-1 split the swing display uses locally. Yahoo blocks option-chain fetches
(and throttles the rest) from cloud IPs, verified 2026-07-06, so deployed
modules must not fetch market data themselves.

Config (Render env vars):
  TRADING_DATA_TOKEN  fine-grained GitHub token, read-only Contents access to
                      trading-data (required)
  TRADING_DATA_REPO   owner/repo override (default rebibomichael-web/trading-data)
"""
import os

import requests

DEFAULT_REPO = "rebibomichael-web/trading-data"


class NotConfigured(Exception):
    """No TRADING_DATA_TOKEN in the environment — caller should fall back."""


def fetch_json(path):
    token = os.environ.get("TRADING_DATA_TOKEN")
    if not token:
        raise NotConfigured()
    repo = os.environ.get("TRADING_DATA_REPO", DEFAULT_REPO)
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    r = requests.get(url, timeout=30, headers={
        "Authorization": f"Bearer {token}",
        # raw media type streams the file even past the 1 MB base64 limit
        "Accept": "application/vnd.github.raw+json",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    r.raise_for_status()
    return r.json()
