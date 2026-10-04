#!/usr/bin/env python3
"""
fetch_macro_quad.py — compute the REAL macro quad from FRED and persist it.

The app derives its growth/inflation "quad" from FRED at runtime (Real GDP YoY +
CPI YoY, then the rate-of-change of each). The bot runner can't fetch FRED the
same way, so it was using a market-implied PROXY. This script writes the actual
quad to a file the runner reads, giving true macro parity:

    data/macro/quad.json

It replicates the app's logic exactly:
  - GDPC1  (Real GDP, quarterly)  → YoY = value / value[-4] - 1
  - CPIAUCSL (CPI, monthly)       → YoY = value / value[-12] - 1
  - align GDP YoY onto CPI dates (buildQuadHistory)
  - growthRoC    = gdpYoY - priorGdpYoY
  - inflationRoC = cpiYoY - cpiYoY 3 months prior
  - quad = classifyQuad(growthRoC, inflationRoC):
        growth↑ inflation↓ → 1 (Goldilocks)
        growth↑ inflation↑ → 2 (Reflation)
        growth↓ inflation↑ → 3 (Stagflation)
        growth↓ inflation↓ → 4 (Deflation)

Requires the FRED key you already use, as a repo secret:  FED_API_KEY
(Free at https://fred.stlouisfed.org/docs/api/api_key.html)

Stdlib only.
"""
import json
import os
import sys
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "macro"
OUT = OUT_DIR / "quad.json"

FRED_KEY = os.environ.get("FED_API_KEY", "").strip()
FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"

SERIES = {"gdp": "GDPC1", "cpi": "CPIAUCSL"}

QUAD_LABELS = {
    1: "Goldilocks (growth↑ inflation↓)",
    2: "Reflation (growth↑ inflation↑)",
    3: "Stagflation (growth↓ inflation↑)",
    4: "Deflation (growth↓ inflation↓)",
}
# Which sectors each quad favors / hurts (mirrors the app's SECTOR_ETFS map),
# embedded so the runner (and you) can read it straight from the file.
QUAD_SECTOR_FAVORS = {
    1: {"favors": ["XLK", "XLY", "XLC", "XLI"], "hurts": ["XLP", "XLU"]},
    2: {"favors": ["XLE", "XLF", "XLI", "XLB"], "hurts": ["XLU", "XLP"]},
    3: {"favors": ["XLE", "XLU", "XLB", "XLV"], "hurts": ["XLY", "XLK", "XLF"]},
    4: {"favors": ["XLP", "XLV", "XLU"], "hurts": ["XLK", "XLB", "XLE"]},
}


def log(*a):
    print("[macro_quad]", *a, flush=True)


def fetch_fred(series_id):
    params = {
        "series_id": series_id,
        "api_key": FRED_KEY,
        "file_type": "json",
        "observation_start": "2005-01-01",
    }
    url = FRED_BASE + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "valuatio-macro-quad"})
    with urllib.request.urlopen(req, timeout=45) as r:
        payload = json.loads(r.read().decode("utf-8"))
    obs = payload.get("observations", [])
    out = []
    for o in obs:
        v = o.get("value")
        if v in (None, ".", ""):
            continue
        try:
            out.append({"date": o["date"], "value": float(v)})
        except (TypeError, ValueError):
            continue
    return out  # oldest → newest


def yoy_series(raw, periods_per_year):
    """value / value[-periodsPerYear] - 1 — identical to computeYoYSeriesFromFred."""
    out = []
    for i in range(periods_per_year, len(raw)):
        cur = raw[i]["value"]
        prior = raw[i - periods_per_year]["value"]
        if prior > 0:
            out.append({"date": raw[i]["date"], "yoy": cur / prior - 1})
    return out


def build_quad_history(gdp_yoy, cpi_yoy, lookback=30):
    """Replicates buildQuadHistory: align GDP YoY onto CPI dates, compute RoCs."""
    recent = cpi_yoy[-lookback:]
    result = []
    for i in range(1, len(recent)):
        cpi_point = recent[i]
        cpi_prior = recent[max(i - 3, 0)]            # 3-month RoC for inflation
        inflation_roc = cpi_point["yoy"] - cpi_prior["yoy"]

        # latest GDP YoY at or before this CPI date
        gdp_point = next((g for g in reversed(gdp_yoy) if g["date"] <= cpi_point["date"]), None)
        gdp_prior = None
        if gdp_point:
            gdp_prior = next((g for g in reversed(gdp_yoy) if g["date"] < gdp_point["date"]), None)
        if not gdp_point or not gdp_prior:
            continue
        growth_roc = gdp_point["yoy"] - gdp_prior["yoy"]

        result.append({
            "date": cpi_point["date"],
            "growthYoY": round(gdp_point["yoy"], 6),
            "inflationYoY": round(cpi_point["yoy"], 6),
            "growthRoC": round(growth_roc, 6),
            "inflationRoC": round(inflation_roc, 6),
            "quad": classify_quad(growth_roc, inflation_roc),
        })
    return result


def classify_quad(growth_roc, inflation_roc):
    g_up = growth_roc > 0
    i_up = inflation_roc > 0
    if g_up and not i_up:
        return 1
    if g_up and i_up:
        return 2
    if not g_up and i_up:
        return 3
    return 4


def main():
    if not FRED_KEY:
        log("⚠ FED_API_KEY not set — DRY RUN (no FRED calls).")
        log("Set the secret to compute the real quad. Would write data/macro/quad.json.")
        return

    log("Fetching GDPC1 (Real GDP)…")
    gdp_raw = fetch_fred(SERIES["gdp"])
    log(f"  {len(gdp_raw)} GDP observations")
    log("Fetching CPIAUCSL (CPI)…")
    cpi_raw = fetch_fred(SERIES["cpi"])
    log(f"  {len(cpi_raw)} CPI observations")

    if len(gdp_raw) < 8 or len(cpi_raw) < 16:
        log("✗ insufficient FRED data — not writing")
        sys.exit(1)

    gdp_yoy = yoy_series(gdp_raw, 4)     # quarterly
    cpi_yoy = yoy_series(cpi_raw, 12)    # monthly
    history = build_quad_history(gdp_yoy, cpi_yoy, lookback=30)
    if not history:
        log("✗ could not align GDP and CPI — not writing")
        sys.exit(1)

    current = history[-1]
    quad = current["quad"]
    out = {
        "_schema": "valuatio-macro-quad/v1",
        "_description": "Hedgeye-style growth/inflation quad from FRED GDPC1 + CPIAUCSL. "
                        "quad: 1=Goldilocks 2=Reflation 3=Stagflation 4=Deflation.",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "FRED (GDPC1, CPIAUCSL)",
        "current": {
            "quad": quad,
            "label": QUAD_LABELS[quad],
            "asOf": current["date"],
            "growthYoY": current["growthYoY"],
            "inflationYoY": current["inflationYoY"],
            "growthRoC": current["growthRoC"],
            "inflationRoC": current["inflationRoC"],
            "growthDir": "accelerating" if current["growthRoC"] > 0 else "decelerating",
            "inflationDir": "accelerating" if current["inflationRoC"] > 0 else "decelerating",
            "favors": QUAD_SECTOR_FAVORS[quad]["favors"],
            "hurts": QUAD_SECTOR_FAVORS[quad]["hurts"],
        },
        "history": history[-18:],     # last ~18 monthly readings for trend
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, separators=(",", ":")))
    c = out["current"]
    log(f"Quad {quad} — {c['label']} · as of {c['asOf']} · "
        f"growth {c['growthYoY']*100:.1f}% ({c['growthDir']}), "
        f"inflation {c['inflationYoY']*100:.1f}% ({c['inflationDir']})")
    log(f"Wrote {OUT}")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:
        log(f"FATAL HTTP {e.code} — check FED_API_KEY")
        sys.exit(1)
    except Exception as e:
        log(f"FATAL: {e}")
        sys.exit(1)
