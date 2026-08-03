"""Dateibasierte Mail-Speicherung.

Jede angenommene Mail wird als ``<id>.eml`` (rohe RFC-5322-Nachricht) plus
einer zugehörigen ``<id>.flags``-Datei (durch Leerzeichen getrennte IMAP-Flags)
in MAIL_DIR geschrieben. IDs sind fortlaufende Ganzzahlen ab 1 und spiegeln
damit das Dateiformat des gosmtp-Referenzservers, sodass Tooling für den einen
auch mit dem anderen kompatibel ist.
"""
import os
import threading
from email import message_from_string
from email.utils import formatdate, getaddresses
from pathlib import Path

MAIL_DIR = Path(os.environ.get("MAIL_DIR", "mails"))

_lock = threading.Lock()


def ensure_mail_dir():
    MAIL_DIR.mkdir(parents=True, exist_ok=True)


def _eml_path(msg_id):
    return MAIL_DIR / f"{msg_id}.eml"


def _flags_path(msg_id):
    return MAIL_DIR / f"{msg_id}.flags"


def next_id():
    max_id = 0
    for p in MAIL_DIR.glob("*.eml"):
        try:
            n = int(p.stem)
        except ValueError:
            continue
        if n > max_id:
            max_id = n
    return max_id + 1


def count_mails():
    return len(list(MAIL_DIR.glob("*.eml")))


def save_mail(from_addr, to_addrs, raw):
    """Speichert einen rohen DATA-Payload und generiert Header, falls keine gesendet wurden."""
    if "SUBJECT:" not in raw.upper():
        header_template = (
            "From: {from_addr}\r\n"
            "To: {to_addrs}\r\n"
            "Subject: Mock Mail {msg_id}\r\n"
            f"Date: {formatdate(localtime=False)}\r\n\r\n"
        )
    else:
        header_template = None

    # next_id() (Verzeichnis-Scan) und das Anlegen der Datei müssen atomar
    # zusammen ablaufen, sonst können zwei parallele Sessions (Threading-
    # Server!) dieselbe ID berechnen und sich gegenseitig überschreiben.
    with _lock:
        msg_id = next_id()

        if header_template is not None:
            header = header_template.format(from_addr=from_addr, to_addrs=", ".join(to_addrs), msg_id=msg_id)
            content = header + raw
        else:
            # Stellt sicher, dass eine Leerzeile zwischen Headern und Body steht,
            # auch wenn der Client nach dem letzten Header keine gesendet hat.
            header_ended = False
            fixed = []
            for line in raw.split("\n"):
                trimmed = line.strip()
                if not header_ended and trimmed != "" and ":" not in line:
                    fixed.append("\r\n")
                    header_ended = True
                if trimmed == "":
                    header_ended = True
                fixed.append(line + "\n")
            content = "".join(fixed)

        content = content.replace("\r\n", "\n").replace("\n", "\r\n")

        with open(_eml_path(msg_id), "w", encoding="utf-8", newline="") as f:
            f.write(content)
        _flags_path(msg_id).write_text("", encoding="utf-8")
        return msg_id


def load_flags(msg_id):
    p = _flags_path(msg_id)
    if not p.exists():
        return []
    return p.read_text(encoding="utf-8").split()


def save_flags(msg_id, flags):
    _flags_path(msg_id).write_text(" ".join(flags), encoding="utf-8")


def load_mail(msg_id):
    p = _eml_path(msg_id)
    if not p.exists():
        return ""
    with open(p, "r", encoding="utf-8", newline="") as f:
        return f.read()


def _format_addr_list(msg, header_name):
    raw_headers = msg.get_all(header_name)
    if not raw_headers:
        return "NIL"
    addrs = getaddresses(raw_headers)
    if not addrs:
        return "NIL"
    parts = []
    for name, addr in addrs:
        name_part = f'"{name}"' if name else "NIL"
        if "@" in addr:
            mailbox, host = addr.split("@", 1)
        else:
            mailbox, host = "user", "unknown"
        parts.append(f'({name_part} NIL "{mailbox}" "{host}")')
    return "(" + " ".join(parts) + ")"


def get_envelope(raw):
    """Baut eine IMAP-ENVELOPE-Struktur für eine gespeicherte Nachricht."""
    if not raw.strip():
        return '(NIL "Format Error: Missing Blank Line" NIL NIL NIL NIL NIL NIL NIL NIL)'

    msg = message_from_string(raw)

    date = msg.get("Date") or formatdate(localtime=False)
    subject = msg.get("Subject") or "No Subject"
    from_ = _format_addr_list(msg, "From")
    reply_to = _format_addr_list(msg, "Reply-To")
    to = _format_addr_list(msg, "To")

    return f'("{date}" "{subject}" {from_} {from_} {reply_to} {to} NIL NIL NIL NIL)'
