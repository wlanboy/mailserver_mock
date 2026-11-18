"""Gemeinsame Socket-Hilfen für SMTP und IMAP: Timeouts und Zeilenlimits."""
import os

# Obergrenze für eine Kommandozeile inklusive Zeilenende. RFC 5321 verlangt nur
# 512 Bytes, RFC 3501 setzt keine feste Grenze; 8 KiB reichen für alle
# unterstützten Kommandos und begrenzen den Speicher pro Verbindung.
MAX_LINE = 8192


def env_timeout(name, default):
    """Liest ein Socket-Timeout in Sekunden aus der Umgebung; ``0`` schaltet es ab."""
    value = float(os.environ.get(name, default))
    return value if value > 0 else None


def read_line(rfile, limit=MAX_LINE):
    """Liest eine Zeile mit höchstens ``limit`` Bytes.

    Rückgabe ``(line, too_long)``. ``line`` ist ``None`` bei Verbindungsende.
    Ist die Zeile zu lang, wird der Rest bis zum Zeilenende gelesen und
    verworfen; ``line`` enthält dann nur den Anfang (z. B. für das IMAP-Tag).
    """
    line = rfile.readline(limit + 1)
    if not line:
        return None, False
    if len(line) <= limit or line.endswith(b"\n"):
        return line, False
    while (chunk := rfile.readline(limit)) and not chunk.endswith(b"\n"):
        pass
    return line, True
