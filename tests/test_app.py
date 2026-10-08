"""The drag-and-drop app, driven through Flask's test client (no browser, no AI)."""

import io
import json
import time

import pytest

from remediator import app as appmod

from make_fixture import build

H = {"X-Remediator": "1"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "JOBS_DIR", tmp_path / "jobs")
    appmod.jobs.clear()
    return appmod.app.test_client()


def _upload(client, tmp_path):
    src = tmp_path / "Annual Report.pdf"
    build(str(src))
    r = client.post("/api/jobs", headers=H, content_type="multipart/form-data",
                    data={"file": (io.BytesIO(src.read_bytes()), "Annual Report.pdf"), "ai": "0"})
    assert r.status_code == 200, r.data
    job_id = r.get_json()["id"]
    for _ in range(100):
        data = client.get(f"/api/jobs/{job_id}").get_json()
        if data["job"]["status"] != "running":
            return job_id, data
        time.sleep(0.1)
    raise AssertionError("job never finished")


def test_full_flow(client, tmp_path):
    job_id, data = _upload(client, tmp_path)
    assert data["job"]["status"] == "done", data["job"]
    assert data["report"]["summary"]["fixed"] > 0
    items = data["review"]["items"]
    fig = next(i for i in items if i["field"] == "alt")

    png = client.get(f"/api/jobs/{job_id}/figure/{fig['id']}.png")
    assert png.status_code == 200 and png.data[:4] == b"\x89PNG"

    r = client.post(f"/api/jobs/{job_id}/apply", headers={**H, "Content-Type": "application/json"},
                    data=json.dumps({"items": [{"id": fig["id"], "decision": "accept",
                                                "value": "Blue chart placeholder"}], "learn": False}))
    assert r.status_code == 200
    pdf = client.get(f"/api/jobs/{job_id}/download/pdf")
    assert pdf.status_code == 200 and pdf.data[:4] == b"%PDF"
    assert b"Blue chart placeholder" in pdf.data or client.get(f"/api/jobs/{job_id}").get_json()["job"]["applied"]
    assert client.get("/api/jobs").get_json()[0]["id"] == job_id


def test_writes_need_the_app_header(client, tmp_path):
    # another website could POST a form to localhost; without the custom header it's refused
    r = client.post("/api/jobs/from-path", json={"paths": [str(tmp_path / "x.pdf")]})
    assert r.status_code == 403


def test_foreign_host_refused(client):
    assert client.get("/api/status", headers={"Host": "evil.example"}).status_code == 403


def test_non_pdf_rejected(client):
    r = client.post("/api/jobs", headers=H, content_type="multipart/form-data",
                    data={"file": (io.BytesIO(b"hello"), "notes.txt")})
    assert r.status_code == 400


def test_shutdown_refused_while_a_job_runs(client):
    appmod.jobs["x"] = {"status": "running"}
    r = client.post("/api/shutdown", headers=H)
    assert r.status_code == 409
    appmod.jobs.clear()


def test_status_reports_code_fingerprint(client):
    s = client.get("/api/status").get_json()
    assert s["code"] == appmod.code_fingerprint()


@pytest.fixture
def hosted(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "correct horse")
    monkeypatch.setattr(appmod, "HOSTED", True)
    monkeypatch.setattr(appmod, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(appmod.time, "sleep", lambda s: None)  # skip the anti-guessing delay
    appmod.jobs.clear()
    return appmod.app.test_client()


def test_hosted_requires_login(hosted):
    assert hosted.get("/api/status").status_code == 401
    r = hosted.get("/")
    assert r.status_code == 302 and r.headers["Location"].endswith("/login")
    assert hosted.get("/healthz").data == b"ok"


def test_hosted_login_flow(hosted):
    assert b"Wrong password" in hosted.post("/login", data={"password": "nope"}).data
    r = hosted.post("/login", data={"password": "correct horse"})
    assert r.status_code == 302
    s = hosted.get("/api/status").get_json()
    assert s["hosted"] is True and s["retention_days"] is None  # no auto-delete by default
    hosted.get("/logout")
    assert hosted.get("/api/status").status_code == 401


def test_hosted_blocks_server_paths_and_folder(hosted):
    hosted.post("/login", data={"password": "correct horse"})
    assert hosted.post("/api/jobs/from-path", headers=H, json={"paths": ["/etc/passwd"]}).status_code == 404
    assert hosted.post("/api/jobs/x/open-folder", headers=H).status_code == 404
