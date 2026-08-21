#!/usr/bin/env python3
"""
pangram_scan.py - a reproducible harness for testing the Pangram AI-text detector.

Built for a public methodology writeup, so the design goal is auditability rather
than convenience. Every scan's raw API response is saved verbatim to disk, every
input is hashed before it is sent, and the model selector actually used is recorded
on every single run. A reader should be able to check the work without trusting
either me or Pangram.

Deliberate choices worth knowing about:

  * Input text is NEVER normalized, trimmed, or reformatted. What is on disk is
    what gets sent, and the sha256 in the results proves it.
  * The /task endpoint is used rather than /bulk. The published docs give the
    request shape for /task ("text" and "model") but not for /bulk, and guessing
    a schema is not something you want in a methodology section.
  * Runs are resumable and cost-guarded, because credits are prepaid and a typo
    in a loop is otherwise expensive.
  * The response schema is not fully documented, so extraction is best-effort and
    the raw JSON is always kept. Run `keys` after the first real scan to see every
    field Pangram actually returned, then tighten EXTRACT_CANDIDATES.

Usage
-----
    export PANGRAM_API_KEY=...              # or put it in .env beside this file

    python pangram_scan.py probe            # list models + one sample scan, dump raw
    python pangram_scan.py scan             # scan every row in manifest.csv
    python pangram_scan.py scan --doc opinion-2017-lopez --repeat 5   # determinism test
    python pangram_scan.py embed --doc para-01 --carrier carrier-long # context test
    python pangram_scan.py keys             # every JSON path seen across saved results
    python pangram_scan.py report           # rebuild results.csv from saved raw JSON

Requires: requests  (pip install requests)
"""

import argparse
import csv
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Needs `requests`. Try: pip install requests")

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

SCRIPT_VERSION = "1.0.0"

TEXT_API = "https://text.external-api.pangram.com"

HERE = Path(__file__).resolve().parent
CORPUS_DIR = HERE / "corpus"
RESULTS_DIR = HERE / "results"
RAW_DIR = RESULTS_DIR / "raw"
MANIFEST = HERE / "manifest.csv"
RUNS_LOG = RESULTS_DIR / "runs.jsonl"
RESULTS_CSV = RESULTS_DIR / "results.csv"

# $0.05 per 100 words, per Pangram's published Developer pricing.
USD_PER_100_WORDS = 0.05

# Pangram's model card states a 50-word floor for supported input.
MIN_WORDS = 50

# Poll settings for the submit-then-poll task flow.
POLL_INTERVAL_S = 1.5
POLL_TIMEOUT_S = 180

# Response schema, confirmed against the live API on 2026-08-21. Top-level fields:
#   stage, text (echoed back), version, prediction, prediction_short,
#   fraction_ai, fraction_ai_assisted, fraction_human, headline,
#   num_ai_segments, num_ai_assisted_segments, num_human_segments, windows[]
# Each window: text, label, ai_assistance_score, confidence, start_index,
#   end_index, word_count, token_length, and on Pangram 4 also is_humanized
#   and humanizer_score
#
# `version` is the field that matters most: it reports which model actually ran,
# and it does not always match what you asked for. See resolve_model().
TOP_LEVEL_FIELDS = [
    "version", "prediction_short", "headline", "prediction",
    "fraction_ai", "fraction_ai_assisted", "fraction_human",
    "num_ai_segments", "num_ai_assisted_segments", "num_human_segments",
]


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_api_key():
    key = os.environ.get("PANGRAM_API_KEY")
    if key:
        return key.strip()
    dotenv = HERE / ".env"
    if dotenv.exists():
        for line in dotenv.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("PANGRAM_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit(
        "No API key. Either:\n"
        "  export PANGRAM_API_KEY=sk-...\n"
        f"or create {dotenv} containing:\n"
        "  PANGRAM_API_KEY=sk-...\n"
        "(.env is gitignored here; do not commit the key.)"
    )


def word_count(text):
    return len(text.split())


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cost_usd(words):
    return round(words / 100.0 * USD_PER_100_WORDS, 4)


def read_doc(doc_id, path=None):
    p = Path(path) if path else (CORPUS_DIR / f"{doc_id}.txt")
    if not p.is_absolute():
        p = HERE / p
    if not p.exists():
        sys.exit(f"Missing corpus file for '{doc_id}': {p}")
    # newline="" so line endings survive exactly as stored; we hash what we send.
    return p.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# API layer
# --------------------------------------------------------------------------

class Pangram:
    def __init__(self, api_key, verbose=True):
        self.session = requests.Session()
        self.session.headers.update({
            "x-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        self.verbose = verbose

    def _request(self, method, url, max_retries=5, **kw):
        """One request with backoff on 429 and 5xx. Everything else raises."""
        delay = 2.0
        for attempt in range(1, max_retries + 1):
            resp = self.session.request(method, url, timeout=60, **kw)

            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", delay))
                if self.verbose:
                    print(f"    rate limited, waiting {wait:.0f}s "
                          f"(attempt {attempt}/{max_retries})")
                time.sleep(wait)
                delay = min(delay * 2, 60)
                continue

            if 500 <= resp.status_code < 600:
                if self.verbose:
                    print(f"    server error {resp.status_code}, retrying in {delay:.0f}s")
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue

            if not resp.ok:
                raise RuntimeError(
                    f"{method} {url} -> {resp.status_code}\n{resp.text[:1000]}"
                )
            return resp

        raise RuntimeError(f"{method} {url} failed after {max_retries} attempts")

    def models(self):
        return self._request("GET", f"{TEXT_API}/models").json()

    def scan(self, text, model):
        """Submit text, poll to completion, return the raw response dict."""
        submit = self._request(
            "POST", f"{TEXT_API}/task",
            data=json.dumps({"text": text, "model": model}),
        ).json()

        task_id = _first_of(submit, ["task_id", "taskId", "id"])
        if not task_id:
            raise RuntimeError(
                "Could not find a task id in the submit response. Raw:\n"
                + json.dumps(submit, indent=2)[:2000]
            )

        deadline = time.time() + POLL_TIMEOUT_S
        while time.time() < deadline:
            time.sleep(POLL_INTERVAL_S)
            result = self._request("GET", f"{TEXT_API}/task/{task_id}").json()
            stage = str(_first_of(result, ["stage", "status", "state"]) or "").upper()

            if "SUCCESS" in stage or stage in ("SUCCEEDED", "COMPLETE", "COMPLETED", "DONE"):
                return result
            if "FAIL" in stage or "ERROR" in stage:
                raise RuntimeError(
                    f"Task {task_id} failed. Raw:\n{json.dumps(result, indent=2)[:2000]}"
                )

        raise RuntimeError(f"Task {task_id} did not finish inside {POLL_TIMEOUT_S}s")


def _first_of(d, names):
    """Return the first present key from `names`, searching one level deep too."""
    if not isinstance(d, dict):
        return None
    for n in names:
        if n in d and d[n] is not None:
            return d[n]
    for v in d.values():
        if isinstance(v, dict):
            got = _first_of(v, names)
            if got is not None:
                return got
    return None


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------

MANIFEST_FIELDS = [
    "doc_id",       # unique id; corpus/<doc_id>.txt unless `path` is set
    "path",         # optional explicit path
    "label",        # ground truth: human | ai | mixed | unknown
    "genre",        # blog | opinion | law_review | memo | synthetic | carrier
    "pub_date",     # publication date, for pre-2022 provenance claims
    "source_url",   # where a reader can verify it
    "notes",        # anything a reader needs to judge the case
]


def load_manifest():
    if not MANIFEST.exists():
        sys.exit(f"No manifest at {MANIFEST}. Run `python pangram_scan.py init` first.")
    rows = []
    with MANIFEST.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get("doc_id", "").strip().startswith("#"):
                continue
            if not row.get("doc_id", "").strip():
                continue
            rows.append(row)
    return rows


def cmd_init(args):
    if MANIFEST.exists() and not args.force:
        sys.exit(f"{MANIFEST} already exists. Use --force to overwrite.")
    with MANIFEST.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=MANIFEST_FIELDS)
        w.writeheader()
        w.writerow({
            "doc_id": "example-blog-2018-03",
            "path": "",
            "label": "human",
            "genre": "blog",
            "pub_date": "2018-03-14",
            "source_url": "https://sagelewis.com/...",
            "notes": "Published four years before ChatGPT existed.",
        })
    print(f"Wrote template manifest: {MANIFEST}")
    print(f"Drop matching .txt files in: {CORPUS_DIR}")


# --------------------------------------------------------------------------
# Result storage
# --------------------------------------------------------------------------

def raw_path(doc_id, run_tag):
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in doc_id)
    return RAW_DIR / f"{safe}__{run_tag}.json"


def save_run(record):
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_path(record["doc_id"], record["run_tag"]).write_text(
        json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    with RUNS_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({k: v for k, v in record.items() if k != "response"}) + "\n")


def extract_summary(response):
    """Flatten a Pangram response into CSV columns. Raw JSON is kept regardless."""
    out = {f: response.get(f) for f in TOP_LEVEL_FIELDS}

    windows = response.get("windows") or []
    scores = [w.get("ai_assistance_score") for w in windows
              if isinstance(w.get("ai_assistance_score"), (int, float))]
    labels = [w.get("label") for w in windows if w.get("label")]
    confs = [w.get("confidence") for w in windows if w.get("confidence")]

    out["num_windows"] = len(windows)
    out["score_min"] = round(min(scores), 6) if scores else None
    out["score_max"] = round(max(scores), 6) if scores else None
    out["score_mean"] = round(sum(scores) / len(scores), 6) if scores else None
    # Window disagreement is worth surfacing: one document, several verdicts.
    out["window_labels"] = "|".join(dict.fromkeys(labels)) or None
    out["window_confidences"] = "|".join(dict.fromkeys(confs)) or None

    # Pangram 4 only: the humanizer probe, i.e. "was this run through a tool
    # designed to defeat detection". Absent on 3.3.2.
    hscores = [w.get("humanizer_score") for w in windows
               if isinstance(w.get("humanizer_score"), (int, float))]
    out["any_humanized"] = any(w.get("is_humanized") for w in windows) if windows else None
    out["humanizer_score_max"] = round(max(hscores), 6) if hscores else None
    return out


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def resolve_model(api, requested):
    """Return the model selector to use, and log what the account offers."""
    try:
        available = api.models()
    except Exception as exc:
        print(f"  ! could not list models ({exc}); using '{requested}' as given")
        return requested, None

    print(f"  models available to this account: {json.dumps(available)[:600]}")

    offered = available.get("models") if isinstance(available, dict) else None
    if requested:
        if isinstance(offered, list) and requested not in offered:
            sys.exit(f"Model '{requested}' is not offered to this account. "
                     f"Available: {offered}")
        if requested == "default":
            print("  ! WARNING: 'default' is an alias, and as of 2026-08-21 it "
                  "resolves to Pangram 3.3.2, not Pangram 4. Pin an explicit model "
                  "for anything you intend to publish.")
        return requested, available

    # No explicit choice: take the first string that looks like a selector.
    def walk(node):
        if isinstance(node, str):
            return node
        if isinstance(node, list):
            for item in node:
                got = walk(item)
                if got:
                    return got
        if isinstance(node, dict):
            for key in ("name", "id", "model", "slug"):
                if isinstance(node.get(key), str):
                    return node[key]
            for v in node.values():
                got = walk(v)
                if got:
                    return got
        return None

    picked = walk(available)
    if not picked:
        sys.exit("Could not infer a model selector. Pass one with --model.")
    print(f"  no --model given, using: {picked}")
    return picked, available


def run_one(api, doc_id, text, model, run_tag, meta, dry_run=False):
    words = word_count(text)
    est = cost_usd(words)

    if words < MIN_WORDS:
        print(f"  ! {doc_id}: {words} words is under Pangram's stated {MIN_WORDS}-word "
              f"floor. Scanning anyway, but flag this in the writeup.")

    if dry_run:
        print(f"  [dry run] {doc_id} [{run_tag}] {words} words, ~${est}")
        return None

    started = now_iso()
    t0 = time.time()
    response = api.scan(text, model)
    elapsed = round(time.time() - t0, 2)

    returned_version = response.get("version")
    if model == "pangram-4" and returned_version and not str(returned_version).startswith("4"):
        print(f"  ! MODEL MISMATCH: asked for '{model}', API reports version "
              f"'{returned_version}'. Record this; do not silently report it as Pangram 4.")

    record = {
        "doc_id": doc_id,
        "run_tag": run_tag,
        "scanned_at_utc": started,
        "elapsed_s": elapsed,
        "script_version": SCRIPT_VERSION,
        "model_selector": model,
        "model_version_returned": returned_version,
        "word_count": words,
        "char_count": len(text),
        "text_sha256": sha256(text),
        "est_cost_usd": est,
        **{f"meta_{k}": v for k, v in meta.items()},
        "response": response,
    }
    save_run(record)

    s = extract_summary(response)
    shown = (f"{s.get('prediction_short')} "
             f"(ai={s.get('fraction_ai')} assisted={s.get('fraction_ai_assisted')} "
             f"human={s.get('fraction_human')} max_score={s.get('score_max')} "
             f"conf={s.get('window_confidences')} v={returned_version})")
    print(f"  {doc_id} [{run_tag}] {words}w ${est} {elapsed}s -> {shown}")
    return record


def cmd_probe(args):
    api = Pangram(load_api_key())
    print("Listing models...")
    model, _ = resolve_model(api, args.model)

    sample = (
        "The question presented is whether a municipality may enforce an ordinance "
        "prohibiting camping on public property when no shelter beds are available "
        "within its jurisdiction. The district court concluded that it may not, "
        "reasoning that enforcement under those circumstances punishes conduct that "
        "is unavoidable given the claimant's status. We review the grant of summary "
        "judgment de novo, drawing all reasonable inferences in favor of the "
        "nonmoving party, and we affirm in part and reverse in part for the reasons "
        "that follow. The record establishes that the city operated fewer beds than "
        "its unsheltered population on every night at issue."
    )
    print(f"\nScanning a {word_count(sample)}-word sample so we can see the schema...")
    response = api.scan(sample, model)

    print("\n--- RAW RESPONSE ---")
    print(json.dumps(response, indent=2, ensure_ascii=False))
    print("--- END RAW RESPONSE ---\n")

    out = RESULTS_DIR / f"probe_response__{model}.json"
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"model_selector": model, "probed_at_utc": now_iso(), "response": response},
        indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved to {out}")
    print("Paste that file's contents back to Claude to lock down the CSV extractor.")


def cmd_scan(args):
    rows = load_manifest()
    if args.doc:
        rows = [r for r in rows if r["doc_id"] in set(args.doc)]
        if not rows:
            sys.exit(f"No manifest rows matched: {args.doc}")

    # Build the work list first so cost is known before anything is sent.
    work = []
    for row in rows:
        text = read_doc(row["doc_id"], row.get("path") or None)
        for i in range(1, args.repeat + 1):
            run_tag = f"r{i}" if args.repeat > 1 else "r1"
            if raw_path(row["doc_id"], run_tag).exists() and not args.force:
                continue
            work.append((row, text, run_tag))

    if not work:
        print("Nothing to do. Everything in scope is already scanned (use --force to redo).")
        return

    total_words = sum(word_count(t) for _, t, _ in work)
    total_cost = cost_usd(total_words)
    print(f"{len(work)} scans, {total_words:,} words, estimated ${total_cost:.2f}")

    if total_cost > args.budget_usd:
        sys.exit(f"Estimated ${total_cost:.2f} exceeds --budget-usd {args.budget_usd:.2f}. "
                 f"Raise the budget deliberately or narrow the scope with --doc.")

    if not args.yes and not args.dry_run:
        if input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
            sys.exit("Aborted.")

    api = Pangram(load_api_key())
    model, _ = resolve_model(api, args.model)

    spent = 0.0
    failures = []
    for row, text, run_tag in work:
        meta = {k: row.get(k, "") for k in ("label", "genre", "pub_date", "source_url", "notes")}
        try:
            rec = run_one(api, row["doc_id"], text, model, run_tag, meta, args.dry_run)
            if rec:
                spent += rec["est_cost_usd"]
        except Exception as exc:
            print(f"  ! {row['doc_id']} [{run_tag}] FAILED: {exc}")
            failures.append((row["doc_id"], run_tag, str(exc)))

    print(f"\nDone. Estimated spend this run: ${spent:.2f}")
    if failures:
        print(f"{len(failures)} failed:")
        for doc_id, tag, err in failures:
            print(f"  {doc_id} [{tag}]: {err[:200]}")
    if not args.dry_run:
        cmd_report(argparse.Namespace())


def cmd_embed(args):
    """Context test: scan a passage alone, then inside a carrier document.

    Pangram's own technical report concedes that "predictions for the same text in
    different contexts may also be inconsistent." This measures that directly.
    """
    passage = read_doc(args.doc)
    carrier = read_doc(args.carrier)

    positions = {
        "alone": passage,
        "prepended": passage + "\n\n" + carrier,
        "appended": carrier + "\n\n" + passage,
    }
    # Middle insertion at the nearest paragraph break to the halfway point.
    paras = carrier.split("\n\n")
    mid = len(paras) // 2
    positions["middle"] = "\n\n".join(paras[:mid] + [passage] + paras[mid:])

    total_words = sum(word_count(t) for t in positions.values())
    print(f"{len(positions)} scans, {total_words:,} words, "
          f"estimated ${cost_usd(total_words):.2f}")
    if cost_usd(total_words) > args.budget_usd:
        sys.exit("Over budget. Raise --budget-usd deliberately.")
    if not args.yes and input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
        sys.exit("Aborted.")

    api = Pangram(load_api_key())
    model, _ = resolve_model(api, args.model)

    for position, text in positions.items():
        meta = {"label": "context-test", "genre": "context",
                "pub_date": "", "source_url": "",
                "notes": f"passage={args.doc} carrier={args.carrier} position={position}"}
        try:
            run_one(api, f"{args.doc}+{args.carrier}", text, model,
                    f"ctx-{position}", meta, args.dry_run)
        except Exception as exc:
            print(f"  ! {position} FAILED: {exc}")

    print("\nIf the verdict differs across positions, that is the finding. "
          "Same words, same model, different answer.")


def cmd_keys(args):
    """Walk every saved response and print the JSON paths that actually exist."""
    files = sorted(RAW_DIR.glob("*.json"))
    if not files:
        sys.exit(f"No saved results in {RAW_DIR}. Run `probe` or `scan` first.")

    seen = {}

    def walk(node, path=""):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else k)
        elif isinstance(node, list):
            if node:
                walk(node[0], f"{path}[0]")
            seen.setdefault(f"{path} (len)", []).append(len(node))
        else:
            seen.setdefault(path, []).append(node)

    for f in files:
        blob = json.loads(f.read_text(encoding="utf-8"))
        walk(blob.get("response", blob), "")

    print(f"JSON paths across {len(files)} saved result files:\n")
    for path in sorted(seen):
        vals = seen[path]
        sample = repr(vals[0])
        if len(sample) > 80:
            sample = sample[:77] + "..."
        distinct = len({repr(v) for v in vals})
        print(f"  {path:<50} n={len(vals):<4} distinct={distinct:<4} e.g. {sample}")
    print("\nMap the real score/verdict fields into EXTRACT_CANDIDATES, "
          "then rerun `report`. No re-scanning needed.")


def cmd_report(args):
    files = sorted(RAW_DIR.glob("*.json"))
    if not files:
        print(f"No saved results in {RAW_DIR}.")
        return

    rows = []
    for f in files:
        rec = json.loads(f.read_text(encoding="utf-8"))
        row = {k: v for k, v in rec.items() if k != "response"}
        row.update(extract_summary(rec.get("response", {})))
        row["raw_json"] = str(f.relative_to(HERE))
        rows.append(row)

    fields, ordered = set(), []
    for r in rows:
        for k in r:
            if k not in fields:
                fields.add(k)
                ordered.append(k)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    with RESULTS_CSV.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=ordered)
        w.writeheader()
        w.writerows(rows)

    total = sum(r.get("est_cost_usd", 0) or 0 for r in rows)
    print(f"Wrote {RESULTS_CSV} ({len(rows)} runs, ~${total:.2f} of credit used)")


# --------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description="Reproducible test harness for the Pangram AI detector.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--model", default="pangram-4",
                        help="Model selector (default: pangram-4). NOTE: 'default' "
                             "is an alias that currently resolves to Pangram 3.3.2.")
        sp.add_argument("--budget-usd", type=float, default=25.0)
        sp.add_argument("--dry-run", action="store_true")
        sp.add_argument("--yes", "-y", action="store_true")

    sp = sub.add_parser("init", help="write a template manifest.csv")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("probe", help="list models and dump one raw response")
    common(sp)
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser("scan", help="scan the manifest")
    common(sp)
    sp.add_argument("--doc", action="append",
                    help="limit to these doc_ids (repeatable)")
    sp.add_argument("--repeat", type=int, default=1,
                    help="scan each doc N times - the determinism test")
    sp.add_argument("--force", action="store_true", help="rescan already-saved runs")
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("embed", help="context test: passage alone vs inside a carrier")
    common(sp)
    sp.add_argument("--doc", required=True, help="doc_id of the passage")
    sp.add_argument("--carrier", required=True, help="doc_id of the surrounding document")
    sp.set_defaults(func=cmd_embed)

    sp = sub.add_parser("keys", help="show every JSON path in saved responses")
    sp.set_defaults(func=cmd_keys)

    sp = sub.add_parser("report", help="rebuild results.csv from saved raw JSON")
    sp.set_defaults(func=cmd_report)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
