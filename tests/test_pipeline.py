import json
from pathlib import Path

import pikepdf
import pytest

from remediator import config
from remediator.apply import apply_review
from remediator.pipeline import remediate
from remediator.structure import StructTree, as_list

from make_fixture import build


@pytest.fixture
def result(tmp_path):
    src = tmp_path / "doc.pdf"
    build(str(src))
    rep = remediate(str(src), config.load(), use_ai=False)
    return rep, tmp_path / "doc_accessible.pdf"


def types(pdf):
    t = StructTree(pdf)
    return t, [(n.depth, t.std_type(n.elem)) for n in t.build()]


def test_document_settings(result):
    _, out = result
    with pikepdf.open(out) as pdf:
        assert str(pdf.docinfo.Title) == "Annual Report"
        assert pdf.Root.ViewerPreferences.DisplayDocTitle is True
        assert str(pdf.Root.Lang) == "en-US"
        assert pdf.Root.MarkInfo.Marked is True
        assert pdf.pages[0].obj.Tabs == pikepdf.Name.S


def test_structure_fixes(result):
    _, out = result
    with pikepdf.open(out) as pdf:
        t, ts = types(pdf)
        names = [s for _, s in ts]
        assert "H3" not in names and names.count("H2") == 2      # H1 -> H3 skip fixed
        assert "LBody" in names                                  # LI got an LBody
        assert "TH" in names                                     # first row promoted
        assert "Link" in names
        # empty P removed: every remaining element has kids
        assert all(as_list(n.elem.get("/K")) for n in t.build())


def test_validation_clean_except_expected(result):
    rep, _ = result
    cats = {c for c, _, _ in rep.remaining}
    # alt text needs AI or a human; base-14 fonts can't be embedded here
    assert cats <= {"figures"}, rep.remaining


def test_untagged_line_became_artifact(result):
    _, out = result
    with pikepdf.open(out) as pdf:
        ops = [str(i.operator) for i in pikepdf.parse_content_stream(pdf.pages[0])]
        i = ops.index("m")
        assert ops[i - 1] == "BMC"


def test_apply_review(result, tmp_path):
    _, out = result
    review_path = tmp_path / "doc_accessible_review.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    for item in review["items"]:
        if item["field"] == "alt":
            item["value"], item["decision"] = "Blue rectangle placeholder chart", "accept"
        if item["field"] == "promote":
            item["decision"] = "accept"
    review_path.write_text(json.dumps(review), encoding="utf-8")
    apply_review(str(out), str(review_path))
    with pikepdf.open(out) as pdf:
        t = StructTree(pdf)
        fig = next(n.elem for n in t.build() if t.std_type(n.elem) == "Figure")
        assert str(fig.Alt) == "Blue rectangle placeholder chart"
        assert sum(1 for n in t.build() if t.std_type(n.elem) == "H2") == 3


def test_idempotent(result, tmp_path):
    """Running the tool on its own output shouldn't change the structure again."""
    _, out = result
    rep2 = remediate(str(out), config.load(), out=str(tmp_path / "again.pdf"), use_ai=False)
    assert not [i for i in rep2.items if i.category == "headings" and "->" in i.message and "Possible" not in i.message]
    assert not [i for i in rep2.items if i.category == "links" and i.severity == "fixed"]


def test_bookmarks_follow_headings(result):
    _, out = result
    with pikepdf.open(out) as pdf, pdf.open_outline() as outline:
        top = outline.root
        assert [i.title for i in top] == ["Annual Report"]
        assert [c.title for c in top[0].children] == ["Introduction", "Summary"]
        assert top[0].children[1].destination[0].objgen == pdf.pages[1].obj.objgen  # jumps to page 2


@pytest.mark.skipif(not __import__("os").path.exists(r"C:\Windows\Fonts\arial.ttf"), reason="needs Windows fonts")
def test_standard_fonts_get_embedded(result):
    rep, out = result
    assert not [c for c, _, _ in rep.remaining if c == "fonts"]
    with pikepdf.open(out) as pdf:
        fonts = pdf.pages[0].obj.Resources.Font
        for _, f in fonts.items():
            assert "/FontFile2" in f.FontDescriptor
            assert "Arial" in str(f.BaseFont)
            assert "/ToUnicode" in f
    import pymupdf
    assert "Annual Report" in pymupdf.open(out)[0].get_text()


def test_non_link_annotations_get_alt_text(tmp_path):
    """PAC: every annotation needs Contents, not just links."""
    src = tmp_path / "att.pdf"
    build(str(src))
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0].obj
        att = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.FileAttachment, Rect=[300, 700, 320, 720],
            FS=pikepdf.Dictionary(Type=pikepdf.Name.Filespec, UF=pikepdf.String("budget.xlsx"))))
        page.Annots.append(att)
        pdf.save(src)
    rep = remediate(str(src), config.load(), use_ai=False)
    with pikepdf.open(tmp_path / "att_accessible.pdf") as pdf:
        contents = [str(a.get("/Contents", "")) for a in pdf.pages[0].obj.Annots]
    assert "Attached file: budget.xlsx" in contents
    assert not [m for c, m, _ in rep.remaining if c == "annotations"]


def test_app_name_title_is_replaced_and_xmp_gets_title(tmp_path):
    """'BoardDocs Plus' is the app a page was printed from, not a title; and a kept
    title must also be in XMP dc:title (PDF/UA 7.1-9)."""
    src = tmp_path / "board.pdf"
    build(str(src))
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        pdf.docinfo.Title = pikepdf.String("BoardDocs® Plus")
        pdf.save(src)
    remediate(str(src), config.load(), use_ai=False)
    with pikepdf.open(tmp_path / "board_accessible.pdf") as pdf:
        assert str(pdf.docinfo.Title) == "Annual Report"
        with pdf.open_metadata() as meta:
            assert meta.get("dc:title") == "Annual Report"

    good = tmp_path / "good.pdf"
    build(str(good))
    with pikepdf.open(good, allow_overwriting_input=True) as pdf:
        pdf.docinfo.Title = pikepdf.String("Transit Committee Agenda")
        pdf.save(good)
    remediate(str(good), config.load(), use_ai=False)
    with pikepdf.open(tmp_path / "good_accessible.pdf") as pdf, pdf.open_metadata() as meta:
        assert meta.get("dc:title") == "Transit Committee Agenda"
