# RYZM dashboard pipeline: routine procedure

Runs 3 times a day at **08:40, 15:40 and 22:40 KST**, right after the ticker
refresh. Code steps are deterministic scripts; the two LLM steps (claims and
verification) are done by the bot inside the scheduled routine, with no API key.
Everything writes cached JSON and drafts in this repo only. Nothing is posted to
any social network, and nothing is published outside ryzm.app.

All commands run from the repo root on a fresh checkout of `main` (or the
working branch). Python 3 standard library only.

## 0. Refresh the ticker and the snapshot JSON (code)

```
python3 scripts/update_ticker.py --json
```

* Writes the ticker block in `index.html` exactly as before (same markup).
* Then writes `data/snapshot.json` (the values shown on the page) and refreshes
  `data/snapshot_history.json` + `data/pipeline/history_full.json`.
* If the ticker was already updated by another writer, use
  `python3 scripts/update_ticker.py --json-only` (no ticker fetch; JSON is
  rebuilt from the ticker already in `index.html`).
* A failing source keeps its last good history (`"stale": true`). Nothing is blanked.

## 1. Step A, code half (code)

```
python3 scripts/pipeline/prepare.py --no-fetch
```

* Writes `data/nodes/<node>.json` for the 4 workers
  (`rates_funding`, `global_credit_macro`, `asia_early_signal`, `crypto_leverage`)
  with deterministic items from sourced prices.
* Writes `data/pipeline/tasks.json`: per node, the research scope, the current
  metrics as context, and the output path.

## 2. Step A, LLM half (routine, 4 workers, may run in parallel)

For each task in `data/pipeline/tasks.json`, research **at most 3** current,
sourced claims inside that node's scope and write
`data/pipeline/claims/<node_id>.json`:

```json
{
  "node_id": "asia_early_signal",
  "generated_at": "2026-11-28T08:45+09:00",
  "items": [
    {
      "node_id": "asia_early_signal",
      "asset_or_theme": "Korean won (USD/KRW)",
      "metric_value": 1337.1,
      "delta": "+0.2% w/w",
      "priced_in_score": 60,
      "momentum_score": -5,
      "tension_score": 30,
      "claim": "One factual sentence, American English, no prediction.",
      "source_url": "https://<primary source>",
      "published_at": "2026-11-27"
    }
  ]
}
```

Rules: `priced_in_score` 0-100, `momentum_score` -50..50, `tension_score` 0-100,
`source_url` must be https and a primary source (central bank, statistics
office, exchange, the publisher's own article). No x.com / twitter.com links, no
social posts, no SavePick. Use the same `asset_or_theme` text as a context item
when the claim is about that asset (it then attaches to that bubble as a note).

Validate every file and **retry up to 2 times** on INVALID:

```
python3 scripts/pipeline/check.py claims data/pipeline/claims/<node_id>.json
```

After 2 failed retries, delete that claims file. The code items for the node
still render, and aggregate.py logs a WARNING.

## 3. Step C, verifier (routine, one fresh empty-context check per claim)

```
python3 scripts/pipeline/check.py ids
```

prints `claim_id  node  claim  source_url` for every valid claim. For **each**
claim, start a fresh subagent with no other context, give it only the claim,
the URL and `published_at`, and ask: is the claim correct, is it current
(within 7 days), and is the source real and does it say this? Collect the answers in
`data/pipeline/verify_log.json`:

```json
{"run_id": "<run_id from tasks.json>",
 "verdicts": [{"claim_id": "1a2b3c4d5e6f", "verdict": "keep", "reason": "Matches the BOK release of Nov 27."}]}
```

```
python3 scripts/pipeline/check.py verify data/pipeline/verify_log.json
```

Claims without a `keep` verdict are dropped by aggregate.py.

## 4. Step B, aggregate (code)

```
python3 scripts/pipeline/aggregate.py
```

* `returned_nodes` must be 4. A failed, invalid or stale node logs
  `WARNING node <id> not returned` and is replaced by
  `data/nodes/_lastgood/<id>.json`; a healthy node becomes the new last-good copy.
* Deduplicates by source_url and topic; picks at most 7 bubbles (USD/KRW, KOSPI
  and memory chips pinned; then the highest tension per group).
* Computes the compass (formula in `scoring.py`) from the snapshot values and
  the ghost needle from `data/compass_history.json` (about a week back; else
  recomputed 5 sessions back). Non-finite coordinates are never written; the
  last published compass is kept instead.

## 5. Step D, render (code)

```
python3 scripts/pipeline/render.py
python3 scripts/validate_home.py index.html
PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests
```

* Writes `data/dashboard_state.json`, appends `data/compass_history.json`,
  rewrites only the `<!-- regime-summary -->` block in `index.html` (regime
  label, 3 drivers, 1 note, JSON-LD) and writes the unpublished draft
  `data/pipeline/drafts/briefing-latest.md`.

## 6. Commit and deploy

Commit only these paths (one commit, message `Dashboard refresh (<date> <HH:MM> KST)`):

```
index.html  data/snapshot.json  data/snapshot_history.json  data/dashboard_state.json
data/compass_history.json  data/nodes/  data/pipeline/
data/regime_ledger.json  regime/index.html
```

When writing through the GitHub API, send file content by file path, never inline, so
`&amp;` and `&quot;` entities in index.html survive. Then compare the ticker,
`ul.breaking-list` and `div.feed` blocks with the previous commit: they must be
byte-identical apart from the ticker refresh.

Pushes do not auto-deploy this project. Create a Vercel deployment for the new
commit (production only after the user has approved the dashboard).

## Failure behavior (what the page shows)

| Failure | Result |
| --- | --- |
| A ticker source fails | Ticker keeps the item from the last run; history marked stale |
| A node fails | WARNING; last valid node cache is used; stamp says "on cached data" |
| Claims invalid twice | Claims for that node are skipped; code items still render |
| Compass inputs missing | Last published compass is kept (`compass.stale: true`) |
| JSON fails to load in the browser | localStorage copy; else the static HTML summary stays |
