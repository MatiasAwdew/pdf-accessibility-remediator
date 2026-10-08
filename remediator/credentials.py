"""API keys: environment variables win, otherwise a per-user credentials file
written by `remediate.py setup` (never stored in the project folder)."""

from __future__ import annotations

import getpass
import json
import os
from pathlib import Path

KEYS = {
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "adobe_client_id": "PDF_SERVICES_CLIENT_ID",
    "adobe_client_secret": "PDF_SERVICES_CLIENT_SECRET",
}


def path() -> Path:
    base = os.environ.get("APPDATA") or os.path.join(Path.home(), ".config")
    return Path(base) / "pdf-remediator" / "credentials.json"


def _file() -> dict:
    try:
        return json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def get(name: str) -> str | None:
    return os.environ.get(KEYS[name]) or _file().get(name) or None


def setup(values: dict[str, str | None]) -> Path:
    """Save keys. Missing values are prompted for (hidden input); blank keeps the old one."""
    data = _file()
    prompts = {
        "anthropic_api_key": "Claude API key (console.anthropic.com > API keys)",
        "adobe_client_id": "Adobe PDF Services client_id",
        "adobe_client_secret": "Adobe PDF Services client_secret",
    }
    for name, label in prompts.items():
        val = values.get(name)
        if val is None:
            current = " [saved - Enter to keep]" if data.get(name) else ""
            val = getpass.getpass(f"{label}{current}: ").strip()
        if val:
            data[name] = val
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return p


def status() -> dict[str, str]:
    out = {}
    f = _file()
    for name, env in KEYS.items():
        if os.environ.get(env):
            out[name] = f"set (env {env})"
        elif f.get(name):
            out[name] = "set (credentials file)"
        else:
            out[name] = "missing"
    return out
