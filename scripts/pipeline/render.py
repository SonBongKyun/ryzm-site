"""Step D: write data/dashboard_state.json, data/compass_history.json, the
plain-HTML regime summary in index.html (label + 3 drivers + 1 note + JSON-LD)
and an unpublished briefing draft (data/pipeline/drafts/briefing-latest.md).

Only the block between <!-- regime-summary --> and <!-- /regime-summary --> in
index.html changes. Ticker, Breaking and Feed bytes are never touched.
"""
import html
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import common as c  # noqa: E402
from pipeline import scoring  # noqa: E402

DISCLAIMER = "Reads recent market structure. Not a forecast, signal or investment advice."
SCENARIOS = [
    ("current", "Current Balance", None, None, "Position from the latest sourced snapshot."),
    ("inflation_shock", "Inflation Re-acceleration Shock", -30, 42,
     "Illustrative: yields reprice higher on hotter inflation while dollar liquidity tightens."),
    ("yen_carry_unwind", "Yen Carry Unwind / Liquidity Crunch", -45, 25,
     "Illustrative: funding trades are cut and dollar liquidity dries up, so risk assets and the won weaken."),
    ("rate_cuts", "Rate Cuts / Liquidity Surge", 44, -38,
     "Illustrative: front-end yields fall and liquidity-sensitive assets are bid."),
]
RING_RULE = "priced_in >= 80 (crowded) or tension >= 75 (stretched)"


def note_for(agg):
    for b in agg["bubbles"]:
        for n in b.get("notes") or []:
            return {"text": n["claim"], "source_url": n["source_url"], "kind": "verified_claim"}
    top = max(agg["bubbles"], key=lambda b: b["tension_score"], default=None)
    if not top:
        return {"text": "Bubble inputs are unavailable this run.", "source_url": None, "kind": "notice"}
    rng = top.get("range_pct")
    return {"text": ("Highest tension: %s (%d), at %d%% of its %s range." % (top.get("short") or top["asset_or_theme"], round(top["tension_score"]), rng, top.get("range_label") or "1-year"))
            if rng is not None else ("Highest tension: %s (%d)." % (top.get("short") or top["asset_or_theme"], round(top["tension_score"]))),
        "source_url": top["source_url"], "kind": "highest_tension"}


def bubble_out(b):
    return {
        "id": b.get("id"), "label": b["asset_or_theme"], "short": b.get("short") or b["asset_or_theme"], "group": b.get("group", ""), "node_id": b["node_id"],
        "metric_value": b["metric_value"], "delta": b["delta"], "range_label": b.get("range_label") or "1-year",
        "priced_in": b["priced_in_score"], "flow": b["momentum_score"], "tension": b["tension_score"],
        "priced_in_score": b["priced_in_score"], "momentum_score": b["momentum_score"], "tension_score": b["tension_score"],
        "concentration": b["tension_score"], "disagreement": b["tension_score"],
        "claim": b["claim"], "read": b.get("read") or b["claim"], "notes": b.get("notes", []),
        "source": b.get("source"), "source_url": b["source_url"], "published_at": b["published_at"],
        "score_method": b.get("score_method", "deterministic"), "stale": bool(b.get("stale")),
        "example": False, "sens": b.get("sens") or {}, "slot": "analysis:%s" % b.get("id"), "detail_url": None,
    }


def build_state(agg, run_id):
    cp = agg["compass"] or {}
    q = cp.get("quadrant")
    bubbles = [bubble_out(b) for b in agg["bubbles"]]
    note = note_for(agg)
    scen = []
    for sid, label, x, y, n in SCENARIOS:
        if sid == "current":
            if not cp:
                continue
            x, y = cp["x"], cp["y"]
        scen.append({"id": sid, "label": label, "x": x, "y": y, "note": n, "illustrative": sid != "current"})
    return {
        "version": 3, "generated_at": c.iso_now(), "run_id": run_id,
        "as_of": agg.get("snapshot_stamp"), "disclaimer": DISCLAIMER,
        "axes": {"x": {"name": "Global Liquidity / Risk Appetite", "key": "liquidity_flow", "min_label": "Contraction", "max_label": "Expansion", "range": [-50, 50]},
                 "y": {"name": "Rates / Tightening Pressure", "key": "rate_pressure", "min_label": "Easing", "max_label": "Tightening", "range": [-50, 50]}},
        "quadrants": scoring.QUADRANTS,
        "compass": dict(cp, snapshot_stamp=agg.get("snapshot_stamp"), prev_needle=agg.get("prev_needle"),
                        stale=agg.get("compass_stale", False)) if cp else None,
        "compass_method": scoring.FORMULA,
        "regime": {"label": scoring.QLABEL.get(q, ""), "quadrant": q, "drivers": agg.get("drivers", []), "note": note},
        "bubble_map": {
            "max_bubbles": 7, "example": False,
            "label": "Sourced prices \u00b7 model scores",
            "scoring": "Deterministic proxies from sourced price history (see scripts/pipeline/scoring.py). "
                       "Not measured positioning. Scenario shifts use illustrative sensitivities.",
            "axes": {"x": "Priced-in (1-year range position, 0-100)", "y": "Flow momentum (-50 outflow to +50 inflow)", "size": "Tension (0-100)"},
            "ring_rule": RING_RULE, "default_selected": "memory" if any(b["id"] == "memory" for b in bubbles) else (bubbles[0]["id"] if bubbles else None),
            "bubbles": bubbles,
        },
        "top_tension": [b["id"] for b in sorted(bubbles, key=lambda b: -b["tension"])],
        "editorial_items": agg.get("editorial", []),
        "scenarios": scen,
        "pipeline": {"returned_nodes": agg["returned_nodes"], "nodes": agg["nodes"], "warnings": agg["warnings"],
                     "claims_kept": agg["claims_kept"], "claims_dropped": len(agg["claims_dropped"]),
                     "claims_rejected": len(agg["claims_rejected"]), "stale_series": agg["stale_series"]},
    }


def fmt(v):
    return ("%+.1f" % v).replace("-", "\u2212")


def summary_html(state):
    cp, rg = state["compass"], state["regime"]
    q = {x["id"]: x for x in state["quadrants"]}.get(rg["quadrant"], {})
    stamp = cp.get("snapshot_stamp") or cp.get("as_of_data") or ""
    drivers = "\n".join('          <li>%s</li>' % html.escape(d["text"]) for d in rg["drivers"][:3])
    note = rg["note"]
    ld = {
        "@context": "https://schema.org", "@type": "Dataset",
        "name": "RYZM Macro Compass reading",
        "description": "%s. Rate pressure %s and liquidity flow %s on a -50 to +50 scale, computed from the RYZM market snapshot (%s). Drivers: %s Reads market structure; not a forecast." % (
            rg["label"], fmt(cp["y"]), fmt(cp["x"]), stamp, " ".join(d["text"] for d in rg["drivers"][:3])),
        "url": "https://ryzm.app/#cockpit", "dateModified": state["generated_at"],
        "creator": {"@id": "https://ryzm.app/#org"}, "isAccessibleForFree": True,
        "variableMeasured": ["US 2-year Treasury yield", "US 10-year Treasury yield", "SOFR", "ON RRP", "US dollar index (DXY)"],
        "distribution": [{"@type": "DataDownload", "encodingFormat": "application/json",
                          "contentUrl": "https://ryzm.app/data/dashboard_state.json"}],
    }
    return """<!-- regime-summary -->
      <div class="rg-static" id="regime-static" data-x="%s" data-y="%s">
        <div class="rg-mode" id="regime-mode">Sourced &middot; snapshot %s</div>
        <div class="rg-name" id="regime-name">%s</div>
        <dl class="rg-nums">
          <div><dt>Rate pressure</dt><dd id="ro-rates">%s</dd></div>
          <div><dt>Liquidity flow</dt><dd id="ro-liq">%s</dd></div>
        </dl>
        <ul class="rg-drivers" id="regime-drivers">
%s
        </ul>
        <p class="rg-read" id="regime-read">%s</p>
        <p class="rg-note" id="regime-note">%s</p>
        <script type="application/ld+json" id="regime-jsonld">
%s
        </script>
      </div>
      <!-- /regime-summary -->""" % (
        cp["x"], cp["y"], html.escape(stamp), html.escape(rg["label"]), fmt(cp["y"]), fmt(cp["x"]), drivers,
        html.escape(" ".join(q.get("read") or [])), html.escape(note["text"]),
        json.dumps(ld, ensure_ascii=False, indent=1).replace("</", "<\\/"))


def write_summary(state):
    if not state.get("compass"):
        return False
    text = open(c.INDEX, encoding="utf-8").read()
    new, n = re.subn(r"<!-- regime-summary -->.*?<!-- /regime-summary -->",
                     lambda _m: summary_html(state), text, count=1, flags=re.S)
    if n and new != text:
        open(c.INDEX, "w", encoding="utf-8").write(new)
        return True
    return False


def append_history(state):
    path = os.path.join(c.DATA, "compass_history.json")
    doc = c.read_json(path, {}) or {}
    entries = doc.get("entries", [])
    cp = state.get("compass")
    if cp and not cp.get("stale") and c.finite(cp.get("x"), cp.get("y")):
        e = {"date": cp["as_of_data"], "snapshot_stamp": cp.get("snapshot_stamp"), "x": cp["x"], "y": cp["y"],
             "quadrant": cp["quadrant"], "recorded_at": state["generated_at"]}
        entries = [x for x in entries if x.get("snapshot_stamp") != e["snapshot_stamp"]] + [e]
        entries.sort(key=lambda x: (x.get("date") or "", x.get("recorded_at") or ""))
    c.write_json(path, {"version": 1, "note": "One entry per published snapshot; the ghost needle reads the entry about a week back.",
                        "entries": entries[-180:]})


def draft(state):
    rg, cp = state["regime"], state["compass"]
    lines = ["# RYZM regime draft (not published)", "",
             "Snapshot: %s" % (cp.get("snapshot_stamp") or ""), "",
             "**%s.** Rate pressure %s, liquidity flow %s." % (rg["label"], fmt(cp["y"]), fmt(cp["x"])), ""]
    lines += ["- %s" % d["text"] for d in rg["drivers"]]
    lines += ["", "Note: %s" % rg["note"]["text"], "", "_%s_" % DISCLAIMER, ""]
    path = os.path.join(c.PIPE, "drafts", "briefing-latest.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w", encoding="utf-8").write("\n".join(lines))


def main():
    agg = c.read_json(os.path.join(c.PIPE, "aggregate.json"))
    if not agg:
        print("ABORT: run aggregate.py first")
        return 1
    tasks = c.read_json(os.path.join(c.PIPE, "tasks.json"), {}) or {}
    state = build_state(agg, tasks.get("run_id"))
    if not state["compass"]:
        print("ABORT: no compass available (not even a cached one); nothing written")
        return 1
    c.write_json(os.path.join(c.DATA, "dashboard_state.json"), state)
    append_history(state)
    if "--no-html" not in sys.argv:
        write_summary(state)
    if state["compass"] and state["regime"]["drivers"]:
        draft(state)
    print("RENDERED", state["regime"]["label"], "bubbles=%d" % len(state["bubble_map"]["bubbles"]),
          "warnings=%d" % len(state["pipeline"]["warnings"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
