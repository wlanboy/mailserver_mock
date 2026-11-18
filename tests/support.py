"""Gemeinsame Hilfen für die End-to-End-Tests.

Kein ``conftest.py``: Die Tests laufen auch mit ``python -m unittest discover``,
das kennt keine pytest-Fixtures. Jedes Testmodul importiert dieses Modul zuerst
(MAIL_DIR muss vor dem Import von ``mailserver_mock`` gesetzt sein) und setzt
``setUpModule = start_servers``.
"""
import base64
import json
import os
import socket
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any

os.environ.setdefault("MAIL_DIR", tempfile.mkdtemp(prefix="mailserver_mock_tests_"))

from mailserver_mock import server, storage  # noqa: E402
from mailserver_mock.http_server import HTTP_HOST, HTTP_PORT  # noqa: E402
from mailserver_mock.imap_server import IMAP_HOST, IMAP_PORT  # noqa: E402
from mailserver_mock.smtp_server import SMTP_HOST, SMTP_PORT  # noqa: E402

__all__ = [
    "HTTP_HOST", "HTTP_PORT", "IMAP_HOST", "IMAP_PORT", "SMTP_HOST", "SMTP_PORT",
    "ALTERNATIVE_MAIL", "MULTIPART_MAIL",
    "dial", "http", "http_json", "imap_login", "imap_read_until", "imap_select", "read_line",
    "reset_mails", "smtp_auth_login", "smtp_login", "smtp_send_mail", "start_servers", "write_line",
]


def start_servers():
    """Startet SMTP, IMAP und HTTP einmal pro Testlauf (idempotent)."""
    server.start_mail_server()
    time.sleep(0.2)


def reset_mails():
    for p in storage.MAIL_DIR.glob("*"):
        p.unlink()


# --- Sockets -----------------------------------------------------------------

def dial(addr):
    sock = socket.create_connection(addr, timeout=5)
    return sock, sock.makefile("rb"), sock.makefile("wb")


def read_line(r):
    line = r.readline()
    return line.decode("utf-8", errors="replace").strip()


def write_line(w, s):
    """Schickt eine Zeile; ``bytes`` gehen unverändert raus (z. B. 8-Bit-Mails)."""
    data = s if isinstance(s, bytes) else s.encode("utf-8")
    w.write(data + b"\r\n")
    w.flush()


# --- SMTP --------------------------------------------------------------------

def smtp_login(r, w):
    read_line(r)  # 220 Banner
    write_line(w, "EHLO localhost")
    read_line(r)  # 250-localhost greets you (Begrüßung)
    read_line(r)  # 250-AUTH LOGIN PLAIN
    read_line(r)  # 250 OK
    write_line(w, "AUTH LOGIN")
    read_line(r)  # 334 Benutzername:
    write_line(w, base64.b64encode(b"testuser").decode())
    read_line(r)  # 334 Passwort:
    write_line(w, base64.b64encode(b"testpass").decode())
    resp = read_line(r)
    assert "235" in resp, resp


def smtp_auth_login(r, w, username, password):
    """Führt EHLO + AUTH LOGIN mit beliebigen Zugangsdaten aus und gibt die
    finale Server-Antwort zurück (ohne Erfolg vorauszusetzen)."""
    read_line(r)  # 220 Banner
    write_line(w, "EHLO localhost")
    read_line(r)
    read_line(r)
    read_line(r)
    write_line(w, "AUTH LOGIN")
    read_line(r)  # 334 Benutzername:
    write_line(w, base64.b64encode(username.encode()).decode())
    read_line(r)  # 334 Passwort:
    write_line(w, base64.b64encode(password.encode()).decode())
    return read_line(r)


def smtp_send_mail(from_addr, to_addr, body):
    sock, r, w = dial((SMTP_HOST, SMTP_PORT))
    try:
        smtp_login(r, w)
        write_line(w, f"MAIL FROM:{from_addr}")
        read_line(r)
        write_line(w, f"RCPT TO:{to_addr}")
        read_line(r)
        write_line(w, "DATA")
        read_line(r)
        write_line(w, body)
        write_line(w, ".")
        resp = read_line(r)
        assert "250" in resp, resp
    finally:
        sock.close()


# --- IMAP --------------------------------------------------------------------

def imap_login(r, w):
    read_line(r)  # * OK IMAP4rev1 Service Ready (Server bereit)
    write_line(w, "A1 LOGIN testuser testpass")
    resp = read_line(r)
    assert "OK" in resp, resp


def imap_select(r, w, tag, cmd="SELECT"):
    """Wählt INBOX aus und liefert die untagged Antworten (FLAGS, EXISTS, UIDVALIDITY, ...)."""
    write_line(w, f"{tag} {cmd} INBOX")
    lines = imap_read_until(r, tag)
    assert "OK" in lines[-1], lines[-1]
    return lines[:-1]


def imap_read_until(r, tag):
    lines = []
    while True:
        line = read_line(r)
        lines.append(line)
        if line.startswith(tag):
            return lines


# --- HTTP --------------------------------------------------------------------

def http(path, method="GET"):
    """Liefert (status, headers, body) ohne bei 4xx/5xx eine Exception zu werfen."""
    req = urllib.request.Request(f"http://{HTTP_HOST}:{HTTP_PORT}{path}", method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def http_json(path, method="GET") -> "tuple[int, Any]":
    status, _, body = http(path, method)
    return status, json.loads(body) if body else None


# --- Testmails ---------------------------------------------------------------

ALTERNATIVE_MAIL = (
    "Subject: multi\r\n"
    "MIME-Version: 1.0\r\n"
    'Content-Type: multipart/alternative; boundary="XX"\r\n'
    "\r\n"
    "--XX\r\n"
    "Content-Type: text/plain; charset=utf-8\r\n"
    "\r\n"
    "plain text\r\n"
    "--XX\r\n"
    "Content-Type: text/html; charset=utf-8\r\n"
    "\r\n"
    "<p>html</p>\r\n"
    "--XX--\r\n"
)

MULTIPART_MAIL = (
    "From: =?utf-8?q?J=C3=BCrgen?= <juergen@example.com>\r\n"
    "To: Alice <alice@example.com>, bob@example.com\r\n"
    "Cc: carol@example.com\r\n"
    "Subject: =?utf-8?q?Gr=C3=BC=C3=9Fe?= aus dem Test\r\n"
    "Date: Tue, 03 Nov 2026 10:00:00 +0000\r\n"
    "MIME-Version: 1.0\r\n"
    'Content-Type: multipart/mixed; boundary="B1"\r\n'
    "\r\n"
    "--B1\r\n"
    'Content-Type: multipart/alternative; boundary="B2"\r\n'
    "\r\n"
    "--B2\r\n"
    "Content-Type: text/plain; charset=utf-8\r\n"
    "\r\n"
    "Hallo Alice,\r\nder Link: https://example.com/reset\r\n"
    "--B2\r\n"
    "Content-Type: text/html; charset=utf-8\r\n"
    "\r\n"
    "<p>Hallo <b>Alice</b></p>\r\n"
    "--B2--\r\n"
    "--B1\r\n"
    "Content-Type: application/pdf\r\n"
    'Content-Disposition: attachment; filename="rechnung.pdf"\r\n'
    "Content-Transfer-Encoding: base64\r\n"
    "\r\n"
    "JVBERi0xLjQK\r\n"
    "--B1--\r\n"
)
