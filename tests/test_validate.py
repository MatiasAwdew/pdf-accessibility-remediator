"""veraPDF validate -> fix -> re-validate loop, with veraPDF faked."""

import pikepdf
from pikepdf import Dictionary, Name

from remediator import config, validate
from remediator.pipeline import remediate
from remediator.validate import Failure

from make_fixture import build


def _with_problems(path):
    build(str(path))
    with pikepdf.open(path, allow_overwriting_input=True) as pdf:
        desc = pdf.make_indirect(Dictionary(Type=Name.FontDescriptor, FontName=Name("/X"),
                                            CIDSet=pdf.make_stream(b"\x00")))
        pdf.Root.OCProperties = Dictionary(OCGs=[], D=Dictionary())
        pdf.Root.Extra = desc
        pdf.save(path)


def test_loop_fixes_known_rules_and_revalidates(tmp_path, monkeypatch):
    src = tmp_path / "doc.pdf"
    _with_problems(src)
    runs = []

    def fake_run(exe, path, timeout=300):
        with pikepdf.open(path) as pdf:
            fails = []
            if any(isinstance(o, Dictionary) and "/CIDSet" in o for o in pdf.objects):
                fails.append(Failure("7.21.4.2", 2, 1, "CIDSet"))
            if not str(pdf.Root.OCProperties.D.get("/Name", "")):
                fails.append(Failure("7.10", 1, 1, "OC name"))
            fails.append(Failure("7.1", 3, 5, "untagged content"))  # not auto-fixable
        runs.append(len(fails))
        return fails

    monkeypatch.setattr(validate, "find_verapdf", lambda configured=None: "fake.bat")
    monkeypatch.setattr(validate, "run_verapdf", fake_run)
    rep = remediate(str(src), config.load(), use_ai=False)
    assert runs == [3, 1]  # validated, fixed two rules, re-validated
    fixes = [i.message for i in rep.items if i.category == "validation" and i.severity == "fixed"]
    assert any("CIDSet" in m for m in fixes) and any("optional-content" in m for m in fixes)
    assert rep.verapdf_compliant is False
    assert any("[7.1-3]" in m for c, m, _ in rep.remaining if c == "PDF/UA")
    with pikepdf.open(tmp_path / "doc_accessible.pdf") as pdf:
        assert str(pdf.Root.OCProperties.D.Name) == "Default"


def test_missing_verapdf_is_reported_not_fatal(tmp_path):
    src = tmp_path / "doc.pdf"
    build(str(src))
    rep = remediate(str(src), config.load(), use_ai=False)
    assert rep.verapdf_compliant is None
    assert any("veraPDF isn't installed" in i.message for i in rep.items)
