"""Learn-from-corrections loop, AI heading review, and eval scoring."""

import json

import pikepdf

from remediator import ai, config, learning
from remediator.ai import AltText, HeadingDecision
from remediator.apply import apply_review
from remediator.evaluate import compare, run_eval, snapshot
from remediator.pipeline import remediate
from remediator.structure import StructTree

from make_fixture import build


def _run(tmp_path, gen=None, monkeypatch=None):
    src = tmp_path / "doc.pdf"
    build(str(src))
    if gen is not None:
        monkeypatch.setattr("remediator.pipeline.make_ai", lambda cfg, enabled: gen)
    rep = remediate(str(src), config.load(), use_ai=gen is not None)
    return rep, tmp_path / "doc_accessible.pdf", tmp_path / "doc_accessible_review.json"


class FakeClaude:
    def __init__(self, heading_levels=None):
        self.heading_levels = heading_levels or {}
        self.entries = None

    def describe(self, page_png, crop_png, nearby):
        return AltText(decorative=False, alt_text="Blue box", confidence="medium", figure_kind="other")

    def classify_headings(self, entries, body_size):
        self.entries = entries
        return {e["index"]: HeadingDecision(index=e["index"], level=self.heading_levels.get(e["text"], 2),
                                            reason="test") for e in entries}


def test_decisions_are_recorded_with_outcomes(tmp_path, monkeypatch):
    _, out, review_path = _run(tmp_path, FakeClaude({"Annual Report": 1, "Results": 2}), monkeypatch)
    review = json.loads(review_path.read_text(encoding="utf-8"))
    for item in review["items"]:
        if item["field"] == "alt":
            item["value"] = "Blue rectangle chart placeholder"          # edited
        elif item["field"] == "summary":
            item["decision"] = "reject"                                  # rejected
    review_path.write_text(json.dumps(review), encoding="utf-8")
    apply_review(str(out), str(review_path))

    rows = [json.loads(l) for l in (learning.data_dir() / "corrections.jsonl").read_text(encoding="utf-8").splitlines()]
    by_field = {r["field"]: r for r in rows}
    assert by_field["alt"]["outcome"] == "edited"
    assert by_field["alt"]["ai_value"] == "Blue box"
    assert by_field["alt"]["final_value"] == "Blue rectangle chart placeholder"
    assert by_field["alt"]["context"]["figure_kind"] == "other"
    assert by_field["summary"]["outcome"] == "rejected"
    assert by_field["level"]["outcome"] == "accepted" and by_field["level"]["context"]["text"]
    assert "promote" not in by_field  # missed heading is only suggested; left at "skip"
    assert learning.stats()["figures/alt"]["edited"] == 1


def test_no_learn_records_nothing(tmp_path):
    _, out, review_path = _run(tmp_path)
    apply_review(str(out), str(review_path), learn=False)
    assert not (learning.data_dir() / "corrections.jsonl").exists()


def test_corrections_become_prompt_examples(tmp_path, monkeypatch):
    learning._append("corrections.jsonl", [{
        "field": "alt", "outcome": "edited", "ai_value": "Image of a logo",
        "final_value": "Agency logo", "context": {"figure_kind": "logo"}}])
    block = ai._examples_block("alt", ai._fmt_alt)
    assert "Agency logo" in block and "Image of a logo" in block


def test_ai_level_changes_on_valid_outline_are_suggestions(tmp_path, monkeypatch):
    # The fixture's outline is valid (H1/H3/H2 -> fixed to H1/H2/H2). Claude wanting
    # "Summary" as H3 must be a suggestion, not an edit: on the PMC benchmark Claude
    # overrode correct publisher levels.
    fake = FakeClaude({"Annual Report": 1, "Introduction": 2, "Results": 2, "Summary": 3})
    rep, out, _ = _run(tmp_path, fake, monkeypatch)
    texts = [e["text"] for e in fake.entries]
    assert texts[:2] == ["Annual Report", "Introduction"]      # reading order
    assert "Results" in texts                                   # paragraph candidate included
    with pikepdf.open(out) as pdf:
        t = StructTree(pdf)
        levels = [t.std_type(n.elem) for n in t.build() if t.std_type(n.elem).startswith("H")]
        assert levels == ["H1", "H2", "H2"]  # "Results" only suggested by default
    assert any('"Summary": Claude suggests H3' in i.message and i.proposal["applied"] is False
               for i in rep.items)
    assert any('Possible missed heading "Results" -> suggest H2' in i.message for i in rep.items)


def test_ai_can_reject_paragraph_candidate(tmp_path, monkeypatch):
    fake = FakeClaude({"Annual Report": 1, "Introduction": 2, "Results": 0, "Summary": 2})
    rep, _, _ = _run(tmp_path, fake, monkeypatch)
    assert not any("Possible missed heading" in i.message for i in rep.items)


def test_eval_against_gold(tmp_path):
    src = tmp_path / "orig.pdf"
    build(str(src))
    # "gold" = what a specialist would have produced from the same file
    gold = tmp_path / "gold.pdf"
    remediate(str(src), config.load(), out=str(tmp_path / "tmp.pdf"), use_ai=False)
    with pikepdf.open(tmp_path / "tmp.pdf") as pdf:
        t = StructTree(pdf)
        for n in t.build():
            if t.std_type(n.elem) == "Figure":
                n.elem.Alt = pikepdf.String("Chart")
        pdf.save(gold)
    result = run_eval(str(src), str(gold), config.load(), use_ai=False, remediate_fn=remediate)
    assert result["headings"]["level_accuracy_pct"] == 100.0
    assert result["title_match"] and result["lang_match"]
    assert result["figures"]["tool_alt_coverage_pct"] == 0.0
    assert learning.eval_history()[-1]["gold"] == "gold.pdf"


def test_compare_flags_wrong_levels():
    tool = {"title": "A", "lang": "en", "headings": [("intro", 3), ("results", 2)], "figures": [],
            "tables": [], "links": 0, "lists": 0}
    gold = dict(tool, headings=[("intro", 2), ("results", 2), ("summary", 2)])
    r = compare(tool, gold)
    assert r["headings"]["level_accuracy_pct"] == 50.0
    assert r["headings"]["missed"] == ["summary"]
    assert r["headings"]["wrong_level"][0]["gold"] == "H2"


def test_alt_text_off_still_reviews_headings(tmp_path, monkeypatch):
    """Turning off alt text must not turn off Claude's heading review."""
    fake = FakeClaude({"Annual Report": 1})
    calls = []
    fake.describe = lambda *a: calls.append("alt")
    monkeypatch.setattr("remediator.pipeline.make_ai", lambda cfg, enabled: fake)
    src = tmp_path / "doc.pdf"
    build(str(src))
    cfg = config.load()
    cfg["figures"]["ai_alt_text"] = False
    remediate(str(src), cfg)
    assert fake.entries is not None and calls == []


def test_auto_promote_opt_in(tmp_path, monkeypatch):
    """With auto_promote_when_ai_agrees on, larger text Claude agrees on is tagged."""
    fake = FakeClaude({"Annual Report": 1, "Introduction": 2, "Results": 2, "Summary": 2})
    monkeypatch.setattr("remediator.pipeline.make_ai", lambda cfg, enabled: fake)
    src = tmp_path / "doc.pdf"
    build(str(src))
    cfg = config.load()
    cfg["headings"]["auto_promote_when_ai_agrees"] = True
    rep = remediate(str(src), cfg)
    assert any('Tagged "Results" as H2' in i.message for i in rep.items)


def test_claude_title_replaces_bad_title(tmp_path, monkeypatch):
    fake = FakeClaude({"Annual Report": 1})
    seen = {}

    def suggest_title(text, headings, filename):
        seen.update(text=text, headings=headings, filename=filename)
        return "Annual Report 2026: Results and Summary"
    fake.suggest_title = suggest_title
    monkeypatch.setattr("remediator.pipeline.make_ai", lambda cfg, enabled: fake)
    src = tmp_path / "doc.pdf"
    build(str(src))  # fixture has no title
    remediate(str(src), config.load())
    with pikepdf.open(tmp_path / "doc_accessible.pdf") as pdf:
        assert str(pdf.docinfo.Title) == "Annual Report 2026: Results and Summary"
    assert "Annual Report" in seen["text"] and seen["filename"] == "doc.pdf"


def test_out_of_credits_stops_all_claude_calls(tmp_path, monkeypatch):
    """One clear message, then no more calls, when API credits run out."""
    calls = []

    class Broke(FakeClaude):
        def describe(self, *a):
            calls.append("alt")
            raise ai.AIUnavailable("Claude API credits are used up")

        def classify_headings(self, *a):
            calls.append("headings")
            raise ai.AIUnavailable("Claude API credits are used up")

        def summarize_table(self, *a):
            calls.append("table")
            raise ai.AIUnavailable("Claude API credits are used up")

    monkeypatch.setattr("remediator.pipeline.make_ai", lambda cfg, enabled: Broke())
    src = tmp_path / "doc.pdf"
    build(str(src))
    rep = remediate(str(src), config.load())
    assert calls == ["headings"]  # first failure disables Claude for the rest of the run
    assert sum("credits are used up" in i.message for i in rep.items) == 1
