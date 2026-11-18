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
COPY main.py /app/
COPY users.json /app/data/config/users.json
RUN python -m compileall -q --invalidation-mode unchecked-hash /app/src /app/main.py && \
    install -d -o 1000 -g 1000 -m 0750 /app/data/mails && \
    chown 1000:1000 /app/data/config /app/data/config/users.json

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
    SMTP_TIMEOUT=300 \
    SMTP_MAX_MESSAGE_SIZE=10485760 \
    SMTP_AUTH_REQUIRED=true \
    IMAP_HOST=0.0.0.0 \
    IMAP_PORT=1143 \
    IMAP_TIMEOUT=1800 \
    HTTP_HOST=0.0.0.0 \
    HTTP_PORT=8025 \
    MAIL_DIR=/app/data/mails \
    USERS_FILE=/app/data/config/users.json

RUN groupadd -r -g 1000 appuser && useradd -r -u 1000 -g appuser -M -s /usr/sbin/nologin appuser

WORKDIR /app

# venv und Code gehören root, der Benutzer darf nur unter /app/data schreiben
# (COPY --from übernimmt die Besitzer aus dem Build-Stage, also UID 1000 für mails/ und config/)
COPY --from=build /opt/venv /opt/venv
COPY --from=build /app /app

USER appuser

EXPOSE 1025 1143 8025

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ['HTTP_PORT']}/health\", timeout=2)"]

# /app/data enthält mails/ (MAIL_DIR, die .eml-Testmails) und config/users.json (USERS_FILE).
# Ein leeres Named Volume übernimmt beim ersten Start beide Unterordner samt users.json aus dem Image.
# Bei einem Bind-Mount muss config/users.json selbst angelegt werden, sonst gilt nur MAIL_USER/MAIL_PASS.
VOLUME ["/app/data"]

CMD ["python", "main.py"]
