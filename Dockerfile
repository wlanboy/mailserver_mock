FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    SMTP_HOST=0.0.0.0 \
    SMTP_PORT=1025 \
    IMAP_HOST=0.0.0.0 \
    IMAP_PORT=1143 \
    MAIL_DIR=/app/mails \
    USERS_FILE=/app/users.json

WORKDIR /app

RUN groupadd -r -g 1000 appuser && useradd -r -g appuser -u 1000 appuser

COPY --chown=appuser:appuser main.py users.json /app/
COPY --chown=appuser:appuser src/ /app/src/

RUN mkdir -p /app/mails && chown -R appuser:appuser /app/mails

USER appuser

EXPOSE 1025 1143

# Mount /app/mails for persisted .eml-Testmails
CMD ["python", "main.py"]
