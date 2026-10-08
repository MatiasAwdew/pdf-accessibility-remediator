import sys, pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))


@pytest.fixture(autouse=True)
def isolated_user_data(tmp_path, monkeypatch):
    """Never touch the real credentials / learning dataset, never call real APIs."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    for var in ("ANTHROPIC_API_KEY", "PDF_SERVICES_CLIENT_ID", "PDF_SERVICES_CLIENT_SECRET"):
        monkeypatch.delenv(var, raising=False)
    # the real veraPDF takes ~20s per file; tests of the loop use a fake (test_validate.py)
    monkeypatch.setattr("remediator.validate.find_verapdf", lambda configured=None: None)
