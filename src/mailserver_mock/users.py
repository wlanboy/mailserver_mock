"""Lädt Test-User aus users.json für Login und simulierte Fehlerszenarien.

Fehlt die Datei, wird ein einzelner Normal-User aus MAIL_USER/MAIL_PASS
gebildet, damit bestehende Umgebungsvariablen-Konfigurationen weiterhin
funktionieren.
"""
import json
import os
from pathlib import Path

USERS_FILE = Path(os.environ.get("USERS_FILE", "users.json"))


def _fallback_users():
    return [
        {
            "username": os.environ.get("MAIL_USER", "testuser"),
            "password": os.environ.get("MAIL_PASS", "testpass"),
            "behavior": "normal",
        }
    ]


def load_users():
    if not USERS_FILE.exists():
        return _fallback_users()
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("users", [])


def find_user(username):
    for user in load_users():
        if user.get("username") == username:
            return user
    return None
