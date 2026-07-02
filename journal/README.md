# Journal module

Put your existing **trade journal** code in this folder.

**The only contract:** expose a function `get_data()` in `journal/module.py`
that returns a **list of dicts** (one dict per trade/row; the keys become the
table columns). The Journal tab in the app will render it automatically.

```python
def get_data():
    return [
        {"date": "2026-06-30", "ticker": "TSLA", "pnl": 240.0, "notes": "..."},
        # ...
    ]
```

Return `[]` while it's not ready — the tab shows a friendly placeholder until
`get_data()` returns rows. Need help adapting your code? Ask Claude in the
`trading-suite` session.
