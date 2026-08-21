#!/usr/bin/env python3
"""
prep_arms.py - normalize the baseline post and every rewrite arm into comparable
plain text, then add them to the scan manifest.

Normalization is applied identically to all arms. Markdown markup is stripped because
it is formatting, not prose, and because Pangram's own documentation excludes tables
from supported input. Dropping table rows uniformly keeps the comparison about writing.
"""
import csv, re
from pathlib import Path

HERE = Path(__file__).resolve().parent
ARMS, OUT = HERE / "arms", HERE / "arms_txt"
OUT.mkdir(exist_ok=True)


def to_prose(md):
    out = []
    for line in md.split("\n"):
        if line.strip().startswith("|"):        # table rows: markup, not prose
            continue
        if re.fullmatch(r"\s*[-=*_]{3,}\s*", line):   # horizontal rules
            continue
        line = re.sub(r"^#{1,6}\s*", "", line)       # heading markers
        line = re.sub(r"^\s*[-*+]\s+", "", line)     # bullets
        line = re.sub(r"\*\*(.+?)\*\*", r"\1", line) # bold
        line = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", line)  # italic
        line = re.sub(r"`(.+?)`", r"\1", line)       # inline code
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


# Baseline: the essay body only, same slice the arms were generated from.
src = (HERE / "POST-DRAFT.md").read_text(encoding="utf-8")
baseline = src[:src.index("---\n\n## Disclosure")].strip()
(OUT / "post-00-baseline.txt").write_text(to_prose(baseline), encoding="utf-8")

rows = [("post-00-baseline", "The post as written by Claude Opus 5 in the author's "
         "documented voice. Not optimized against any detector. Essay body only; "
         "the disclosure section is metadata and was excluded from every arm.")]

for f in sorted(ARMS.glob("*.md")):
    name = f"post-{f.stem}"
    (OUT / f"{name}.txt").write_text(to_prose(f.read_text(encoding="utf-8")), encoding="utf-8")
    rows.append((name, f"Baseline rewritten by {f.stem}. Prompt, verbatim and identical "
                       f"for every arm: \"Please write this so it doesn't sound like AI "
                       f"wrote it.\" First response, no retries, no selection."))

FIELDS = ["doc_id", "path", "label", "genre", "pub_date", "source_url", "notes"]
existing = list(csv.DictReader(open(HERE / "manifest.csv", newline="", encoding="utf-8")))
have = {r["doc_id"] for r in existing}
for doc_id, note in rows:
    if doc_id not in have:
        existing.append({"doc_id": doc_id, "path": f"arms_txt/{doc_id}.txt", "label": "ai",
                         "genre": "post_arm", "pub_date": "2026", "source_url": "",
                         "notes": note})

with open(HERE / "manifest.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=FIELDS); w.writeheader(); w.writerows(existing)

total = sum(len((OUT / f"{d}.txt").read_text(encoding='utf-8').split()) for d, _ in rows)
print(f"{len(rows)} documents prepared")
for d, _ in rows:
    print(f"  {d:<38} {len((OUT/f'{d}.txt').read_text(encoding='utf-8').split()):>5,} words")
print(f"\n{total:,} words to scan  ~${total/100*0.05:.2f}")
