"""Production entry point: gunicorn remediator.wsgi:app (see Dockerfile)."""
from .app import HOSTED, RETENTION_DAYS, app, start_cleanup_thread

if HOSTED and RETENTION_DAYS > 0:  # optional auto-delete
    start_cleanup_thread()
