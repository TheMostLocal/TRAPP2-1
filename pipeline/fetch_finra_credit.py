#!/usr/bin/env python3
# ============================================================
#  >>> DESTINATION: TheMostLocal/TRAPP2-1
#  >>> FILE PATH:   pipeline/fetch_finra_credit.py
#
#  z132 - FINRA TRACE corporate-bond market breadth, from FINRA's official Query API
#  (api.finra.org). Investment grade vs high yield: issues traded, advances, declines,
#  unchanged, 52-week highs / lows - the junk-bond market's own breadth, the bond
#  version of an advance/decline line. Optional: runs only when the repo has the
#  secrets FINRA_CLIENT_ID / FINRA_CLIENT_SECRET (free FINRA API Console credentials);
#  without them it logs a skip and exits 0. FINRA publishes this for noncommercial use.
#
#    data/credit/finra_breadth.json   { updatedAt, dataset, rows: [ {date, category, ...} ] }
#    data/credit/finra_sentiment.json { updatedAt, dataset, rows: [...] }   (when available)
#
#  Field names are kept as FINRA sends them (the app reads the ones it knows and shows
#  the rest), so a renamed column never silently becomes zero. Never writes NaN.
# ============================================================
import base64
import json
import os
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "credit"
TOKEN_URL = os.environ.get("FINRA_TOKEN_URL", "https://ews.fip.finra.org/fip/rest/ews/oauth2/access_token?grant_type=client_credentials")
API = os.environ.get("FINRA_API_BASE", "https://api.finra.org")
DAYS = int(os.environ.get("FINRA_DAYS", "400"))
DATASETS = [("fixedIncomeMarket", "corporateDebtMarketBreadth", "finra_breadth.json"),
            ("fixedIncomeMarket", "corporateDebtMarketSentiment", "finra_sentiment.json")]


def log(*a):
    print("[finra]", *a, flush=True)


def token(cid, secret):
    req = urllib.request.Request(TOKEN_URL, method="POST", data=b"",
                                 headers={"Authorization": "Basic " + base64.b64encode(f"{cid}:{secret}".encode()).decode()})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())["access_token"]


def query(tok, group, name, start, end):
    body = {"limit": 5000, "dateRangeFilters": [{"fieldName": "tradeReportDate", "startDate": start, "endDate": end}]}
    req = urllib.request.Request(f"{API}/data/group/{group}/name/{name}", method="POST", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {tok}", "Accept": "application/json", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.loads(r.read().decode() or "[]")
    return out if isinstance(out, list) else (out.get("data") or [])


def clean(v):
    if isinstance(v, float) and v != v:
        return None
    return v


def main():
    cid, secret = os.environ.get("FINRA_CLIENT_ID", "").strip(), os.environ.get("FINRA_CLIENT_SECRET", "").strip()
    if not cid or not secret:
        log("skip: no FINRA_CLIENT_ID / FINRA_CLIENT_SECRET secrets (free at the FINRA API Console)")
        return 0
    try:
        tok = token(cid, secret)
    except Exception as e:
        log(f"token request failed: {e}")
        return 0
    end = date.today().isoformat()
    start = (date.today() - timedelta(days=DAYS)).isoformat()
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for group, name, fname in DATASETS:
        try:
            rows = query(tok, group, name, start, end)
        except Exception as e:
            log(f"{name}: query failed: {e}")
            continue
        rows = [{k: clean(v) for k, v in r.items()} for r in rows if isinstance(r, dict)]
        if not rows:
            log(f"{name}: no rows")
            continue
        (OUT / fname).write_text(json.dumps({"updatedAt": stamp, "dataset": f"{group}/{name}", "start": start, "end": end, "rows": rows}, separators=(",", ":")))
        log(f"{name}: {len(rows)} rows · fields {sorted(rows[0].keys())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
