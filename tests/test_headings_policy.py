"""Heading rules learned from the PMC benchmark (real publisher PDFs)."""

import pikepdf
from pikepdf import Array, Dictionary, Name

from remediator import config
from remediator.pipeline import remediate
from remediator.structure import StructTree

from make_fixture import build


def _variant(tmp_path, mutate):
    src = tmp_path / "doc.pdf"
    build(str(src))
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        t = StructTree(pdf)
        mutate(pdf, t, [n.elem for n in t.build()])
        pdf.save(src)
    rep = remediate(str(src), config.load(), use_ai=False)
    with pikepdf.open(tmp_path / "doc_accessible.pdf") as pdf:
        t = StructTree(pdf)
        heads = [t.std_type(n.elem) for n in t.build() if t.std_type(n.elem) in
                 ("H1", "H2", "H3", "H4", "H5", "H6")]
        types = [t.std_type(n.elem) for n in t.build()]
    return rep, heads, types


def _by_type(t, elems, s):
    return [e for e in elems if t.std_type(e) == s]


def test_publisher_heading_tags_mapped_to_p_are_promoted(tmp_path):
    # Frontiers-style: CHAP_TITLE / HEAD_1 role-mapped to P => no headings at all
    def mutate(pdf, t, elems):
        h1, = _by_type(t, elems, "H1")
        h3, = _by_type(t, elems, "H3")
        h2, = _by_type(t, elems, "H2")
        h1.S, h3.S, h2.S = Name("/CHAP_TITLE"), Name("/HEAD_1"), Name("/HEAD_1")
        pdf.Root.StructTreeRoot.RoleMap = Dictionary(CHAP_TITLE=Name.P, HEAD_1=Name.P)

    rep, heads, _ = _variant(tmp_path, mutate)
    assert heads == ["H1", "H2", "H2"]
    assert any("title/heading tag" in i.message for i in rep.items)


def test_valid_outline_is_not_releveled_by_font_size(tmp_path):
    # H1 / H2 / H3 is a valid outline even though Summary (18pt) is the same size as
    # Introduction; font-size ranking would wrongly flatten it to H2.
    def mutate(pdf, t, elems):
        _by_type(t, elems, "H3")[0].S = Name.H2
        _by_type(t, elems, "H2")[-1].S = Name.H3

    rep, heads, _ = _variant(tmp_path, mutate)
    assert heads == ["H1", "H2", "H3"]
    assert not [i for i in rep.items if i.category == "headings" and "->" in i.message and "H" in i.message
                and "Possible" not in i.message]


def test_masthead_before_title_is_demoted_not_shifted(tmp_path):
    # journal name tagged H2 above the real H1 title (Elsevier-style)
    def mutate(pdf, t, elems):
        _by_type(t, elems, "H1")[0].S = Name.H2       # "Annual Report" = masthead
        _by_type(t, elems, "H3")[0].S = Name.H1       # "Introduction" = real title

    rep, heads, _ = _variant(tmp_path, mutate)
    assert heads == ["H1", "H2"]                      # Introduction H1, Summary H2
    assert any("comes before the document title" in i.message for i in rep.items)


def test_empty_table_cells_are_kept(tmp_path):
    def mutate(pdf, t, elems):
        _by_type(t, elems, "TD")[1].K = Array()        # blank cell

    _, _, types = _variant(tmp_path, mutate)
    assert types.count("TD") + types.count("TH") == 4


def test_pdf2_title_becomes_only_h1(tmp_path):
    # Adobe AutoTag style: <Title> for the article title, sections as H1
    def mutate(pdf, t, elems):
        _by_type(t, elems, "H1")[0].S = Name.Title    # "Annual Report"
        _by_type(t, elems, "H3")[0].S = Name.H1       # "Introduction"
        _by_type(t, elems, "H2")[-1].S = Name.H1      # "Summary"

    rep, heads, _ = _variant(tmp_path, mutate)
    assert heads == ["H1", "H2", "H2"]
    assert any("only H1" in i.message for i in rep.items)


def test_multilevel_parent_tree_is_editable(tmp_path):
    # Adobe writes ParentTree as /Kids with /Limits; adding a Link must still work
    def mutate(pdf, t, elems):
        root = pdf.Root.StructTreeRoot
        nums = root.ParentTree.Nums
        leaf = pdf.make_indirect(Dictionary(Limits=Array([0, 1]), Nums=Array(list(nums))))
        root.ParentTree = pdf.make_indirect(Dictionary(Kids=Array([leaf])))

    rep, _, types = _variant(tmp_path, mutate)
    assert "Link" in types
    assert not [c for c, _, _ in rep.remaining if c == "structure"]
