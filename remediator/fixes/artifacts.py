"""Turning content into Artifacts: decorative images, tagged or not.

Two cases, both of which need the page's content stream rewritten:

* A tagged <Figure> judged decorative: its marked content
  (/Figure <</MCID n>> BDC ... EMC) becomes /Artifact BMC ... EMC, the Figure
  element is removed, and its ParentTree slots are cleared.
* An image drawn outside the tag tree (AutoTag missed it): if it's small or the
  same image repeats in the same spot on several pages (logos, corner marks),
  it's wrapped in /Artifact BMC ... EMC. Larger one-off images are real content
  and are reported instead.
"""

from __future__ import annotations

from collections import Counter, defaultdict

import pikepdf
from pikepdf import Dictionary, Name

IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _mul(m, n):
    """m x n for PDF matrices [a b c d e f]."""
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (a * A + b * C, a * B + b * D, c * A + d * C, c * B + d * D, e * A + f * C + E, e * B + f * D + F)


def _apply(m, x, y):
    a, b, c, d, e, f = m
    return a * x + c * y + e, b * x + d * y + f


def _box(m, x0, y0, x1, y1):
    pts = [_apply(m, x, y) for x, y in ((x0, y0), (x1, y0), (x0, y1), (x1, y1))]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _xobject_box(xo, ctm):
    if not isinstance(xo, pikepdf.Stream):
        return None
    if xo.get("/Subtype") == Name.Image:
        return _box(ctm, 0, 0, 1, 1)
    if xo.get("/Subtype") == Name.Form and "/BBox" in xo:
        m = tuple(float(v) for v in xo.get("/Matrix", [1, 0, 0, 1, 0, 0]))
        x0, y0, x1, y1 = (float(v) for v in xo.BBox)
        return _box(_mul(m, ctm), x0, y0, x1, y1)
    return None


def has_own_marked_content(xo) -> bool:
    """A form XObject with its own marked content (tagged or artifact) must not be
    wrapped again at page level: that nests tags in artifacts or vice versa."""
    if not isinstance(xo, pikepdf.Stream) or xo.get("/Subtype") != Name.Form:
        return False
    try:
        return any(str(getattr(i, "operator", "")) in ("BDC", "BMC") for i in pikepdf.parse_content_stream(xo))
    except Exception:
        return True


def untagged_draws(pdf, page):
    """(instruction index, xobject, bbox) for every Do outside marked content."""
    try:
        ops = pikepdf.parse_content_stream(page)
    except Exception:
        return [], None
    xobjects = (page.obj.get("/Resources") or {}).get("/XObject") or Dictionary()
    out, depth, ctm, stack = [], 0, IDENTITY, []
    for i, ins in enumerate(ops):
        op = str(getattr(ins, "operator", ""))
        if op in ("BDC", "BMC"):
            depth += 1
        elif op == "EMC":
            depth = max(0, depth - 1)
        elif op == "q":
            stack.append(ctm)
        elif op == "Q":
            ctm = stack.pop() if stack else IDENTITY
        elif op == "cm":
            ctm = _mul(tuple(float(v) for v in ins.operands), ctm)
        elif op == "Do" and depth == 0:
            xo = xobjects.get(str(ins.operands[0]))
            box = _xobject_box(xo, ctm)
            if box and not has_own_marked_content(xo):
                out.append((i, xo, box))
    return out, ops


def decorative_untagged(pdf, small_fraction: float) -> dict[int, set[int]]:
    """page -> instruction indexes of untagged images that are decorative."""
    found = []
    for pno, page in enumerate(pdf.pages):
        mb = [float(v) for v in page.mediabox]
        page_area = abs((mb[2] - mb[0]) * (mb[3] - mb[1])) or 1
        draws, _ = untagged_draws(pdf, page)
        for i, xo, box in draws:
            area = (box[2] - box[0]) * (box[3] - box[1])
            key = (xo.objgen if xo.is_indirect else None, tuple(round(v / 5) for v in box))
            found.append((pno, i, area / page_area, key))
    repeats = Counter((k[0], k[1]) for _, _, _, k in found)
    same_image = Counter(k[0] for _, _, _, k in found if k[0] is not None)
    decorative = defaultdict(set)
    for pno, i, frac, key in found:
        repeated = repeats[key] >= 2 or (key[0] is not None and same_image[key[0]] >= 2 and frac < 0.05)
        if frac < small_fraction or repeated:
            decorative[pno].add(i)
    return decorative


def artifact_figure(ctx, elem) -> bool:
    """Change a tagged figure's marked content to an Artifact and drop the tag.
    Returns False (changing nothing) if any of its content can't be located."""
    tree = ctx.tree
    if tree.objrs(elem):
        return False
    by_page: dict[int, set[int]] = defaultdict(set)
    for page, mcid in tree.all_mcids(elem):
        if page is None:
            return False
        by_page[page].add(mcid)
    if not by_page:
        return False

    rewritten = {}
    for pno, mcids in by_page.items():
        ops = pikepdf.parse_content_stream(ctx.pdf.pages[pno])
        out, hit = [], set()
        for ins in ops:
            if (str(getattr(ins, "operator", "")) == "BDC" and len(ins.operands) == 2
                    and isinstance(ins.operands[1], Dictionary) and "/MCID" in ins.operands[1]
                    and int(ins.operands[1].MCID) in mcids):
                hit.add(int(ins.operands[1].MCID))
                out.append(pikepdf.ContentStreamInstruction([Name.Artifact], pikepdf.Operator("BMC")))
            else:
                out.append(ins)
        if hit != mcids:  # content lives in an XObject or a /Properties reference: leave it
            return False
        rewritten[pno] = out

    for pno, out in rewritten.items():
        page = ctx.pdf.pages[pno]
        page.obj.Contents = ctx.pdf.make_stream(pikepdf.unparse_content_stream(out))
        key = page.obj.get("/StructParents")
        if key is not None:
            arr = tree.parent_tree[int(key)]
            for mcid in by_page[pno]:
                if mcid < len(arr):
                    arr[mcid] = pikepdf.Object.parse(b"null")
    tree.remove_elem(elem)
    return True
