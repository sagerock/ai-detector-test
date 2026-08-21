#!/usr/bin/env python3
"""
fetch_wikipedia.py - pull pre-2020 revisions of biography articles as a test corpus.

The hypothesis under test: what trips the detector is not polished prose but
*condensed neutral summary with evaluative shorthand*. Encyclopedia biography is that
register in its purest form, and MediaWiki revision history makes the date provable,
so the human label rests on a timestamp rather than on anyone's word.

Cutoff is 2018-01-01, comfortably before any plausible LLM involvement in Wikipedia
editing. The exact revision id and timestamp are recorded for every article so a reader
can pull the identical text.
"""
import csv, json, re, urllib.parse, urllib.request
from pathlib import Path

API = "https://en.wikipedia.org/w/api.php"
CUTOFF = "2018-01-01T00:00:00Z"
WORDS = 400
HERE = Path(__file__).resolve().parent
OUT = HERE / "corpus"

# Military and public-service biographies, matching the register of the paragraph that
# flagged in the author's own blog post. Audie Murphy is the closest analogue there is:
# a decorated-veteran biography written in exactly that credentialed-summary voice.
ARTICLES = [
    "Audie Murphy", "Colin Powell", "John McCain", "Ruth Bader Ginsburg",
    "Norman Schwarzkopf Jr.", "Thurgood Marshall", "Chesley Sullenberger",
    "Tammy Duckworth", "Sandra Day O'Connor", "Oliver North",
]


def clean(wt):
    """Wikitext to plain prose. Templates, refs, and markup are not writing."""
    for _ in range(8):                                   # nested templates
        wt = re.sub(r"\{\{[^{}]*\}\}", "", wt)
    wt = re.sub(r"<ref[^>]*>.*?</ref>", "", wt, flags=re.S)
    wt = re.sub(r"<ref[^>]*/>", "", wt)
    wt = re.sub(r"<!--.*?-->", "", wt, flags=re.S)
    wt = re.sub(r"\[\[(?:File|Image|Category):[^\]]*\]\]", "", wt)
    wt = re.sub(r"\[\[[^|\]]*\|([^\]]*)\]\]", r"\1", wt)  # piped links
    wt = re.sub(r"\[\[([^\]]*)\]\]", r"\1", wt)
    wt = re.sub(r"^\s*[=]{2,}.*?[=]{2,}\s*$", "", wt, flags=re.M)   # headings
    wt = re.sub(r"'''?", "", wt)
    wt = re.sub(r"^[\*#:;].*$", "", wt, flags=re.M)      # lists, indents
    wt = re.sub(r"<[^>]+>", "", wt)
    return re.sub(r"\s+", " ", wt).strip()


def revision(title):
    q = urllib.parse.urlencode({
        "action": "query", "prop": "revisions", "titles": title,
        "rvlimit": 1, "rvstart": CUTOFF, "rvdir": "older",
        "rvprop": "timestamp|ids|content", "rvslots": "main", "format": "json",
        "formatversion": 2})
    req = urllib.request.Request(f"{API}?{q}",
        headers={"User-Agent": "pangram-register-test/1.0 (research; contact via github)"})
    page = json.load(urllib.request.urlopen(req, timeout=60))["query"]["pages"][0]
    rev = page["revisions"][0]
    return rev["revid"], rev["timestamp"], rev["slots"]["main"]["content"]


if __name__ == "__main__":
    rows = []
    for title in ARTICLES:
        try:
            revid, ts, wikitext = revision(title)
            words = clean(wikitext).split()
            if len(words) < WORDS + 50:
                print(f"  {title}: only {len(words)} words, skipped"); continue
            excerpt = " ".join(words[:WORDS])   # the lead: the summary register itself
            slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
            doc_id = f"wiki-{ts[:4]}-{slug}"
            (OUT / f"{doc_id}.txt").write_text(excerpt, encoding="utf-8")
            rows.append({"doc_id": doc_id, "path": "", "label": "human", "genre": "wikipedia",
                "pub_date": ts[:10], "source_url":
                f"https://en.wikipedia.org/w/index.php?oldid={revid}",
                "notes": f"{title}, revision {revid} of {ts}, the last revision before "
                         f"{CUTOFF[:10]}. Lead section, first {WORDS} words after stripping "
                         f"templates, refs and markup. Ground truth is the revision timestamp."})
            print(f"  {doc_id}  rev {revid}  {ts[:10]}", flush=True)
        except Exception as e:
            print(f"  {title}: FAILED {type(e).__name__} {str(e)[:120]}", flush=True)

    FIELDS = ["doc_id","path","label","genre","pub_date","source_url","notes"]
    existing = list(csv.DictReader(open(HERE/"manifest.csv", newline="", encoding="utf-8")))
    have = {r["doc_id"] for r in existing}
    existing += [r for r in rows if r["doc_id"] not in have]
    with open(HERE/"manifest.csv","w",newline="",encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS); w.writeheader(); w.writerows(existing)
    print(f"\n{len(rows)} articles, {len(rows)*WORDS:,} words, ~${len(rows)*WORDS/100*0.05:.2f}")
