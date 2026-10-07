#!/usr/bin/env python3
"""
fetch_macro_trade.py — US trade-balance series from FRED -> data/macro_trade.json

The Global Trade tab reads data/macro_trade.json (schema valuatio-macro-trade-v1)
and its help text names this script, but the script was missing from the repo,
so the file had been frozen since 2026-06-18. Rebuilt to the exact schema the
app reads (`j.series[ID] = {label, units, cadence, latest, prevValue,
changePct, history}`).

Key: FRED_API_KEY (or FED_API_KEY). No key -> warning, file left as-is.
Keeps the last 36 observations per series, matching the previous file.
"""
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
OUT = DATA / "macro_trade.json"
KEEP = 36

SERIES = [
    ("BOPGSTB", "Goods Trade Balance (BOP)",             "USD millions", "monthly"),
    ("BOPGEXP", "Goods Exports (BOP)",                   "USD millions", "monthly"),
    ("BOPGIMP", "Goods Imports (BOP)",                   "USD millions", "monthly"),
    ("BOPTEXP", "Goods & Services Exports (BOP)",        "USD millions", "monthly"),
    ("BOPTIMP", "Goods & Services Imports (BOP)",        "USD millions", "monthly"),
    ("EXPGS",   "Exports of Goods & Services (BEA)",     "USD billions", "quarterly"),
    ("IMPGS",   "Imports of Goods & Services (BEA)",     "USD billions", "quarterly"),
]


def fetch_observations(series_id, api_key, start="2015-01-01"):
    params = {"series_id": series_id, "api_key": api_key, "file_type": "json",
              "observation_start": start}
    url = "https://api.stlouisfed.org/fred/series/observations?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "valuatio-macro-trade"})
    with urllib.request.urlopen(req, timeout=30) as r:
        obs = json.loads(r.read().decode("utf-8")).get("observations", [])
    out = []
    for o in obs:
        try:
            v = float(o.get("value"))
        except (TypeError, ValueError):
            continue  # FRED uses "." for missing
        if math.isfinite(v):
            out.append({"date": o.get("date"), "value": v})
    return out


def build_series(label, units, cadence, obs):
    hist = obs[-KEEP:]
    if not hist:
        return None
    latest = hist[-1]
    prev = hist[-2]["value"] if len(hist) > 1 else None
    chg = None
    if prev not in (None, 0):
        chg = round((latest["value"] - prev) / abs(prev) * 100, 2)
    return {"label": label, "units": units, "cadence": cadence,
            "latest": {"date": latest["date"], "value": latest["value"]},
            "prevValue": prev, "changePct": chg, "history": hist}


def main(fetch=fetch_observations):
    key = (os.environ.get("FRED_API_KEY") or os.environ.get("FED_API_KEY") or "").strip()
    if not key:
        print("::warning::macro trade: FRED_API_KEY / FED_API_KEY not set - data/macro_trade.json left unchanged")
        return 0
    prev = {}
    if OUT.exists():
        try:
            prev = json.loads(OUT.read_text()).get("series") or {}
        except Exception:
            prev = {}
    series, failed = {}, []
    for sid, label, units, cadence in SERIES:
        try:
            s = build_series(label, units, cadence, fetch(sid, key))
        except Exception as e:
            s = None
            print(f"  {sid}: {type(e).__name__}: {str(e)[:120]}")
        if s:
            series[sid] = s
        else:
            failed.append(sid)
            if sid in prev:
                series[sid] = prev[sid]   # keep the last good copy rather than drop it
        time.sleep(0.3)
    if not any(sid not in failed for sid, *_ in SERIES):
        print(f"::error::macro trade: every series failed ({', '.join(failed)}) - file left unchanged")
        return 1
    doc = {
        "_schema": "valuatio-macro-trade-v1",
        "_description": "US trade-balance series from FRED. Monthly cadence; US monthly trade "
                        "data publishes ~5 weeks after month-end.",
        "updatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": "FRED (api.stlouisfed.org)",
        "series": series,
    }
    OUT.write_text(json.dumps(doc, indent=2, allow_nan=False))
    print(f"✓ data/macro_trade.json: {len(SERIES) - len(failed)}/{len(SERIES)} series refreshed"
          + (f" (kept previous for {', '.join(failed)})" if failed else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
