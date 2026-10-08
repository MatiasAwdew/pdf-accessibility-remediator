"""Tag-tree hygiene: the stuff Acrobat's Preflight fixups and the
"reading order" panel make you clean up by hand after AutoTag."""

from __future__ import annotations

import pikepdf
from pikepdf import Array, Name

from ..structure import STANDARD_TYPES, as_list, is_struct_elem

PAINT_OPS = {"S", "s", "f", "F", "f*", "B", "B*", "b", "b*", "sh"}
PATH_OPS = {"m", "l", "c", "v", "y", "h", "re", "n", "W", "W*"}


def ensure_document_root(ctx) -> None:
    tree, root = ctx.tree, ctx.tree.root
    top = tree.top_level()
    if len(top) == 1 and tree.std_type(top[0]) == "Document":
        return
    doc = tree.pdf.make_indirect(pikepdf.Dictionary(
        Type=Name.StructElem, S=Name.Document, P=root, K=Array(top)))
    for e in top:
        e.P = doc
    root.K = doc
    ctx.report.fixed("structure", "Wrapped top-level tags in a <Document> element")


# Empty cells are real: a blank top-left header or an empty data cell keeps the
# grid regular. Deleting them makes tables irregular (PAC error).
KEEP_EMPTY = {"Document", "TH", "TD", "TR", "THead", "TBody", "TFoot", "Table"}


def remove_empty(ctx) -> None:
    tree = ctx.tree
    removed = 0
    for _ in range(10):  # removing a child can empty its parent
        empties = [n for n in tree.build()
                   if not as_list(n.elem.get("/K")) and tree.std_type(n.elem) not in KEEP_EMPTY]
        if not empties:
            break
        for n in empties:
            tree.remove_elem(n.elem)
            removed += 1
    if removed:
        ctx.report.fixed("structure", f"Removed {removed} empty tag(s)")


def fix_lists(ctx) -> None:
    """L must contain LI; LI must contain Lbl/LBody. AutoTag often gets this wrong."""
    tree = ctx.tree
    fixed = 0
    for n in tree.build():
        t = tree.std_type(n.elem)
        kids = as_list(n.elem.get("/K"))
        if t == "L":
            bad = [i for i, k in enumerate(kids)
                   if not (is_struct_elem(k) and tree.std_type(k) in ("LI", "L", "Caption"))]
            for i in reversed(bad):
                tree.wrap_kids(n.elem, [i], "LI")
                fixed += 1
        elif t == "LI":
            bad = [i for i, k in enumerate(kids)
                   if not (is_struct_elem(k) and tree.std_type(k) in ("Lbl", "LBody"))]
            if bad:
                tree.wrap_kids(n.elem, bad, "LBody")
                fixed += 1
    if fixed:
        ctx.report.fixed("lists", f"Repaired {fixed} list item(s) (added missing LI/LBody)")


def check_role_map(ctx) -> None:
    tree = ctx.tree
    used = {n.raw_type for n in tree.build()}
    for t in sorted(used - STANDARD_TYPES):
        target = t
        seen = set()
        while target in tree.role_map and target not in seen:
            seen.add(target)
            target = tree.role_map[target]
        if target not in STANDARD_TYPES:
            ctx.report.error("structure", f"Tag <{t}> is not a standard type and has no valid role mapping")


def artifact_untagged_paths(ctx) -> None:
    """Wrap decorative drawing (rules, boxes, backgrounds) that sits outside any
    marked content in /Artifact BMC..EMC -- PAC's "untagged content" errors.

    Path painting is always artifacted. Untagged images are artifacted when
    they're decorative (small, or the same image repeated in the same spot on
    several pages, e.g. logos); other images and all untagged *text* are real
    content and are reported instead.
    """
    from .artifacts import decorative_untagged
    decorative = decorative_untagged(ctx.pdf, ctx.config["figures"]["small_image_page_fraction"])         if ctx.config["figures"]["artifact_decorative"] else {}
    wrapped_pages = images_artifacted = 0
    for pno, page in enumerate(ctx.pdf.pages):
        try:
            ops = pikepdf.parse_content_stream(page)
        except Exception as e:  # malformed stream: leave it alone
            ctx.report.error("content", f"Could not parse content stream: {e}", page=pno)
            continue
        out, run = [], []
        depth = 0  # marked-content nesting
        in_text = False
        untagged_images = 0
        changed = False

        def flush():
            nonlocal changed
            if run and any(str(o.operator) in PAINT_OPS for o in run):
                out.append(pikepdf.ContentStreamInstruction([Name.Artifact], pikepdf.Operator("BMC")))
                out.extend(run)
                out.append(pikepdf.ContentStreamInstruction([], pikepdf.Operator("EMC")))
                changed = True
            else:
                out.extend(run)
            run.clear()

        for idx, ins in enumerate(ops):
            op = str(ins.operator) if hasattr(ins, "operator") else "INLINE IMAGE"
            if op in ("BDC", "BMC"):
                flush(); depth += 1; out.append(ins); continue
            if op == "EMC":
                flush(); depth = max(0, depth - 1); out.append(ins); continue
            if op == "BT":
                in_text = True
            if op == "ET":
                in_text = False
            if depth == 0 and not in_text and (op in PATH_OPS or op in PAINT_OPS):
                run.append(ins)
                continue
            if depth == 0 and op == "Do" and idx in decorative.get(pno, ()):
                flush()
                out.append(pikepdf.ContentStreamInstruction([Name.Artifact], pikepdf.Operator("BMC")))
                out.append(ins)
                out.append(pikepdf.ContentStreamInstruction([], pikepdf.Operator("EMC")))
                changed = True
                images_artifacted += 1
                continue
            if depth == 0 and op in ("Do", "INLINE IMAGE", "BI"):
                untagged_images += 1
            flush()
            out.append(ins)
        flush()

        if changed:
            page.obj.Contents = ctx.pdf.make_stream(pikepdf.unparse_content_stream(out))
            wrapped_pages += 1
        if untagged_images:
            ctx.report.error("content", f"{untagged_images} image/XObject(s) drawn outside the tag tree "
                             "(tag as Figure or mark as Artifact)", page=pno)
    if wrapped_pages:
        ctx.report.fixed("content", f"Marked untagged lines/shapes as artifacts on {wrapped_pages} page(s)")
    if images_artifacted:
        ctx.report.fixed("content", f"Marked {images_artifacted} decorative untagged image(s) (logos, repeated "
                         "marks) as artifacts")


def run(ctx) -> None:
    c = ctx.config["cleanup"]
    if c["ensure_document_root"]:
        ensure_document_root(ctx)
    if c["fix_list_structure"]:
        fix_lists(ctx)
    if c["remove_empty_elements"]:
        remove_empty(ctx)
    if c["artifact_untagged_paths"]:
        artifact_untagged_paths(ctx)
