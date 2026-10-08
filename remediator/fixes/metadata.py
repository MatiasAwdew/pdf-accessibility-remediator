"""Document-level settings: title, show-title, language, tagged flag, PDF/UA id,
tab order. All deterministic -- the Acrobat checker's "Document" section."""

from __future__ import annotations

import re
from pathlib import Path

from pikepdf import Dictionary, Name, String

# file names, "Microsoft Word - x", and the names of the apps a page was printed
# from (BoardDocs, Legistar, ...) are not document titles
BAD_TITLE = re.compile(
    r"(\.(docx?|pdf|pptx?|indd|xlsx?|html?|aspx?)$)|^microsoft (word|powerpoint|excel)|^untitled|^document\d*$"
    r"|^(boarddocs|legistar|granicus|novus|civicclerk|primegov|laserfiche|docusign|adobe acrobat)\b"
    r"|^(powerpoint presentation|presentation\d*|slide \d+|title|page \d+|print|new document|doc\d*)$",
    re.I)


def _current_title(pdf) -> str:
    t = ""
    if pdf.docinfo is not None and "/Title" in pdf.docinfo:
        t = str(pdf.docinfo.Title)
    if not t.strip():
        try:
            with pdf.open_metadata(set_pikepdf_as_editor=False) as meta:
                t = str(meta.get("dc:title", "") or "")
        except Exception:
            pass
    return t.strip()


def _ai_title(ctx) -> str | None:
    suggest = getattr(ctx.ai, "suggest_title", None)
    if suggest is None:
        return None
    tree, content = ctx.tree, ctx.content
    text = "\n".join(mc.text.strip() for page in content.pages[:2] for mc in page.values() if mc.text.strip())
    heads = [content.describe(tree.all_mcids(n.elem)).text.strip() for n in tree.build()
             if tree.std_type(n.elem) in ("H1", "H2", "H3")]
    from ..ai import AIUnavailable
    try:
        return suggest(text, [h for h in heads if h], Path(ctx.original_path or ctx.source_path).name)
    except AIUnavailable as e:
        ctx.ai = None
        ctx.report.error("metadata", str(e))
        return None
    except Exception as e:  # AI problems never block the title fix
        ctx.report.info("metadata", f"AI title suggestion failed, used a heuristic: {e}")
        return None


def _derive_title(ctx) -> tuple[str, str]:
    ai_title = _ai_title(ctx)
    if ai_title:
        return ai_title[:200], "page 1 (Claude)"
    tree, content = ctx.tree, ctx.content
    for level in ("H1", "H2"):
        for n in tree.build():
            if tree.std_type(n.elem) == level:
                text = content.describe(tree.all_mcids(n.elem)).text.strip()
                if 3 <= len(text) <= 200:
                    return text, f"first {level}"
    if content.pages:
        biggest = max(content.pages[0].values(), key=lambda mc: (mc.size or 0), default=None)
        if biggest is not None and biggest.text.strip():
            return " ".join(biggest.text.split())[:200], "largest text on page 1"
    stem = Path(ctx.source_path).stem.replace("_", " ").replace("-", " ")
    return stem, "file name"


def run(ctx) -> None:
    pdf, report = ctx.pdf, ctx.report

    # Title + "Show document title" (Initial View > Window Options)
    title = _current_title(pdf)
    if not title or BAD_TITLE.search(title):
        new, source = _derive_title(ctx)
        with pdf.open_metadata(set_pikepdf_as_editor=False) as meta:
            meta["dc:title"] = new
        pdf.docinfo.Title = String(new)
        report.review("metadata", f'Set document title from {source}: "{new}"',
                      proposal={"field": "title", "old": title, "value": new, "applied": True})
    else:
        # keep a good title, but make sure both places have it: PDF/UA (7.1-9)
        # requires dc:title in the XMP metadata, not just the Info dictionary
        if pdf.docinfo.get("/Title") is None:
            pdf.docinfo.Title = String(title)
        with pdf.open_metadata(set_pikepdf_as_editor=False) as meta:
            if not str(meta.get("dc:title", "") or "").strip():
                meta["dc:title"] = title
    vp = pdf.Root.get("/ViewerPreferences")
    if vp is None:
        pdf.Root.ViewerPreferences = vp = Dictionary()
    if vp.get("/DisplayDocTitle") is not True:
        vp.DisplayDocTitle = True
        report.fixed("metadata", "Set Initial View to show the document title")

    # Language
    if not str(pdf.Root.get("/Lang", "")).strip():
        pdf.Root.Lang = String(ctx.config["language"])
        report.fixed("metadata", f"Set document language to {ctx.config['language']}")

    # Tagged PDF flag
    mi = pdf.Root.get("/MarkInfo")
    if mi is None:
        pdf.Root.MarkInfo = mi = Dictionary()
    if mi.get("/Marked") is not True:
        mi.Marked = True
        report.fixed("metadata", "Marked the document as tagged (MarkInfo /Marked true)")
    if mi.get("/Suspects") is True:
        mi.Suspects = False

    # PDF/UA identifier (PAC checks for it)
    if ctx.config["pdfua_identifier"]:
        with pdf.open_metadata(set_pikepdf_as_editor=False) as meta:
            if str(meta.get("pdfuaid:part", "")) != "1":
                meta["pdfuaid:part"] = "1"
                report.fixed("metadata", "Added PDF/UA identifier to XMP metadata")

    # Tab order = structure order on every page (Acrobat's checker flags any page
    # without it, even pages with no links)
    fixed_tabs = 0
    for page in pdf.pages:
        if page.obj.get("/Tabs") != Name.S:
            page.obj.Tabs = Name.S
            fixed_tabs += 1
    if fixed_tabs:
        report.fixed("metadata", f"Set tab order to 'use document structure' on {fixed_tabs} page(s)")
