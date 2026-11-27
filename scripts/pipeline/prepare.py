"""Step A (code half) + LLM task sheet.

1. Refreshes the snapshot/history store (snapshot.py) unless --no-fetch.
2. Writes one node file per worker, data/nodes/<node_id>.json, holding the
   deterministic items computed from sourced prices (score_method "deterministic").
3. Writes data/pipeline/tasks.json: what the scheduled routine (LLM) must
   research per node, the exact output path and the item schema.

The LLM never edits data/nodes/*.json. It writes data/pipeline/claims/<node>.json,
which aggregate.py validates, verifies against verify_log.json and merges.

Run from the repo root:  python3 scripts/pipeline/prepare.py [--no-fetch]
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import common as c  # noqa: E402
from pipeline import scoring  # noqa: E402
from pipeline import snapshot as snap  # noqa: E402

# id, series key, label, group, orientation, scenario sensitivities
# (priced_in per +1 liquidity, priced_in per +1 rates, flow per +1 liquidity, flow per +1 rates),
# structural one-liner. Sensitivities are illustrative, not estimated.
CANDIDATES = [
    ("us2y", "US2Y", "US 2Y Treasury", "Rates", -1, (-0.05, -0.30, -0.10, -0.35),
     "Front-end yields carry the policy path. Lower yields price in more easing."),
    ("us10y", "US10Y", "US 10Y Treasury", "Rates", -1, (-0.10, -0.20, -0.20, -0.25),
     "Long-end yields mix policy, inflation and term premium."),
    ("usd", "DXY", "US dollar (DXY)", "FX", 1, (-0.25, 0.20, -0.30, 0.20),
     "A firm dollar tightens global funding and reaches Korea through the won."),
    ("krw", "USDKRW", "Korean won (USD/KRW)", "Korea", -1, (0.30, -0.15, 0.35, -0.15),
     "Where US rates and dollar funding meet Korean flows. A higher USD/KRW reads as outflow."),
    ("kospi", "KOSPI", "KOSPI", "Korea", 1, (0.35, -0.10, 0.40, -0.15),
     "Foreign equity flow sets the index, so it turns first when global liquidity does."),
    ("memory", "MEMORY", "Memory chips (Samsung, SK hynix)", "Korea", 1, (0.30, -0.05, 0.35, -0.05),
     "AI memory dominates index weight, so the pair moves with US tech funding."),
    ("yen_carry", "USDJPY", "Yen carry (USD/JPY)", "FX", 1, (0.25, 0.10, 0.30, 0.10),
     "A higher USD/JPY rewards yen borrowing. A sharp drop signals a carry unwind."),
    ("sox", "SOX", "US semiconductors (SOX)", "Equities", 1, (0.30, -0.10, 0.35, -0.10),
     "The funding-sensitive end of US tech. It leads Korean chip flows."),
    ("hy", "HYOAS", "US high-yield credit", "Credit", -1, (0.25, -0.10, 0.30, -0.15),
     "Tight spreads price credit for calm. Widening is an early stress signal."),
    ("ig", "IGOAS", "US investment-grade credit", "Credit", -1, (0.15, -0.10, 0.20, -0.10),
     "Shows how easily large borrowers fund themselves."),
    ("btc_lev", "BTCFUND", "BTC leverage (perp funding)", "Crypto", 1, (0.35, -0.10, 0.40, -0.10),
     "Positive funding means longs pay shorts. High funding is crowded leverage."),
    ("eth_lev", "ETHFUND", "ETH leverage (perp funding)", "Crypto", 1, (0.35, -0.10, 0.40, -0.10),
     "The higher-beta read on crypto leverage."),
]
SHORT = {"us2y": "US 2Y", "us10y": "US 10Y", "usd": "DXY", "krw": "KRW", "kospi": "KOSPI", "memory": "KR Memory",
         "yen_carry": "Yen Carry", "sox": "US Semis", "hy": "HY Credit", "ig": "IG Credit", "btc_lev": "BTC Lev",
         "eth_lev": "ETH Lev"}   # map labels; the full label shows on hover/focus and in the inspector
PINNED = ("krw", "kospi", "memory")   # Korea transmission assets, always shown
MAX_BUBBLES = 7

TASKS = {
    "rates_funding": "US policy-rate path, Treasury auctions and supply, SOFR and repo funding, Fed balance-sheet and reserve news.",
    "global_credit_macro": "Credit spreads, defaults and issuance, US growth and inflation releases, financial-conditions news.",
    "asia_early_signal": "USD/KRW and Korean FX policy, KOSPI foreign flows, Bank of Japan and yen carry, Korean memory-chip demand and export data.",
    "crypto_leverage": "Bitcoin and Ether perpetual funding, open interest, liquidations, ETF flows and exchange leverage.",
}


def fmt_value(unit, v):
    if unit == "bp":
        return "%.2f%%" % v
    if unit == "usd_bn":
        return "$%.2fbn" % v
    if unit == "level":
        return "%.2f bp/8h" % v
    if unit == "index100":
        return "%.1f (1y ago = 100)" % v
    return "{:,.2f}".format(v)


def fmt_delta(unit, d):
    s = "+" if d > 0 else ("-" if d < 0 else "")
    a = abs(d)
    if unit == "bp":
        return "%s%.0f bp" % (s, a)
    if unit == "usd_bn":
        return "%s$%.2fbn" % (s, a)
    if unit == "level":
        return "%s%.2f bp/8h" % (s, a)
    return "%s%.1f%%" % (s, a)


def md(iso):
    d = datetime.strptime(iso[:10], "%Y-%m-%d")
    return "%s %d" % (d.strftime("%b"), d.day)


def code_items(full, snapshot):
    series = {s["key"]: s for s in full.get("series", [])}
    out = {n: [] for n in c.NODE_IDS}
    for bid, key, label, group, orient, sens, read in CANDIDATES:
        s = series.get(key)
        if not s or not s.get("history"):
            continue
        hist = [(h["date"], h["value"]) for h in s["history"]]
        snap_item = (snapshot.get("items") or {}).get(key)
        last_v, last_d = hist[-1][1], hist[-1][0]
        if snap_item and snap_item.get("date") and snap_item["date"] >= last_d:
            last_v, last_d = snap_item["value"], snap_item["date"]
            hist = hist + [(last_d, last_v)] if snap_item["date"] > hist[-1][0] else hist[:-1] + [(last_d, last_v)]
        sc = scoring.bubble_scores(hist, s["unit"], orient)
        if not sc:
            continue
        unit = "index100" if key == "MEMORY" else s["unit"]
        range_pct = round(sc["priced_in_score"] if orient > 0 else 100 - sc["priced_in_score"])
        claim = "%s at %s on %s; %s over 20 sessions; %d%% of its 1-year range." % (
            s["label"], fmt_value(unit, last_v), md(last_d), fmt_delta(s["unit"], sc["change_20"]), range_pct)
        out[s["node_id"]].append({
            "node_id": s["node_id"], "asset_or_theme": label,
            "metric_value": fmt_value(unit, last_v), "delta": fmt_delta(s["unit"], sc["change_20"]) + " (20 sessions)",
            "priced_in_score": sc["priced_in_score"], "momentum_score": sc["momentum_score"],
            "tension_score": sc["tension_score"], "claim": claim,
            "source_url": s["source_url"], "published_at": last_d,
            # extras (allowed by the schema)
            "id": bid, "series_key": key, "group": group, "orientation": orient,
            "score_method": "deterministic", "stale": bool(s.get("stale")), "source": s["source"],
            "z20": sc["z20"], "z5": sc["z5"], "delta_value": sc["change_20"], "delta_unit": unit,
            "sens": {"priced_in_x": sens[0], "priced_in_y": sens[1], "flow_x": sens[2], "flow_y": sens[3]},
            "read": read, "short": SHORT.get(bid, label), "range_pct": range_pct,
        })
    return out


def node_status(items, full, node):
    keys = [s for s in full.get("series", []) if s["node_id"] == node]
    if not items:
        return "failed"
    if any(s.get("stale") for s in keys):
        return "stale"
    newest = max(it["published_at"] for it in items)
    age = (c.now_kst().date() - datetime.strptime(newest, "%Y-%m-%d").date()).days
    return "ok" if age <= 7 else "stale"


def main():
    if "--no-fetch" not in sys.argv:
        r = snap.refresh(fetch=True)
        for e in r.get("errors", []):
            print("source error:", e, file=sys.stderr)
    full = c.read_json(os.path.join(c.PIPE, "history_full.json"), {}) or {}
    snapshot = c.read_json(os.path.join(c.DATA, "snapshot.json"), {}) or {}
    items = code_items(full, snapshot)
    run_id = c.now_kst().strftime("%Y%m%dT%H%M")
    tasks = []
    for node in c.NODE_IDS:
        doc = {"node_id": node, "run_id": run_id, "generated_at": c.iso_now(), "producer": "code",
               "status": node_status(items[node], full, node), "items": items[node]}
        problems = c.validate_node(doc, node)
        if problems:
            doc["status"] = "failed"
            doc["problems"] = problems
        c.write_json(os.path.join(c.NODES, node + ".json"), doc)
        tasks.append({"node_id": node, "scope": TASKS[node],
                      "context": [{k: it[k] for k in ("asset_or_theme", "metric_value", "delta", "published_at", "source_url")}
                                  for it in items[node]],
                      "output": "data/pipeline/claims/%s.json" % node, "max_items": 3})
        print("NODE", node, doc["status"], len(doc["items"]))
    c.write_json(os.path.join(c.PIPE, "tasks.json"), {
        "run_id": run_id, "generated_at": c.iso_now(),
        "procedure": "scripts/pipeline/ROUTINE.md", "schema": "scripts/pipeline/schema/item.schema.json",
        "verify_log": "data/pipeline/verify_log.json", "tasks": tasks})
    return 0


if __name__ == "__main__":
    sys.exit(main())
