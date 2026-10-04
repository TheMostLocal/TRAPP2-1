#!/usr/bin/env python3
"""
fetch_comtrade.py — UN Comtrade product-level bilateral trade flows.

Pulls "country X's exports/imports of product Y to/from country Z" for a curated
set of the world's most important bilateral relationships, and writes a single
denormalized file the app reads:

    data/comtrade_flows.json

QUOTA-SMART DESIGN
------------------
The free UN Comtrade key allows ~500 calls/day. Instead of querying each
product × pair separately (10 × 20 = 200 calls), this fetches ALL HS 2-digit
chapters for a pair in ONE call (cmdCode=AG2), both directions (flowCode=M,X),
across several recent years (comma-separated period) — so the whole job is ~1
call PER PAIR. ~24 pairs ⇒ ~24 calls/month, far under the daily cap. The top-10
products per relationship are then selected in post-processing.

Cadence: MONTHLY (annual trade data only updates a few times a year; a monthly
run keeps the latest published year current without burning quota).

API (new Comtrade, comtradeapi.un.org)
--------------------------------------
GET https://comtradeapi.un.org/data/v1/get/{typeCode}/{freqCode}/{clCode}
    typeCode = C   (commodities / goods)
    freqCode = A   (annual)
    clCode   = HS  (Harmonized System)
  params:
    reporterCode  M49 numeric (842=USA, 156=China, …)
    partnerCode   M49 numeric
    partner2Code  0  (world — standard bilateral)
    period        comma-separated years, e.g. 2022,2023,2024
    cmdCode       AG2  (all HS 2-digit chapters) — or TOTAL for the headline only
    flowCode      M,X  (imports + exports)
    includeDesc   true (return text descriptions so we don't hardcode them)
  auth: header  Ocp-Apim-Subscription-Key: <key>   (or ?subscription-key=<key>)
  response: { "data": [ { reporterCode, reporterDesc, partnerCode, partnerDesc,
                          cmdCode, cmdDesc, flowCode, flowDesc, period,
                          primaryValue, ... }, ... ] }

Set the key as a repo secret:  COMTRADE_API_KEY
(Get one free at https://comtradeplus.un.org → "Sign up for API key".)

Stdlib only (urllib + json). No pip installs.

NOTE: comtradeapi.un.org could not be reached from the build sandbox, so the
exact field names (primaryValue, cmdDesc, flowCode values) follow the published
v1 spec. If a field differs, adjust _record_value()/_record_fields() below — the
rest of the aggregation is generic.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = DATA / "comtrade_flows.json"

API_BASE = "https://comtradeapi.un.org/data/v1/get/C/A/HS"
API_KEY = os.environ.get("COMTRADE_API_KEY", "").strip()

# How many recent annual periods to request (the API returns whatever exists).
# In mid-2026 the latest complete annual is typically 2024; asking for a window
# means trends + YoY work as soon as a new year publishes.
NOW_YEAR = datetime.now(timezone.utc).year
PERIODS = [NOW_YEAR - 4, NOW_YEAR - 3, NOW_YEAR - 2, NOW_YEAR - 1]  # e.g. 2022..2025

# M49 country codes for readable config.
C = {
    "USA": 842, "China": 156, "Germany": 276, "Japan": 392, "UK": 826,
    "France": 250, "India": 356, "South Korea": 410, "Mexico": 484,
    "Canada": 124, "Brazil": 76, "Italy": 380, "Netherlands": 528,
    "Switzerland": 756, "Australia": 36, "Russia": 643, "Vietnam": 704,
    "Taiwan": 490, "Singapore": 702, "Saudi Arabia": 682, "UAE": 784,
    "Spain": 724, "Ireland": 372, "Indonesia": 360,
}

# Curated, high-signal bilateral relationships (reporter ↔ partner). One call
# each fetches BOTH directions, so "pair" here is undirected. Keep ~24 to stay
# well under quota while covering the relationships that move markets.
PAIRS = [
    ("USA", "China"), ("USA", "Mexico"), ("USA", "Canada"), ("USA", "Germany"),
    ("USA", "Japan"), ("USA", "South Korea"), ("USA", "India"), ("USA", "UK"),
    ("USA", "Vietnam"), ("USA", "Taiwan"), ("USA", "Ireland"), ("USA", "Switzerland"),
    ("China", "Germany"), ("China", "Japan"), ("China", "South Korea"),
    ("China", "Australia"), ("China", "Russia"), ("China", "Brazil"),
    ("China", "Vietnam"), ("China", "Saudi Arabia"),
    ("Germany", "France"), ("Germany", "Netherlands"), ("Germany", "Italy"),
    ("Japan", "South Korea"),
]

CODE_TO_NAME = {v: k for k, v in C.items()}


def log(*a):
    print("[comtrade]", *a, flush=True)


def _request(reporter_code, partner_code):
    """One API call: all HS-2 chapters, both flows, all periods for a pair."""
    params = {
        "reporterCode": str(reporter_code),
        "partnerCode": str(partner_code),
        "partner2Code": "0",
        "period": ",".join(str(p) for p in PERIODS),
        "cmdCode": "AG2",
        "flowCode": "M,X",
        "includeDesc": "true",
    }
    # Key as query param too (some gateways prefer it); header is primary.
    if API_KEY:
        params["subscription-key"] = API_KEY
    url = API_BASE + "?" + urllib.parse.urlencode(params)
    headers = {"User-Agent": "valuatio-comtrade"}
    if API_KEY:
        headers["Ocp-Apim-Subscription-Key"] = API_KEY
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        payload = json.loads(r.read().decode("utf-8"))
    # The v1 endpoint returns {"data":[...]}; older/preview returns a bare list.
    if isinstance(payload, dict):
        return payload.get("data", []) or []
    if isinstance(payload, list):
        return payload
    return []


def _record_value(rec):
    """Trade value in USD. v1 field is primaryValue; tolerate alternatives."""
    for k in ("primaryValue", "PrimaryValue", "TradeValue", "tradeValue", "value"):
        v = rec.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _record_fields(rec):
    """Normalize the fields we use across v1/preview naming."""
    flow = (rec.get("flowCode") or rec.get("FlowCode") or rec.get("rgCode") or "").upper()
    # Map any numeric/preview flow encodings to M/X.
    if flow in ("1", "M", "IMPORT", "IMPORTS"):
        flow = "M"
    elif flow in ("2", "X", "EXPORT", "EXPORTS"):
        flow = "X"
    cmd = str(rec.get("cmdCode") or rec.get("CmdCode") or rec.get("commodityCode") or "")
    desc = rec.get("cmdDesc") or rec.get("CmdDesc") or rec.get("commodity") or ""
    period = str(rec.get("period") or rec.get("Period") or rec.get("yr") or "")
    return flow, cmd, desc, period


def fetch_pair(rep_name, par_name):
    rep, par = C[rep_name], C[par_name]
    records = _request(rep, par)
    # Aggregate: per period, per flow, per HS-2 chapter → summed value.
    # Structure: agg[period][flow][cmd] = {"value":x, "desc":d}
    agg = {}
    chap_desc = {}
    for rec in records:
        flow, cmd, desc, period = _record_fields(rec)
        if flow not in ("M", "X") or not period:
            continue
        # Keep only real 2-digit chapters; skip the 'TOTAL'/'AG'/'ALL' rollups so
        # our own totals are a clean sum of chapters (no double counting).
        if not (len(cmd) == 2 and cmd.isdigit()):
            continue
        val = _record_value(rec)
        agg.setdefault(period, {}).setdefault(flow, {})
        cur = agg[period][flow].get(cmd, 0.0)
        agg[period][flow][cmd] = cur + val
        if desc:
            chap_desc[cmd] = desc

    if not agg:
        return None

    periods_sorted = sorted(agg.keys())
    latest = periods_sorted[-1]

    def totals_for(period):
        x = sum(agg.get(period, {}).get("X", {}).values())
        m = sum(agg.get(period, {}).get("M", {}).values())
        return x, m

    def top_products(period, flow, n=10):
        chapters = agg.get(period, {}).get(flow, {})
        ranked = sorted(chapters.items(), key=lambda kv: kv[1], reverse=True)[:n]
        return [{"code": c, "desc": chap_desc.get(c, c), "value": round(v, 2)} for c, v in ranked]

    exp_total, imp_total = totals_for(latest)
    trend = {}
    for p in periods_sorted:
        x, m = totals_for(p)
        trend[p] = {"exports": round(x, 2), "imports": round(m, 2), "balance": round(x - m, 2)}

    # YoY on the latest vs previous available period.
    yoy = {}
    if len(periods_sorted) >= 2:
        prev = periods_sorted[-2]
        px, pm = totals_for(prev)
        yoy = {
            "exports": round((exp_total - px) / px, 4) if px else None,
            "imports": round((imp_total - pm) / pm, 4) if pm else None,
            "vsPeriod": prev,
        }

    return {
        "reporterCode": rep, "reporter": rep_name,
        "partnerCode": par, "partner": par_name,
        "latestPeriod": latest,
        "exports": {"total": round(exp_total, 2), "topProducts": top_products(latest, "X")},
        "imports": {"total": round(imp_total, 2), "topProducts": top_products(latest, "M")},
        "balance": round(exp_total - imp_total, 2),   # reporter's balance with partner
        "trend": trend,
        "yoy": yoy,
    }


def main():
    if not API_KEY:
        log("⚠ COMTRADE_API_KEY not set. Doing a DRY RUN (no calls). "
            "Set the secret to fetch real data.")
        log(f"Would fetch {len(PAIRS)} pairs · periods {PERIODS} · ~{len(PAIRS)} calls.")
        return

    DATA.mkdir(parents=True, exist_ok=True)
    pairs_out, errors = [], []
    calls = 0
    for rep_name, par_name in PAIRS:
        try:
            data = fetch_pair(rep_name, par_name)
            calls += 1
            if data:
                pairs_out.append(data)
                log(f"  ✓ {rep_name} ↔ {par_name}: "
                    f"X ${data['exports']['total']/1e9:.1f}B · "
                    f"M ${data['imports']['total']/1e9:.1f}B ({data['latestPeriod']})")
            else:
                errors.append(f"{rep_name}-{par_name}: no data")
                log(f"  · {rep_name} ↔ {par_name}: no data returned")
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8")[:200]
            except Exception:
                pass
            errors.append(f"{rep_name}-{par_name}: HTTP {e.code} {body}")
            log(f"  ✗ {rep_name} ↔ {par_name}: HTTP {e.code} {body}")
            if e.code in (401, 403):
                log("    (auth error — check COMTRADE_API_KEY)")
            if e.code == 429:
                log("    (rate limited — backing off 30s)")
                time.sleep(30)
        except Exception as e:
            errors.append(f"{rep_name}-{par_name}: {e}")
            log(f"  ✗ {rep_name} ↔ {par_name}: {e}")
        time.sleep(0.5)   # gentle pacing

    # Build the country directory actually present in the output.
    countries = {}
    for p in pairs_out:
        countries[str(p["reporterCode"])] = p["reporter"]
        countries[str(p["partnerCode"])] = p["partner"]

    latest_period = max((p["latestPeriod"] for p in pairs_out), default=None)
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "UN Comtrade (comtradeapi.un.org)",
        "classification": "HS",
        "frequency": "A",
        "periods": PERIODS,
        "latestPeriod": latest_period,
        "countries": countries,
        "pairs": pairs_out,
        "_meta": {
            "callsUsed": calls,
            "pairsOk": len(pairs_out),
            "pairsRequested": len(PAIRS),
            "errors": errors,
        },
    }
    OUT.write_text(json.dumps(out, separators=(",", ":")))
    log(f"Wrote {OUT} · {len(pairs_out)}/{len(PAIRS)} pairs · {calls} calls · "
        f"{len(errors)} error(s)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FATAL: {e}")
        sys.exit(1)
