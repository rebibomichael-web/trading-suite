# Swing module

Put your existing **swing-trader** code in this folder.

**The only contract:** expose a function `get_data()` in `swing/module.py` that
returns a **list of dicts** (one dict per row; the keys become the table
columns). The Swing tab in the app will render it automatically.

```python
def get_data():
    return [
        {"ticker": "NVDA", "setup": "pullback", "entry": 120.5, "stop": 115},
        # ...
    ]
```

Return `[]` while it's not ready — the tab shows a friendly placeholder until
`get_data()` returns rows. Need help adapting your code? Ask Claude in the
`trading-suite` session.
