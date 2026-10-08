"""Figures: missing alt text is the #1 checker failure after AutoTag."""

from __future__ import annotations

import pymupdf
from pikepdf import String

from ..ai import AIUnavailable
from ..structure import as_list, pdf_str


def _bbox(ctx, node):
    """Figure box in PDF coordinates: Layout /BBox attribute, else the drawn content."""
    bb = ctx.tree.get_attr(node.elem, "/Layout", "/BBox")
    if bb is not None and len(bb) == 4:
        x0, y0, x1, y1 = (float(v) for v in bb)
        return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
    return ctx.content.describe(ctx.tree.all_mcids(node.elem)).bbox


def _nearby_text(ctx, node) -> str:
    if node.parent is None:
        return ""
    sibs = node.parent.children
    i = next((k for k, s in enumerate(sibs) if s is node), None)
    if i is None:
        return ""
    parts = []
    for s in sibs[max(0, i - 1): i] + sibs[i + 1: i + 2]:
        parts.append(ctx.content.describe(ctx.tree.all_mcids(s.elem)).text)
    return "\n".join(p for p in parts if p)


def _render(doc, page_idx: int, bbox) -> tuple[bytes, bytes]:
    page = doc[page_idx]
    full = page.get_pixmap(dpi=72).tobytes("png")
    rect = pymupdf.Rect(*bbox) * page.transformation_matrix
    rect = (rect + (-6, -6, 6, 6)) & page.rect
    crop = page.get_pixmap(dpi=150, clip=rect).tobytes("png")
    return full, crop


def _iou(a, b) -> float:
    x0, y0, x1, y1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x1 - x0) * max(0, y1 - y0)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def carry_over_alt(ctx, figures) -> None:
    """AutoTag throws away the existing tags - including alt text someone already
    wrote. Copy it back onto the new Figure in the same place on the same page."""
    import pikepdf
    from types import SimpleNamespace

    from ..content import ContentIndex
    from ..structure import StructTree
    try:
        with pikepdf.open(ctx.original_path) as old:
            otree = StructTree(old)
            if not otree.tagged:
                return
            octx = SimpleNamespace(tree=otree, content=ContentIndex.load(ctx.original_path))
            old_figs = []
            for n in otree.build():
                if otree.std_type(n.elem) not in ("Figure", "Formula"):
                    continue
                alt = pdf_str(n.elem.get("/Alt")).strip()
                box = _bbox(octx, n)
                if alt and box:
                    old_figs.append((otree.page_of(n.elem), box, alt))
    except Exception as e:
        ctx.report.info("figures", f"Could not read alt text from the original: {e}")
        return
    copied = 0
    for n in figures:
        if pdf_str(n.elem.get("/Alt")).strip():
            continue
        page, box = ctx.tree.page_of(n.elem), _bbox(ctx, n)
        if box is None:
            continue
        best = max(((p, b, a) for p, b, a in old_figs if p == page),
                   key=lambda f: _iou(f[1], box), default=None)
        if best and _iou(best[1], box) >= 0.5:
            n.elem.Alt = String(best[2])
            copied += 1
    if copied:
        ctx.report.fixed("figures", f"Kept existing alt text on {copied} figure(s) that AutoTag had dropped")


def run(ctx) -> None:
    tree, report, cfg = ctx.tree, ctx.report, ctx.config["figures"]
    figures = [n for n in tree.build() if tree.std_type(n.elem) in ("Figure", "Formula")]
    if not figures:
        return
    if getattr(ctx, "original_path", None):
        carry_over_alt(ctx, figures)

    from collections import defaultdict

    from .artifacts import artifact_figure

    # the same figure position on several pages = a repeated logo / mark
    info, pages_at = [], defaultdict(set)
    for n in figures:
        page, bbox = tree.page_of(n.elem), _bbox(ctx, n)
        key = tuple(round(v / 5) for v in bbox) if bbox else None
        info.append((n, page, bbox, key))
        if key:
            pages_at[key].add(page)

    need = []
    for n, page, bbox, key in info:
        has_alt = pdf_str(n.elem.get("/Alt")).strip() or pdf_str(n.elem.get("/ActualText")).strip()
        if not as_list(n.elem.get("/K")):
            report.error("figures", "Empty Figure tag (no content) - delete it", page=page, elem=n.elem)
            continue
        mc = ctx.content.describe(tree.all_mcids(n.elem))
        text = mc.text.strip()
        # AutoTag sometimes tags a text block or small table as a Figure; its alt
        # text then hides the real text from screen readers
        if cfg["retag_text_figures"] and len(text) >= 20 and not mc.has_image:
            if not mc.has_paths:
                tree.set_type(n.elem, "P")
                for k in ("/Alt", "/ActualText"):
                    if k in n.elem:
                        del n.elem[k]
                report.review("figures", f'Tagged as a Figure but it is text ("{text[:50]}...") - changed to P',
                              page=page, elem=n.elem)
            else:
                report.error("figures", f'Tagged as a Figure but contains {len(text)} characters of text '
                             f'("{text[:50]}...") - probably a table or text block; retag it in Acrobat',
                             page=page, elem=n.elem)
                # Until it's retagged it's still a Figure. ActualText = its real text
                # lets screen readers read the words and numbers, not a description.
                if not (pdf_str(n.elem.get("/Alt")).strip() or pdf_str(n.elem.get("/ActualText")).strip()):
                    n.elem.ActualText = String(" ".join(text.split()))
                    report.review("figures", "Gave the text-filled Figure its own text as ActualText "
                                  "so screen readers read it", page=page, elem=n.elem)
            continue
        area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) if bbox else None
        tiny = area is not None and area < cfg["decorative_max_area"]
        repeated = key is not None and len(pages_at[key]) >= 2
        if (tiny or repeated) and cfg["artifact_decorative"] and artifact_figure(ctx, n.elem):
            why = "repeats on several pages" if repeated else "is tiny"
            report.review("figures", f"Decorative figure ({why}) changed to an Artifact", page=page)
            continue
        if has_alt:
            continue
        if tiny:
            report.review("figures", "Tiny figure without alt text - probably decorative, "
                          "consider changing it to an Artifact", page=page, elem=n.elem)
            continue
        need.append((n, page, bbox))

    gen = ctx.ai if cfg["ai_alt_text"] else None
    doc = pymupdf.open(ctx.source_path) if (gen and need) else None
    limit = ctx.config["ai"]["max_figures"]

    def missing(n, page, kind, why, context):
        report.error("figures", f"{kind} is missing alt text{why}", page=page, elem=n.elem,
                     proposal={"field": "alt", "old": "", "value": "", "ai_value": "", "applied": False,
                               "context": context})

    for i, (n, page, bbox) in enumerate(need):
        kind = tree.std_type(n.elem)
        nearby = _nearby_text(ctx, n)
        context = {"page": None if page is None else page + 1, "nearby_text": nearby[:300],
                   "bbox": [round(v) for v in bbox] if bbox else None}
        if gen is None or page is None or bbox is None or i >= limit:
            missing(n, page, kind, "" if gen else " (AI alt text off)", context)
            continue
        try:
            full, crop = _render(doc, page, bbox)
            result = gen.describe(full, crop, nearby)
        except AIUnavailable as e:
            gen = ctx.ai = None
            report.error("figures", str(e))
            missing(n, page, kind, "", context)
            continue
        except Exception as e:
            missing(n, page, kind, f" (AI failed: {e})", context)
            continue
        if result is None:
            missing(n, page, kind, " (AI declined)", context)
            continue
        context["figure_kind"] = result.figure_kind
        if result.decorative and cfg["artifact_decorative"] and artifact_figure(ctx, n.elem):
            report.review("figures", f"Claude judged this {result.figure_kind} decorative - changed to an Artifact",
                          page=page, proposal=None)
            continue
        if result.decorative:
            # to disagree: type alt text into "value" and set decision "accept"
            report.review("figures", "AI thinks this figure is decorative - change it to an Artifact "
                          "(Acrobat: right-click tag > Change Tag to Artifact)", page=page, elem=n.elem,
                          proposal={"field": "alt", "old": "", "value": "", "ai_value": "[decorative]",
                                    "applied": False, "context": context})
            continue
        n.elem.Alt = String(result.alt_text)
        report.review("figures", f"AI alt text ({result.confidence} confidence): {result.alt_text}",
                      page=page, elem=n.elem,
                      proposal={"field": "alt", "old": "", "value": result.alt_text, "applied": True,
                                "confidence": result.confidence, "context": context})
    if doc:
        doc.close()
