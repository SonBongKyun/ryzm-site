#!/usr/bin/env python3
"""Refresh the home page ticker in index.html from public data sources.

Usage (from the repo root):
  python3 scripts/update_ticker.py            # fetch and write
  python3 scripts/update_ticker.py --dry-run  # print the new block only
  python3 scripts/update_ticker.py --json     # also write data/snapshot.json and
                                              # refresh data/snapshot_history.json
  python3 scripts/update_ticker.py --json-only  # no ticker fetch; rebuild the JSON
                                              # from the ticker already in index.html

The ticker HTML is written exactly as before; --json only adds the JSON files
(scripts/pipeline/snapshot.py). If the JSON step fails, the ticker still updates.

Sources (no API keys):
  US 2Y / 10Y   US Treasury daily par yield curve (CSV)
  SOFR          Federal Reserve Bank of New York reference rates API
  ON RRP        Federal Reserve Bank of New York repo operations API
  USD/KRW, KOSPI, DXY   Yahoo Finance chart API (delayed market data)

Each item carries its own as-of date. An item whose source fails is dropped;
if fewer than four items remain, nothing is written. Only the block between
<!-- ticker --> and <!-- /ticker --> in index.html changes, and the file's
existing line endings are kept. Python 3 standard library only.
"""
import csv
import io
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "index.html")
KST = timezone(timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 (compatible; ryzm-ticker/1.0)"}
BLOCK_RE = re.compile(r"(<!-- ticker -->)(.*?)(\s*<!-- /ticker -->)", re.S)


def get(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")


def md(d):
    """'Nov 23' without a leading zero, on any platform."""
    return "%s %d" % (d.strftime("%b"), d.day)


def treasury():
    year = datetime.now(KST).year
    url = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
           "daily-treasury-rates.csv/%d/all?type=daily_treasury_yield_curve"
           "&field_tdr_date_value=%d&page&_format=csv" % (year, year))
    rows = list(csv.DictReader(io.StringIO(get(url))))
    row = rows[0]  # newest first
    d = datetime.strptime(row["Date"], "%m/%d/%Y")
    return [("US2Y", "%.2f%%" % float(row["2 Yr"]), md(d)),
            ("US10Y", "%.2f%%" % float(row["10 Yr"]), md(d))]


def sofr():
    data = json.loads(get("https://markets.newyorkfed.org/api/rates/secured/sofr/last/1.json"))
    r = data["refRates"][0]
    d = datetime.strptime(r["effectiveDate"], "%Y-%m-%d")
    return [("SOFR", "%.2f%%" % float(r["percentRate"]), md(d))]


def rrp():
    start = (datetime.now(KST) - timedelta(days=14)).strftime("%Y-%m-%d")
    data = json.loads(get("https://markets.newyorkfed.org/api/rp/reverserepo/propositions/search.json?startDate=" + start))
    ops = [o for o in data["repo"]["operations"] if o.get("totalAmtAccepted") is not None]
    ops.sort(key=lambda o: o["operationDate"], reverse=True)
    o = ops[0]
    d = datetime.strptime(o["operationDate"], "%Y-%m-%d")
    bn = float(o["totalAmtAccepted"]) / 1e9
    val = "$%.2fbn" % bn if bn < 10 else "$%.0fbn" % bn
    return [("ON RRP", val, md(d))]


def yahoo(symbol, label, fmt, show_change=False):
    data = json.loads(get("https://query1.finance.yahoo.com/v8/finance/chart/%s?range=5d&interval=1d" % symbol))
    m = data["chart"]["result"][0]["meta"]
    price = float(m["regularMarketPrice"])
    # exchange calendar date (gmtoffset), the same rule as pipeline/common.py, so the
    # snapshot date matches the history date for the same session
    d = datetime.fromtimestamp(int(m["regularMarketTime"]), timezone(timedelta(seconds=int(m.get("gmtoffset") or 0))))
    val = fmt.format(price)
    if show_change and m.get("regularMarketChangePercent") is not None:
        val += " %+.1f%%" % float(m["regularMarketChangePercent"])
    return [(label, val, md(d))]


SOURCES = [
    treasury,
    sofr,
    rrp,
    lambda: yahoo("KRW%3DX", "USD/KRW", "{:,.1f}"),
    lambda: yahoo("%5EKS11", "KOSPI", "{:,.2f}", show_change=True),
    lambda: yahoo("DX-Y.NYB", "DXY", "{:.2f}"),
]


def collect():
    items, errors = [], []
    for fn in SOURCES:
        try:
            items.extend(fn())
        except Exception as e:  # one failing source must not block the rest
            errors.append(str(e)[:120])
    return items, errors


def render(items, nl):
    def one(label, val, d):
        return ('      <span class="ticker-item"><strong>%s</strong> %s <em>%s</em></span>'
                % (label, val, d))
    now = datetime.now(KST)
    when = "%s %s KST" % (md(now), now.strftime("%H:%M"))
    # A static snapshot, not a scrolling marquee: one set of items plus an update stamp.
    lines = (['      <span class="ticker-label">Market snapshot</span>']
             + [one(*i) for i in items]
             + ['      <span class="ticker-item ticker-stamp">Updated %s</span>' % when])
    return nl + nl.join(lines)


def write_json_snapshot():
    """data/snapshot.json + history, parsed from the ticker now in index.html."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from pipeline import snapshot
        r = snapshot.refresh(fetch=True)
        for e in r.get("errors", []):
            print("history source error:", e, file=sys.stderr)
        print("SNAPSHOT_JSON_%s" % ("OK" if r["ok"] else "FAILED"))
    except Exception as e:  # the ticker must never fail because of the JSON step
        print("SNAPSHOT_JSON_FAILED", str(e)[:160], file=sys.stderr)


def main():
    dry = "--dry-run" in sys.argv
    if "--json-only" in sys.argv:
        write_json_snapshot()
        return 0
    items, errors = collect()
    for e in errors:
        print("source error:", e, file=sys.stderr)
    if len(items) < 4:
        print("ABORT: only %d ticker items fetched; index.html unchanged" % len(items))
        return 1
    raw = open(INDEX, "rb").read().decode("utf-8")
    nl = "\r\n" if "\r\n" in raw else "\n"
    block = render(items, nl)
    if dry:
        print(block)
        return 0
    new, n = BLOCK_RE.subn(lambda m: m.group(1) + block + m.group(3), raw, count=1)
    if n != 1:
        print("ABORT: ticker markers not found in index.html")
        return 1
    if new != raw:
        open(INDEX, "wb").write(new.encode("utf-8"))
        print("TICKER_UPDATED %d items" % len(items))
    else:
        print("TICKER_UNCHANGED")
    if "--json" in sys.argv:
        write_json_snapshot()
    return 0


if __name__ == "__main__":
    sys.exit(main())
