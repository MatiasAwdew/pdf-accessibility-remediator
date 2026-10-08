"""Table rules learned from a real remediated meeting agenda."""

import pikepdf
from pikepdf import Array, Name

from remediator import config
from remediator.fixes.tables import _cells, _rows
from remediator.pipeline import remediate
from remediator.structure import StructTree

from make_fixture import build


def _run(tmp_path, mutate, **cfg_over):
    src = tmp_path / "doc.pdf"
    build(str(src))
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        t = StructTree(pdf)
        mutate(t, [n.elem for n in t.build()])
        pdf.save(src)
    cfg = config.load()
    cfg["tables"].update(cfg_over)
    remediate(str(src), cfg, use_ai=False)
    with pikepdf.open(tmp_path / "doc_accessible.pdf") as pdf:
        t = StructTree(pdf)
        table = next(n.elem for n in t.build() if t.std_type(n.elem) == "Table")
        return [[t.std_type(c) for c in _cells(t, r)] for r in _rows(t, table)]


def _all_th(t, elems):
    for e in elems:
        if t.std_type(e) == "TD":
            e.S = Name.TH


def test_headers_in_data_rows_become_td(tmp_path):
    assert _run(tmp_path, _all_th) == [["TH", "TH"], ["TD", "TD"]]


def test_row_headers_kept_when_configured(tmp_path):
    assert _run(tmp_path, _all_th, data_row_headers="keep") == [["TH", "TH"], ["TH", "TH"]]


def test_short_rows_padded_to_regular(tmp_path):
    def drop_cell(t, elems):
        tr = [e for e in elems if t.std_type(e) == "TR"][1]
        tr.K = Array([tr.K[0]])

    assert _run(tmp_path, drop_cell) == [["TH", "TH"], ["TD", "TD"]]


def test_two_header_rows_are_kept(tmp_path):
    """Group headings over column headings (e.g. TCEP 'Funds by Target' over column names)."""
    def two_header_rows(t, elems):
        trs = [e for e in elems if t.std_type(e) == "TR"]
        for tr in trs[:1]:
            for c in tr.K:
                c.S = Name.TH
        # add a data row below the two header rows
        tr2 = trs[1]
        for c in tr2.K:
            c.S = Name.TH
        table = tr2.P
        pdf_new = table.objgen  # noqa: F841 - keep flake quiet
        extra = t.new_elem("TR", table, 0)
        extra.K = Array([t.new_elem("TD", extra, 0), t.new_elem("TH", extra, 0)])
        table.K = Array(list(table.K) + [extra])

    assert _run(tmp_path, two_header_rows) == [["TH", "TH"], ["TH", "TH"], ["TD", "TD"]]


def test_summary_never_states_size(tmp_path):
    src = tmp_path / "doc.pdf"
    build(str(src))
    rep = remediate(str(src), config.load(), use_ai=False)
    summaries = [i.message for i in rep.items if "Added summary" in i.message]
    assert summaries and all("rows" not in s and "columns" not in s.lower().split("column headers")[0]
                             for s in summaries)
    assert "Column headers: Name, Score." in summaries[0]


def test_claude_table_summary_used(tmp_path, monkeypatch):
    seen = {}

    class Fake:
        def summarize_table(self, headers, sample, context, n_rows, n_cols):
            seen.update(headers=headers, sample=sample)
            return "Scores for each participant in the annual review."
    monkeypatch.setattr("remediator.pipeline.make_ai", lambda cfg, enabled: Fake())
    src = tmp_path / "doc.pdf"
    build(str(src))
    rep = remediate(str(src), config.load())
    assert any("Added summary (Claude): Scores for each participant" in i.message for i in rep.items)
    assert seen["headers"] == [["Name", "Score"]] and seen["sample"] == [["Alice", "90"]]
