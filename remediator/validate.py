"""PDF/UA-1 validation with veraPDF, plus automatic fixes for what it finds.

PAC can't be run from a script, so after remediating we
validate with veraPDF, the PDF Association's open-source PDF/UA validator. It
implements the same Matterhorn-based machine checks (and is a little stricter).
Failures the tool knows how to fix are fixed and the file re-validated, up to a
few rounds; whatever is left is reported. PAC remains the final sign-off.

Install: https://verapdf.org (installed to %LOCALAPPDATA%\\veraPDF by default).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass

import pikepdf
from pikepdf import Array, Dictionary, Name, String

CANDIDATES = [
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "veraPDF", "verapdf.bat"),
    r"C:\Program Files\veraPDF\verapdf.bat",
]


@dataclass
class Failure:
    clause: str
    test: int
    count: int
    description: str

    @property
    def rule(self) -> str:
        return f"{self.clause}-{self.test}"


def find_verapdf(configured: str | None = None) -> str | None:
    env = os.environ.get("VERAPDF_PATH")
    for p in ([configured] if configured else []) + ([env] if env else []) + CANDIDATES:
        if p and os.path.exists(p):
            return p
    return shutil.which("verapdf")


def run_verapdf(exe: str, pdf_path: str, timeout: int = 300) -> list[Failure]:
    """Return failed PDF/UA-1 rules (empty list = compliant)."""
    cmd = [exe, "--flavour", "ua1", "--format", "json", os.path.abspath(pdf_path)]
    if exe.lower().endswith(".bat"):
        cmd = ["cmd", "/c"] + cmd
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    data = json.loads(out.stdout[out.stdout.find("{"):])
    job = data["report"]["jobs"][0]
    vr = job.get("validationResult")
    if vr is None:
        raise RuntimeError(f"veraPDF could not validate the file: {job.get('taskException') or out.stderr[:300]}")
    vr = vr[0] if isinstance(vr, list) else vr
    return [Failure(r["clause"], int(r["testNumber"]), int(r["failedChecks"]), r["description"])
            for r in vr["details"].get("ruleSummaries", [])]


# ---------------------------------------------------------------- automatic fixes

def _fix_cidset(pdf) -> int:
    """7.21.4.2-2: an incomplete CIDSet is invalid; the stream is optional, so drop it."""
    n = 0
    for obj in pdf.objects:
        if isinstance(obj, Dictionary) and obj.get("/Type") == Name.FontDescriptor and "/CIDSet" in obj:
            del obj["/CIDSet"]
            n += 1
    return n


def _fix_charset(pdf) -> int:
    """7.21.4.2-1 (Type1 CharSet incomplete): the key is optional, drop it."""
    n = 0
    for obj in pdf.objects:
        if isinstance(obj, Dictionary) and obj.get("/Type") == Name.FontDescriptor and "/CharSet" in obj:
            del obj["/CharSet"]
            n += 1
    return n


def _fix_oc_names(pdf) -> int:
    """7.10-1: every optional-content (layer) configuration needs a /Name."""
    ocp = pdf.Root.get("/OCProperties")
    if not isinstance(ocp, Dictionary):
        return 0
    n = 0
    configs = [ocp.get("/D")] + list(ocp.get("/Configs") or [])
    for i, cfg in enumerate(configs):
        if isinstance(cfg, Dictionary) and not str(cfg.get("/Name", "")).strip():
            cfg.Name = String("Default" if i == 0 else f"Configuration {i}")
            n += 1
    return n


def _fix_display_title(pdf) -> int:
    """7.1-10: ViewerPreferences DisplayDocTitle must be true."""
    vp = pdf.Root.get("/ViewerPreferences")
    if not isinstance(vp, Dictionary):
        pdf.Root.ViewerPreferences = vp = Dictionary()
    if vp.get("/DisplayDocTitle") is not True:
        vp.DisplayDocTitle = True
        return 1
    return 0


def _fix_suspects(pdf) -> int:
    """7.1-11: MarkInfo /Suspects must not be true."""
    mi = pdf.Root.get("/MarkInfo")
    if isinstance(mi, Dictionary) and mi.get("/Suspects") is True:
        mi.Suspects = False
        return 1
    return 0


def _fix_pdfuaid(pdf) -> int:
    """5-1 / 5-2: XMP needs the PDF/UA identification schema with part 1."""
    with pdf.open_metadata(set_pikepdf_as_editor=False) as meta:
        if str(meta.get("pdfuaid:part", "")) != "1":
            meta["pdfuaid:part"] = "1"
            return 1
    return 0


FIXERS = {
    "7.21.4.2-2": ("removed incomplete CIDSet font data", _fix_cidset),
    "7.21.4.2-1": ("removed incomplete CharSet font data", _fix_charset),
    "7.10-1": ("named the optional-content (layer) configurations", _fix_oc_names),
    "7.1-10": ("set DisplayDocTitle", _fix_display_title),
    "7.1-11": ("cleared MarkInfo Suspects", _fix_suspects),
    "5-1": ("added the PDF/UA identifier", _fix_pdfuaid),
    "5-2": ("added the PDF/UA identifier", _fix_pdfuaid),
}


def validate_and_fix(pdf_path: str, exe: str, max_rounds: int = 3, log=None) -> tuple[list[Failure], list[str]]:
    """Validate; fix what we can in place; re-validate. Returns (remaining failures, fixes made)."""
    fixes_made: list[str] = []
    failures = run_verapdf(exe, pdf_path)
    for _ in range(max_rounds):
        fixable = [f for f in failures if f.rule in FIXERS]
        if not fixable:
            break
        changed = False
        with pikepdf.open(pdf_path, allow_overwriting_input=True) as pdf:
            for f in fixable:
                label, fixer = FIXERS[f.rule]
                n = fixer(pdf)
                if n:
                    fixes_made.append(f"{label} ({f.rule})")
                    changed = True
            if changed:
                pdf.save(pdf_path)
        if not changed:
            break
        failures = run_verapdf(exe, pdf_path)
    return failures, fixes_made


# Plain-language explanations for the rules people actually hit
EXPLAIN = {
    "7.1-3": "Content that's neither tagged nor marked as an artifact (usually pages AutoTag missed or a "
             "scanned page) - AutoTag/OCR it in Acrobat",
    "7.21.4.1-1": "A font that isn't embedded and has no Windows substitute - use Acrobat Preflight 'Embed fonts'",
    "7.18.1-2": "An annotation without alternate text",
    "7.18.5-1": "A link annotation that isn't inside a <Link> tag",
    "7.3-1": "A figure without alternate text",
    "7.2-34": "A heading level is skipped",
    "7.5-1": "A table header cell (TH) without Scope",
    "7.21.8-1": "Text uses a character its font doesn't contain (.notdef). When the font is Acrobat's "
                "OCR text layer (HiddenHorzOCR) this comes from Acrobat's OCR; PAC accepts it",
}
