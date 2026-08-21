#!/usr/bin/env python3
"""
rewrite_arms.py - ask several models to make the post "not sound like AI wrote it,"
using one identical, deliberately naive prompt.

The point is not sophisticated evasion. The point is the thing a student would actually
type into a chat box in ten seconds. Pangram's technical report claims a 97.67% detection
rate on humanized AI text, so this tests a published claim with the most obvious possible
method, and the prompt is disclosed in full because it is one sentence anyone would guess.

  python rewrite_arms.py            # generate every arm
"""
import json, os, re, urllib.request
from pathlib import Path

PROMPT = "Please write this so it doesn't sound like AI wrote it."

# Tier-matched across two vendors: flagship, mid, and cheap on both sides, so the
# comparison is about capability tier rather than about which company you happened to
# pick. OpenAI's 5.6 generation names its tiers instead of numbering them: Sol is the
# flagship, Terra the balanced workhorse, Luna the fast and cheap one.
ARMS = [
    ("claude-opus-5",             "anthropic", "flagship"),
    ("gpt-5.6-sol",               "openai",    "flagship"),
    ("claude-sonnet-5",           "anthropic", "mid"),
    ("gpt-5.6-terra",             "openai",    "mid"),
    ("claude-haiku-4-5-20251001", "anthropic", "cheap"),
    ("gpt-5.6-luna",              "openai",    "cheap"),
    # Fable 5 is its own line in the Claude 5 family rather than a rung on the
    # flagship/mid/cheap ladder, so it gets its own arm instead of being forced
    # into the grid.
    ("claude-fable-5",            "anthropic", "fable"),
]

HERE = Path(__file__).resolve().parent
ARMDIR = HERE / "arms"; ARMDIR.mkdir(exist_ok=True)


def essay_body():
    """The argument only. Disclosure and metadata are not part of what is being tested."""
    s = (HERE / "POST-DRAFT.md").read_text(encoding="utf-8")
    return s[:s.index("---\n\n## Disclosure")].strip()


def post(url, payload, headers):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", **headers})
    return json.load(urllib.request.urlopen(req, timeout=600))


def anthropic(model, text):
    r = post("https://api.anthropic.com/v1/messages",
             {"model": model, "max_tokens": 8000,
              "messages": [{"role": "user", "content": f"{PROMPT}\n\n{text}"}]},
             {"x-api-key": os.environ["ANTHROPIC_API_KEY"],
              "anthropic-version": "2023-06-01"})
    return "".join(b.get("text", "") for b in r["content"])


def openai(model, text):
    body = {"model": model, "messages": [{"role": "user", "content": f"{PROMPT}\n\n{text}"}]}
    # GPT-5 rejects max_tokens and pins temperature; older models take the classic params.
    body["max_completion_tokens" if model.startswith("gpt-5") else "max_tokens"] = 8000
    r = post("https://api.openai.com/v1/chat/completions", body,
             {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"})
    return r["choices"][0]["message"]["content"]


if __name__ == "__main__":
    text = essay_body()
    print(f"source: {len(text.split()):,} words\nprompt: {PROMPT!r}\n")
    for model, vendor, tier in ARMS:
        out = ARMDIR / f"{model}.md"
        if out.exists():
            print(f"  [{tier:8}] {model}: already generated, skipping", flush=True)
            continue
        try:
            result = (anthropic if vendor == "anthropic" else openai)(model, text)
            out.write_text(result.strip(), encoding="utf-8")
            print(f"  [{tier:8}] {model}: {len(result.split()):,} words -> {out.name}", flush=True)
        except Exception as e:
            print(f"  [{tier:8}] {model}: FAILED {type(e).__name__}: {str(e)[:300]}", flush=True)
