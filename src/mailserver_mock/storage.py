"""Dateibasierte Mail-Speicherung.

Jede angenommene Mail wird als ``<id>.eml`` (rohe RFC-5322-Nachricht) plus
einer zugehörigen ``<id>.flags``-Datei (durch Leerzeichen getrennte IMAP-Flags)
in MAIL_DIR geschrieben. IDs sind fortlaufende Ganzzahlen ab 1 und spiegeln
damit das Dateiformat des gosmtp-Referenzservers, sodass Tooling für den einen
auch mit dem anderen kompatibel ist.
"""
import os
import re
import threading
from email import message_from_string
from email.utils import formatdate, getaddresses
from pathlib import Path

MAIL_DIR = Path(os.environ.get("MAIL_DIR", "mails"))

_lock = threading.Lock()


def ensure_mail_dir():
    MAIL_DIR.mkdir(parents=True, exist_ok=True)


def _checked_id(msg_id):
    """Lässt nur positive Integer-IDs zu, damit kein Client-Input (z. B.
    "1:1" oder "../../evil") in einen Dateipfad gelangt."""
    if isinstance(msg_id, bool) or not isinstance(msg_id, int) or msg_id < 1:
        raise ValueError(f"invalid message id: {msg_id!r}")
    return msg_id


def _eml_path(msg_id):
    return MAIL_DIR / f"{_checked_id(msg_id)}.eml"


def _flags_path(msg_id):
    return MAIL_DIR / f"{_checked_id(msg_id)}.flags"


def _ensure_header_separator(raw):
    """Fügt eine Leerzeile vor dem Body ein, falls der Client nach dem letzten
    Header keine gesendet hat. Der Body selbst bleibt unverändert."""
    lines = raw.replace("\r\n", "\n").split("\n")
    for idx, line in enumerate(lines):
        if line.strip() == "":
            break  # reguläres Header-Ende
        if line[0] in " \t":
            continue  # gefaltete Header-Zeile (RFC 5322 §2.2.3)
        name, sep, _ = line.partition(":")
        if sep and name and not any(c.isspace() for c in name):
            continue  # Header-Feld "Name: Wert"
        lines.insert(idx, "")
        break
    return "\n".join(lines)


def list_ids():
    """Liefert alle vorhandenen Mail-IDs (= IMAP-UIDs) aufsteigend sortiert.

    Einzige Quelle für Zählung und ID-Vergabe: Lücken (z. B. eine von Hand
    gelöschte ``2.eml``) bleiben Lücken in den UIDs, die IMAP-Sequenznummern
    1..N werden darauf abgebildet.
    """
    ids = []
    for p in MAIL_DIR.glob("*.eml"):
        try:
            ids.append(int(p.stem))
        except ValueError:
            continue
    ids.sort()
    return ids


def next_id():
    ids = list_ids()
    return (ids[-1] if ids else 0) + 1


def count_mails():
    return len(list_ids())


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
            content = _ensure_header_separator(raw)

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


def mail_path(msg_id):
    """Pfad der ``.eml``-Datei, z. B. für Größe und Empfangszeitpunkt (mtime)."""
    return _eml_path(msg_id)


def delete_mail(msg_id):
    """Löscht eine Mail samt Flags. Liefert False, wenn sie nicht existierte."""
    with _lock:
        eml = _eml_path(msg_id)
        if not eml.exists():
            return False
        eml.unlink(missing_ok=True)
        _flags_path(msg_id).unlink(missing_ok=True)
        return True


def delete_all():
    """Löscht alle Mails (Reset für Integrationstests), liefert die Anzahl.

    Läuft unter demselben Lock wie save_mail, damit eine parallel eingehende
    Mail entweder vollständig vor oder vollständig nach dem Reset landet.
    """
    with _lock:
        ids = list_ids()
        for msg_id in ids:
            _eml_path(msg_id).unlink(missing_ok=True)
            _flags_path(msg_id).unlink(missing_ok=True)
        for orphan in MAIL_DIR.glob("*.flags"):
            orphan.unlink(missing_ok=True)
        return len(ids)


def _imap_string(value):
    """Kodiert einen Wert als IMAP-String (RFC 3501 §4.3).

    Reines ASCII ohne CR/LF wird als quoted string mit escaptem \\ und "
    gesendet, alles andere (z. B. unkodierte Umlaute) als Literal {n}.
    """
    if value is None:
        return "NIL"
    value = str(value)
    # Gefaltete Header entfalten, verbleibende Zeilenumbrüche entfernen.
    value = re.sub(r"\r?\n(?=[ \t])", "", value).replace("\r", "").replace("\n", "")
    if value.isascii():
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    data = value.encode("utf-8")
    return f"{{{len(data)}}}\r\n{value}"


def _format_addr_list(msg, header_name):
    raw_headers = msg.get_all(header_name)
    if not raw_headers:
        return "NIL"
    addrs = getaddresses(raw_headers)
    if not addrs:
        return "NIL"
    parts = []
    for name, addr in addrs:
        name_part = _imap_string(name) if name else "NIL"
        if "@" in addr:
            mailbox, host = addr.split("@", 1)
        else:
            mailbox, host = "user", "unknown"
        parts.append(f"({name_part} NIL {_imap_string(mailbox)} {_imap_string(host)})")
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

    return f"({_imap_string(date)} {_imap_string(subject)} {from_} {from_} {reply_to} {to} NIL NIL NIL NIL)"
