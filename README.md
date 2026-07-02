# Trading Suite

Three trading tools consolidated into **one Flask app with three tabs**:

| Tab | Module | Status |
|-----|--------|--------|
| 🎯 **LEAP** | `leap/` | ✅ Working — ported from the original stock-tracker |
| 🌊 **Swing** | `swing/` | ⛏️ Stub — drop your swing code into `swing/module.py` |
| 📓 **Journal** | `journal/` | ⛏️ Stub — drop your journal code into `journal/module.py` |

Shared market-data helpers (pivots, ATH/52-week, Barchart) live in `common/`.

## Layout

```
trading-suite/
├── app.py                # Flask shell: serves the page + JSON API per module
├── common/market_data.py # shared: pivots, ATH/52w, Barchart opinion
├── leap/scanner.py       # LEAP scanner + scoring (working)
├── swing/module.py       # get_data() stub — your swing code goes here
├── journal/module.py     # get_data() stub — your journal code goes here
├── templates/index.html  # the three-tab UI
├── requirements.txt
└── render.yaml           # Render deploy config
```

## Run it locally

```bash
pip install -r requirements.txt
python app.py
```

Open http://localhost:10000 — the LEAP tab loads live data (1–2 min on first
scan); Swing and Journal show a "drop your code here" panel until wired.

## Deploy on Render

Push this repo to GitHub, create a Render **Web Service** from it, and Render
auto-detects `render.yaml`. It serves with `gunicorn app:app`.

## Add your Swing / Journal code

Each stub module just needs a `get_data()` that returns a **list of dicts**
(one per row). The matching tab renders it automatically — see
`swing/README.md` and `journal/README.md`. Or ask Claude in a `trading-suite`
session to migrate your existing files for you.
