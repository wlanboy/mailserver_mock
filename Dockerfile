ARG PYTHON_TAG=3.15-slim

FROM python:${PYTHON_TAG} AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app
# Nur fertige Wheels, keine sdists: der Build braucht keinen Compiler.
# venv ohne pip, installiert wird mit dem pip des Base-Images: kein pip im Runtime-Image.
COPY requirements.txt /tmp/requirements.txt
RUN python -m venv --without-pip /opt/venv && \
    pip --python /opt/venv/bin/python install --only-binary=:all: -r /tmp/requirements.txt
# Nur das Paket kopieren (ohne *.md aus src/) und Bytecode vorab erzeugen.
# unchecked-hash: unabhängig von mtimes, zur Laufzeit wird nichts geschrieben (read-only FS).
COPY src/mailserver_mock/ /app/src/mailserver_mock/
COPY main.py users.json /app/
RUN python -m compileall -q --invalidation-mode unchecked-hash /app/src /app/main.py && \
    install -d -o 1000 -g 1000 -m 0750 /app/mails

FROM python:${PYTHON_TAG}
ARG REVISION=unknown
LABEL org.opencontainers.image.title="mailserver-mock" \
      org.opencontainers.image.description="mail server mock" \
      org.opencontainers.image.version="0.1.0" \
      org.opencontainers.image.vendor="wlanboy" \
      org.opencontainers.image.source="https://github.com/wlanboy/mailserver_mock" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.base.name="python:3.15-slim"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    SMTP_HOST=0.0.0.0 \
    SMTP_PORT=1025 \
    IMAP_HOST=0.0.0.0 \
    IMAP_PORT=1143 \
    HTTP_HOST=0.0.0.0 \
    HTTP_PORT=8025 \
    MAIL_DIR=/app/mails \
    USERS_FILE=/app/users.json

RUN groupadd -r -g 1000 appuser && useradd -r -u 1000 -g appuser -M -s /usr/sbin/nologin appuser

WORKDIR /app

# venv und Code gehören root, der Benutzer darf nur in /app/mails schreiben
# (COPY --from übernimmt die Besitzer aus dem Build-Stage, also UID 1000 für /app/mails)
COPY --from=build /opt/venv /opt/venv
COPY --from=build /app /app

USER appuser

EXPOSE 1025 1143 8025

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ['HTTP_PORT']}/health\", timeout=2)"]

# /app/mails als Volume mounten, um die .eml-Testmails zu behalten
CMD ["python", "main.py"]
