"""Local auto-tagging for untagged PDFs (no Acrobat, no Adobe API).

Roughly what Acrobat's "Automatically tag PDF" does, for born-digital PDFs:

1. Every untagged text-showing operator gets its own marked-content id (MCID)
   and every untagged image its own /Figure MCID.
2. The file is re-read to learn where each MCID is on the page (text, size, box).
3. Ruled tables are found (pdfplumber) and built as Table/TR/TH/TD.
4. The rest of the text is grouped into lines, then paragraphs (line spacing,
   font size, indentation); bulleted/numbered paragraphs become lists.
5. Blocks are added to the tag tree in content-stream order, and the page's
   ParentTree entries are written.

Headings, alt text, title, fonts, artifacts etc. are then handled by the
normal steps. Scanned pages (images of text) still need OCR in Acrobat.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber
import pikepdf
from pikepdf import Array, Dictionary, Name

from ..content import ContentIndex

TEXT_SHOW = {"Tj", "TJ", "'", '"'}
PAGINATION = re.compile(r"^\s*(page\s*)?\d{1,4}(\s*(of|/)\s*\d{1,4})?\s*$", re.I)
BULLET = re.compile(r"^\s*(?:(?:[•●◦▪■–—\-\*o·‣⁃➢]|\(?\d{1,2}[.)]|\(?[a-zA-Z][.)])\s+|(?:\(cid:\d+\)|[-])\s*(?=\S))")
# (cid:N) and private-use characters: bullets drawn in Symbol / Wingdings fonts (Word does this)


@dataclass
class Unit:
    mcid: int
    kind: str  # text | image
    order: int


@dataclass
class Block:
    kind: str  # P | LI | Figure | Table
    order: int
    mcids: list = field(default_factory=list)
    rows: list = field(default_factory=list)  # tables: list of rows of lists of mcids


def _max_mcid(ops) -> int:
    m = -1
    for ins in ops:
        if str(getattr(ins, "operator", "")) == "BDC" and len(ins.operands) > 1 \
                and isinstance(ins.operands[1], Dictionary) and "/MCID" in ins.operands[1]:
            m = max(m, int(ins.operands[1].MCID))
    return m


def _mark_page(pdf, page, decorative: set, owned: set) -> list[Unit]:
    """Wrap untagged text shows and images in their own marked content, and pick
    up orphaned marked content (has an MCID but no tag owns it)."""
    ops = pikepdf.parse_content_stream(page)
    next_id = _max_mcid(ops) + 1
    out, units = [], []
    depth = 0
    changed = False
    for i, ins in enumerate(ops):
        op = str(getattr(ins, "operator", "INLINE IMAGE"))
        # (Word sometimes gives artifacts an MCID too; those stay artifacts)
        if (op == "BDC" and depth == 0 and len(ins.operands) > 1 and isinstance(ins.operands[1], Dictionary)
                and ins.operands[0] != Name.Artifact
                and "/MCID" in ins.operands[1] and int(ins.operands[1].MCID) not in owned):
            kind = "image" if ins.operands[0] == Name.Figure else "text"
            units.append(Unit(int(ins.operands[1].MCID), kind, len(units)))
        # "/P BMC ... EMC" (a tag name but no MCID): looks marked, isn't linked to
        # anything, so it counts as untagged. Give it an MCID so it can be tagged.
        elif (op in ("BMC", "BDC") and depth == 0 and ins.operands and ins.operands[0] != Name.Artifact
                and not (op == "BDC" and len(ins.operands) > 1 and isinstance(ins.operands[1], Dictionary)
                         and "/MCID" in ins.operands[1])):
            props = Dictionary()
            if op == "BDC" and len(ins.operands) > 1:
                p = ins.operands[1]
                if not isinstance(p, Dictionary):  # named property list: /Tag /MC1 BDC
                    p = ((page.obj.get("/Resources") or {}).get("/Properties") or {}).get(str(p))
                if isinstance(p, Dictionary):
                    props = Dictionary({k: v for k, v in p.items()})
            already_linked = "/MCID" in props
            # what's inside, up to the matching EMC?
            d, has_text, has_image = 0, False, False
            for later in ops[i:]:
                lop = str(getattr(later, "operator", "INLINE IMAGE"))
                d += lop in ("BMC", "BDC")
                d -= lop == "EMC"
                has_text |= lop in TEXT_SHOW
                has_image |= lop in ("Do", "INLINE IMAGE")
                if d == 0:
                    break
            if not already_linked and not (has_text or has_image):
                # only drawing (rules, logos drawn as paths, backgrounds): decoration
                ins = pikepdf.ContentStreamInstruction([Name.Artifact], pikepdf.Operator("BMC"))
                changed = True
            elif not already_linked:
                props.MCID = next_id
                ins = pikepdf.ContentStreamInstruction([ins.operands[0], props], pikepdf.Operator("BDC"))
                units.append(Unit(next_id, "text" if has_text else "image", len(units)))
                next_id += 1
                changed = True
        if op in ("BMC", "BDC"):
            depth += 1
        elif op == "EMC":
            depth = max(0, depth - 1)
        if op == "Do" and depth == 0:
            from .artifacts import has_own_marked_content
            xo = ((page.obj.get("/Resources") or {}).get("/XObject") or {}).get(str(ins.operands[0]))
            if has_own_marked_content(xo):  # already marked inside: don't nest another marking around it
                out.append(ins)
                continue
        if depth == 0 and (op in TEXT_SHOW or (op in ("Do", "INLINE IMAGE") and i not in decorative)):
            kind = "text" if op in TEXT_SHOW else "image"
            tag = Name.P if kind == "text" else Name.Figure
            out.append(pikepdf.ContentStreamInstruction([tag, Dictionary(MCID=next_id)], pikepdf.Operator("BDC")))
            out.append(ins)
            out.append(pikepdf.ContentStreamInstruction([], pikepdf.Operator("EMC")))
            units.append(Unit(next_id, kind, len(units)))
            next_id += 1
            changed = True
        else:
            out.append(ins)
    if changed:
        page.obj.Contents = pdf.make_stream(pikepdf.unparse_content_stream(out))
    return units


def _center(box):
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2


def _tables(plumb_page, page_h, units, content, pno) -> tuple[list[Block], set]:
    """Ruled tables -> Blocks with rows of cell MCID lists; returns used MCIDs."""
    blocks, used = [], set()
    try:
        found = plumb_page.find_tables()
    except Exception:
        return blocks, used
    for t in found:
        rows = []
        for row in t.rows:
            cells = []
            for cell in row.cells:
                if cell is None:
                    continue
                x0, top, x1, bottom = cell
                box = (x0, page_h - bottom, x1, page_h - top)
                ids = []
                for u in units:
                    mc = content.get(pno, u.mcid)
                    if u.mcid in used or mc is None or mc.bbox is None:
                        continue
                    cx, cy = _center(mc.bbox)
                    if box[0] <= cx <= box[2] and box[1] <= cy <= box[3]:
                        ids.append(u.mcid)
                used.update(ids)
                cells.append(ids)
            if cells and any(cells):  # skip rows with no text at all (spacer rows)
                rows.append(cells)
        filled = sum(1 for r in rows for c in r if c)
        if len(rows) >= 2 and max(len(r) for r in rows) >= 2 and filled >= 3:
            order = min((u.order for u in units if any(u.mcid in c for r in rows for c in r)), default=0)
            blocks.append(Block("Table", order, rows=rows))
        else:  # not a real table: give the text back to the paragraph grouping
            for r in rows:
                for c in r:
                    used.difference_update(c)
    return blocks, used


def _paragraphs(units, content, pno, used) -> list[Block]:
    blocks: list[Block] = []
    cur: Block | None = None
    last = None  # (x0, y_bottom, size) of the previous line
    for u in units:
        if u.kind != "text" or u.mcid in used:
            continue
        mc = content.get(pno, u.mcid)
        if mc is None or mc.bbox is None or not mc.text.strip():
            if cur is not None:  # whitespace-only show: keep it with the current paragraph
                cur.mcids.append(u.mcid)
            continue
        size = mc.size or 10
        x0, y0, x1, y1 = mc.bbox
        new = cur is None
        if not new and last is not None:
            lx0, ly0, lsize = last
            same_line = abs(y0 - ly0) < 0.5 * size
            if not same_line:
                gap = ly0 - y1  # previous bottom to this top (PDF y grows upward)
                new = (gap > 0.9 * max(size, lsize) or y0 > ly0 + 0.5 * size  # big gap, or moved up
                       or abs(size - lsize) > 1.0 or bool(BULLET.match(mc.text)))
            elif abs(size - lsize) > 1.5:
                new = True
        if new:
            cur = Block("LI" if BULLET.match(mc.text) else "P", u.order)
            blocks.append(cur)
        cur.mcids.append(u.mcid)
        if last is None or abs(y0 - last[1]) >= 0.5 * size:
            last = (x0, y0, size)
    return blocks


def _elem(tree, s_type, parent, pno):
    return tree.new_elem(s_type, parent, pno)


def autotag(ctx) -> bool:
    """Tag untagged content in place. Returns True if anything was tagged."""
    from .artifacts import decorative_untagged
    pdf, report = ctx.pdf, ctx.report
    decorative = decorative_untagged(pdf, ctx.config["figures"]["small_image_page_fraction"])
    owned: dict[int, set] = {}
    if ctx.tree.tagged:
        for n in ctx.tree.build():
            for page_i, m in ctx.tree.direct_mcids(n.elem):
                owned.setdefault(page_i, set()).add(m)
    was_untagged = not ctx.tree.tagged or ctx.content.untagged_share > 0.5

    # Stale /StructParents numbers (left by the producer of an untagged file, or
    # shared by several pages) would make pages overwrite each other's ParentTree
    # slot. Drop any that are missing, shared, or in a file without a tag tree;
    # set_mcid_owner then assigns fresh ones.
    known = {int(k) for k, _ in ctx.tree.parent_tree.items()} if ctx.tree.tagged else set()
    seen_keys: dict[int, int] = {}
    for page in pdf.pages:
        if "/StructParents" in page.obj:
            k = int(page.obj.StructParents)
            seen_keys[k] = seen_keys.get(k, 0) + 1
    for page in pdf.pages:
        if "/StructParents" in page.obj:
            k = int(page.obj.StructParents)
            if not ctx.tree.tagged or k not in known or seen_keys[k] > 1:
                del page.obj["/StructParents"]
    marked = {pno: _mark_page(pdf, page, decorative.get(pno, set()), owned.get(pno, set()))
              for pno, page in enumerate(pdf.pages)}
    if not any(marked.values()):
        return False

    # re-read the marked file to learn where every MCID is
    tmp = Path(tempfile.mkdtemp(prefix="remediator-tag-")) / "marked.pdf"
    pdf.save(tmp)
    content = ContentIndex.load(str(tmp))

    # tag tree root + Document
    if "/StructTreeRoot" not in pdf.Root:
        pdf.Root.StructTreeRoot = pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot, K=Array()))
    from ..structure import StructTree, as_list
    tree = StructTree(pdf)
    top = tree.top_level()
    if len(top) == 1 and tree.std_type(top[0]) == "Document":
        doc = top[0]
    else:
        doc = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.Document, P=tree.root, K=Array(top)))
        for e in top:
            e.P = doc
        tree.root.K = doc

    counts = {"P": 0, "L": 0, "Table": 0, "Figure": 0}
    with pdfplumber.open(str(tmp)) as plumb:
        for pno, units in marked.items():
            if not units:
                continue
            page_h = content.page_sizes[pno][1]
            tables, used = _tables(plumb.pages[pno], page_h, units, content, pno)
            blocks = tables + _paragraphs(units, content, pno, used)
            blocks += [Block("Figure", u.order, [u.mcid]) for u in units if u.kind == "image"]
            # any text not placed anywhere (e.g. whitespace before the first paragraph)
            placed = {m for b in blocks for m in b.mcids} | used
            stray = [u for u in units if u.mcid not in placed]
            if stray:
                blocks.append(Block("P", stray[0].order, [u.mcid for u in stray]))
            blocks.sort(key=lambda b: b.order)

            # insert this page's new blocks after the existing tags of pages <= this one
            existing = list(as_list(doc.get("/K")))
            at = len(existing)
            for i in range(len(existing) - 1, -1, -1):
                pg = tree.page_of(existing[i]) if isinstance(existing[i], Dictionary) else None
                if pg is not None and pg > pno:
                    at = i
                elif pg is not None:
                    break
            kids, tail = existing[:at], existing[at:]
            list_elem = None
            for b in blocks:
                if b.kind == "LI":
                    if list_elem is None:
                        list_elem = _elem(tree, "L", doc, pno)
                        kids.append(list_elem)
                        counts["L"] += 1
                    li = _elem(tree, "LI", list_elem, pno)
                    body = _elem(tree, "LBody", li, pno)
                    body.K = Array(b.mcids)
                    li.K = Array([body])
                    list_elem.K = Array(list(as_list(list_elem.get("/K"))) + [li])
                    for m in b.mcids:
                        tree.set_mcid_owner(pno, m, body)
                    continue
                list_elem = None
                if b.kind == "Table":
                    table = _elem(tree, "Table", doc, pno)
                    trs = []
                    for r, row in enumerate(b.rows):
                        tr = _elem(tree, "TR", table, pno)
                        cells = []
                        for ids in row:
                            cell = _elem(tree, "TH" if r == 0 else "TD", tr, pno)
                            cell.K = Array(ids)
                            for m in ids:
                                tree.set_mcid_owner(pno, m, cell)
                            cells.append(cell)
                        tr.K = Array(cells)
                        trs.append(tr)
                    table.K = Array(trs)
                    kids.append(table)
                    counts["Table"] += 1
                    continue
                e = _elem(tree, b.kind, doc, pno)
                e.K = Array(b.mcids)
                for m in b.mcids:
                    tree.set_mcid_owner(pno, m, e)
                kids.append(e)
                counts[b.kind] += 1
            doc.K = Array(kids + tail)

    # page numbers in the margins and whitespace-only paragraphs aren't content
    ctx.tree = StructTree(pdf)
    ctx.content = content
    from .artifacts import artifact_figure
    artifacted = 0
    for n in ctx.tree.build():
        if ctx.tree.std_type(n.elem) != "P" or any(True for _ in ctx.tree.objrs(n.elem)):
            continue
        mc = content.describe(ctx.tree.all_mcids(n.elem))
        pg = ctx.tree.page_of(n.elem)
        if pg is None:
            continue
        h = content.page_sizes[pg][1]
        in_margin = mc.bbox is not None and (mc.bbox[1] > h * 0.9 or mc.bbox[3] < h * 0.1)
        if (not mc.text.strip() or (in_margin and PAGINATION.match(mc.text))) and artifact_figure(ctx, n.elem):
            artifacted += 1
    if artifacted:
        report.fixed("content", f"Marked {artifacted} page number(s)/empty text block(s) as artifacts")
    ctx.source_path = str(tmp)
    ctx.locally_tagged = was_untagged
    mi = pdf.Root.get("/MarkInfo")
    if not isinstance(mi, Dictionary):
        pdf.Root.MarkInfo = mi = Dictionary()
    mi.Marked = True
    report.review("structure", ("Tagged the untagged document locally" if was_untagged else
                                "Tagged leftover untagged content") + f": {counts['P']} paragraph(s), "
                  f"{counts['L']} list(s), {counts['Table']} table(s), {counts['Figure']} figure(s). "
                  "Check the reading order and tables in Acrobat")
    return True
