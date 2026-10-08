"""PDF/UA-1 standard-structure fixes found on real government / college PDFs.

* Role mapping (Matterhorn 7.1-5): Word, newer Acrobat and PDF 2.0 producers
  use tags PDF 1.7 doesn't define (Aside, Footnote, Title, StyleSpan, ...).
  PDF/UA-1 requires each to be role-mapped to its nearest standard type.
  Runs last, so earlier steps still see the richer meaning (Title -> H1, ...).
* <Artifact> used as a structure element (Word does this): not allowed in the
  tag tree. Its content is turned into real artifacts and the element removed.
* Artifacts nested in tagged content, or tagged content inside an artifact
  (7.1-1 / 7.1-2): the inner/outer artifact marking is removed so the content
  is plainly tagged.
"""

from __future__ import annotations

import pikepdf
from pikepdf import Dictionary, Name

from ..structure import NEAREST_STANDARD, STANDARD_TYPES, as_list, is_struct_elem


def artifact_elements(ctx) -> None:
    """<Artifact> structure elements -> real artifacts (content) + removed tags."""
    from .artifacts import artifact_figure
    tree = ctx.tree
    converted = failed = 0
    for n in reversed(tree.build()):  # children before parents
        if n.raw_type != "Artifact":
            continue
        if any(is_struct_elem(k) for k in as_list(n.elem.get("/K"))):
            # hoist real children out, then artifact what's left
            parent = n.elem.get("/P")
            kids = as_list(n.elem.get("/K"))
            keep = [k for k in kids if is_struct_elem(k)]
            for k in keep:
                k.P = parent
            siblings = as_list(parent.get("/K")) if parent is not None else []
            idx = next((i for i, s in enumerate(siblings) if isinstance(s, Dictionary) and s.is_indirect
                        and s.objgen == n.elem.objgen), len(siblings))
            parent.K = pikepdf.Array(siblings[:idx + 1] + keep + siblings[idx + 1:])
            n.elem.K = pikepdf.Array([k for k in kids if not is_struct_elem(k)])
        if not as_list(n.elem.get("/K")):
            tree.remove_elem(n.elem)
            converted += 1
        elif artifact_figure(ctx, n.elem):
            converted += 1
        else:
            failed += 1
    if converted:
        ctx.report.fixed("structure", f"Converted {converted} <Artifact> tag(s) into real artifacts "
                         "(Artifact isn't a valid tag in PDF/UA-1)")
    if failed:
        tree.role_map["Artifact"] = "Span"
        ctx.report.error("structure", f"{failed} <Artifact> tag(s) couldn't be converted; mapped to Span")


def nested_artifacts(ctx) -> None:
    """Drop artifact marking that's inside tagged content, or that wraps tagged content."""
    fixed_pages = 0
    for page in ctx.pdf.pages:
        try:
            ops = pikepdf.parse_content_stream(page)
        except Exception:
            continue
        # match BMC/BDC with EMC and record each sequence's kind and nesting
        seqs, stack = {}, []
        for i, ins in enumerate(ops):
            op = str(getattr(ins, "operator", ""))
            if op in ("BMC", "BDC"):
                tag = str(ins.operands[0]) if ins.operands else ""
                has_mcid = (op == "BDC" and len(ins.operands) > 1 and isinstance(ins.operands[1], Dictionary)
                            and "/MCID" in ins.operands[1])
                kind = "artifact" if tag == "/Artifact" else ("mcid" if has_mcid else "other")
                seqs[i] = {"kind": kind, "end": None, "anc_mcid": any(seqs[j]["kind"] == "mcid" for j in stack),
                           "desc_mcid": False}
                if kind == "mcid":
                    for j in stack:
                        seqs[j]["desc_mcid"] = True
                stack.append(i)
            elif op == "EMC" and stack:
                seqs[stack.pop()]["end"] = i
        drop = set()
        for start, s in seqs.items():
            if s["kind"] == "artifact" and s["end"] is not None and (s["anc_mcid"] or s["desc_mcid"]):
                drop.update((start, s["end"]))
        if drop:
            page.obj.Contents = ctx.pdf.make_stream(pikepdf.unparse_content_stream(
                [ins for i, ins in enumerate(ops) if i not in drop]))
            fixed_pages += 1
    if fixed_pages:
        ctx.report.fixed("content", f"Removed artifact marking mixed into tagged content on {fixed_pages} page(s)")


def map_nonstandard(ctx) -> None:
    """Role-map every non-standard tag to its nearest PDF 1.7 standard type."""
    tree = ctx.tree
    root = tree.root
    rolemap = root.get("/RoleMap")
    if not isinstance(rolemap, Dictionary):
        rolemap = Dictionary()
    added = []
    for n in tree.build():
        name = n.raw_type
        if name in STANDARD_TYPES:
            continue
        target, seen = name, set()
        while target not in STANDARD_TYPES and target in tree.role_map and target not in seen:
            seen.add(target)
            target = tree.role_map[target]
        if target in STANDARD_TYPES:
            continue
        # unresolved (unmapped, mapped to a non-standard type, or circular)
        best = NEAREST_STANDARD.get(name) or NEAREST_STANDARD.get(target)
        if best is None:
            best = "Div" if any(is_struct_elem(k) for k in as_list(n.elem.get("/K"))) else "Span"
        rolemap[Name("/" + name)] = Name("/" + best)
        tree.role_map[name] = best
        added.append(f"{name}->{best}")
    # PDF/UA: standard types must not be remapped
    for key in list(rolemap.keys()):
        if key[1:] in STANDARD_TYPES:
            del rolemap[key]
            tree.role_map.pop(key[1:], None)
            added.append(f"removed remap of standard {key[1:]}")
    if added:
        root.RoleMap = rolemap
        ctx.report.fixed("structure", "Role-mapped non-standard tags for PDF/UA-1: " + ", ".join(sorted(set(added))[:12]))


def run_early(ctx) -> None:
    if not ctx.tree.tagged:
        return
    artifact_elements(ctx)
    nested_artifacts(ctx)


def run_late(ctx) -> None:
    map_nonstandard(ctx)
