"""Adobe PDF Services "PDF Accessibility Auto-Tag" API: the same engine as
Acrobat's "Automatically tag PDF", as a REST call.

Flow: token -> create asset (pre-signed upload URL) -> PUT file ->
POST /operation/autotag -> poll the Location URL -> download tagged PDF
(and optionally the Excel tagging report).

Credentials (client_id / client_secret) come from the Adobe Developer Console;
see README > Setup.
"""

from __future__ import annotations

import time
from pathlib import Path

import requests

from . import credentials

REGIONS = {
    "default": "https://pdf-services.adobe.io",
    "us": "https://pdf-services-ue1.adobe.io",
    "eu": "https://pdf-services-ew1.adobe.io",
}


class AutoTagError(RuntimeError):
    pass


def available() -> bool:
    return bool(credentials.get("adobe_client_id") and credentials.get("adobe_client_secret"))


class AutoTagClient:
    def __init__(self, region: str = "default", timeout_s: int = 600):
        self.client_id = credentials.get("adobe_client_id")
        self.client_secret = credentials.get("adobe_client_secret")
        if not (self.client_id and self.client_secret):
            raise AutoTagError("Adobe credentials missing - run `python remediate.py setup`")
        self.base = REGIONS.get(region, REGIONS["default"])
        self.timeout_s = timeout_s
        self.session = requests.Session()
        self._token = None

    # ---------- HTTP helpers ----------

    def _check(self, r: requests.Response, what: str) -> requests.Response:
        if r.status_code >= 400:
            try:
                err = r.json().get("error", r.json())
            except ValueError:
                err = r.text[:300]
            hint = ""
            if r.status_code in (401, 403):
                hint = " (check the client_id/secret; run `python remediate.py setup`)"
            elif r.status_code == 429:
                hint = " (rate limit or monthly quota reached - check the Adobe Developer Console)"
            raise AutoTagError(f"{what} failed: HTTP {r.status_code} {err}{hint}")
        return r

    def _headers(self, json_body: bool = True) -> dict:
        if self._token is None:
            r = self.session.post(f"{self.base}/token", timeout=60,
                                  data={"client_id": self.client_id, "client_secret": self.client_secret})
            self._token = self._check(r, "Adobe authentication").json()["access_token"]
        h = {"Authorization": f"Bearer {self._token}", "X-API-Key": self.client_id}
        if json_body:
            h["Content-Type"] = "application/json"
        return h

    # ---------- operation ----------

    def autotag(self, src: str, dest: str, report_dest: str | None = None,
                shift_headings: bool = False, log=print) -> str:
        data = Path(src).read_bytes()

        log("  AutoTag: uploading to Adobe PDF Services...")
        r = self._check(self.session.post(f"{self.base}/assets", headers=self._headers(), timeout=60,
                                          json={"mediaType": "application/pdf"}), "Create asset")
        asset = r.json()
        self._check(self.session.put(asset["uploadUri"], data=data, timeout=300,
                                     headers={"Content-Type": "application/pdf"}), "Upload")

        body = {"assetID": asset["assetID"], "shiftHeadings": shift_headings,
                "generateReport": report_dest is not None}
        r = self._check(self.session.post(f"{self.base}/operation/autotag", headers=self._headers(),
                                          json=body, timeout=60), "Start AutoTag job")
        location = r.headers.get("location") or r.headers.get("Location")
        if not location:
            raise AutoTagError("AutoTag job started but Adobe returned no status URL")

        log("  AutoTag: tagging", end="", flush=True)
        deadline = time.time() + self.timeout_s
        while True:
            r = self._check(self.session.get(location, headers=self._headers(False), timeout=60), "Job status")
            job = r.json()
            status = job.get("status")
            if status == "done":
                break
            if status == "failed":
                err = job.get("error") or {}
                raise AutoTagError(f"AutoTag failed: {err.get('code', '')} {err.get('message', job)}")
            if time.time() > deadline:
                raise AutoTagError(f"AutoTag timed out after {self.timeout_s}s")
            log(".", end="", flush=True)
            try:
                wait = float(r.headers.get("retry-after", 2))
            except ValueError:
                wait = 2
            time.sleep(min(max(wait, 1), 10))
        log(" done")

        tagged = job.get("tagged-pdf") or {}
        if "downloadUri" not in tagged:
            raise AutoTagError(f"AutoTag finished but returned no tagged PDF: {job}")
        self._download(tagged["downloadUri"], dest)
        if report_dest and (job.get("report") or {}).get("downloadUri"):
            self._download(job["report"]["downloadUri"], report_dest)
        return dest

    def _download(self, uri: str, dest: str) -> None:
        r = self._check(self.session.get(uri, timeout=300), "Download")
        Path(dest).write_bytes(r.content)
