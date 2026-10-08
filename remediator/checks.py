"""Post-fix validation: an approximation of the Acrobat checker + PAC rules we
can evaluate ourselves. PAC stays the final authority; this tells you what's
left before you open it."""

from __future__ import annotations

from pikepdf import Dictionary, Name

from .structure import HEADING_TYPES, pdf_str


def run(ctx) -> list[tuple[str, str, int | None]]:
    """Return (category, message, page) for every remaining failure."""
    pdf, tree = ctx.pdf, ctx.tree
    out = []

    def fail(cat, msg, page=None):
        out.append((cat, msg, page))

    if not str(pdf.docinfo.get("/Title", "")).strip():
        fail("metadata", "No document title")
    vp = pdf.Root.get("/ViewerPreferences")
    if not (isinstance(vp, Dictionary) and vp.get("/DisplayDocTitle") is True):
        fail("metadata", "Document title is not shown in the title bar")
    if not str(pdf.Root.get("/Lang", "")).strip():
        fail("metadata", "No document language")
    mi = pdf.Root.get("/MarkInfo")
    if not (isinstance(mi, Dictionary) and mi.get("/Marked") is True):
        fail("metadata", "Not marked as tagged")
    if not tree.tagged:
        fail("structure", "No tag tree")
        return out

    nodes = tree.build()
    prev = 0
    for n in nodes:
        t = tree.std_type(n.elem)
        page = tree.page_of(n.elem)
        if t in HEADING_TYPES:
            lvl = int(t[1])
            if prev == 0 and lvl != 1:
                fail("headings", f"First heading is H{lvl}, not H1", page)
            elif lvl > prev + 1 and prev:
                fail("headings", f"Heading level skipped (H{prev} -> H{lvl})", page)
            prev = lvl
        elif t in ("Figure", "Formula"):
            if not (pdf_str(n.elem.get("/Alt")).strip() or pdf_str(n.elem.get("/ActualText")).strip()):
                fail("figures", f"{t} without alternate text", page)
        elif t == "Table":
            if tree.get_attr(n.elem, "/Table", "/Summary") is None:
                fail("tables", "Table without a summary (Acrobat checker: Tables - Summary)", page)
        elif t == "TH":
            if tree.get_attr(n.elem, "/Table", "/Scope") is None:
                fail("tables", "Header cell without Scope", page)

    for pno, page in enumerate(pdf.pages):
        annots = page.obj.get("/Annots") or []
        link_count = 0
        for a in annots:
            if not isinstance(a, Dictionary):
                continue
            sub = a.get("/Subtype")
            if sub in (Name.Popup,) or (a.get("/F", 0) and int(a.F) & 2):  # popups, hidden
                continue
            if sub == Name.Link:
                link_count += 1
                if not str(a.get("/Contents", "")).strip():
                    fail("links", "Link annotation without alternate text", pno)
            if sub not in (Name.Link, Name.Widget, Name.PrinterMark, Name.TrapNet)                     and not str(a.get("/Contents", "")).strip():
                fail("annotations", f"{str(sub)[1:]} annotation without alternate text", pno)
            if sub != Name.Link and sub not in (Name.Widget,):
                continue
            if "/StructParent" not in a:
                fail("links" if sub == Name.Link else "forms",
                     f"{str(sub)[1:]} annotation is not in the tag tree", pno)
        if annots and page.obj.get("/Tabs") != Name.S:
            fail("metadata", "Tab order is not set to document structure", pno)

    outlines = pdf.Root.get("/Outlines")
    if len(pdf.pages) > 20 and not (isinstance(outlines, Dictionary) and outlines.get("/First") is not None):
        fail("bookmarks", "Document over 20 pages has no bookmarks")

    bad = _parent_tree_mismatches(tree, nodes)
    if bad:
        fail("structure", f"{bad} ParentTree entries don't point back to their tag "
             "(Acrobat/PAC 'structure tree' errors)")

    for pno, n in enumerate(ctx.content.untagged_text):
        if n:
            fail("content", f"{n} untagged characters", pno)

    for font_name, embedded in _fonts(pdf):
        if not embedded:
            fail("fonts", f"Font not embedded: {font_name}")
    return out


def _parent_tree_mismatches(tree, nodes) -> int:
    """Every MCID / OBJR a tag owns must map back to that tag in the ParentTree."""
    if "/ParentTree" not in tree.root:
        return sum(len(tree.direct_mcids(n.elem)) for n in nodes)
    pt = dict(tree.parent_tree.items())
    bad = 0
    for n in nodes:
        for page, mcid in tree.direct_mcids(n.elem):
            if page is None:
                bad += 1
                continue
            key = tree.pdf.pages[page].obj.get("/StructParents")
            arr = pt.get(int(key)) if key is not None else None
            target = arr[mcid] if arr is not None and mcid < len(arr) else None
            if not (isinstance(target, Dictionary) and target.objgen == n.elem.objgen):
                bad += 1
        for objr in tree.objrs(n.elem):
            key = objr.Obj.get("/StructParent") if isinstance(objr.get("/Obj"), Dictionary) else None
            target = pt.get(int(key)) if key is not None else None
            if not (isinstance(target, Dictionary) and target.objgen == n.elem.objgen):
                bad += 1
    return bad


def _fonts(pdf):
    from .fixes.fonts import font_resource_dicts
    seen = {}
    for fonts in font_resource_dicts(pdf):
        for _, f in fonts.items():
            if not isinstance(f, Dictionary):
                continue
            name = str(f.get("/BaseFont", "?"))[1:]
            if name in seen:
                continue
            desc = f.get("/FontDescriptor")
            if desc is None and "/DescendantFonts" in f:
                desc = f.DescendantFonts[0].get("/FontDescriptor")
            if f.get("/Subtype") == Name.Type3:
                seen[name] = True
                continue
            seen[name] = isinstance(desc, Dictionary) and any(
                k in desc for k in ("/FontFile", "/FontFile2", "/FontFile3"))
    return seen.items()
