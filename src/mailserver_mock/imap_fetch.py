"""FETCH-Items parsen und Antworten bauen (RFC 3501 §6.4.5, §7.4.2).

``parse_items`` zerlegt den Item-Teil eines FETCH-Kommandos, ``render`` baut
daraus die untagged ``* <seq> FETCH (...)``-Antwort für eine Nachricht. Es
werden nur die angefragten Items geliefert.
"""
import email.policy
import re
import time
from dataclasses import dataclass
from email import message_from_bytes

from . import storage

_MACROS = {
    "ALL": ("FLAGS", "INTERNALDATE", "RFC822.SIZE", "ENVELOPE"),
    "FAST": ("FLAGS", "INTERNALDATE", "RFC822.SIZE"),
    "FULL": ("FLAGS", "INTERNALDATE", "RFC822.SIZE", "ENVELOPE", "BODY"),
}
_SIMPLE_ITEMS = {
    "FLAGS",
    "UID",
    "ENVELOPE",
    "INTERNALDATE",
    "RFC822",
    "RFC822.HEADER",
    "RFC822.SIZE",
    "RFC822.TEXT",
    "BODY",
    "BODYSTRUCTURE",
}
_SECTION_SPECS = {"", "HEADER", "TEXT", "MIME", "HEADER.FIELDS", "HEADER.FIELDS.NOT"}
_ITEM_RE = re.compile(r"([A-Za-z0-9.]+)(?:\[([^\]]*)\])?(?:<(\d+)(?:\.(\d+))?>)?")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_PART_POLICY = email.policy.compat32.clone(linesep="\r\n", mangle_from_=False)


@dataclass(frozen=True)
class FetchItem:
    """Ein FETCH-Item. ``name`` ist ``"BODY[]"`` für alle ``BODY[...]``-Varianten."""

    name: str
    part: tuple = ()  # Part-Nummern, z. B. (1, 2) für "1.2"
    spec: str = ""  # "", HEADER, TEXT, MIME, HEADER.FIELDS, HEADER.FIELDS.NOT
    fields: tuple = ()  # Header-Namen für HEADER.FIELDS(.NOT)
    peek: bool = False
    start: int | None = None
    length: int | None = None

    def response_name(self):
        if self.name != "BODY[]":
            return self.name
        section = ".".join([str(n) for n in self.part] + ([self.spec] if self.spec else []))
        if self.fields:
            section += f" ({' '.join(self.fields)})"
        origin = f"<{self.start}>" if self.start is not None else ""
        return f"BODY[{section}]{origin}"


def _parse_section(section):
    section = section.strip().upper()
    fields = ()
    if "(" in section:
        section, _, field_list = section.partition("(")
        if not field_list.endswith(")"):
            raise ValueError("invalid header field list")
        fields = tuple(f.strip('"') for f in field_list[:-1].split())
        section = section.strip()

    tokens = section.split(".") if section else []
    part = []
    while tokens and tokens[0].isdigit():
        number = int(tokens.pop(0))
        if number < 1:
            raise ValueError("invalid part number")
        part.append(number)
    spec = ".".join(tokens)

    if spec not in _SECTION_SPECS:
        raise ValueError(f"unknown section {spec}")
    if spec == "MIME" and not part:
        raise ValueError("MIME requires a part number")
    if spec.startswith("HEADER.FIELDS") != bool(fields):
        raise ValueError("HEADER.FIELDS requires a field list")
    return tuple(part), spec, fields


def parse_items(spec):
    """Zerlegt z. B. ``(UID FLAGS BODY.PEEK[HEADER.FIELDS (From Subject)])``.

    Wirft ``ValueError`` bei unbekannten oder fehlerhaften Items.
    """
    spec = spec.strip()
    if spec.startswith("(") and spec.endswith(")"):
        spec = spec[1:-1]

    items = []
    pos = 0
    while pos < len(spec):
        if spec[pos] == " ":
            pos += 1
            continue
        match = _ITEM_RE.match(spec, pos)
        if not match or match.end() == pos:
            raise ValueError(f"invalid item at {spec[pos:]!r}")
        pos = match.end()
        name, section, start, length = match.groups()
        name = name.upper()

        if section is None:
            if start is not None:
                raise ValueError(f"partial without section: {name}")
            if name in _MACROS:
                items.extend(FetchItem(n) for n in _MACROS[name])
            elif name in _SIMPLE_ITEMS:
                items.append(FetchItem(name))
            else:
                raise ValueError(f"unknown item {name}")
            continue

        if name not in ("BODY", "BODY.PEEK"):
            raise ValueError(f"unknown item {name}[...]")
        part, sec_spec, fields = _parse_section(section)
        items.append(
            FetchItem(
                "BODY[]",
                part=part,
                spec=sec_spec,
                fields=fields,
                peek=name == "BODY.PEEK",
                start=int(start) if start is not None else None,
                length=int(length) if length is not None else None,
            )
        )

    if not items:
        raise ValueError("no fetch items")
    return items


def marks_seen(items):
    """RFC 3501 §6.4.5: BODY[...] ohne PEEK, RFC822 und RFC822.TEXT setzen \\Seen."""
    return any((i.name == "BODY[]" and not i.peek) or i.name in ("RFC822", "RFC822.TEXT") for i in items)


def delivers_content(items):
    """True, wenn Header oder Body der Nachricht ausgeliefert werden (fürs Logging)."""
    return any(i.name in ("BODY[]", "RFC822", "RFC822.HEADER", "RFC822.TEXT") for i in items)


class StoredMessage:
    """Lädt eine gespeicherte Mail erst, wenn ein Item ihren Inhalt braucht."""

    def __init__(self, msg_id):
        self.msg_id = msg_id
        self._raw = None
        self._parsed = None

    @property
    def raw(self):
        if self._raw is None:
            self._raw = storage.load_mail(self.msg_id)
        return self._raw

    @property
    def parsed(self):
        if self._parsed is None:
            self._parsed = message_from_bytes(self.raw)
        return self._parsed


def _split_raw(raw):
    """Teilt in Header (inkl. Leerzeile) und Body. Ohne Leerzeile ist alles Header."""
    positions = [(i, len(sep)) for sep in (b"\r\n\r\n", b"\n\n") if (i := raw.find(sep)) != -1]
    if not positions:
        header = raw if raw.endswith(b"\n") or not raw else raw + b"\r\n"
        return header + b"\r\n", b""
    i, sep_len = min(positions)
    return raw[: i + sep_len], raw[i + sep_len :]


def _filter_header(header, fields, negate):
    wanted = {f.upper() for f in fields}
    kept = []
    for field in re.split(rb"\r?\n(?![ \t])", header):
        if not field.strip():
            continue
        name = field.split(b":", 1)[0].strip().decode("ascii", errors="replace").upper()
        if (name in wanted) != negate:
            kept.append(field + b"\r\n")
    return b"".join(kept) + b"\r\n"


def _part_bytes(part):
    try:
        return part.as_bytes(policy=_PART_POLICY)
    except (KeyError, LookupError, UnicodeError):
        return b""


def _find_part(msg, numbers):
    """Navigiert zu Part ``1.2.…``. Eine Nachricht ohne Multipart hat nur Part 1."""
    current = msg
    for number in numbers:
        if current.get_content_maintype() == "multipart":
            children = current.get_payload()
            if not isinstance(children, list) or number > len(children):
                return None
            current = children[number - 1]
        elif current.get_content_type() == "message/rfc822" and current is not msg:
            inner = current.get_payload()
            current = inner[0] if isinstance(inner, list) and inner else None
            if current is None:
                return None
            if current.get_content_maintype() != "multipart" and number != 1:
                return None
        elif number != 1:
            return None
    return current


def _apply_spec(raw, spec, fields):
    header, text = _split_raw(raw)
    if spec == "":
        return raw
    if spec == "HEADER":
        return header
    if spec == "TEXT":
        return text
    return _filter_header(header, fields, negate=spec.endswith(".NOT"))


def _section_data(message, item):
    if not item.part:
        data = _apply_spec(message.raw, item.spec, item.fields)
    else:
        part = _find_part(message.parsed, item.part)
        if part is None:
            data = b""
        else:
            # Part 1 einer Nachricht ohne Multipart ist die Nachricht selbst.
            part_raw = message.raw if part is message.parsed else _part_bytes(part)
            if item.spec == "":
                data = _split_raw(part_raw)[1]
            elif item.spec == "MIME":
                data = _split_raw(part_raw)[0]
            elif part.get_content_type() == "message/rfc822":
                inner = part.get_payload()
                inner_raw = _part_bytes(inner[0]) if isinstance(inner, list) and inner else b""
                data = _apply_spec(inner_raw, item.spec, item.fields)
            else:
                data = b""

    if item.start is not None:
        end = item.start + item.length if item.length is not None else None
        data = data[item.start : end]
    return data


def _literal(name, data):
    return f"{name} {{{len(data)}}}\r\n".encode("utf-8") + data


def _params(part):
    params = part.get_params()
    if not params:
        return '("CHARSET" "us-ascii")' if part.get_content_maintype() == "text" else "NIL"
    pairs = [f"{storage.imap_string(k.upper())} {storage.imap_string(v)}" for k, v in params[1:] if isinstance(v, str)]
    return f"({' '.join(pairs)})" if pairs else "NIL"


def _body_structure(part, raw=None):
    """BODY/BODYSTRUCTURE ohne Extension-Daten (die sind laut Grammatik optional)."""
    if part.get_content_maintype() == "multipart":
        children = part.get_payload()
        if isinstance(children, list) and children:
            inner = "".join(_body_structure(child) for child in children)
            return f"({inner} {storage.imap_string(part.get_content_subtype().upper())})"

    body = _split_raw(raw if raw is not None else _part_bytes(part))[1]
    maintype = part.get_content_maintype().upper()
    fields = " ".join(
        [
            storage.imap_string(maintype),
            storage.imap_string(part.get_content_subtype().upper()),
            _params(part),
            storage.imap_string(part.get("Content-ID")),
            storage.imap_string(part.get("Content-Description")),
            storage.imap_string((part.get("Content-Transfer-Encoding") or "7BIT").strip().upper()),
            str(len(body)),
        ]
    )
    lines = body.count(b"\n") + (1 if body and not body.endswith(b"\n") else 0)
    if maintype == "TEXT":
        fields += f" {lines}"
    elif part.get_content_type() == "message/rfc822":
        inner = part.get_payload()
        if isinstance(inner, list) and inner:
            inner_raw = _part_bytes(inner[0])
            envelope = storage.get_envelope(inner_raw)
            fields += f" {envelope} {_body_structure(inner[0], inner_raw)} {lines}"
    return f"({fields})"


def _internal_date(msg_id):
    """Empfangszeitpunkt = mtime der ``.eml``-Datei, in UTC."""
    try:
        t = time.gmtime(storage.mail_path(msg_id).stat().st_mtime)
    except OSError:
        t = time.gmtime()
    return f'"{t.tm_mday:02d}-{_MONTHS[t.tm_mon - 1]}-{t.tm_year} {t.tm_hour:02d}:{t.tm_min:02d}:{t.tm_sec:02d} +0000"'


def _render_item(item, message, flags):
    if item.name == "UID":
        return f"UID {message.msg_id}".encode()
    if item.name == "FLAGS":
        return f"FLAGS ({' '.join(flags)})".encode()
    if item.name == "INTERNALDATE":
        return f"INTERNALDATE {_internal_date(message.msg_id)}".encode()
    if item.name == "RFC822.SIZE":
        return f"RFC822.SIZE {len(message.raw)}".encode()
    if item.name == "ENVELOPE":
        return storage.encode_response(f"ENVELOPE {storage.get_envelope(message.raw)}")
    if item.name in ("BODY", "BODYSTRUCTURE"):
        return storage.encode_response(f"{item.name} {_body_structure(message.parsed, message.raw)}")
    if item.name == "RFC822":
        return _literal("RFC822", message.raw)
    if item.name == "RFC822.HEADER":
        return _literal("RFC822.HEADER", _split_raw(message.raw)[0])
    if item.name == "RFC822.TEXT":
        return _literal("RFC822.TEXT", _split_raw(message.raw)[1])
    return _literal(item.response_name(), _section_data(message, item))


def render(seq, message, flags, items, include_uid=False, include_flags=False):
    """Baut die komplette ``* <seq> FETCH (...)``-Antwort als Bytes.

    ``include_uid`` erzwingt UID (Pflicht bei UID FETCH), ``include_flags``
    hängt FLAGS an, wenn sich \\Seen geändert hat und FLAGS nicht angefragt war.
    """
    names = {i.name for i in items}
    items = list(items)
    if include_uid and "UID" not in names:
        items.insert(0, FetchItem("UID"))
    if include_flags and "FLAGS" not in names:
        items.append(FetchItem("FLAGS"))

    seen = set()
    rendered = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        rendered.append(_render_item(item, message, flags))
    return f"* {seq} FETCH (".encode() + b" ".join(rendered) + b")\r\n"
