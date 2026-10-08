"""Public benchmark: PubMed Central Open Access articles.

Each PMC article comes with the publisher's PDF *and* JATS XML, which marks up
the true document structure (section nesting, figures with captions and
sometimes author-written alt text, tables with header cells). The XML is the
answer key, so we can score the tool on real documents without anyone
remediating them by hand.

  remediate.py benchmark fetch --n 20     # download CC BY / CC0 articles
  remediate.py benchmark run              # score before vs. after the tool

Source: NIH NLM NCBI PubMed Central (PMC) Article Datasets on AWS,
https://registry.opendata.aws/ncbi-pmc (only CC BY / CC BY-SA / CC0 are used).
"""

from __future__ import annotations

import io
import json
import re
import statistics
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import pikepdf

from . import learning
from .content import ContentIndex
from .evaluate import compare, snapshot

BUCKET = "https://pmc-oa-opendata.s3.amazonaws.com/"
OPEN_LICENSES = {"CC BY", "CC BY-SA", "CC0"}
SECTIONING = {"sec", "ack", "ref-list", "app", "app-group", "glossary", "fn-group", "notes"}


def _get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "pdf-remediator-benchmark"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _s3(url: str) -> str:
    return BUCKET + url.split("s3://pmc-oa-opendata/", 1)[1].split("?", 1)[0]


# ---------------------------------------------------------------- answer key

def _strip_ns(root: ET.Element) -> None:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]


def _text(el) -> str:
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def gold_from_jats(xml_bytes: bytes) -> dict:
    """What a correctly remediated version of this article should contain."""
    root = ET.fromstring(xml_bytes)
    _strip_ns(root)
    lang = root.get("{http://www.w3.org/XML/1998/namespace}lang") or root.get("xml:lang") or ""
    title = _text(root.find("front/article-meta/title-group/article-title"))

    headings: list[tuple[str, int]] = []
    if title:
        headings.append((title, 1))

    def walk(el, depth):
        for child in el:
            if child.tag in SECTIONING:
                t = child.find("title")
                label = child.find("label")
                if t is not None and _text(t):
                    text = (_text(label) + " " if label is not None else "") + _text(t)
                    headings.append((text, min(depth, 6)))
                    walk(child, depth + 1)
                else:
                    walk(child, depth)  # untitled wrapper: children stay at this depth

    # Structured-abstract labels ("Methods:", "Results:") are run-in text, not
    # headings, in a remediated PDF; only an explicit "Abstract" title counts.
    for abstract in root.findall("front/article-meta/abstract"):
        t = abstract.find("title")
        if t is not None and _text(t) and abstract.get("abstract-type") in (None, "", "structured"):
            headings.append((_text(t), 2))
    for part in ("body", "back"):
        el = root.find(part)
        if el is not None:
            walk(el, 2)

    figures = [{"page": None, "alt": _text(f.find("alt-text")), "caption": _text(f.find("caption"))[:500]}
               for f in root.iter("fig")]
    tables = [{"page": None, "th": sum(1 for _ in tw.iter("th")),
               "td": sum(1 for _ in tw.iter("td"))} for tw in root.iter("table-wrap")]
    return {"title": title, "lang": lang, "headings": headings, "figures": figures, "tables": tables,
            "links": None, "lists": sum(1 for _ in root.iter("list"))}


def gold_snapshot(gold: dict) -> dict:
    from .evaluate import _norm
    snap = dict(gold)
    snap["headings"] = [(_norm(text), lvl) for text, lvl in gold["headings"]]
    return snap


# ---------------------------------------------------------------- fetch

def fetch(dest: str, n: int = 20, start: str = "PMC12", tagged_only: bool = True,
          max_scan: int = 400, max_mb: float = 15, log=print) -> int:
    out = Path(dest)
    out.mkdir(parents=True, exist_ok=True)
    have = {p.name for p in out.iterdir() if (p / "gold.json").exists()}
    got, scanned, token = len(have), 0, None
    while got < n and scanned < max_scan:
        url = f"{BUCKET}?list-type=2&prefix=metadata/{start}&max-keys=200"
        if token:
            url += "&continuation-token=" + urllib.request.quote(token)
        listing = _get(url).decode()
        keys = re.findall(r"<Key>(.*?)</Key>", listing)
        m = re.search(r"<NextContinuationToken>(.*?)</NextContinuationToken>", listing)
        token = m.group(1) if m else None
        for key in keys:
            if got >= n or scanned >= max_scan:
                break
            try:
                meta = json.loads(_get(BUCKET + key))
            except Exception:
                continue
            pmcid = meta.get("pmcid")
            if (pmcid in have or not meta.get("pdf_url") or meta.get("license_code") not in OPEN_LICENSES
                    or meta.get("is_retracted") or meta.get("is_historical_ocr")):
                continue
            scanned += 1
            try:
                pdf_bytes = _get(_s3(meta["pdf_url"]), timeout=120)
                if len(pdf_bytes) > max_mb * 1e6:
                    continue
                with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
                    tagged = "/StructTreeRoot" in pdf.Root
                if tagged_only and not tagged:
                    continue
                xml = _get(_s3(meta["xml_url"]))
                gold = gold_from_jats(xml)
            except Exception as e:
                log(f"  skip {pmcid}: {e}")
                continue
            if len(gold["headings"]) < 3:
                continue
            d = out / pmcid
            d.mkdir(exist_ok=True)
            (d / "original.pdf").write_bytes(pdf_bytes)
            (d / "article.xml").write_bytes(xml)
            (d / "gold.json").write_text(json.dumps(gold, indent=1, ensure_ascii=False), encoding="utf-8")
            (d / "source.json").write_text(json.dumps({
                "pmcid": pmcid, "license": meta["license_code"], "citation": meta.get("citation"),
                "doi": meta.get("doi"), "tagged": tagged,
                "attribution": "NIH NLM NCBI PubMed Central (PMC) Article Datasets on AWS"}, indent=1),
                encoding="utf-8")
            got += 1
            have.add(pmcid)
            log(f"  [{got}/{n}] {pmcid} {meta['license_code']} {'tagged' if tagged else 'untagged'}  "
                f"{(meta.get('citation') or '')[:50]}")
        if not token:
            break
    return got


# ---------------------------------------------------------------- run

def run(dest: str, config: dict, remediate_fn, use_ai: bool, log=print) -> dict:
    rows, skipped = [], []
    for d in sorted(p for p in Path(dest).iterdir() if (p / "gold.json").exists()):
        gold = gold_snapshot(gold_from_jats((d / "article.xml").read_bytes()))  # rebuilt: key logic may improve
        orig = str(d / "original.pdf")
        try:
            before = compare(snapshot(orig), gold)
            out = str(d / "tool_output.pdf")
            remediate_fn(orig, config, out=out, use_ai=use_ai)
            after = compare(snapshot(out), gold)
            untagged = ContentIndex.load(out).untagged_share
        except Exception as e:
            log(f"  {d.name}: failed: {type(e).__name__}: {e}")
            continue
        row = {"doc": d.name, "before": before, "after": after, "untagged_share": round(untagged, 2)}
        if untagged > config["autotag"]["untagged_text_threshold"]:
            # still mostly untagged after the tool = AutoTag didn't run (no Adobe keys);
            # scoring its headings would only measure that.
            row["needs_autotag"] = True
            (d / "result.json").write_text(json.dumps(row, indent=1, ensure_ascii=False), encoding="utf-8")
            log(f"  {d.name:14} needs AutoTag ({untagged:.0%} of text untagged) - not scored")
            skipped.append(d.name)
            continue
        (d / "result.json").write_text(json.dumps(row, indent=1, ensure_ascii=False), encoding="utf-8")
        rows.append(row)
        log(f"  {d.name:14} headings found {_f(before['headings']['found_pct'])} -> "
            f"{_f(after['headings']['found_pct'])}   level ok {_f(before['headings']['level_accuracy_pct'])} -> "
            f"{_f(after['headings']['level_accuracy_pct'])}   title {'OK' if after['title_match'] else 'DIFF'}   "
            f"fig alt {_f(after['figures']['tool_alt_coverage_pct'])}")

    def avg(stage, *path):
        vals = []
        for r in rows:
            v = r[stage]
            for p in path:
                v = v[p]
            if v is not None:
                vals.append(float(v))
        return round(statistics.mean(vals), 1) if vals else None

    summary = {
        "docs": len(rows), "ai": use_ai, "needs_autotag": skipped,
        "headings_found_pct": {"before": avg("before", "headings", "found_pct"),
                               "after": avg("after", "headings", "found_pct")},
        "heading_level_accuracy_pct": {"before": avg("before", "headings", "level_accuracy_pct"),
                                       "after": avg("after", "headings", "level_accuracy_pct")},
        "title_match_pct": {"before": _pct(rows, "before"), "after": _pct(rows, "after")},
        "figure_alt_coverage_pct": {"before": avg("before", "figures", "tool_alt_coverage_pct"),
                                    "after": avg("after", "figures", "tool_alt_coverage_pct")},
        "table_header_match_pct": {"before": avg("before", "tables", "header_cells_match_pct"),
                                   "after": avg("after", "tables", "header_cells_match_pct")},
    }
    (Path(dest) / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    learning.record_eval({"original": f"benchmark:{Path(dest).name} ({len(rows)} docs)", "gold": "PMC JATS",
                          "ai": use_ai,
                          "headings": {"level_accuracy_pct": summary["heading_level_accuracy_pct"]["after"]},
                          "figures": {"tool_alt_coverage_pct": summary["figure_alt_coverage_pct"]["after"]},
                          "summary": summary})
    return summary


def _f(v) -> str:
    return "  -  " if v is None else f"{v:5.1f}%"


def _pct(rows, stage):
    return round(100 * sum(1 for r in rows if r[stage]["title_match"]) / len(rows), 1) if rows else None


def format_summary(s: dict) -> str:
    lines = [f"Benchmark: {s['docs']} documents scored (AI {'on' if s['ai'] else 'off'})"
             + (f", {len(s['needs_autotag'])} skipped: need AutoTag" if s.get("needs_autotag") else ""),
             f"  {'metric':32} {'original':>9} {'tool':>9}"]
    for k, v in s.items():
        if isinstance(v, dict):
            lines.append(f"  {k:32} {_f(v['before']):>9} {_f(v['after']):>9}")
    return "\n".join(lines)
