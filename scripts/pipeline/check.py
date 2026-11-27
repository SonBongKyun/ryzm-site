"""Validator the routine runs on every LLM output before aggregation.

  python3 scripts/pipeline/check.py claims data/pipeline/claims/<node>.json
      -> exit 0 and prints one claim_id per valid item; exit 2 with the
         problems if the file must be rewritten (the routine retries up to 2x).
  python3 scripts/pipeline/check.py verify data/pipeline/verify_log.json
      -> checks every verdict is {claim_id, verdict: keep|drop, reason}.
  python3 scripts/pipeline/check.py ids
      -> lists claim_id, node, claim and source_url for every claims file,
         i.e. the work list for the Step C verifier.
"""
import glob
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import common as c  # noqa: E402


def claim_id(it):
    raw = "%s|%s|%s" % (it.get("node_id"), it.get("source_url"), it.get("claim"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def check_claims(path):
    doc = c.read_json(path)
    if doc is None:
        return ["not valid JSON (or missing)"], []
    node = os.path.splitext(os.path.basename(path))[0]
    errs = c.validate_node(doc, node)
    if isinstance(doc, dict) and isinstance(doc.get("items"), list) and len(doc["items"]) > 3:
        errs.append("at most 3 items per node")
    ids = [claim_id(it) for it in doc.get("items", [])] if not errs else []
    return errs, ids


def check_verify(path):
    doc = c.read_json(path)
    if not isinstance(doc, dict) or not isinstance(doc.get("verdicts"), list):
        return ["verify_log must be {\"run_id\": ..., \"verdicts\": [...]}"]
    errs = []
    for i, v in enumerate(doc["verdicts"]):
        if not isinstance(v, dict):
            errs.append("verdicts[%d] is not an object" % i)
            continue
        if not isinstance(v.get("claim_id"), str) or len(v["claim_id"]) != 12:
            errs.append("verdicts[%d].claim_id must be the 12-char id from check.py ids" % i)
        if v.get("verdict") not in ("keep", "drop"):
            errs.append("verdicts[%d].verdict must be keep or drop" % i)
        if not isinstance(v.get("reason"), str) or not v["reason"].strip():
            errs.append("verdicts[%d].reason is required" % i)
    return errs


def main(argv):
    if len(argv) >= 3 and argv[1] == "claims":
        errs, ids = check_claims(argv[2])
        if errs:
            print("INVALID", argv[2])
            for e in errs:
                print(" -", e)
            return 2
        print("VALID", argv[2])
        for i in ids:
            print(i)
        return 0
    if len(argv) >= 3 and argv[1] == "verify":
        errs = check_verify(argv[2])
        print("INVALID" if errs else "VALID", argv[2])
        for e in errs:
            print(" -", e)
        return 2 if errs else 0
    if len(argv) >= 2 and argv[1] == "ids":
        for path in sorted(glob.glob(os.path.join(c.PIPE, "claims", "*.json"))):
            errs, _ = check_claims(path)
            if errs:
                print("# skipped invalid", path)
                continue
            for it in c.read_json(path)["items"]:
                print("%s\t%s\t%s\t%s" % (claim_id(it), it["node_id"], it["claim"], it["source_url"]))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
