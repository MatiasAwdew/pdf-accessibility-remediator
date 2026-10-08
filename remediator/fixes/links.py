"""Links: "Link annotation not nested in Link tag" / "link alt text missing".

For each /Link annotation that isn't referenced from the tag tree we build
<Link> [ <marked content of the link text>, OBJR -> annotation ] and splice it
in where the link text currently lives, keeping the ParentTree consistent.
"""

from __future__ import annotations

from pikepdf import Array, Dictionary, Name, String

from ..structure import as_list, is_mcr


# Annotations that don't need /Contents: popups belong to their parent, form
# fields use /TU, and PrinterMark/TrapNet are print-production marks.
NO_ALT_NEEDED = {"/Popup", "/Widget", "/PrinterMark", "/TrapNet", "/Link"}


def _describe_annotation(annot) -> str:
    """Alt text for a non-link annotation (PDF/UA: every annotation needs Contents)."""
    sub = str(annot.get("/Subtype", "/Annot"))[1:]
    if sub == "FileAttachment":
        fs = annot.get("/FS")
        name = ""
        if isinstance(fs, Dictionary):
            name = str(fs.get("/UF") or fs.get("/F") or "")
            desc = str(fs.get("/Desc") or "")
            if desc and not name:
                name = desc
        elif fs is not None:
            name = str(fs)
        return f"Attached file: {name}" if name else "Attached file"
    return {"Text": "Comment", "FreeText": "Text comment", "Stamp": "Stamp", "Highlight": "Highlighted text",
            "Underline": "Underlined text", "StrikeOut": "Struck-out text", "Square": "Rectangle markup",
            "Circle": "Circle markup", "Line": "Line markup", "Ink": "Drawing", "Sound": "Sound clip",
            "Screen": "Media", "RichMedia": "Media", "3D": "3D model"}.get(sub, f"{sub} annotation")


def _describe_target(ctx, annot) -> str:
    action = annot.get("/A")
    if isinstance(action, Dictionary):
        if action.get("/S") == Name.URI and "/URI" in action:
            return str(action.URI)
        if action.get("/S") == Name.GoTo:
            return "Link to another location in this document"
    if "/Dest" in annot:
        dest = annot.Dest
        if isinstance(dest, Array) and len(dest) and isinstance(dest[0], Dictionary):
            pno = ctx.tree.page_index.get(dest[0].objgen)
            if pno is not None:
                return f"Link to page {pno + 1}"
        return "Link to another location in this document"
    return ""


def _mcid_owners(ctx) -> dict:
    """(page, mcid) -> (owner elem, index into owner's /K)."""
    owners = {}
    for n in ctx.tree.build():
        own_page = ctx.tree.page_of(n.elem)
        for i, kid in enumerate(as_list(n.elem.get("/K"))):
            if isinstance(kid, int):
                owners[(own_page, int(kid))] = (n.elem, i)
            elif is_mcr(kid) and "/MCID" in kid:
                pg = kid.get("/Pg")
                page = ctx.tree.page_index.get(pg.objgen) if pg is not None else own_page
                owners[(page, int(kid.MCID))] = (n.elem, i)
    return owners


def _hit_mcids(ctx, pno, rect) -> list[int]:
    x0, x1 = sorted((float(rect[0]), float(rect[2])))
    y0, y1 = sorted((float(rect[1]), float(rect[3])))
    hits = []
    for mcid, mc in ctx.content.pages[pno].items():
        for (a, b, c, d) in mc.chars_boxes:
            cx, cy = (a + c) / 2, (b + d) / 2
            if x0 - 1 <= cx <= x1 + 1 and y0 - 1 <= cy <= y1 + 1:
                hits.append(mcid)
                break
    return sorted(hits)


def run(ctx) -> None:
    tree, report, cfg = ctx.tree, ctx.report, ctx.config["links"]
    referenced = {int(k) for k, _ in tree.parent_tree.items()}
    owners = None
    doc_elem = tree.top_level()[0] if tree.top_level() else None
    filled = wrapped = 0

    for pno, page in enumerate(ctx.pdf.pages):
        annots = page.obj.get("/Annots")
        if annots is None:
            continue
        for ai, annot in enumerate(list(annots)):
            if not isinstance(annot, Dictionary) or annot.get("/Subtype") != Name.Link:
                continue
            if not annot.is_indirect:
                annot = ctx.pdf.make_indirect(annot)
                annots[ai] = annot

            if cfg["fill_contents"] and not str(annot.get("/Contents", "")).strip():
                target = _describe_target(ctx, annot)
                if target:
                    annot.Contents = String(target)
                    filled += 1
                else:
                    report.error("links", "Link has no destination and no alternate text", page=pno)

            if "/StructParent" in annot and int(annot.StructParent) in referenced:
                # only counts if the entry is really a <Link> that holds this annotation;
                # real PDFs point links at P/Span tags or stale entries (veraPDF 7.18.5-1)
                owner = tree.parent_tree[int(annot.StructParent)]
                if (isinstance(owner, Dictionary) and tree.std_type(owner) == "Link"
                        and any(o.get("/Obj") is not None and o.Obj.objgen == annot.objgen
                                for o in tree.objrs(owner))):
                    continue
                if isinstance(owner, Dictionary):  # drop the OBJR from the wrong owner
                    kept = [k for k in as_list(owner.get("/K"))
                            if not (isinstance(k, Dictionary) and k.get("/Type") == Name.OBJR
                                    and k.get("/Obj") is not None and k.Obj.objgen == annot.objgen)]
                    owner.K = Array(kept)
            if not cfg["wrap_untagged_annotations"]:
                report.error("links", "Link annotation is not in a <Link> tag", page=pno)
                continue

            owners = owners if owners is not None else _mcid_owners(ctx)
            hits = [m for m in _hit_mcids(ctx, pno, annot.Rect) if (pno, m) in owners]
            objr = Dictionary(Type=Name.OBJR, Obj=annot, Pg=page.obj)
            if hits:
                owner, _ = owners[(pno, hits[0])]
                same = [m for m in hits if owners[(pno, m)][0].objgen == owner.objgen]
                idxs = sorted(owners[(pno, m)][1] for m in same)
                link = tree.wrap_kids(owner, idxs, "Link")
                link.K = Array(list(link.K) + [objr])
                owners = None  # indices shifted; rebuild lazily
                msg = "Wrapped link text and annotation in a <Link> tag"
            elif doc_elem is not None:
                link = tree.new_elem("Link", doc_elem, pno)
                link.K = Array([objr])
                doc_elem.K = Array(as_list(doc_elem.get("/K")) + [link])
                msg = "Added <Link> tag for annotation (no link text found - check reading order)"
                report.review("links", msg, page=pno, elem=link)
            else:
                continue
            tree.set_objr_owner(annot, link)
            referenced.add(int(annot.StructParent))
            wrapped += 1

    other = nested = 0
    for pno, page in enumerate(ctx.pdf.pages):
        annots = page.obj.get("/Annots") or []
        for ai, annot in enumerate(list(annots)):
            if not isinstance(annot, Dictionary) or str(annot.get("/Subtype")) in NO_ALT_NEEDED:
                continue
            if annot.get("/F", 0) and int(annot.F) & 2:  # hidden
                continue
            if not str(annot.get("/Contents", "")).strip():
                annot.Contents = String(_describe_annotation(annot))
                other += 1
            # PDF/UA 7.18.1-1: every non-link annotation must sit in an <Annot> tag
            if doc_elem is None:
                continue
            if not annot.is_indirect:
                annot = ctx.pdf.make_indirect(annot)
                annots[ai] = annot
            sp = annot.get("/StructParent")
            owner = tree.parent_tree[int(sp)] if sp is not None and int(sp) in referenced else None
            if isinstance(owner, Dictionary) and any(o.get("/Obj") is not None and o.Obj.objgen == annot.objgen
                                                     for o in tree.objrs(owner)):
                continue
            elem = tree.new_elem("Annot", doc_elem, pno)
            elem.K = Array([Dictionary(Type=Name.OBJR, Obj=annot, Pg=page.obj)])
            doc_elem.K = Array(as_list(doc_elem.get("/K")) + [elem])
            tree.set_objr_owner(annot, elem)
            referenced.add(int(annot.StructParent))
            nested += 1
    if other:
        report.fixed("links", f"Added alternate text to {other} other annotation(s) (file attachments, comments...)")
    if nested:
        report.fixed("links", f"Put {nested} annotation(s) (stamps, comments, attachments) into <Annot> tags")
    if filled:
        report.fixed("links", f"Added alternate text (Contents) to {filled} link(s)")
    if wrapped:
        report.fixed("links", f"Put {wrapped} link annotation(s) into <Link> tags")
