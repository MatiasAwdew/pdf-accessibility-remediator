"""Local auto-tagging of untagged PDFs (no Acrobat / Adobe)."""

import pikepdf
import pytest
from pikepdf import Dictionary, Name

from remediator import config
from remediator.content import ContentIndex
from remediator.pipeline import remediate
from remediator.structure import StructTree


def _untagged(path):
    """A born-digital, untagged report: title, paragraphs, bullets, a ruled table."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(72, 720, "Quarterly Transit Report")
    c.setFont("Helvetica", 11)
    y = 690
    for line in ["Ridership rose across all regions this quarter, led by weekday",
                 "commuter service and expanded evening routes in the valley."]:
        c.drawString(72, y, line)
        y -= 14
    y -= 16
    for item in ["• Weekday ridership up 8%", "• Evening routes added in two cities",
                 "• On-time performance at 94%"]:
        c.drawString(90, y, item)
        y -= 16
    # ruled 3x3 table
    top, left, w, h = y - 20, 72, 150, 20
    rows = [["Region", "Riders", "Change"], ["North", "12,400", "+6%"], ["South", "9,800", "+11%"]]
    for r, row in enumerate(rows):
        for col, text in enumerate(row):
            x0, y0 = left + col * w, top - (r + 1) * h
            c.rect(x0, y0, w, h)
            c.drawString(x0 + 4, y0 + 6, text)
    c.setFont("Helvetica", 9)
    c.drawString(300, 30, "1")  # page number
    c.save()


@pytest.fixture
def tagged(tmp_path):
    src = tmp_path / "report.pdf"
    _untagged(src)
    with pikepdf.open(src) as pdf:
        assert "/StructTreeRoot" not in pdf.Root
    rep = remediate(str(src), config.load(), use_ai=False)
    return rep, tmp_path / "report_accessible.pdf"


def test_untagged_pdf_gets_a_tag_tree(tagged):
    rep, out = tagged
    with pikepdf.open(out) as pdf:
        t = StructTree(pdf)
        types = [t.std_type(n.elem) for n in t.build()]
    assert types[0] == "Document"
    assert "H1" in types                     # the big bold title
    assert types.count("LI") == 3 and "L" in types
    assert "Table" in types and types.count("TR") == 3 and types.count("TH") == 3
    assert types.count("TD") == 6
    assert any("Tagged the untagged document locally" in i.message for i in rep.items)


def test_all_text_is_tagged_and_page_number_is_artifact(tagged):
    rep, out = tagged
    assert ContentIndex(str(out)).untagged_share == 0
    assert not [m for c, m, _ in rep.remaining if c in ("content", "structure")]
    with pikepdf.open(out) as pdf:
        t = StructTree(pdf)
        ci = ContentIndex(str(out))
        texts = [ci.describe(t.all_mcids(n.elem)).text.strip() for n in t.build()]
    assert "1" not in texts                  # page number isn't a tag


def test_reading_order_follows_the_page(tagged):
    _, out = tagged
    with pikepdf.open(out) as pdf:
        t = StructTree(pdf)
        ci = ContentIndex(str(out))
        blocks = [ci.describe(t.all_mcids(n.elem)).text for n in t.build() if n.depth == 1]
    order = [next(i for i, b in enumerate(blocks) if key in b)
             for key in ("Quarterly", "Ridership", "Weekday", "Region")]
    assert order == sorted(order)
