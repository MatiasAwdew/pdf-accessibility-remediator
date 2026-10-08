"""Settings. Defaults below; override with a remediator.toml (see README)."""

from __future__ import annotations

import copy
import tomllib
from pathlib import Path

DEFAULTS = {
    "language": "en-US",
    "pdfua_identifier": True,
    "autotag": {
        # Adobe PDF Services AutoTag for untagged PDFs (needs `remediate.py setup`)
        "enabled": True,
        "retag_tagged": False,      # true = re-tag even PDFs that already have tags
        "untagged_text_threshold": 0.5,
        "reuse_previous": True,
        "local": True,              # tag untagged PDFs / leftover content locally when Adobe isn't used     # reuse <name>_autotagged.pdf from an earlier run (saves Adobe quota)  # re-tag when more than this share of text is untagged
        "shift_headings": False,    # Adobe option: shift heading levels so the doc starts at H1
        "save_adobe_report": False, # also download Adobe's Excel tagging report
        "keep_intermediate": True,  # keep <name>_autotagged.pdf for comparison
        "region": "default",        # default | us | eu
        "timeout_seconds": 600,
    },
    "headings": {
        # ai: font-size ranking, then Claude reviews the outline (falls back to fontsize)
        # fontsize: re-level headings by visual size, then remove skipped levels
        # normalize: keep autotag's levels, only remove skipped levels
        # off: leave headings alone
        "mode": "ai",
        "first_heading_h1": True,
        "title_as_only_h1": True,             # <Title>/CHAP_TITLE becomes H1, sections shift to H2
        "always_rank_by_size": False,        # true = re-level even a valid outline by font size
        "auto_promote_when_ai_agrees": False, # true = tag missed headings when heuristic + Claude agree (else: suggest)
        "auto_promote_min_size_ratio": 1.15, # ...and only if the text is this much larger than body text
        "suggest_paragraph_headings": True,
    },
    "figures": {
        "ai_alt_text": True,
        "decorative_max_area": 400.0,  # pt^2; smaller figures are decorative
        "artifact_decorative": True,   # change decorative figures/images to Artifacts (not just flag them)
        "small_image_page_fraction": 0.015,  # untagged images under 1.5% of the page are decorative
        "retag_text_figures": True,    # "figures" that contain only text become readable text again
    },
    "fonts": {"embed_missing": True},  # embed Times/Helvetica/Courier from Windows fonts
    "bookmarks": {
        "enabled": True,
        "min_pages": 2,             # Acrobat's checker requires them from 21 pages; most remediators add them sooner
        "max_level": 3,             # bookmark H1-H3 only; deeper levels clutter the panel
        "replace_existing": False,  # true = rebuild even if the PDF already has bookmarks
    },
    "links": {"wrap_untagged_annotations": True, "fill_contents": True},
    "tables": {
        "promote_first_row_headers": True,
        "data_row_headers": "demote",  # demote: only row 1 is headers (AutoTag over-uses TH) | keep
        "pad_short_rows": True,        # add empty TD cells so every row has the same column count
        "fill_scope": True,
        "add_summary": True,
    },
    "cleanup": {
        "remove_empty_elements": True,
        "ensure_document_root": True,
        "fix_list_structure": True,
        "artifact_untagged_paths": True,
    },
    "learning": {
        "record_decisions": True,   # log review decisions (apply) to the local dataset
        "use_as_examples": True,    # show past decisions to Claude as examples
    },
    "validate": {
        "verapdf": True,       # validate the result against PDF/UA-1 (PAC's standard) and auto-fix
        "verapdf_path": None,  # default: %LOCALAPPDATA%\veraPDF\verapdf.bat
        "max_rounds": 3,
    },
    "ai": {
        "model": "claude-opus-5",
        "max_figures": 200,
    },
}


def _merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load(path: str | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    candidates = [Path(path)] if path else [Path.cwd() / "remediator.toml",
                                            Path(__file__).resolve().parent.parent / "remediator.toml"]
    for p in candidates:
        if p.is_file():
            with open(p, "rb") as f:
                _merge(cfg, tomllib.load(f))
            break
    return cfg
