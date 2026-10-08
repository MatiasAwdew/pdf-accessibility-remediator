# PDF Remediator - hosted version (Railway, Render, Fly.io, any Docker host).
# Needs APP_PASSWORD (login) and, for Claude features, ANTHROPIC_API_KEY. See DEPLOY.md.
FROM python:3.13-slim

# Java for veraPDF (the PDF/UA check), and fonts with the same widths as the
# Microsoft fonts documents use (Liberation = Arial/Times/Courier, Carlito =
# Calibri, Caladea = Cambria), so embedding them doesn't move the layout.
RUN apt-get update && apt-get install -y --no-install-recommends \
        default-jre-headless curl unzip fontconfig \
        fonts-liberation fonts-crosextra-carlito fonts-crosextra-caladea fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# veraPDF, installed headless to /opt/verapdf
RUN curl -sSL -o /tmp/verapdf.zip https://software.verapdf.org/releases/verapdf-installer.zip \
    && cd /tmp && unzip -q verapdf.zip \
    && printf '%s\n' \
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>' \
        '<AutomatedInstallation langpack="eng">' \
        '<com.izforge.izpack.panels.htmlhello.HTMLHelloPanel id="welcome"/>' \
        '<com.izforge.izpack.panels.target.TargetPanel id="install_dir"><installpath>/opt/verapdf</installpath></com.izforge.izpack.panels.target.TargetPanel>' \
        '<com.izforge.izpack.panels.packs.PacksPanel id="sdk_pack_select">' \
        '<pack index="0" name="veraPDF GUI" selected="true"/><pack index="1" name="veraPDF Batch files" selected="true"/>' \
        '<pack index="2" name="veraPDF Validation model" selected="true"/><pack index="3" name="veraPDF Documentation" selected="false"/>' \
        '<pack index="4" name="veraPDF Sample Plugins" selected="false"/></com.izforge.izpack.panels.packs.PacksPanel>' \
        '<com.izforge.izpack.panels.install.InstallPanel id="install"/><com.izforge.izpack.panels.finish.FinishPanel id="finish"/>' \
        '</AutomatedInstallation>' > /tmp/auto-install.xml \
    && java -jar /tmp/verapdf-greenfield-*/verapdf-izpack-installer-*.jar /tmp/auto-install.xml \
    && chmod +x /opt/verapdf/verapdf \
    && rm -rf /tmp/verapdf*

WORKDIR /app
COPY requirements-server.txt .
RUN pip install --no-cache-dir -r requirements-server.txt
COPY remediate.py .
COPY remediator ./remediator

ENV PYTHONUNBUFFERED=1 JOBS_DIR=/data/jobs VERAPDF_PATH=/opt/verapdf/verapdf HOME=/data
RUN mkdir -p /data/jobs
EXPOSE 8080
# One process (jobs run in background threads and share its memory), several threads,
# long timeout for big documents.
CMD ["sh", "-c", "exec gunicorn -w 1 --threads 8 -t 900 -b 0.0.0.0:${PORT:-8080} remediator.wsgi:app"]
