"""Step B: pure code. Merge nodes, enforce the 4-node rule, compute the compass.

Inputs
  data/nodes/<node>.json            code items from prepare.py
  data/pipeline/claims/<node>.json  optional LLM items (routine), schema-checked here
  data/pipeline/verify_log.json     Step C verdicts; only "keep" claims survive
  data/snapshot.json, data/pipeline/history_full.json
  data/compass_history.json         stored needles (ghost needle source)
Output
  data/pipeline/aggregate.json      consumed by render.py

Rules
  * returned_nodes must equal 4. A node that is missing, invalid, failed or stale
    raises a WARNING and is replaced by data/nodes/_lastgood/<node>.json.
  * A node that passes becomes the new _lastgood copy.
  * Bubble map: at most 7. USD/KRW, KOSPI and memory chips are pinned; the rest
    are the highest-tension item per group, then the next-highest tension.
  * Items are deduplicated by source_url and by normalized topic.
  * Coordinates are only written when finite and in range; otherwise the last
    dashboard_state.json compass is kept and flagged stale.
"""
import os
import re
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import common as c  # noqa: E402
from pipeline import scoring  # noqa: E402
from pipeline.check import check_claims, claim_id  # noqa: E402
from pipeline.prepare import MAX_BUBBLES, PINNED  # noqa: E402


def topic(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def load_nodes(warnings):
    used, items, returned = {}, [], 0
    for node in c.NODE_IDS:
        path = os.path.join(c.NODES, node + ".json")
        doc = c.read_json(path)
        problems = c.validate_node(doc, node) if doc is not None else ["missing"]
        ok = not problems and doc.get("status") == "ok"
        if ok:
            returned += 1
            c.write_json(os.path.join(c.LASTGOOD, node + ".json"), doc)
            used[node] = {"status": "ok", "source": "current", "generated_at": doc.get("generated_at"), "items": len(doc["items"])}
            items += [dict(it, stale=bool(it.get("stale"))) for it in doc["items"]]
            continue
        reason = (doc or {}).get("status") if not problems else "; ".join(problems[:3])
        warnings.append("WARNING node %s not returned (%s); using last valid cache" % (node, reason))
        lg = c.read_json(os.path.join(c.LASTGOOD, node + ".json"))
        if lg and not c.validate_node(lg, node):
            used[node] = {"status": "fallback", "source": "lastgood", "generated_at": lg.get("generated_at"), "items": len(lg["items"])}
            items += [dict(it, stale=True) for it in lg["items"]]
        elif doc and isinstance(doc.get("items"), list) and not problems:
            used[node] = {"status": "stale", "source": "current-stale", "generated_at": doc.get("generated_at"), "items": len(doc["items"])}
            items += [dict(it, stale=True) for it in doc["items"]]
        else:
            used[node] = {"status": "missing", "source": "none", "items": 0}
            warnings.append("WARNING node %s has no cache; its bubbles are omitted" % node)
    return used, items, returned


def load_claims(warnings):
    vlog = c.read_json(os.path.join(c.PIPE, "verify_log.json"), {}) or {}
    verdicts = {v.get("claim_id"): v for v in vlog.get("verdicts", []) if isinstance(v, dict)}
    kept, dropped, rejected = [], [], []
    for node in c.NODE_IDS:
        path = os.path.join(c.PIPE, "claims", node + ".json")
        if not os.path.exists(path):
            continue
        errs, _ids = check_claims(path)
        if errs:
            rejected.append({"node_id": node, "problems": errs[:5]})
            warnings.append("WARNING claims for %s rejected by schema" % node)
            continue
        for it in c.read_json(path)["items"]:
            cid = claim_id(it)
            v = verdicts.get(cid)
            if v and v.get("verdict") == "keep":
                kept.append(dict(it, claim_id=cid, score_method="llm", verify_reason=v.get("reason")))
            else:
                dropped.append({"claim_id": cid, "node_id": node,
                                "reason": (v or {}).get("reason") or "not verified"})
    return kept, dropped, rejected


def dedupe(code, llm):
    seen_url, seen_topic, out_code, notes, extra = set(), {}, [], [], []
    for it in code:
        seen_topic[topic(it["asset_or_theme"])] = it
        out_code.append(it)
    for it in llm:
        t = topic(it["asset_or_theme"])
        if it["source_url"] in seen_url:
            continue
        seen_url.add(it["source_url"])
        if t in seen_topic:          # same asset as a code item: attach as a note
            notes.append(dict(it, attaches_to=seen_topic[t].get("id")))
            continue
        seen_topic[t] = it
        extra.append(dict(it, id="llm_" + it["claim_id"], group=it.get("group") or "Theme", read=it["claim"],
                          sens={"priced_in_x": 0.2, "priced_in_y": -0.1, "flow_x": 0.25, "flow_y": -0.1}))
    return out_code, notes, extra


def pick_bubbles(cands):
    good = [b for b in cands if c.finite(b["priced_in_score"], b["momentum_score"], b["tension_score"])]
    pinned = [b for b in good if b.get("id") in PINNED]
    rest = sorted((b for b in good if b.get("id") not in PINNED), key=lambda b: -b["tension_score"])
    # one bubble per group first (variety), then fill by tension
    first, groups = [], set()
    for b in rest:
        if b.get("group") not in groups:
            first.append(b)
            groups.add(b.get("group"))
    fill = [b for b in rest if b not in first]
    chosen = (pinned + sorted(first, key=lambda b: -b["tension_score"]) + fill)[:MAX_BUBBLES]
    return chosen


def ghost(history, snapshot, now, hist_entries):
    """Ghost needle = stored needle from about a week before; else recompute 5 sessions back."""
    if now:
        try:
            cutoff = (datetime.strptime(now["as_of_data"], "%Y-%m-%d") - timedelta(days=6)).strftime("%Y-%m-%d")
        except ValueError:
            cutoff = None
        older = [e for e in hist_entries if cutoff and e.get("date", "9999") <= cutoff and c.finite(e.get("x"), e.get("y"))]
        if older:
            e = older[-1]
            return {"x": e["x"], "y": e["y"], "as_of": e["date"], "source": "compass_history"}
    pw = scoring.compass(history, snapshot, back=c.WEEK)
    if pw:
        return {"x": pw["x"], "y": pw["y"], "as_of": pw["as_of_data"], "source": "recomputed_5_sessions_back"}
    return None


def drivers(now):
    labels = {"US2Y": "US 2Y yield", "US10Y": "US 10Y yield", "SOFR": "SOFR", "DXY": "The dollar index (DXY)", "ONRRP": "ON RRP usage"}
    out = []
    for k, w in sorted(now["contributions"].items(), key=lambda kv: -abs(kv[1]))[:3]:
        inp = now["inputs"][k]
        ch = inp["change"]
        unit = inp["unit"]
        s = "+" if ch > 0 else ("\u2212" if ch < 0 else "\u00b1")
        a = abs(ch)
        val = ("%s%.0f bp" % (s, a)) if unit == "bp" else ("%s$%.2fbn" % (s, a)) if unit == "usd_bn" else ("%s%.1f%%" % (s, a))
        axis = "rate pressure" if k in ("US2Y", "US10Y", "SOFR") else "dollar liquidity"
        if k in ("US2Y", "US10Y", "SOFR"):
            effect = "added to rate pressure" if w > 0 else "eased rate pressure" if w < 0 else "left rate pressure flat"
        else:
            effect = "tightened dollar liquidity" if w < 0 else "eased dollar liquidity" if w > 0 else "left dollar liquidity flat"
        out.append({"key": k, "axis": axis, "change": ch, "unit": unit, "from_date": inp["base_date"], "to_date": inp["end_date"],
                    "text": "%s %s over 20 sessions %s." % (labels[k], val, effect)})
    return out


def main():
    warnings = []
    full = c.read_json(os.path.join(c.PIPE, "history_full.json"), {}) or {}
    snapshot = c.read_json(os.path.join(c.DATA, "snapshot.json"), {}) or {}
    history = {s["key"]: [(h["date"], h["value"]) for h in s.get("history", [])] for s in full.get("series", [])}
    used, code, returned = load_nodes(warnings)
    if returned != len(c.NODE_IDS):
        warnings.append("WARNING returned_nodes=%d (expected %d)" % (returned, len(c.NODE_IDS)))
    llm, dropped, rejected = load_claims(warnings)
    code, notes, extra = dedupe(code, llm)
    bubbles = pick_bubbles(code + extra)
    for b in bubbles:
        b["notes"] = [{"claim": n["claim"], "source_url": n["source_url"], "published_at": n["published_at"]}
                      for n in notes if n.get("attaches_to") == b.get("id")]
    now = scoring.compass(history, snapshot.get("items") or {})
    prev_state = c.read_json(os.path.join(c.DATA, "dashboard_state.json"), {}) or {}
    compass_stale = False
    if not now:
        warnings.append("WARNING compass inputs incomplete; keeping the last published compass")
        now = (prev_state.get("compass") or None)
        compass_stale = True
    hist_entries = (c.read_json(os.path.join(c.DATA, "compass_history.json"), {}) or {}).get("entries", [])
    agg = {
        "generated_at": c.iso_now(), "snapshot_stamp": snapshot.get("snapshot_stamp"),
        "returned_nodes": returned, "nodes": used, "warnings": warnings,
        "compass": now, "compass_stale": compass_stale,
        "prev_needle": (now or {}).get("prev_needle") if compass_stale else ghost(history, snapshot.get("items") or {}, now, hist_entries),
        "drivers": drivers(now) if now and not compass_stale and now.get("contributions") else (prev_state.get("regime") or {}).get("drivers", []),
        "bubbles": bubbles, "claims_kept": len(llm), "claims_dropped": dropped, "claims_rejected": rejected,
        "stale_series": full.get("stale_series", []), "source_errors": full.get("errors", []),
    }
    c.write_json(os.path.join(c.PIPE, "aggregate.json"), agg)
    for w in warnings:
        print(w, file=sys.stderr)
    print("AGGREGATE returned_nodes=%d bubbles=%d compass=%s" % (
        returned, len(bubbles), now and (now.get("quadrant_label") or now.get("quadrant"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
