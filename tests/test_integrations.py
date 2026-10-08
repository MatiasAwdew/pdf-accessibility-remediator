"""Adobe AutoTag and Claude alt text, with the network mocked out."""

import shutil

import pikepdf
import pytest

from remediator import autotag, config, credentials
from remediator.ai import AltText
from remediator.pipeline import remediate
from remediator.structure import StructTree

from make_fixture import build


class FakeResp:
    def __init__(self, status=200, json=None, content=b"", headers=None):
        self.status_code, self._json, self.content = status, json, content
        self.headers = headers or {}
        self.text = str(json)

    def json(self):
        return self._json


class FakeAdobe:
    """Mimics pdf-services.adobe.io: token, assets, upload, job, 2 polls, download."""

    def __init__(self, tagged_bytes, fail=False):
        self.tagged, self.fail, self.calls, self.polls = tagged_bytes, fail, [], 0

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        if url.endswith("/token"):
            assert kw["data"]["client_id"] == "cid"
            return FakeResp(json={"access_token": "tok"})
        if url.endswith("/assets"):
            assert kw["headers"]["Authorization"] == "Bearer tok" and kw["headers"]["X-API-Key"] == "cid"
            return FakeResp(json={"uploadUri": "https://s3/upload", "assetID": "A1"})
        if url.endswith("/operation/autotag"):
            assert kw["json"]["assetID"] == "A1"
            return FakeResp(201, headers={"location": "https://pdf-services.adobe.io/operation/autotag/J1/status"})
        raise AssertionError(url)

    def put(self, url, **kw):
        self.calls.append(("PUT", url, kw))
        assert kw["data"][:4] == b"%PDF"
        return FakeResp()

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        if url.endswith("/status"):
            self.polls += 1
            if self.polls < 2:
                return FakeResp(json={"status": "in progress"}, headers={"retry-after": "0"})
            if self.fail:
                return FakeResp(json={"status": "failed", "error": {"code": "BAD_PDF", "message": "nope"}})
            return FakeResp(json={"status": "done", "tagged-pdf": {"assetID": "T1", "downloadUri": "https://s3/t"}})
        if url == "https://s3/t":
            return FakeResp(content=self.tagged)
        raise AssertionError(url)


@pytest.fixture
def adobe_env(monkeypatch, tmp_path):
    monkeypatch.setenv("PDF_SERVICES_CLIENT_ID", "cid")
    monkeypatch.setenv("PDF_SERVICES_CLIENT_SECRET", "secret")
    monkeypatch.setattr(autotag.time, "sleep", lambda s: None)
    tagged = tmp_path / "tagged_source.pdf"
    build(str(tagged))
    untagged = tmp_path / "scan.pdf"
    with pikepdf.open(tagged) as pdf:  # same pages, tags stripped
        del pdf.Root["/StructTreeRoot"]
        pdf.save(untagged)
    fake = FakeAdobe(tagged.read_bytes())
    monkeypatch.setattr(autotag.requests, "Session", lambda: fake)
    return untagged, fake


def test_autotag_client_flow(adobe_env, tmp_path):
    untagged, fake = adobe_env
    out = tmp_path / "out.pdf"
    autotag.AutoTagClient().autotag(str(untagged), str(out), log=lambda *a, **k: None)
    assert [c[0] for c in fake.calls] == ["POST", "POST", "PUT", "POST", "GET", "GET", "GET"]
    with pikepdf.open(out) as pdf:
        assert "/StructTreeRoot" in pdf.Root


def test_untagged_pdf_goes_through_autotag(adobe_env, tmp_path):
    untagged, _ = adobe_env
    rep = remediate(str(untagged), config.load(), use_ai=False)
    assert any(i.category == "autotag" and i.severity == "fixed" for i in rep.items)
    with pikepdf.open(tmp_path / "scan_accessible.pdf") as pdf:
        t = StructTree(pdf)
        assert "LBody" in [t.std_type(n.elem) for n in t.build()]  # full pipeline ran on the tagged copy
    assert (tmp_path / "scan_autotagged.pdf").exists()


def test_autotag_failure_is_reported(adobe_env, tmp_path):
    untagged, fake = adobe_env
    fake.fail = True
    rep = remediate(str(untagged), config.load(), use_ai=False)
    msgs = [i.message for i in rep.items if i.category == "autotag"]
    assert msgs and "BAD_PDF" in msgs[0]


def test_already_tagged_skips_autotag(adobe_env, tmp_path):
    _, fake = adobe_env
    src = tmp_path / "tagged.pdf"
    build(str(src))
    remediate(str(src), config.load(), use_ai=False)
    assert fake.calls == []


def test_ai_alt_text_applied(tmp_path, monkeypatch):
    src = tmp_path / "doc.pdf"
    build(str(src))
    seen = {}

    class FakeGen:
        def describe(self, page_png, crop_png, nearby):
            seen["png"] = crop_png[:4]
            seen["nearby"] = nearby
            return AltText(decorative=False, alt_text="Bar chart of revenue by quarter",
                           confidence="high", figure_kind="chart")

    monkeypatch.setattr("remediator.pipeline.make_ai", lambda cfg, enabled: FakeGen())
    rep = remediate(str(src), config.load())
    assert seen["png"] == b"\x89PNG"
    assert "Results" in seen["nearby"]  # neighbouring text passed as context
    with pikepdf.open(tmp_path / "doc_accessible.pdf") as pdf:
        t = StructTree(pdf)
        fig = next(n.elem for n in t.build() if t.std_type(n.elem) == "Figure")
        assert str(fig.Alt) == "Bar chart of revenue by quarter"
    assert not [c for c, _, _ in rep.remaining if c == "figures"]


def test_credentials_env_overrides_file(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    credentials.setup({"anthropic_api_key": "from-file", "adobe_client_id": "", "adobe_client_secret": ""})
    assert credentials.get("anthropic_api_key") == "from-file"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    assert credentials.get("anthropic_api_key") == "from-env"


def test_existing_alt_text_survives_autotag(adobe_env, tmp_path):
    """If AutoTag drops alt text that was already there, copy it back."""
    _, fake = adobe_env
    src = tmp_path / "has_alt.pdf"
    build(str(src))
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        t = StructTree(pdf)
        fig = next(n.elem for n in t.build() if t.std_type(n.elem) == "Figure")
        fig.Alt = pikepdf.String("Quarterly revenue chart")
        pdf.save(src)
    # fake Adobe returns the same page content, freshly tagged, without the alt text
    rep = remediate(str(src), config.load(), use_ai=False, force_autotag=True)
    with pikepdf.open(tmp_path / "has_alt_accessible.pdf") as pdf:
        t = StructTree(pdf)
        fig = next(n.elem for n in t.build() if t.std_type(n.elem) == "Figure")
        assert str(fig.Alt) == "Quarterly revenue chart"
    assert any("Kept existing alt text" in i.message for i in rep.items)
