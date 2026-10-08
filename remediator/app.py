"""Drag-and-drop app.

Local:  python -m remediator.app  (or the desktop shortcut) - 127.0.0.1 only.
Hosted: set APP_PASSWORD (see Dockerfile / DEPLOY.md) - password login, served
        by gunicorn via remediator.wsgi. Files are kept unless RETENTION_DAYS is set.


Runs a small web server on 127.0.0.1 only and opens it in your browser.
PDFs stay on this computer (jobs are kept in Documents/PDF Remediator);
the only things that leave it are the same Claude calls the CLI makes.

Dropping PDF files onto the desktop shortcut passes their paths here: they're
queued and the browser opens on the first one.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from pathlib import Path

import hmac
from datetime import timedelta

from flask import Flask, abort, jsonify, redirect, request, send_file, session

from . import config as config_mod
from . import credentials
from .apply import apply_review
from .pipeline import output_paths, remediate

PORT = 8765
HOSTED = bool(os.environ.get("APP_PASSWORD"))
RETENTION_DAYS = float(os.environ.get("RETENTION_DAYS", "0"))  # 0 = keep files (no auto-delete)
JOBS_DIR = Path(os.environ.get("JOBS_DIR") or
                Path(os.environ.get("USERPROFILE", Path.home())) / "Documents" / "PDF Remediator")
WEB = Path(__file__).with_name("web")

app = Flask(__name__)
app.config.update(
    MAX_CONTENT_LENGTH=200 * 1024 * 1024,  # agenda packets can be large
    SECRET_KEY=os.environ.get("SECRET_KEY") or os.urandom(32),
    SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=HOSTED,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)
jobs: dict[str, dict] = {}
lock = threading.Lock()


def code_fingerprint() -> str:
    """Changes whenever the tool's code or page changes. A running app compares it
    with the files on disk so a double-click never leaves you on stale code."""
    root = Path(__file__).parent
    newest = max(p.stat().st_mtime_ns for p in list(root.rglob("*.py")) + list(root.rglob("*.html")))
    return str(newest)


STARTED_WITH = code_fingerprint()


# ---------------------------------------------------------------- safety

OPEN_PATHS = {"/login", "/healthz", "/favicon.ico"}


@app.before_request
def guard():
    if HOSTED:
        # hosted: everything except the login page needs the password
        if request.path not in OPEN_PATHS and not session.get("ok"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "login required"}), 401
            return redirect("/login")
    else:
        # local: only answer requests addressed to this machine (blocks DNS rebinding)
        host = (request.host or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            abort(403)
    # writes need a custom header, so other websites can't drive the app
    if request.method == "POST" and request.path != "/login" and request.headers.get("X-Remediator") != "1":
        abort(403)


@app.get("/healthz")
def healthz():
    return "ok"


LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Sign in - PDF Remediator</title>
<link rel="icon" href="/favicon.ico"><style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f5f7fb;color:#0f172a;
font:15px/1.5 "Segoe UI",system-ui,sans-serif}
form{background:#fff;border:1px solid #e2e7ef;border-radius:14px;box-shadow:0 4px 16px rgba(15,23,42,.06);
padding:32px;width:min(360px,90vw)}
h1{font-size:20px;margin:12px 0 18px}label{font-weight:600;font-size:13px;display:block;margin-bottom:6px}
input{width:100%;box-sizing:border-box;font:inherit;padding:10px 12px;border:1px solid #cfd7e3;border-radius:9px}
button{margin-top:16px;width:100%;font:inherit;font-weight:600;padding:10px;border:0;border-radius:9px;
background:#0b5cad;color:#fff;cursor:pointer}:focus-visible{outline:3px solid #0b5cad;outline-offset:2px}
.err{color:#b42318;background:#fdecea;border-radius:9px;padding:8px 12px;margin-bottom:12px}
</style></head><body><form method="post" action="/login">
<img src="/favicon.ico" alt="" width="36" height="36"><h1>PDF Remediator</h1>{error}
<label for="pw">Password</label><input id="pw" name="password" type="password" autocomplete="current-password" autofocus required>
<button type="submit">Sign in</button></form></body></html>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    if not HOSTED:
        return redirect("/")
    error = ""
    if request.method == "POST":
        if hmac.compare_digest(request.form.get("password", "").encode(), os.environ["APP_PASSWORD"].encode()):
            session.clear()
            session["ok"] = True
            session.permanent = True
            return redirect("/")
        time.sleep(1.5)  # slow down guessing
        error = '<div class="err" role="alert">Wrong password.</div>'
    return LOGIN_PAGE.replace("{error}", error)


@app.get("/logout")
def logout():
    session.clear()
    return redirect("/login" if HOSTED else "/")


def cleanup_old_jobs() -> int:
    """Delete jobs older than RETENTION_DAYS (optional; off by default)."""
    if RETENTION_DAYS <= 0 or not JOBS_DIR.exists():
        return 0
    cutoff = time.time() - RETENTION_DAYS * 86400
    removed = 0
    for d in JOBS_DIR.iterdir():
        if d.is_dir() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            with lock:
                jobs.pop(d.name, None)
            removed += 1
    return removed


def start_cleanup_thread() -> None:
    def loop():
        while True:
            try:
                cleanup_old_jobs()
            except Exception:
                traceback.print_exc()
            time.sleep(3600)
    threading.Thread(target=loop, daemon=True).start()


# ---------------------------------------------------------------- jobs

def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).stem)[:60] or "document"


def _job_dir(job_id: str) -> Path:
    d = (JOBS_DIR / job_id).resolve()
    if JOBS_DIR.resolve() not in d.parents:
        abort(404)
    return d


def create_job(src_path: Path, original_name: str, use_ai: bool) -> str:
    job_id = time.strftime("%Y%m%d-%H%M%S-") + _slug(original_name)
    d = JOBS_DIR / job_id
    d.mkdir(parents=True, exist_ok=True)
    dest = d / (_slug(original_name) + ".pdf")
    shutil.copyfile(src_path, dest)
    meta = {"id": job_id, "name": original_name, "input": str(dest), "ai": use_ai,
            "status": "running", "started": time.time(), "applied": False}
    (d / "job.json").write_text(json.dumps(meta), encoding="utf-8")
    with lock:
        jobs[job_id] = meta
    threading.Thread(target=_run, args=(job_id,), daemon=True).start()
    return job_id


def _run(job_id: str) -> None:
    meta = jobs[job_id]
    try:
        cfg = config_mod.load()
        remediate(meta["input"], cfg, use_ai=meta["ai"])
        meta["status"] = "done"
    except Exception as e:
        meta["status"] = "error"
        meta["error"] = f"{type(e).__name__}: {e}"
        (Path(meta["input"]).parent / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
    meta["finished"] = time.time()
    (Path(meta["input"]).parent / "job.json").write_text(json.dumps(meta), encoding="utf-8")


def _load_job(job_id: str) -> dict:
    with lock:
        if job_id in jobs:
            return jobs[job_id]
    f = _job_dir(job_id) / "job.json"
    if not f.exists():
        abort(404)
    meta = json.loads(f.read_text(encoding="utf-8"))
    if meta.get("status") == "running":  # app was closed mid-run
        meta["status"] = "error"
        meta["error"] = "The app was closed before this job finished. Drop the file again."
    with lock:
        jobs[job_id] = meta
    return meta


def _paths(meta: dict) -> dict:
    return output_paths(meta["input"], None, "_accessible")


# ---------------------------------------------------------------- API

@app.get("/")
def index():
    return send_file(WEB / "index.html")


@app.get("/favicon.ico")
def favicon():
    return send_file(WEB / "remediator.ico", mimetype="image/x-icon")


@app.get("/api/status")
def status():
    from . import validate
    cfg = config_mod.load()
    return jsonify({"keys": credentials.status(), "jobs_dir": str(JOBS_DIR), "code": STARTED_WITH,
                    "hosted": HOSTED, "retention_days": RETENTION_DAYS if HOSTED and RETENTION_DAYS > 0 else None,
                    "verapdf": bool(validate.find_verapdf(cfg["validate"].get("verapdf_path")))})


@app.post("/api/shutdown")
def shutdown():
    """Used by the launcher to replace an app that's running outdated code."""
    with lock:
        busy = [j for j in jobs.values() if j.get("status") == "running"]
    if busy:
        return jsonify({"ok": False, "busy": len(busy)}), 409
    threading.Timer(0.3, lambda: os._exit(0)).start()
    return jsonify({"ok": True})


@app.get("/api/jobs")
def list_jobs():
    out = []
    if JOBS_DIR.exists():
        for f in sorted(JOBS_DIR.glob("*/job.json"), reverse=True)[:30]:
            try:
                m = _load_job(f.parent.name)
                out.append({k: m.get(k) for k in ("id", "name", "status", "applied", "started")})
            except Exception:
                continue
    return jsonify(out)


@app.post("/api/jobs")
def upload():
    f = request.files.get("file")
    if f is None or not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Please drop a PDF file."}), 400
    tmp = JOBS_DIR / f"_upload-{time.time_ns()}.tmp"
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    f.save(tmp)
    job_id = create_job(tmp, f.filename, request.form.get("ai", "1") == "1")
    tmp.unlink(missing_ok=True)
    return jsonify({"id": job_id})


@app.post("/api/jobs/from-path")
def from_path():
    if HOSTED:  # server paths aren't the user's files
        abort(404)
    ids = []
    for p in request.get_json(force=True).get("paths", []):
        path = Path(p)
        if path.suffix.lower() == ".pdf" and path.is_file():
            ids.append(create_job(path, path.name, True))
    return jsonify({"ids": ids})


@app.get("/api/jobs/<job_id>")
def job(job_id):
    meta = _load_job(job_id)
    out = {"job": meta}
    if meta["status"] == "done":
        p = _paths(meta)
        out["report"] = json.loads(Path(p["json"]).read_text(encoding="utf-8"))
        if Path(p["review"]).exists():
            out["review"] = json.loads(Path(p["review"]).read_text(encoding="utf-8"))
    return jsonify(out)


@app.get("/api/jobs/<job_id>/figure/<item_id>.png")
def figure(job_id, item_id):
    """Crop of the figure a review item is about, so you can judge the alt text."""
    import pymupdf
    meta = _load_job(job_id)
    review = json.loads(Path(_paths(meta)["review"]).read_text(encoding="utf-8"))
    item = next((i for i in review["items"] if i["id"] == item_id), None)
    ctx = (item or {}).get("context") or {}
    if not item or not ctx.get("page"):
        abort(404)
    doc = pymupdf.open(_paths(meta)["pdf"])
    page = doc[ctx["page"] - 1]
    if ctx.get("bbox"):
        rect = pymupdf.Rect(*ctx["bbox"]) * page.transformation_matrix
        rect = (rect + (-8, -8, 8, 8)) & page.rect
        png = page.get_pixmap(dpi=110, clip=rect).tobytes("png")
    else:
        png = page.get_pixmap(dpi=50).tobytes("png")
    out = _job_dir(job_id) / f"preview-{_slug(item_id)}.png"
    out.write_bytes(png)
    return send_file(out, mimetype="image/png")


@app.post("/api/jobs/<job_id>/apply")
def apply(job_id):
    meta = _load_job(job_id)
    p = _paths(meta)
    review = json.loads(Path(p["review"]).read_text(encoding="utf-8"))
    changes = {c["id"]: c for c in request.get_json(force=True).get("items", [])}
    for item in review["items"]:
        c = changes.get(item["id"])
        if c:
            item["decision"] = c.get("decision", item["decision"])
            if "value" in c:
                item["value"] = c["value"]
    Path(p["review"]).write_text(json.dumps(review, indent=2, ensure_ascii=False), encoding="utf-8")
    learn = request.get_json(force=True).get("learn", True) and config_mod.load()["learning"]["record_decisions"]
    log = apply_review(p["pdf"], p["review"], learn=learn)
    meta["applied"] = True
    (Path(meta["input"]).parent / "job.json").write_text(json.dumps(meta), encoding="utf-8")
    return jsonify({"log": log})


@app.get("/api/jobs/<job_id>/download/<kind>")
def download(job_id, kind):
    meta = _load_job(job_id)
    p = _paths(meta)
    if kind == "pdf":
        name = Path(meta["name"]).stem + "_accessible.pdf"
        return send_file(p["pdf"], as_attachment=True, download_name=name)
    if kind == "report":
        return send_file(p["html"])
    abort(404)


@app.post("/api/jobs/<job_id>/open-folder")
def open_folder(job_id):
    if HOSTED:
        abort(404)
    meta = _load_job(job_id)
    if hasattr(os, "startfile"):
        os.startfile(Path(meta["input"]).parent)  # noqa: S606 - local folder we created
    return jsonify({"ok": True})


# ---------------------------------------------------------------- launcher

CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\Application\chrome.exe"),
]


def open_browser(url: str) -> None:
    """Open in Chrome when it's installed (the user's browser), else the default browser."""
    import subprocess
    chrome = next((p for p in CHROME_PATHS if os.path.exists(p)), None)
    if chrome:
        try:
            subprocess.Popen([chrome, url])
            return
        except OSError:
            pass
    webbrowser.open(url)


def _status() -> dict | None:
    try:
        return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/status", timeout=1).read())
    except Exception:
        return None


def _running() -> bool:
    return _status() is not None


def _replace_if_stale() -> bool:
    """If an app with older code is running, stop it. Returns True if one is still running."""
    s = _status()
    if s is None:
        return False
    if s.get("code") == code_fingerprint():
        return True
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/shutdown", data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json", "X-Remediator": "1"})
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        return True  # busy with a job (409) or unreachable: keep using it
    for _ in range(50):
        if not _running():
            return False
        time.sleep(0.1)
    return True


def _post_paths(paths: list[str]) -> list[str]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/api/jobs/from-path", data=json.dumps({"paths": paths}).encode(),
        headers={"Content-Type": "application/json", "X-Remediator": "1"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())["ids"]


def main(argv=None) -> None:
    paths = [a for a in (sys.argv[1:] if argv is None else argv) if a.lower().endswith(".pdf")]
    if _replace_if_stale():  # second launch / file dropped on the icon: hand it to the running app
        ids = _post_paths(paths) if paths else []
        open_browser(f"http://127.0.0.1:{PORT}/" + (f"#{ids[0]}" if ids else ""))
        return
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None or sys.executable.lower().endswith("pythonw.exe"):  # no console: log to file
        log = open(JOBS_DIR / "app.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log

    def opener():
        for _ in range(50):
            if _running():
                ids = _post_paths(paths) if paths else []
                open_browser(f"http://127.0.0.1:{PORT}/" + (f"#{ids[0]}" if ids else ""))
                return
            time.sleep(0.2)

    threading.Thread(target=opener, daemon=True).start()
    app.run(host="127.0.0.1", port=PORT, threaded=True)


if __name__ == "__main__":
    main()
