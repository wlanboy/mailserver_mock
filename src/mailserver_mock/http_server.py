"""Kleine HTTP-API und Web-UI nach dem Vorbild von MailHog/Mailpit.

Endpunkte:

- ``GET    /health``          Zustand von SMTP, IMAP und Mail-Verzeichnis (200 / 503)
- ``GET    /mails``           Liste aller Mails, neueste zuerst; Filter ``?to=``, ``?from=``, ``?subject=``
- ``GET    /mails/{id}``      Einzelne Mail mit Headern, Text-/HTML-Body und Anhangsliste
- ``GET    /mails/{id}/raw``  Rohe RFC-5322-Nachricht (``message/rfc822``)
- ``DELETE /mails``           Alle Mails löschen (Reset zwischen Tests)
- ``DELETE /mails/{id}``      Einzelne Mail löschen
- ``GET    /``                Web-UI (statische Dateien aus ``static/``)

Die API liest nur aus dem Storage und setzt keine IMAP-Flags.
"""
import json
import logging
import os
import re
from datetime import datetime, timezone
from email import message_from_string, policy
from email.utils import getaddresses
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import storage

HTTP_HOST = os.environ.get("HTTP_HOST", "127.0.0.1")
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8025"))

STATIC_DIR = Path(__file__).parent / "static"

# Feste Whitelist statt Dateisystem-Lookup: kein Pfad aus dem Request landet im open().
_STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/static/app.css": ("app.css", "text/css; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/favicon.svg": ("favicon.svg", "image/svg+xml"),
}

# HTML-Mails werden im UI in einem <iframe sandbox srcdoc> gerendert, das diese
# Policy erbt: keine Skripte, keine externen Ressourcen (Tracking-Pixel), aber
# Inline-Styles, ohne die kaum eine HTML-Mail lesbar ist.
_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
)

_MAIL_RE = re.compile(r"^/mails/([1-9][0-9]*)(/raw)?$")
_SNIPPET_LEN = 160

logger = logging.getLogger(__name__)


def _header(msg, name):
    """Liest einen Header dekodiert (RFC 2047); kaputte Header fallen auf den Rohwert zurück."""
    try:
        value = msg.get(name)
    except Exception:  # defekte Encoded-Words o. Ä.
        value = msg.get_all(name, failobj=[None])[0]
    return None if value is None else str(value)


def _addresses(msg, name):
    try:
        values = msg.get_all(name) or []
        return [
            {"name": n, "address": a}
            for n, a in getaddresses([str(v) for v in values])
            if a or n
        ]
    except Exception:
        return []


def _part_content(part):
    if part is None:
        return None
    try:
        return part.get_content()
    except Exception:
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes):
            return payload.decode("utf-8", errors="replace")
        return None if payload is None else str(payload)


def _bodies(msg):
    try:
        text_part = msg.get_body(preferencelist=("plain",))
        html_part = msg.get_body(preferencelist=("html",))
    except Exception:
        text_part = html_part = None
    text = _part_content(text_part)
    html = _part_content(html_part)
    if text is None and html is None and not msg.is_multipart():
        # Nicht-MIME-Mail ohne Content-Type: als Text behandeln.
        text = _part_content(msg)
    return text, html


def _attachments(msg):
    result = []
    try:
        for part in msg.iter_attachments():
            payload = part.get_payload(decode=True)
            result.append({
                "filename": part.get_filename(),
                "content_type": part.get_content_type(),
                "size": len(payload) if isinstance(payload, bytes) else 0,
            })
    except Exception:
        pass
    return result


def _received_at(msg_id):
    try:
        mtime = storage.mail_path(msg_id).stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(timespec="seconds")


def _summary(msg_id, raw, msg, text):
    flags = storage.load_flags(msg_id)
    snippet = " ".join((text or "").split())[:_SNIPPET_LEN]
    return {
        "id": msg_id,
        "from": _addresses(msg, "From"),
        "to": _addresses(msg, "To"),
        "cc": _addresses(msg, "Cc"),
        "subject": _header(msg, "Subject") or "",
        "date": _header(msg, "Date"),
        "received": _received_at(msg_id),
        "size": len(raw.encode("utf-8")),
        "flags": flags,
        "seen": "\\Seen" in flags,
        "snippet": snippet,
    }


def _load(msg_id):
    """Liefert (raw, msg) oder None, wenn die Mail nicht (mehr) existiert."""
    if not storage.mail_path(msg_id).exists():
        return None
    raw = storage.load_mail(msg_id)
    return raw, message_from_string(raw, policy=policy.default)


def mail_summary(msg_id):
    loaded = _load(msg_id)
    if loaded is None:
        return None
    raw, msg = loaded
    text, _ = _bodies(msg)
    return _summary(msg_id, raw, msg, text)


def mail_detail(msg_id):
    loaded = _load(msg_id)
    if loaded is None:
        return None
    raw, msg = loaded
    text, html = _bodies(msg)
    detail = _summary(msg_id, raw, msg, text)
    detail.update({
        "headers": [[k, str(v)] for k, v in msg.raw_items()],
        "text": text,
        "html": html,
        "attachments": _attachments(msg),
    })
    return detail


def _matches(summary, filters):
    def addr_text(key):
        return " ".join(f"{a['name']} {a['address']}" for a in summary[key]).lower()

    for key, needle in filters.items():
        if key == "subject":
            haystack = summary["subject"].lower()
        elif key == "to":
            haystack = addr_text("to") + " " + addr_text("cc")
        else:
            haystack = addr_text("from")
        if needle.lower() not in haystack:
            return False
    return True


def list_mails(filters=None):
    filters = filters or {}
    result = []
    for msg_id in reversed(storage.list_ids()):
        summary = mail_summary(msg_id)
        if summary is not None and _matches(summary, filters):
            result.append(summary)
    return result


def default_health():
    """Fallback, wenn kein Callback übergeben wurde: nur das Mail-Verzeichnis prüfen."""
    return {"mail_dir": _mail_dir_ok()}


def _mail_dir_ok():
    d = storage.MAIL_DIR
    return d.is_dir() and os.access(d, os.R_OK | os.W_OK | os.X_OK)


class HTTPHandler(BaseHTTPRequestHandler):
    server_version = "mailserver-mock"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # --- Antwort-Hilfen -----------------------------------------------------

    def _send(self, status, body=b"", content_type="application/json", extra_headers=None):
        self.send_response(status)
        if status != HTTPStatus.NO_CONTENT:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status, message, allow=None):
        body = json.dumps({"error": message}).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", {"Allow": allow} if allow else None)

    def log_message(self, format, *args):  # noqa: A002 - Signatur der Basisklasse
        # Health-Probes laufen alle paar Sekunden, daher nur auf DEBUG.
        logger.debug("%s - %s", self.address_string(), format % args)

    # --- Routing ------------------------------------------------------------

    def do_GET(self):
        url = urlsplit(self.path)
        path = url.path

        if path == "/health":
            return self._health()
        if path in _STATIC_FILES:
            return self._static(*_STATIC_FILES[path])
        if path == "/mails":
            query = parse_qs(url.query)
            filters = {k: query[k][0] for k in ("to", "from", "subject") if query.get(k, [""])[0]}
            mails = list_mails(filters)
            return self._json(HTTPStatus.OK, {"total": storage.count_mails(), "count": len(mails), "mails": mails})

        m = _MAIL_RE.match(path)
        if m:
            msg_id = int(m.group(1))
            if m.group(2):
                if not storage.mail_path(msg_id).exists():
                    return self._error(HTTPStatus.NOT_FOUND, f"mail {msg_id} not found")
                raw = storage.load_mail(msg_id).encode("utf-8")
                return self._send(HTTPStatus.OK, raw, "message/rfc822", {
                    "Content-Disposition": f'attachment; filename="{msg_id}.eml"',
                })
            detail = mail_detail(msg_id)
            if detail is None:
                return self._error(HTTPStatus.NOT_FOUND, f"mail {msg_id} not found")
            return self._json(HTTPStatus.OK, detail)

        if path.startswith("/mails/"):
            return self._error(HTTPStatus.NOT_FOUND, "invalid mail id")
        return self._error(HTTPStatus.NOT_FOUND, "not found")

    do_HEAD = do_GET

    def do_DELETE(self):
        path = urlsplit(self.path).path
        if path == "/mails":
            deleted = storage.delete_all()
            logger.info("deleted all mails count=%d", deleted)
            return self._json(HTTPStatus.OK, {"deleted": deleted})
        m = _MAIL_RE.match(path)
        if m and not m.group(2):
            msg_id = int(m.group(1))
            if not storage.delete_mail(msg_id):
                return self._error(HTTPStatus.NOT_FOUND, f"mail {msg_id} not found")
            logger.info("deleted mail id=%d", msg_id)
            return self._send(HTTPStatus.NO_CONTENT)
        if path in _STATIC_FILES or path == "/health" or (m and m.group(2)):
            return self._error(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed", allow="GET, HEAD")
        return self._error(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self):
        self._method_not_allowed()

    do_PUT = do_PATCH = do_POST

    def _method_not_allowed(self):
        path = urlsplit(self.path).path
        allow = "GET, HEAD, DELETE" if path == "/mails" or _MAIL_RE.match(path) else "GET, HEAD"
        self._error(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed", allow=allow)

    # --- Endpunkte ----------------------------------------------------------

    def _health(self):
        checks = self.server.health_check()  # type: ignore[attr-defined]
        checks["mail_dir"] = checks.get("mail_dir", True) and _mail_dir_ok()
        ok = all(checks.values())
        payload = {
            "status": "ok" if ok else "error",
            "checks": {k: "up" if v else "down" for k, v in checks.items()},
            "mails": storage.count_mails() if checks["mail_dir"] else None,
        }
        self._json(HTTPStatus.OK if ok else HTTPStatus.SERVICE_UNAVAILABLE, payload)

    def _static(self, filename, content_type):
        try:
            body = (STATIC_DIR / filename).read_bytes()
        except OSError:
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        headers = {"Content-Security-Policy": _CSP} if content_type.startswith("text/html") else None
        self._send(HTTPStatus.OK, body, content_type, headers)


class HTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, handler, health_check=None):
        super().__init__(address, handler)
        self.health_check = health_check or default_health


def start_http(host=HTTP_HOST, port=HTTP_PORT, health_check=None):
    return HTTPServer((host, port), HTTPHandler, health_check)
