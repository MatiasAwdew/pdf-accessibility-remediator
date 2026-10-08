"""Score the tool against a PDF you remediated by hand (the "gold" version).

  remediate.py eval original.pdf your_finished.pdf

Runs the tool on original.pdf, then compares its output tag-by-tag with your
finished file. Every run is appended to the learning folder so you can see
whether changes to the tool (or Claude's examples) are actually helping.

Elements are matched by their text, so this works even when the two files
have different tag trees.
"""

from __future__ import annotations

import difflib
import re
import tempfile
from pathlib import Path

import pikepdf

from . import learning
from .content import ContentIndex
from .structure import HEADING_TYPES, StructTree, pdf_str


NUMBERING = re.compile(r"^((\d+(\.\d+)*\.?)|([ivxlcdm]+\.)|([a-z][.)])|(section|chapter|appendix)\s+\S+)\s+")


def _norm(text: str) -> str:
    """Compare headings by wording: case, punctuation and "2.1" style numbering ignored."""
    t = NUMBERING.sub("", " ".join(text.lower().split()))
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def snapshot(path: str) -> dict:
    """What a remediator cares about, extracted from one PDF."""
    content = ContentIndex.load(path)
    with pikepdf.open(path) as pdf:
        tree = StructTree(pdf)
        snap = {
            "title": str(pdf.docinfo.get("/Title", "")).strip(),
            "lang": str(pdf.Root.get("/Lang", "")).strip(),
            "headings": [], "figures": [], "tables": [], "links": 0, "lists": 0,
        }
        for n in tree.build():
            t = tree.std_type(n.elem)
            if t in HEADING_TYPES:
                text = _norm(content.describe(tree.all_mcids(n.elem)).text)
                if text:
                    snap["headings"].append((text, int(t[1])))
            elif t == "Figure":
                alt = pdf_str(n.elem.get("/Alt")).strip()
                snap["figures"].append({"page": (tree.page_of(n.elem) or 0) + 1, "alt": alt})
            elif t == "Table":
                th = sum(1 for d in _descendants(tree, n.elem) if tree.std_type(d) == "TH")
                td = sum(1 for d in _descendants(tree, n.elem) if tree.std_type(d) == "TD")
                snap["tables"].append({"page": (tree.page_of(n.elem) or 0) + 1, "th": th, "td": td})
            elif t == "Link":
                snap["links"] += 1
            elif t == "L":
                snap["lists"] += 1
    return snap


def _descendants(tree, elem):
    from .structure import as_list, is_struct_elem
    for k in as_list(elem.get("/K")):
        if is_struct_elem(k):
            yield k
            yield from _descendants(tree, k)


def _align(tool_h: list, gold_h: list) -> list[tuple[int, int]]:
    """Pair up headings in reading order (so two "Methods" headings don't collide)."""
    sm = difflib.SequenceMatcher(a=[t for t, _ in tool_h], b=[t for t, _ in gold_h], autojunk=False)
    return [(blk.a + k, blk.b + k) for blk in sm.get_matching_blocks() for k in range(blk.size)]


def compare(tool: dict, gold: dict) -> dict:
    th, gh = list(tool["headings"]), list(gold["headings"])
    pairs = _align(th, gh)
    both = len(pairs)
    level_ok = sum(1 for i, j in pairs if th[i][1] == gh[j][1])
    wrong = [{"text": gh[j][0][:80], "tool": f"H{th[i][1]}", "gold": f"H{gh[j][1]}"}
             for i, j in pairs if th[i][1] != gh[j][1]]
    matched_t, matched_g = {i for i, _ in pairs}, {j for _, j in pairs}
    missed = [gh[j][0][:80] for j in range(len(gh)) if j not in matched_g]
    extra = [th[i][0][:80] for i in range(len(th)) if i not in matched_t]

    gold_pages = [f["page"] for f in gold["figures"]]
    tool_figs_with_alt = sum(1 for f in tool["figures"] if f["alt"])
    table_pairs = list(zip(tool["tables"], gold["tables"]))

    def pct(a, b):
        return round(100 * a / b, 1) if b else None

    return {
        "title_match": _norm(tool["title"]) == _norm(gold["title"]),
        "title": {"tool": tool["title"], "gold": gold["title"]},
        "lang_match": tool["lang"].lower().split("-")[0] == gold["lang"].lower().split("-")[0],
        "headings": {
            "gold_count": len(gh), "tool_count": len(th),
            "found_pct": pct(both, len(gh)),
            "level_accuracy_pct": pct(level_ok, both),
            "wrong_level": wrong[:30], "missed": missed[:30], "extra": extra[:30],
        },
        "figures": {
            "gold_count": len(gold_pages), "tool_count": len(tool["figures"]),
            "tool_alt_coverage_pct": pct(tool_figs_with_alt, len(tool["figures"])),
            # gold has fewer figures -> you artifacted decorative ones the tool left in
            "decorative_gap": len(tool["figures"]) - len(gold_pages),
        },
        "tables": {
            "gold_count": len(gold["tables"]), "tool_count": len(tool["tables"]),
            "header_cells_match_pct": pct(sum(1 for a, b in table_pairs if a["th"] == b["th"]), len(table_pairs)),
        },
        "links": {"gold": gold["links"], "tool": tool["links"]},
        "lists": {"gold": gold["lists"], "tool": tool["lists"]},
    }


def run_eval(original: str, gold: str, config: dict, use_ai: bool, remediate_fn,
             compare_only: bool = False) -> dict:
    if compare_only:
        tool_pdf = original
    else:
        tmp = Path(tempfile.mkdtemp(prefix="remediator-eval-"))
        tool_pdf = str(tmp / (Path(original).stem + "_accessible.pdf"))
        remediate_fn(original, config, out=tool_pdf, use_ai=use_ai)
    result = compare(snapshot(tool_pdf), snapshot(gold))
    result.update({"original": Path(original).name, "gold": Path(gold).name, "ai": use_ai})
    learning.record_eval(result)
    return result


def format_result(r: dict) -> str:
    h, f, t = r["headings"], r["figures"], r["tables"]
    lines = [
        f"Eval: {r['original']}  vs  {r['gold']}",
        f"  title      {'OK' if r['title_match'] else 'DIFF'}   tool={r['title']['tool']!r} gold={r['title']['gold']!r}",
        f"  language   {'OK' if r['lang_match'] else 'DIFF'}",
        f"  headings   found {h['found_pct']}% of yours, correct level on {h['level_accuracy_pct']}% "
        f"({h['tool_count']} tool / {h['gold_count']} gold)",
        f"  figures    {f['tool_count']} tool / {f['gold_count']} gold, alt coverage {f['tool_alt_coverage_pct']}%"
        + (f", {f['decorative_gap']} you artifacted that the tool kept" if f["decorative_gap"] > 0 else ""),
        f"  tables     {t['tool_count']} tool / {t['gold_count']} gold, header cells match {t['header_cells_match_pct']}%",
        f"  links      {r['links']['tool']} tool / {r['links']['gold']} gold",
        f"  lists      {r['lists']['tool']} tool / {r['lists']['gold']} gold",
    ]
    for w in h["wrong_level"][:10]:
        lines.append(f"    level: {w['text']!r} tool {w['tool']} vs your {w['gold']}")
    for m in h["missed"][:10]:
        lines.append(f"    missed heading: {m!r}")
    for e in h["extra"][:10]:
        lines.append(f"    extra heading: {e!r}")
    return "\n".join(lines)
