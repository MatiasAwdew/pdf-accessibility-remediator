"""Runs the remediation steps in the same order you'd do them in Acrobat."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pikepdf

from . import checks
from .content import ContentIndex
from .fixes import bookmarks, cleanup, figures, fonts, headings, links, metadata, standards, tables
from .report import Report
from .structure import StructTree

def localtag_step(ctx) -> None:
    if not ctx.config["autotag"]["local"]:
        return
    from .fixes import localtag
    localtag.autotag(ctx)


STEPS = [
    ("standards", standards.run_early),  # <Artifact> tags, artifacts mixed into tagged content
    ("localtag", localtag_step),         # tag untagged pages / leftovers / orphaned content
    ("cleanup", cleanup.run),     # tag-tree hygiene + preflight-style artifacting
    ("headings", headings.run),   # fix heading structure
    ("metadata", metadata.run),   # title (uses H1), show title, lang, tab order, PDF/UA id
    ("figures", figures.run),     # alt text
    ("links", links.run),         # Link tags + link alt text
    ("tables", tables.run),       # TH / Scope / Summary / regularity
    ("rolemap", standards.run_late),     # map PDF 2.0 / custom tags to PDF 1.7 standard types
    ("bookmarks", bookmarks.run), # bookmarks from the corrected headings
    ("fonts", fonts.run),         # embed missing fonts (Preflight fixup)
]


@dataclass
class Context:
    pdf: pikepdf.Pdf
    tree: StructTree
    content: ContentIndex
    report: Report
    config: dict
    ai: object | None
    source_path: str
    original_path: str | None = None  # the input before AutoTag replaced its tags
    locally_tagged: bool = False      # the tool tagged this document itself (no AutoTag)


def output_paths(src: str, out: str | None, suffix: str = "_accessible") -> dict[str, str]:
    p = Path(src)
    pdf_out = Path(out) if out else p.with_name(p.stem + suffix + ".pdf")
    base = pdf_out.with_suffix("")
    return {"pdf": str(pdf_out), "json": f"{base}_report.json", "html": f"{base}_report.html",
            "review": f"{base}_review.json",
            "autotagged": str(p.with_name(p.stem + "_autotagged.pdf")),
            "autotag_report": str(p.with_name(p.stem + "_autotag_report.xlsx"))}


def make_ai(config: dict, enabled: bool):
    # figures.ai_alt_text only switches off alt text (checked in fixes/figures.py);
    # heading review still uses Claude.
    if not enabled:
        return None
    from . import credentials
    has_profile = (Path.home() / ".config" / "anthropic").is_dir()
    if not (credentials.get("anthropic_api_key") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or has_profile):
        print("  (AI alt text off: no Claude API key - run `python remediate.py setup`)")
        return None
    try:
        from .ai import AltTextGenerator
        return AltTextGenerator(config["ai"]["model"], config["learning"]["use_as_examples"])
    except Exception as e:  # SDK missing or no credentials
        print(f"  (AI alt text unavailable: {e})")
        return None


def _is_tagged(path: str) -> bool:
    with pikepdf.open(path) as pdf:
        return "/StructTreeRoot" in pdf.Root


def _mostly_untagged(path: str, threshold: float) -> float | None:
    """Share of text outside the tag tree, if above threshold (e.g. only figures were tagged)."""
    share = ContentIndex.load(path).untagged_share
    return share if share > threshold else None


def _autotag(src: str, paths: dict, config: dict, report: Report, force: bool) -> str:
    """Tag src with Adobe's AutoTag API if it needs it. Returns the file to remediate."""
    cfg = config["autotag"]
    tagged = _is_tagged(src)
    partial = _mostly_untagged(src, cfg["untagged_text_threshold"]) if tagged else None
    if tagged and partial is None and not (force or cfg["retag_tagged"]):
        return src
    if partial is not None and not (force or cfg["retag_tagged"]):
        report.info("autotag", f"{partial:.0%} of the text is outside the tag tree - re-tagging")
    if not cfg["enabled"]:
        return src
    cached = Path(paths["autotagged"])
    if cfg.get("reuse_previous", True) and cached.exists() and cached.stat().st_mtime >= Path(src).stat().st_mtime:
        report.info("autotag", f"Reused earlier AutoTag result {cached.name} (delete it to re-tag)")
        return str(cached)
    from . import autotag
    if not autotag.available():
        if not tagged:
            print("  (AutoTag unavailable: no Adobe credentials - run `python remediate.py setup`)")
        elif partial is not None:
            report.error("structure", f"{partial:.0%} of the text isn't tagged - AutoTag this PDF "
                         "(add Adobe credentials with `remediate.py setup`, or use Acrobat) and re-run")
        return src
    try:
        client = autotag.AutoTagClient(cfg["region"], cfg["timeout_seconds"])
        client.autotag(src, paths["autotagged"],
                       report_dest=paths["autotag_report"] if cfg["save_adobe_report"] else None,
                       shift_headings=cfg["shift_headings"])
    except Exception as e:
        hint = ""
        if "QUOTA" in str(e) or "429" in str(e):
            hint = " Your Adobe API quota is used up: AutoTag this PDF in Acrobat, save it, and run it again."
        report.error("autotag", f"Adobe AutoTag failed: {e}.{hint}")
        return src
    report.fixed("autotag", "Tagged with Adobe AutoTag" + (" (replaced existing tags)" if tagged else ""))
    return paths["autotagged"]


def remediate(src: str, config: dict, out: str | None = None, use_ai: bool = True,
              only_check: bool = False, force_autotag: bool = False) -> Report:
    paths = output_paths(src, out, "_check" if only_check else "_accessible")
    report = Report(src)
    original = src
    if not only_check:
        src = _autotag(src, paths, config, report, force_autotag)
        if not config["autotag"]["keep_intermediate"] and src == paths["autotagged"]:
            import atexit
            atexit.register(lambda: os.path.exists(src) and os.remove(src))
    pdf = pikepdf.open(src)
    report.pages = len(pdf.pages)
    tree = StructTree(pdf)
    content = ContentIndex.load(src)

    if not tree.tagged and (only_check or not config["autotag"]["local"]):
        report.error("structure", "PDF is not tagged. AutoTag it in Acrobat "
                     "(Prepare for accessibility > Automatically tag PDF) and re-run.")
        ctx = Context(pdf, tree, content, report, config, None, src)
        if not only_check:
            metadata.run(ctx)
        return _finish(pdf, report, paths, only_check, ctx)

    ctx = Context(pdf, tree, content, report, config, None if only_check else make_ai(config, use_ai), src,
                  original if original != src else None)
    if not only_check:
        for name, step in STEPS:
            try:
                step(ctx)
            except Exception as e:
                import traceback
                where = traceback.extract_tb(e.__traceback__)[-1]
                report.error(name, f"Step crashed, skipped: {type(e).__name__}: {e} "
                             f"({Path(where.filename).name}:{where.lineno})")
    return _finish(pdf, report, paths, only_check, ctx)


def _finish(pdf, report, paths, only_check, ctx) -> Report:
    report.remaining = checks.run(ctx)
    report.resolve_paths(ctx.tree.build())
    if not only_check:
        report.output = paths["pdf"]
        pdf.save(paths["pdf"])
        report.write_review(paths["review"])
    source_file = pdf.filename
    pdf.close()
    _verapdf(report, paths["pdf"] if not only_check else source_file, ctx.config, fix=not only_check)
    report.write_json(paths["json"])
    report.write_html(paths["html"])
    return report


def _verapdf(report: Report, path: str, config: dict, fix: bool) -> None:
    """Validate the finished file against PDF/UA-1 (PAC's standard) with veraPDF,
    fix what the tool can, re-validate, and report what's left."""
    cfg = config["validate"]
    if not cfg["verapdf"]:
        return
    from . import validate
    exe = validate.find_verapdf(cfg.get("verapdf_path"))
    if not exe:
        report.info("validation", "veraPDF isn't installed, so PDF/UA wasn't checked automatically "
                    "(see README); check the file in PAC")
        return
    try:
        if fix:
            failures, fixes = validate.validate_and_fix(path, exe, cfg["max_rounds"])
        else:
            failures, fixes = validate.run_verapdf(exe, path), []
    except Exception as e:
        report.info("validation", f"veraPDF check failed to run: {e}")
        return
    for f in fixes:
        report.fixed("validation", f"PDF/UA check: {f}")
    for f in failures:
        why = validate.EXPLAIN.get(f.rule, f.description)
        report.remaining.append(("PDF/UA", f"[{f.rule}] {why} ({f.count}x)", None))
    report.verapdf_compliant = not failures
    report.info("validation", "PDF/UA-1 (veraPDF): " + ("PASS" if not failures else f"{len(failures)} rule(s) failing"))


def iter_inputs(inputs: list[str]):
    for item in inputs:
        if os.path.isdir(item):
            for p in sorted(Path(item).glob("*.pdf")):
                if not p.stem.endswith("_accessible"):
                    yield str(p)
        else:
            yield item
