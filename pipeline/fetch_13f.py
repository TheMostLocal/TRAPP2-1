#!/usr/bin/env python3
"""
fetch_13f.py — Pull institutional 13F holdings from SEC EDGAR and write
data/13f/<CIK>.json for each tracked institution, plus a reverse index
data/13f/by_ticker.json (which institutions hold each ticker).

Runs QUARTERLY via GitHub Actions. 13F filings report end-of-quarter holdings,
filed up to 45 days after quarter-end, so a monthly cron catches them when they
post without hammering EDGAR.

For each institution it computes the QUARTER-OVER-QUARTER change per position:
  NEW        — held this quarter, not last
  SOLD       — held last quarter, gone this quarter (shown with 0 shares)
  INCREASED  — share count up vs last quarter
  TRIMMED    — share count down vs last quarter
  UNCHANGED  — same share count

Option positions (PUT/CALL) are included and their value is shown as the
reported market value (EDGAR reports the notional/market value of the option
position in the `value` column), so they appear as regular market value.

EDGAR requires a descriptive User-Agent with contact info — set EDGAR_UA below.

Output path resolves to <repo-root>/data/13f/ regardless of where invoked.
Requires: stdlib only (urllib, xml, json).
"""
import json
import os
import sys
import time
import datetime
import urllib.request
import xml.etree.ElementTree as ET

# SEC requires a real User-Agent identifying you. EDIT THIS to your info.
EDGAR_UA = "Collin McGough collinmcgough@gmail.com"

# Institutions to track: { "Display Name": "10-digit zero-padded CIK" }
# Find a CIK at https://www.sec.gov/cgi-bin/browse-edgar (search the manager).
TRACKED_INSTITUTIONS = {
    "Berkshire Hathaway":        "0001067983",
    "Scion Asset Management":    "0001649339",  # Michael Burry
    "Bridgewater Associates":    "0001350694",
    "Renaissance Technologies":  "0001037389",
    "Pershing Square":           "0001336528",  # Bill Ackman
    "Citadel Advisors":          "0001423053",
    "Tiger Global":              "0001167483",
    "Baupost Group":             "0001061768",   # Seth Klarman
    # Add your own here.
}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
OUT_DIR = os.path.join(REPO_ROOT, "data", "13f")

SEC_HEADERS = {"User-Agent": EDGAR_UA, "Accept-Encoding": "gzip, deflate"}


def _get(url, is_json=False):
    """GET with SEC headers + polite rate limiting."""
    req = urllib.request.Request(url, headers=SEC_HEADERS)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                # SEC may gzip even when not requested via this path; urllib
                # handles Accept-Encoding transparently in most setups, but
                # guard for gzip magic bytes just in case.
                if raw[:2] == b"\x1f\x8b":
                    import gzip
                    raw = gzip.decompress(raw)
                text = raw.decode("utf-8", errors="replace")
                return json.loads(text) if is_json else text
        except Exception as e:
            print(f"   retry {attempt+1} {url}: {e}", file=sys.stderr)
            time.sleep(1.5 * (attempt + 1))
    return None


def latest_13f_filings(cik, limit=2):
    """Return up to `limit` most recent 13F-HR accession numbers + dates,
    newest first, via the submissions JSON feed."""
    cik_int = str(int(cik))  # strip leading zeros for this endpoint
    url = f"https://data.sec.gov/submissions/CIK{cik.zfill(10)}.json"
    data = _get(url, is_json=True)
    if not data:
        return []
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accns = recent.get("accessionNumber", [])
    dates = recent.get("filingDate", [])
    report_dates = recent.get("reportDate", [])
    out = []
    for i, form in enumerate(forms):
        if form in ("13F-HR", "13F-HR/A"):
            out.append({
                "accession": accns[i].replace("-", ""),
                "accession_dashed": accns[i],
                "filed": dates[i],
                "period": report_dates[i] if i < len(report_dates) else None,
            })
        if len(out) >= limit:
            break
    return out


def fetch_info_table(cik, accession):
    """Fetch + parse the 13F INFORMATION TABLE for one filing.
    Returns list of holdings dicts."""
    # The filing's index lists its documents; the info table is an .xml file.
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}"
    idx = _get(f"{base}/index.json", is_json=True)
    if not idx:
        return []
    # Find the information-table xml (name varies; pick the xml that isn't the
    # primary_doc / header).
    info_xml_name = None
    for item in idx.get("directory", {}).get("item", []):
        nm = item.get("name", "")
        low = nm.lower()
        if low.endswith(".xml") and ("info" in low or "table" in low or "form13f" in low and "primary" not in low):
            info_xml_name = nm
            break
    # Fallback: any xml that isn't primary_doc.xml
    if not info_xml_name:
        for item in idx.get("directory", {}).get("item", []):
            nm = item.get("name", "")
            if nm.lower().endswith(".xml") and "primary_doc" not in nm.lower():
                info_xml_name = nm
                break
    if not info_xml_name:
        return []

    xml_text = _get(f"{base}/{info_xml_name}")
    if not xml_text:
        return []

    holdings = []
    try:
        # 13F info tables use a namespace; strip it for easy tag matching.
        root = ET.fromstring(xml_text)
        def local(tag):
            return tag.split("}")[-1]
        for el in root.iter():
            if local(el.tag) != "infoTable":
                continue
            h = {}
            for child in el:
                tag = local(child.tag)
                if tag == "nameOfIssuer":
                    h["name"] = (child.text or "").strip()
                elif tag == "cusip":
                    h["cusip"] = (child.text or "").strip()
                elif tag == "value":
                    # EDGAR value is in thousands of USD (pre-2023) or dollars
                    # (post-2023 rule change reports in dollars). We normalize
                    # to dollars below using a heuristic.
                    try:
                        h["value_raw"] = float((child.text or "0").strip())
                    except ValueError:
                        h["value_raw"] = 0.0
                elif tag == "shrsOrPrnAmt":
                    for gc in child:
                        if local(gc.tag) == "sshPrnamt":
                            try:
                                h["shares"] = float((gc.text or "0").strip())
                            except ValueError:
                                h["shares"] = 0.0
                        elif local(gc.tag) == "sshPrnamtType":
                            h["share_type"] = (gc.text or "").strip()
                elif tag == "putCall":
                    # Option positions: PUT or CALL. Treated as market value.
                    h["option"] = (child.text or "").strip().upper()
                elif tag == "titleOfClass":
                    h["class"] = (child.text or "").strip()
            if h.get("cusip") or h.get("name"):
                holdings.append(h)
    except ET.ParseError as e:
        print(f"   XML parse error: {e}", file=sys.stderr)
        return []
    return holdings


def normalize_values(holdings):
    """Normalize the EDGAR `value` field to dollars. Pre-2023 filings report
    in THOUSANDS; the 2023 rule changed to whole dollars. Heuristic: if the
    summed value is implausibly small for an institutional 13F (< $10M when
    there are real share counts), it's in thousands → multiply by 1000."""
    total = sum(h.get("value_raw", 0) for h in holdings)
    multiplier = 1.0
    if total > 0 and total < 1e7 and any(h.get("shares", 0) > 1000 for h in holdings):
        multiplier = 1000.0
    for h in holdings:
        h["value"] = h.get("value_raw", 0) * multiplier
    return holdings


def diff_quarters(current, previous):
    """Annotate each current holding with its change vs the previous quarter,
    and append SOLD entries for positions that disappeared.
    Change keyed by CUSIP (+ option type, so a CALL and the common stock are
    tracked separately)."""
    def key(h):
        return f"{h.get('cusip','')}|{h.get('option','')}"

    prev_map = {key(h): h for h in previous}
    cur_map = {key(h): h for h in current}

    for h in current:
        k = key(h)
        ph = prev_map.get(k)
        if ph is None:
            h["change"] = "NEW"
            h["shares_prev"] = 0
        else:
            sp = ph.get("shares", 0)
            sc = h.get("shares", 0)
            h["shares_prev"] = sp
            if sc > sp * 1.0001:
                h["change"] = "INCREASED"
            elif sc < sp * 0.9999:
                h["change"] = "TRIMMED"
            else:
                h["change"] = "UNCHANGED"
            if sp > 0:
                h["pct_change"] = round((sc - sp) / sp * 100, 1)

    # SOLD: in previous, not in current
    sold = []
    for k, ph in prev_map.items():
        if k not in cur_map:
            sold.append({
                **ph,
                "change": "SOLD",
                "shares_prev": ph.get("shares", 0),
                "shares": 0,
                "value": 0,
            })
    return current + sold


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    by_ticker = {}   # cusip -> [{institution, shares, value, change, option}]
    summary = []

    for name, cik in TRACKED_INSTITUTIONS.items():
        print(f"[13f] {name} (CIK {cik})")
        filings = latest_13f_filings(cik, limit=2)
        time.sleep(0.4)  # polite
        if not filings:
            print(f"   no 13F filings found", file=sys.stderr)
            continue

        current = fetch_info_table(cik, filings[0]["accession"])
        time.sleep(0.4)
        current = normalize_values(current)

        previous = []
        if len(filings) > 1:
            previous = fetch_info_table(cik, filings[1]["accession"])
            time.sleep(0.4)
            previous = normalize_values(previous)

        annotated = diff_quarters(current, previous)
        # Sort by value desc (sold positions, value 0, sink to bottom)
        annotated.sort(key=lambda h: h.get("value", 0), reverse=True)

        total_value = sum(h.get("value", 0) for h in annotated if h.get("change") != "SOLD")
        doc = {
            "_schema": "valuatio-13f-v1",
            "institution": name,
            "cik": cik,
            "period": filings[0].get("period"),
            "filed": filings[0].get("filed"),
            "prevPeriod": filings[1].get("period") if len(filings) > 1 else None,
            "totalValue": round(total_value, 2),
            "positionCount": len([h for h in annotated if h.get("change") != "SOLD"]),
            "updatedAt": datetime.datetime.utcnow().isoformat() + "Z",
            "holdings": annotated,
        }
        out_path = os.path.join(OUT_DIR, f"{cik}.json")
        with open(out_path, "w") as f:
            json.dump(doc, f, indent=2)
        print(f"   wrote {doc['positionCount']} positions, ${total_value/1e6:.0f}M → {out_path}")

        summary.append({
            "institution": name, "cik": cik, "period": doc["period"],
            "positionCount": doc["positionCount"], "totalValue": doc["totalValue"],
        })

        # Build reverse index by CUSIP
        for h in annotated:
            if h.get("change") == "SOLD":
                continue
            cusip = h.get("cusip")
            if not cusip:
                continue
            by_ticker.setdefault(cusip, []).append({
                "institution": name,
                "cik": cik,
                "name": h.get("name"),
                "shares": h.get("shares", 0),
                "value": h.get("value", 0),
                "change": h.get("change"),
                "option": h.get("option", ""),
                "period": doc["period"],
            })

    # Write the reverse index + a manifest of tracked institutions
    with open(os.path.join(OUT_DIR, "by_ticker.json"), "w") as f:
        json.dump({
            "_schema": "valuatio-13f-by-ticker-v1",
            "updatedAt": datetime.datetime.utcnow().isoformat() + "Z",
            "note": "Keyed by CUSIP. The app maps ticker→CUSIP via its own data.",
            "byCusip": by_ticker,
        }, f, indent=2)

    with open(os.path.join(OUT_DIR, "institutions.json"), "w") as f:
        json.dump({
            "_schema": "valuatio-13f-institutions-v1",
            "updatedAt": datetime.datetime.utcnow().isoformat() + "Z",
            "institutions": summary,
        }, f, indent=2)

    print(f"[13f] done — {len(summary)} institutions, {len(by_ticker)} unique CUSIPs")


if __name__ == "__main__":
    main()
