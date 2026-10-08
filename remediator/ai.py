"""Claude-powered decisions: figure alt text and heading levels.

Claude only *decides*; the code in fixes/ makes the actual tag-tree edits.
Past human corrections (learning.py) are included as examples so the output
drifts toward how you remediate.

Credentials: the key saved by `remediate.py setup`, else the SDK's own lookup
(ANTHROPIC_API_KEY, or a profile from `ant auth login`).
"""

from __future__ import annotations

import base64
import json
import re
from typing import Literal

from pydantic import BaseModel, Field

from . import learning

ALT_SYSTEM = """You write alternative text for figures in PDF documents that are being \
remediated for accessibility (WCAG 2.1 AA / PDF/UA). You are shown the whole page for \
context and a crop of the figure itself.

Rules:
- Describe the information the figure conveys in context, not its appearance for its own sake.
- Charts/graphs: state the chart type, what is measured, and the key trend or values.
- Logos: "<Organization> logo". Photos: the relevant subject in one sentence.
- Do not start with "Image of" / "Picture of". No more than ~250 characters unless the \
figure is a complex chart that genuinely needs more.
- If the figure repeats text that already appears next to it, or is purely decorative \
(borders, outline boxes around text, flourishes, spacer shapes, background textures), \
mark it decorative. A small logo or brand mark in a page corner, header or footer \
(not the letterhead identifying who wrote the document) is decorative too.
- If a caption is provided, don't just repeat the caption; complement it."""

HEADING_SYSTEM = """You assign heading levels for a PDF being remediated for accessibility \
(PDF/UA, WCAG 1.3.1). You get the document's candidate headings in reading order, with \
font size, boldness, page, the tag they have now, and a size-based guess.

Decide the correct level for each: 1-6, or 0 if it is not actually a heading (e.g. a \
pull quote, a label, a bold sentence in body text). Use every cue a human remediator \
would: numbering (1, 1.1, 1.1.1; A., B.; Chapter/Section/Appendix), visual size and \
weight, what the text says, and document logic. There should normally be one H1 (the \
document title or first main heading). Levels must not skip (H2 -> H4). Repeated running \
headers or slide titles that are the same size are the same level."""


TITLE_SYSTEM = """You write the document title for a PDF being remediated for accessibility. It goes in the PDF metadata, is shown in the title bar, and is the first thing a screen reader announces, so it must tell the user what this document is.

Rules:
- Use the document's own words where possible (its heading, subject line, memo subject).
- Concise: usually under 80 characters. No file names, extensions, dates-only, or "PDF".
- Never use boilerplate as the title (email warning banners, "Page 1", letterhead addresses).
- For agenda/meeting packet items, include the item or tab number and the subject, e.g. "Tab 75: Late Letters on the Coronado Vertical Cantilever Project".
- For correspondence packets, say what the letters are about."""


TABLE_SYSTEM = """You write the /Summary for a data table in a PDF being remediated for \
accessibility. Screen readers read it before the table.

Rules:
- One or two sentences: what the table shows and what it's for, in the document's own terms.
- Mention structure only when it's non-obvious: grouped or spanning column headers, row \
headers, totals rows, multiple header rows.
- Never state the number of rows or columns - screen readers already announce that.
- Don't list every column; don't start with "This table" or "Table of"; no markup.
- Under 250 characters."""


class TableSummary(BaseModel):
    summary: str


LEGACY_SUMMARY = re.compile(r"^Table with \d+ rows? and \d+ columns?", re.I)


def _fmt_summary(r: dict) -> str:
    # the old size-only template was accepted by default in bulk; it isn't a style to copy
    if r["outcome"] == "accepted" and LEGACY_SUMMARY.match(str(r.get("final_value", ""))):
        return ""
    if r["outcome"] == "accepted":
        return f"summary approved as-is: {r['final_value']!r}"
    if r["outcome"] == "rejected":
        return f"summary {r['ai_value']!r} was rejected"
    return f"tool wrote {r['ai_value']!r}; specialist changed it to {r['final_value']!r}"


class TitleSuggestion(BaseModel):
    title: str
    reason: str = Field(description="Short justification, under 15 words")


class AIUnavailable(RuntimeError):
    """Credentials missing/invalid: stop calling the API for the rest of the run."""


class AltText(BaseModel):
    decorative: bool = Field(description="True if the figure conveys no information")
    alt_text: str = Field(description="Alternative text; empty string if decorative")
    confidence: Literal["high", "medium", "low"]
    figure_kind: Literal["photo", "chart", "diagram", "logo", "icon", "map", "equation",
                         "screenshot", "text_as_image", "decorative", "other"]


class HeadingDecision(BaseModel):
    index: int
    level: int = Field(description="1-6, or 0 if not a heading")
    reason: str = Field(description="Short justification, under 15 words")


class HeadingPlan(BaseModel):
    decisions: list[HeadingDecision]


def _examples_block(field: str, fmt) -> str:
    # fmt returns "" for records that shouldn't teach anything
    lines = [l for l in (fmt(r) for r in learning.examples(field, limit=60)) if l][:8]
    if not lines:
        return ""
    return ("\n\nDecisions previously made by the accessibility specialist you are assisting. "
            "Follow the same judgment:\n" + "\n".join(f"- {l}" for l in lines if l))


def _fmt_alt(r: dict) -> str:
    ctx = r.get("context", {})
    kind = ctx.get("figure_kind", "figure")
    if r["outcome"] == "accepted":
        return f"[{kind}] AI alt text approved as-is: {r['final_value']!r}"
    if r["outcome"] == "rejected":
        return f"[{kind}] AI proposed {r['ai_value']!r}; specialist rejected it"
    if not r.get("ai_value"):
        near = ctx.get("nearby_text", "")[:100]
        return f"[figure near {near!r}] specialist wrote {r['final_value']!r}"
    return f"[{kind}] AI proposed {r['ai_value']!r}; specialist changed it to {r['final_value']!r}"


def _fmt_heading(r: dict) -> str:
    ctx = r.get("context", {})
    text = ctx.get("text", "")[:80]
    size = ctx.get("size")
    if r["outcome"] == "accepted":
        return f"{text!r} (size {size}) -> H{r['final_value']} confirmed"
    return f"{text!r} (size {size}): tool said {r['ai_value']}, specialist chose {r['final_value']}"


def _fmt_title(r: dict) -> str:
    if r["outcome"] == "accepted":
        return f"title approved as-is: {r['final_value']!r}"
    return f"tool proposed {r['ai_value']!r}; specialist chose {r['final_value']!r}"


class AltTextGenerator:
    """Named for its first job; also decides heading levels."""

    def __init__(self, model: str, use_examples: bool = True):
        import anthropic  # imported lazily so the tool runs without the SDK

        from . import credentials
        self.anthropic = anthropic
        key = credentials.get("anthropic_api_key")
        self.client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
        self.model = model
        self.use_examples = use_examples

    def _parse(self, system: str, content: list, schema):
        try:
            response = self.client.messages.parse(
                model=self.model,
                max_tokens=8000,
                system=system,
                messages=[{"role": "user", "content": content}],
                output_format=schema,
                # if the model declines, the API retries on a fallback model in the same call
                extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
                extra_body={"fallbacks": "default"},
            )
        except (self.anthropic.AuthenticationError, self.anthropic.PermissionDeniedError) as e:
            raise AIUnavailable(f"Claude API key rejected ({e.status_code}) - run `python remediate.py setup`") from e
        except self.anthropic.APIStatusError as e:
            if "credit balance" in str(e.message).lower():
                raise AIUnavailable("Claude API credits are used up - add credits at console.anthropic.com "
                                    "(Plans & Billing). Alt text, titles and heading review were skipped") from e
            raise RuntimeError(f"Claude API error {e.status_code}: {e.message}") from e
        if response.stop_reason == "refusal":
            return None
        return response.parsed_output

    def describe(self, page_png: bytes, crop_png: bytes, nearby_text: str) -> AltText | None:
        def img(data: bytes) -> dict:
            return {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                "data": base64.standard_b64encode(data).decode()}}

        prompt = "Figure crop is the second image."
        if nearby_text:
            prompt += f"\n\nText immediately around the figure (may include its caption):\n{nearby_text[:1500]}"
        system = ALT_SYSTEM + (_examples_block("alt", _fmt_alt) if self.use_examples else "")
        return self._parse(system, [img(page_png), img(crop_png), {"type": "text", "text": prompt}], AltText)

    def suggest_title(self, first_pages_text: str, headings: list[str], filename: str) -> str | None:
        system = TITLE_SYSTEM + (_examples_block("title", _fmt_title) if self.use_examples else "")
        text = (f"File name: {filename}\nHeadings: {json.dumps(headings[:15], ensure_ascii=False)}\n\n"
                f"Text of the first pages:\n{first_pages_text[:3000]}")
        out = self._parse(system, [{"type": "text", "text": text}], TitleSuggestion)
        title = " ".join(out.title.split()) if out else ""
        return title or None

    def summarize_table(self, headers: list[list[str]], sample_rows: list[list[str]], context: str,
                        n_rows: int, n_cols: int) -> str | None:
        system = TABLE_SYSTEM + (_examples_block("summary", _fmt_summary) if self.use_examples else "")
        text = (f"Text just before the table (heading/caption): {context[:600] or '(none)'}\n"
                f"Header rows: {json.dumps(headers, ensure_ascii=False)}\n"
                f"First data rows: {json.dumps(sample_rows, ensure_ascii=False)}\n"
                f"(Size, for your judgment only - don't mention it: {n_rows} rows x {n_cols} columns)")
        out = self._parse(system, [{"type": "text", "text": text}], TableSummary)
        summary = " ".join(out.summary.split()) if out else ""
        return summary[:300] or None

    def classify_headings(self, entries: list[dict], body_size: float | None) -> dict[int, HeadingDecision] | None:
        system = HEADING_SYSTEM + (_examples_block("level", _fmt_heading) if self.use_examples else "")
        text = (f"Body text size: {body_size}\n\nCandidates (JSON, reading order):\n"
                + json.dumps(entries, ensure_ascii=False, indent=0)
                + "\n\nReturn one decision per index.")
        plan = self._parse(system, [{"type": "text", "text": text}], HeadingPlan)
        if plan is None:
            return None
        return {d.index: d for d in plan.decisions}
