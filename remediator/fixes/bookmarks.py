"""Bookmarks from the heading structure (Acrobat checker: "Bookmarks").

Runs after the heading step, so bookmarks follow the corrected outline:
H1 at the top level, H2 nested under it, and so on. Each one jumps to the
heading's position on its page.
"""

from __future__ import annotations

from pikepdf import OutlineItem, PageLocation

from ..structure import HEADING_TYPES


def _has_outline(pdf) -> bool:
    outlines = pdf.Root.get("/Outlines")
    return outlines is not None and outlines.get("/First") is not None


def run(ctx) -> None:
    cfg = ctx.config["bookmarks"]
    pdf, tree, content, report = ctx.pdf, ctx.tree, ctx.content, ctx.report
    if not cfg["enabled"] or len(pdf.pages) < cfg["min_pages"]:
        return
    if _has_outline(pdf) and not cfg["replace_existing"]:
        return

    heads = []
    for n in tree.build():
        t = tree.std_type(n.elem)
        if t not in HEADING_TYPES or int(t[1]) > cfg["max_level"]:
            continue
        mc = content.describe(tree.all_mcids(n.elem))
        text = " ".join(mc.text.split())[:150]
        page = tree.page_of(n.elem)
        if text and page is not None:
            top = mc.bbox[3] + 4 if mc.bbox else None
            heads.append((int(t[1]), text, page, top))
    if not heads:
        report.review("bookmarks", "No headings to build bookmarks from")
        return

    with pdf.open_outline() as outline:
        outline.root.clear()
        stack: list[tuple[int, OutlineItem]] = []  # (level, item) chain of open parents
        for level, text, page, top in heads:
            item = OutlineItem(text, page, PageLocation.XYZ, left=0, top=top) if top \
                else OutlineItem(text, page)
            while stack and stack[-1][0] >= level:
                stack.pop()
            (stack[-1][1].children if stack else outline.root).append(item)
            stack.append((level, item))
    report.fixed("bookmarks", f"Created {len(heads)} bookmark(s) from the headings")
