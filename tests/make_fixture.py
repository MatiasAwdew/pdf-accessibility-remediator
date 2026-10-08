"""Build a small tagged PDF with the typical post-AutoTag problems:

no title / lang / MarkInfo, H1 -> H3 skip, a bold "heading" tagged as P,
figure without alt, link annotation outside the tag tree with no Contents,
table with no TH, LI without LBody, an empty tag, an untagged drawn line.
"""

from __future__ import annotations

import sys

import pikepdf
from pikepdf import Array, Dictionary, Name, String


def _text(tag, mcid, font, size, x, y, s):
    s = s.replace("(", r"\(").replace(")", r"\)")
    return f"/{tag} <</MCID {mcid}>> BDC BT /{font} {size} Tf {x} {y} Td ({s}) Tj ET EMC\n"


def build(path: str) -> None:
    pdf = pikepdf.new()
    fonts = Dictionary(
        F1=Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica, Encoding=Name.WinAnsiEncoding),
        F2=Dictionary(Type=Name.Font, Subtype=Name("/Type1"), BaseFont=Name("/Helvetica-Bold"),
                      Encoding=Name.WinAnsiEncoding),
    )
    res = Dictionary(Font=fonts)

    body = "This is body text for the report and it continues for a while."
    c1 = (
        _text("H1", 0, "F1", 24, 72, 720, "Annual Report")
        + _text("H3", 1, "F1", 18, 72, 680, "Introduction")
        + _text("P", 2, "F1", 11, 72, 650, body)
        + _text("P", 3, "F2", 18, 72, 620, "Results")
        + "/Figure <</MCID 4>> BDC 0 0 1 rg 72 400 200 150 re f EMC\n"
        + _text("P", 5, "F1", 11, 72, 370, "Visit example.com for details")
        + "0 0 0 RG 1 w 72 355 m 540 355 l S\n"
        + _text("TD", 6, "F1", 11, 72, 330, "Name")
        + _text("TD", 7, "F1", 11, 200, 330, "Score")
        + _text("TD", 8, "F1", 11, 72, 310, "Alice")
        + _text("TD", 9, "F1", 11, 200, 310, "90")
        + _text("LI", 10, "F1", 11, 72, 280, "First item")
    )
    c2 = _text("H2", 0, "F1", 18, 72, 720, "Summary") + _text("P", 1, "F1", 11, 72, 690, body)

    pages = []
    for c in (c1, c2):
        page = pikepdf.Page(Dictionary(Type=Name.Page, MediaBox=Array([0, 0, 612, 792]),
                                       Resources=res, Contents=pdf.make_stream(c.encode("latin-1"))))
        pdf.pages.append(page)
    p1, p2 = pdf.pages[0].obj, pdf.pages[1].obj

    root = pdf.make_indirect(Dictionary(Type=Name.StructTreeRoot))
    doc = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name.Document, P=root, K=Array()))
    root.K = doc

    def el(s, parent, pg, k):
        e = pdf.make_indirect(Dictionary(Type=Name.StructElem, S=Name("/" + s), P=parent, Pg=pg, K=k))
        return e

    by_mcid1 = {}
    kids = []
    for s, m in (("H1", 0), ("H3", 1), ("P", 2), ("P", 3), ("Figure", 4), ("P", 5)):
        e = el(s, doc, p1, m)
        by_mcid1[m] = e
        kids.append(e)
    table = el("Table", doc, p1, Array())
    rows = []
    for a, b in ((6, 7), (8, 9)):
        tr = el("TR", table, p1, Array())
        tds = [el("TD", tr, p1, a), el("TD", tr, p1, b)]
        by_mcid1[a], by_mcid1[b] = tds
        tr.K = Array(tds)
        rows.append(tr)
    table.K = Array(rows)
    lst = el("L", doc, p1, Array())
    li = el("LI", lst, p1, 10)
    by_mcid1[10] = li
    lst.K = Array([li])
    empty = el("P", doc, p1, Array())
    kids += [table, lst, empty]

    h2 = el("H2", doc, p2, 0)
    p_last = el("P", doc, p2, 1)
    kids += [h2, p_last]
    doc.K = Array(kids)

    arr1 = pdf.make_indirect(Array([by_mcid1[i] for i in range(11)]))
    arr2 = pdf.make_indirect(Array([h2, p_last]))
    p1.StructParents = 0
    p2.StructParents = 1
    root.ParentTree = pdf.make_indirect(Dictionary(Nums=Array([0, arr1, 1, arr2])))
    root.ParentTreeNextKey = 2
    pdf.Root.StructTreeRoot = root

    link = pdf.make_indirect(Dictionary(
        Type=Name.Annot, Subtype=Name.Link, Rect=Array([70, 365, 250, 382]), Border=Array([0, 0, 0]),
        A=Dictionary(S=Name.URI, URI=String("https://example.com"))))
    p1.Annots = Array([link])
    pdf.save(path)


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "fixture.pdf")
