"""Apply the decisions from a *_review.json file to the remediated PDF.

Workflow: run -> open *_review.json, flip decisions / type alt text -> apply.
"""

from __future__ import annotations

import json
from pathlib import Path

import pikepdf
from pikepdf import String

from . import learning
from .structure import StructTree


def apply_review(pdf_path: str, review_path: str, out: str | None = None, learn: bool = True) -> list[str]:
    with open(review_path, encoding="utf-8") as f:
        review = json.load(f)
    pdf = pikepdf.open(pdf_path, allow_overwriting_input=out is None)
    tree = StructTree(pdf)
    by_path = {n.path: n.elem for n in tree.build()}
    log = []

    for item in review["items"]:
        decision = item.get("decision", "accept")
        if decision == "skip":
            continue
        accept = decision == "accept"
        field = item.get("field")
        value = item.get("value") if accept else item.get("old")
        was_applied = item.get("applied", True)
        if field == "title":
            if value:
                pdf.docinfo.Title = String(value)
                with pdf.open_metadata(set_pikepdf_as_editor=False) as meta:
                    meta["dc:title"] = value
                log.append(f"title = {value!r}")
            continue
        elem = by_path.get(item.get("path"))
        if elem is None:
            log.append(f"SKIP {item['id']}: tag {item.get('path')} not found (was the PDF edited?)")
            continue
        if field == "alt":
            if value:
                elem.Alt = String(value)
                log.append(f"{item['id']}: alt = {value!r}")
            elif "/Alt" in elem and not accept:
                del elem["/Alt"]
                log.append(f"{item['id']}: alt removed")
        elif field == "summary":
            if value:
                tree.set_attr(elem, "/Table", "/Summary", String(value))
                log.append(f"{item['id']}: summary = {value!r}")
        elif field == "level":
            if not accept and not was_applied:
                continue  # rejected suggestion: nothing was changed
            lvl = int(value)
            tree.set_type(elem, f"H{lvl}")
            log.append(f"{item['id']}: -> H{lvl}")
        elif field == "promote":
            if accept or was_applied:
                tree.set_type(elem, value if accept else "P")
                log.append(f"{item['id']}: -> {value if accept else 'P'}")
        elif field == "demote":
            if accept:
                tree.set_type(elem, "P")
                log.append(f"{item['id']}: heading -> P")

    pdf.save(out or pdf_path)
    pdf.close()
    if learn:
        n = learning.record_review(review, Path(pdf_path).name)
        log.append(f"recorded {n} decision(s) to {learning.data_dir()}")
    return log
