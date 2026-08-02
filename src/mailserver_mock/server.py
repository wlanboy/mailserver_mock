"""Startet die SMTP- und IMAP-Mock-Listener in Hintergrund-Threads."""
import logging
import threading
import time

from . import imap_server, smtp_server, storage

_lock = threading.Lock()
_servers = None


def start_mail_server():
    """Startet beide Listener idempotent; kann gefahrlos mehrfach aufgerufen werden."""
    global _servers

    storage.ensure_mail_dir()

    with _lock:
        if _servers is not None:
            return _servers

        smtp = smtp_server.start_smtp()
        imap = imap_server.start_imap()

        threading.Thread(target=smtp.serve_forever, daemon=True).start()
        threading.Thread(target=imap.serve_forever, daemon=True).start()

        _servers = (smtp, imap)

    time.sleep(0.3)
    return _servers


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    start_mail_server()
    print(f"SMTP listening on {smtp_server.SMTP_HOST}:{smtp_server.SMTP_PORT}")
    print(f"IMAP listening on {imap_server.IMAP_HOST}:{imap_server.IMAP_PORT}")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
