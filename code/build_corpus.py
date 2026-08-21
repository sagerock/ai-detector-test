#!/usr/bin/env python3
"""
build_corpus.py - sample pre-2020 published judicial opinions from the CourtListener
bulk export into a Pangram test corpus.

Ground truth here is the filing date. Anything filed before 2020-01-01 predates any
plausible LLM involvement in judicial drafting, so the "not machine-generated" label
is as solid as a label gets.

Two passes, because the opinions export is 343 GB and a full scan is unnecessary:

  Pass 1  Stream opinion-clusters (12 GB) and reservoir-sample cluster ids that are
          pre-cutoff, Published, and name a judge. Cheap and gives a genuinely random
          target set rather than "whatever is at the top of the file."
  Pass 2  Stream opinions and pull text for those ids, stopping once the target count
          is hit. Opinion ids are interleaved rather than sorted, so hits arrive early
          and only a few GB get read.

Excerpt rule, applied uniformly and disclosed in the manifest: collapse all whitespace,
then take words [SKIP : SKIP+LENGTH]. The skip exists to clear the caption block, the
docket numbers, and the clerk's filing stamp, none of which are judicial prose. Full
text is preserved alongside every excerpt so a reader can widen the window and re-run.

  python build_corpus.py clusters          # pass 1
  python build_corpus.py opinions --target 80
"""

import argparse, csv, json, os, random, re, sys
from pathlib import Path

csv.field_size_limit(sys.maxsize)

CL = Path(os.environ.get("COURTLISTENER_DIR", "./courtlistener"))
CLUSTERS = CL / "opinion-clusters-2025-12-02.csv"
OPINIONS = CL / "opinions-2025-12-02.csv"

HERE = Path(__file__).resolve().parent
CORPUS = HERE / "corpus"
FULLTEXT = HERE / "corpus_fulltext"
TARGETS = HERE / "results" / "cluster_targets.json"
MANIFEST = HERE / "manifest.csv"

CUTOFF = "2020-01-01"
POOL = 20000          # cluster ids sampled in pass 1
SKIP_WORDS = 200      # clears caption / docket / filing stamp
EXCERPT_WORDS = 400   # what actually gets scanned
MIN_WORDS = 900       # opinion must be long enough to survive skip + excerpt

# Postgres COPY dialect: backslash escapes, no doubled quotes. Without this the
# columns silently drift and HTML ends up in the boolean fields.
DIALECT = dict(escapechar="\\", doublequote=False)


def collapse(text):
    return re.sub(r"\s+", " ", text).strip()


def cmd_clusters(args):
    rng = random.Random(args.seed)
    reservoir, seen = [], 0

    with CLUSTERS.open(newline="", encoding="utf-8", errors="replace") as fh:
        for i, row in enumerate(csv.DictReader(fh, **DIALECT), 1):
            if i % 500_000 == 0:
                print(f"  {i:,} clusters read, {seen:,} eligible, "
                      f"{len(reservoir):,} held", flush=True)
            date_filed = (row.get("date_filed") or "").strip()
            if not date_filed or date_filed >= CUTOFF:
                continue
            if (row.get("precedential_status") or "").strip() != "Published":
                continue
            judges = (row.get("judges") or "").strip()
            if not judges:
                continue

            seen += 1
            rec = {
                "cluster_id": row["id"],
                "date_filed": date_filed,
                "case_name": collapse(row.get("case_name") or "")[:300],
                "judges": collapse(judges)[:200],
                "citation_count": row.get("citation_count") or "",
            }
            # Reservoir sampling: every eligible cluster gets an equal shot without
            # holding ten million of them in memory.
            if len(reservoir) < POOL:
                reservoir.append(rec)
            else:
                j = rng.randrange(seen)
                if j < POOL:
                    reservoir[j] = rec

    TARGETS.parent.mkdir(parents=True, exist_ok=True)
    TARGETS.write_text(json.dumps(
        {"cutoff": CUTOFF, "seed": args.seed, "eligible_total": seen,
         "pool": {r["cluster_id"]: r for r in reservoir}}, indent=1), encoding="utf-8")
    print(f"\n{seen:,} eligible clusters; sampled {len(reservoir):,} -> {TARGETS}")


def cmd_opinions(args):
    blob = json.loads(TARGETS.read_text(encoding="utf-8"))
    pool = blob["pool"]
    print(f"Looking for {len(pool):,} target clusters (pre-{blob['cutoff']}, Published)")

    CORPUS.mkdir(parents=True, exist_ok=True)
    FULLTEXT.mkdir(parents=True, exist_ok=True)
    rows, scanned = [], 0

    with OPINIONS.open(newline="", encoding="utf-8", errors="replace") as fh:
        for op in csv.DictReader(fh, **DIALECT):
            scanned += 1
            if scanned % 100_000 == 0:
                print(f"  {scanned:,} opinions read, {len(rows)} kept", flush=True)

            meta = pool.get((op.get("cluster_id") or "").strip())
            if not meta:
                continue
            # OCR text carries scanning artifacts that have nothing to do with
            # authorship; excluding it keeps the test about prose.
            if (op.get("extracted_by_ocr") or "").strip().lower() in ("t", "true", "1"):
                continue
            if (op.get("type") or "").strip() not in ("010combined", "020lead"):
                continue

            words = collapse(op.get("plain_text") or "").split()
            if len(words) < MIN_WORDS:
                continue

            excerpt = " ".join(words[SKIP_WORDS:SKIP_WORDS + EXCERPT_WORDS])
            if len(excerpt.split()) < EXCERPT_WORDS:
                continue

            doc_id = f"op-{meta['date_filed'][:4]}-{op['id']}"
            (CORPUS / f"{doc_id}.txt").write_text(excerpt, encoding="utf-8")
            (FULLTEXT / f"{doc_id}.txt").write_text(
                " ".join(words), encoding="utf-8")

            rows.append({
                "doc_id": doc_id, "path": "", "label": "human", "genre": "opinion",
                "pub_date": meta["date_filed"],
                "source_url": f"https://www.courtlistener.com/opinion/{meta['cluster_id']}/",
                "notes": (f"{meta['case_name']} | judges: {meta['judges']} | "
                          f"CourtListener bulk 2025-12-02 | Published | "
                          f"excerpt=words[{SKIP_WORDS}:{SKIP_WORDS + EXCERPT_WORDS}] "
                          f"of {len(words)} | opinion_id={op['id']}"),
            })
            print(f"  + {doc_id}  {meta['date_filed']}  {meta['case_name'][:60]}", flush=True)
            if len(rows) >= args.target:
                break

    write_manifest(rows, append=args.append)
    words_total = sum(EXCERPT_WORDS for _ in rows)
    print(f"\n{len(rows)} opinions from {scanned:,} rows read.")
    print(f"{words_total:,} words to scan, about ${words_total / 100 * 0.05:.2f} of credit.")


def write_manifest(rows, append=False):
    fields = ["doc_id", "path", "label", "genre", "pub_date", "source_url", "notes"]
    existing = []
    if append and MANIFEST.exists():
        with MANIFEST.open(newline="", encoding="utf-8") as fh:
            existing = [r for r in csv.DictReader(fh)]
    have = {r["doc_id"] for r in existing}
    with MANIFEST.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(existing)
        w.writerows(r for r in rows if r["doc_id"] not in have)
    print(f"Wrote {MANIFEST}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("clusters"); c.add_argument("--seed", type=int, default=20260821)
    c.set_defaults(func=cmd_clusters)
    o = sub.add_parser("opinions")
    o.add_argument("--target", type=int, default=80)
    o.add_argument("--append", action="store_true")
    o.set_defaults(func=cmd_opinions)
    a = p.parse_args(); a.func(a)
