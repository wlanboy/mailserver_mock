"""Startet die SMTP-, IMAP- und HTTP-Listener in Hintergrund-Threads."""
import logging
import signal
import threading
import time

from . import http_server, imap_server, smtp_server, storage

_lock = threading.Lock()
_servers = None
_threads = {}


def _listener_up(name):
    """True, solange der serve_forever-Thread läuft und der Socket offen ist."""
    entry = _threads.get(name)
    if entry is None:
        return False
    srv, thread = entry
    return thread.is_alive() and srv.socket.fileno() != -1


def health_check():
    return {"smtp": _listener_up("smtp"), "imap": _listener_up("imap")}


def _serve(name, srv):
    thread = threading.Thread(target=srv.serve_forever, name=f"{name}-server", daemon=True)
    _threads[name] = (srv, thread)
    thread.start()


def start_mail_server():
    """Startet alle Listener idempotent; kann gefahrlos mehrfach aufgerufen werden."""
    global _servers

    storage.ensure_mail_dir()

    with _lock:
        if _servers is not None:
            return _servers

        smtp = smtp_server.start_smtp()
        imap = imap_server.start_imap()
        http = http_server.start_http(health_check=health_check)

        _serve("smtp", smtp)
        _serve("imap", imap)
        _serve("http", http)

        _servers = (smtp, imap, http)

    time.sleep(0.3)
    return _servers


def stop_mail_server():
    """Stoppt alle Listener und schließt ihre Sockets; idempotent."""
    global _servers

    with _lock:
        servers, _servers = _servers, None
    if servers is None:
        return
    for srv in servers:
        srv.shutdown()
        srv.server_close()
    _threads.clear()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    start_mail_server()
    print(f"SMTP listening on {smtp_server.SMTP_HOST}:{smtp_server.SMTP_PORT}")
    print(f"IMAP listening on {imap_server.IMAP_HOST}:{imap_server.IMAP_PORT}")
    print(f"HTTP listening on http://{http_server.HTTP_HOST}:{http_server.HTTP_PORT}/")

    # Als PID 1 im Container gibt es für SIGTERM keine Default-Aktion, daher eigener Handler.
    # Der Handler setzt nur das Event; shutdown() läuft im Haupt-Thread nach wait().
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda signum, frame: stop.set())
    stop.wait()

    logging.getLogger(__name__).info("Signal empfangen, stoppe Listener")
    stop_mail_server()
