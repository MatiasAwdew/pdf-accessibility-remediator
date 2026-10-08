""""Learn from my corrections": every decision you make in a *_review.json is
logged with its context, then fed back to Claude as examples next time.

Stored locally (per user, outside the project):
  %APPDATA%/pdf-remediator/learning/corrections.jsonl   one line per decision
  %APPDATA%/pdf-remediator/learning/evals.jsonl         `remediate.py eval` history

Only short text snippets and layout numbers are stored, never the PDF.
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter, defaultdict
from pathlib import Path


def data_dir() -> Path:
    base = os.environ.get("APPDATA") or os.path.join(Path.home(), ".config")
    d = Path(base) / "pdf-remediator" / "learning"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _append(name: str, rows: list[dict]) -> None:
    with open(data_dir() / name, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _read(name: str) -> list[dict]:
    p = data_dir() / name
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def outcome(item: dict) -> str:
    """accepted: kept the tool's answer; edited: typed a different one; rejected: said no."""
    ai = item.get("ai_value", item.get("value"))
    if item.get("decision", "accept") != "accept":
        return "rejected"
    return "accepted" if item.get("value") == ai else "edited"


def record_review(review: dict, doc_name: str) -> int:
    """Log every decision in a review file. "skip" (the default for suggestions you
    didn't look at) carries no signal and is not recorded."""
    rows = []
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    for item in review.get("items", []):
        if item.get("decision") == "skip":
            continue
        out = outcome(item)
        rows.append({
            "ts": now, "doc": doc_name, "category": item.get("category"), "field": item.get("field"),
            "ai_value": item.get("ai_value", item.get("value")),
            "final_value": item.get("value") if out != "rejected" else item.get("old"),
            "outcome": out, "context": item.get("context", {}),
        })
    _append("corrections.jsonl", rows)
    return len(rows)


def examples(field: str, limit: int = 8) -> list[dict]:
    """Most useful past decisions for a field: corrections first, then confirmations."""
    rows = [r for r in _read("corrections.jsonl") if r.get("field") == field]
    rows.reverse()  # newest first
    fixes = [r for r in rows if r["outcome"] in ("edited", "rejected")]
    oks = [r for r in rows if r["outcome"] == "accepted"]
    picked = fixes[: max(1, limit * 3 // 4)] + oks
    return picked[:limit]  # callers may ask for more and filter


def stats() -> dict:
    by = defaultdict(Counter)
    for r in _read("corrections.jsonl"):
        by[f"{r.get('category')}/{r.get('field')}"][r["outcome"]] += 1
    return {k: dict(v) for k, v in sorted(by.items())}


def record_eval(result: dict) -> None:
    _append("evals.jsonl", [{"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **result}])


def eval_history() -> list[dict]:
    return _read("evals.jsonl")
