"""Real-world corpus of public, unremediated government / college PDFs.

  remediate.py corpus fetch URL... [--dir benchmark/corpus]
  remediate.py corpus run [--dir ...] [--no-ai]   # remediate + veraPDF, aggregate failures by rule

Used to find what the tool still gets wrong on real-world government documents
remediates. Keep a held-out split (--dir benchmark/corpus_holdout) that you
don't look at while tuning.
"""

from __future__ import annotations

import collections
import hashlib
import io
import json
import re
import urllib.request
from pathlib import Path

import pikepdf

from . import validate
from .content import ContentIndex


def fetch(urls: list[str], dest: str, max_pages: int = 40, max_mb: float = 20, log=print) -> int:
    out = Path(dest)
    out.mkdir(parents=True, exist_ok=True)
    got = 0
    for url in urls:
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", url.split("/")[-1].split("?")[0])[:70] or "doc.pdf"
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        target = out / name
        if target.exists():
            continue
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (pdf-remediator corpus)"})
            data = urllib.request.urlopen(req, timeout=60).read()
            if len(data) > max_mb * 1e6 or not data.startswith(b"%PDF"):
                log(f"  skip (size/type) {url}")
                continue
            with pikepdf.open(io.BytesIO(data)) as pdf:
                pages = len(pdf.pages)
                tagged = "/StructTreeRoot" in pdf.Root
            if pages > max_pages:
                log(f"  skip ({pages} pages) {url}")
                continue
        except Exception as e:
            log(f"  skip ({type(e).__name__}) {url}")
            continue
        target.write_bytes(data)
        (out / (name + ".json")).write_text(json.dumps({
            "url": url, "pages": pages, "tagged": tagged,
            "sha1": hashlib.sha1(data).hexdigest()}, indent=1), encoding="utf-8")
        got += 1
        log(f"  [{got}] {pages:3}p {'tagged  ' if tagged else 'UNTAGGED'} {name}")
    return got


def classify(path: str) -> str:
    """untagged | scanned | tagged"""
    with pikepdf.open(path) as pdf:
        tagged = "/StructTreeRoot" in pdf.Root
    ci = ContentIndex.load(path)
    total = ci.tagged_text + sum(ci.untagged_text)
    if total < 50:
        return "scanned"  # images of text, needs OCR
    if not tagged or ci.untagged_share > 0.5:
        return "untagged"
    return "tagged"


def run(dest: str, config: dict, remediate_fn, use_ai: bool, log=print) -> dict:
    import copy
    config = copy.deepcopy(config)
    config["autotag"]["enabled"] = False  # never upload corpus files to Adobe; untagged ones are handled locally
    exe = validate.find_verapdf(config["validate"].get("verapdf_path"))
    if not exe:
        raise RuntimeError("veraPDF is required for corpus runs")
    rows = []
    for pdf in sorted(Path(dest).glob("*.pdf")):
        if pdf.stem.endswith(("_accessible", "_autotagged")):
            continue
        import time
        started = time.time()
        kind = classify(str(pdf))
        before = [f.rule for f in validate.run_verapdf(exe, str(pdf))]
        out = pdf.with_name(pdf.stem + "_accessible.pdf")
        try:
            rep = remediate_fn(str(pdf), config, out=str(out), use_ai=use_ai)
        except Exception as e:
            log(f"  CRASH {pdf.name}: {type(e).__name__}: {e}")
            rows.append({"doc": pdf.name, "kind": kind, "crash": f"{type(e).__name__}: {e}"})
            continue
        after = [p[1].split("]")[0].strip("[") for p in rep.remaining if p[0] == "PDF/UA"]
        rows.append({"doc": pdf.name, "kind": kind, "before": before, "after": after,
                     "pass": rep.verapdf_compliant is True})
        log(f"  {'PASS' if rows[-1]['pass'] else 'fail'}  {kind:8} {pdf.name[:50]:50} "
            f"rules {len(before):2} -> {len(after):2}  {time.time() - started:5.0f}s  {', '.join(after)[:70]}", flush=True)
    by_kind = collections.Counter(r["kind"] for r in rows)
    passed = collections.Counter(r["kind"] for r in rows if r.get("pass"))
    failing = collections.Counter(rule for r in rows if r.get("kind") == "tagged" for rule in r.get("after", []))
    summary = {"docs": len(rows), "by_kind": dict(by_kind), "passed_by_kind": dict(passed),
               "crashes": [r["doc"] for r in rows if "crash" in r],
               "top_failing_rules_on_tagged_docs": failing.most_common(15)}
    (Path(dest) / "corpus_results.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=1),
                                                     encoding="utf-8")
    return summary
