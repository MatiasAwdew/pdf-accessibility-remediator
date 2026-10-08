"""Collects everything the pipeline did or couldn't do, and renders it."""

from __future__ import annotations

import html
import json
from dataclasses import dataclass, field, fields
from typing import Any

# fixed   - changed automatically, nothing to do
# review  - changed or proposed, but a human should confirm (AI alt text, heading guesses)
# error   - still a problem in the output; needs manual work in Acrobat
# info    - FYI
SEVERITIES = ("error", "review", "fixed", "info")


@dataclass
class Item:
    category: str
    severity: str
    message: str
    page: int | None = None  # 1-based for humans
    path: str | None = None  # structure-tree path, filled in before saving
    proposal: dict[str, Any] | None = None
    id: str = ""
    elem: Any = field(default=None, repr=False)  # live pikepdf object, not serialized


class Report:
    def __init__(self, source: str):
        self.source = source
        self.output: str | None = None
        self.items: list[Item] = []
        self.pages = 0
        self.remaining: list[tuple[str, str, int | None]] = []  # final validation failures
        self.verapdf_compliant: bool | None = None  # None = veraPDF not run

    def add(self, category, severity, message, page=None, elem=None, proposal=None) -> Item:
        item = Item(category, severity, message,
                    page=None if page is None else page + 1, proposal=proposal, elem=elem)
        self.items.append(item)
        return item

    def fixed(self, category, message, **kw):
        return self.add(category, "fixed", message, **kw)

    def review(self, category, message, **kw):
        return self.add(category, "review", message, **kw)

    def error(self, category, message, **kw):
        return self.add(category, "error", message, **kw)

    def info(self, category, message, **kw):
        return self.add(category, "info", message, **kw)

    def resolve_paths(self, nodes) -> None:
        by_obj = {n.elem.objgen: n.path for n in nodes if n.elem.is_indirect}
        for i, item in enumerate(self.items):
            item.id = f"{item.category}-{i}"
            if item.elem is not None and getattr(item.elem, "is_indirect", False):
                item.path = by_obj.get(item.elem.objgen)

    def counts(self) -> dict[str, int]:
        return {s: sum(1 for i in self.items if i.severity == s) for s in SEVERITIES}

    # ---------- output ----------

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "output": self.output,
            "pages": self.pages,
            "summary": {**self.counts(), "validation_failures": len(self.remaining),
                        "pdfua_verapdf": self.verapdf_compliant},
            "remaining": [{"category": c, "message": m, "page": None if p is None else p + 1}
                          for c, m, p in self.remaining],
            "items": [{f.name: getattr(i, f.name) for f in fields(i) if f.name != "elem"} for i in self.items],
        }

    def write_json(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, default=str)

    def write_review(self, path: str) -> int:
        """Editable file of just the decisions a human should make. Returns item count."""
        items = []
        for i in self.items:
            if not i.proposal or not (i.path or i.proposal.get("field") == "title"):
                continue
            applied = i.proposal.get("applied", True)
            items.append({
                "id": i.id, "page": i.page, "path": i.path, "category": i.category,
                "message": i.message, "ai_value": i.proposal.get("value"), **i.proposal,
                # accept: keep/apply "value"   reject: undo / say no   skip: leave as-is, don't learn
                # to use your own text, edit "value" and set decision "accept"
                "decision": "accept" if applied and i.proposal.get("value") else "skip",
            })
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"pdf": self.output, "items": items}, f, indent=2, ensure_ascii=False)
        return len(items)

    def summary_text(self) -> str:
        c = self.counts()
        lines = [
            f"{self.source}  ({self.pages} pages)",
            f"  fixed automatically : {c['fixed']}",
            f"  needs your review   : {c['review']}",
            f"  manual work needed  : {c['error']}",
            f"  validation failures : {len(self.remaining)}",
            "  PDF/UA-1 (veraPDF)  : " + {True: "PASS", False: "FAIL", None: "not checked"}[self.verapdf_compliant],
        ]
        for sev in ("error", "review"):
            for i in [x for x in self.items if x.severity == sev][:25]:
                where = f"p.{i.page} " if i.page else ""
                lines.append(f"  [{sev.upper():6}] {where}{i.category}: {i.message}")
        if self.remaining:
            lines.append("  Remaining validation failures:")
            for cat, msg, page in self.remaining[:40]:
                where = f"p.{page + 1} " if page is not None else ""
                lines.append(f"    - {where}{cat}: {msg}")
        return "\n".join(lines)

    def write_html(self, path: str) -> None:
        c = self.counts()
        rows = []
        order = {s: n for n, s in enumerate(SEVERITIES)}
        for i in sorted(self.items, key=lambda x: (order[x.severity], x.page or 0)):
            detail = ""
            if i.proposal:
                detail = html.escape(json.dumps(i.proposal, ensure_ascii=False))
            rows.append(
                f"<tr class='{i.severity}'><td>{i.severity}</td><td>{i.page or ''}</td>"
                f"<td>{html.escape(i.category)}</td><td>{html.escape(i.message)}</td>"
                f"<td><code>{detail}</code></td></tr>"
            )
        doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Remediation report</title>
<style>
body{{font-family:system-ui,sans-serif;margin:2rem;color:#1a1a1a;background:#fff}}
table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #ccc;padding:.4rem;text-align:left;vertical-align:top}}
tr.error td:first-child{{background:#fde2e1}}tr.review td:first-child{{background:#fff4ce}}
tr.fixed td:first-child{{background:#dff6dd}}code{{font-size:.8rem;white-space:pre-wrap}}
</style></head><body>
<h1>Remediation report</h1>
<p><strong>Source:</strong> {html.escape(self.source)}<br><strong>Output:</strong> {html.escape(self.output or '')}
<br><strong>Pages:</strong> {self.pages}</p>
<ul><li>Fixed automatically: {c['fixed']}</li><li>Needs review: {c['review']}</li>
<li>Manual work needed: {c['error']}</li><li>Validation failures: {len(self.remaining)}</li></ul>
<h2>Remaining validation failures</h2>
<ul>{''.join(f"<li>{'p.' + str(p + 1) + ' ' if p is not None else ''}{html.escape(c_)}: {html.escape(m)}</li>" for c_, m, p in self.remaining) or '<li>None</li>'}</ul>
<h2>Everything the tool did</h2>
<table><caption>All items</caption><thead><tr><th scope="col">Status</th><th scope="col">Page</th>
<th scope="col">Category</th><th scope="col">Message</th><th scope="col">Proposal</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></body></html>"""
        with open(path, "w", encoding="utf-8") as f:
            f.write(doc)
