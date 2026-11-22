"""Persist local projects and isolate browser sessions; no access gate or quota."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from .domain import models


def encode(value):
    if isinstance(value, Enum):
        return {"$enum": type(value).__name__, "value": value.value}
    if is_dataclass(value):
        return {"$model": type(value).__name__, "fields": {f.name: encode(getattr(value, f.name)) for f in fields(value)}}
    if isinstance(value, dict):
        return {k: encode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [encode(v) for v in value]
    return value


def decode(value):
    if isinstance(value, list):
        return [decode(v) for v in value]
    if not isinstance(value, dict):
        return value
    if "$model" in value:
        cls = getattr(models, value["$model"], None)
        if not cls or not is_dataclass(cls):
            raise ValueError("Invalid stored model")
        allowed = {f.name for f in fields(cls)}
        return cls(**{k: decode(v) for k, v in value["fields"].items() if k in allowed})
    if "$enum" in value:
        cls = getattr(models, value["$enum"], None)
        if not cls or not issubclass(cls, Enum):
            raise ValueError("Invalid stored enum")
        return cls(value["value"])
    return {k: decode(v) for k, v in value.items()}


class SessionStore:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        # Preserve existing project records and signing secret from earlier builds.
        self.path = directory / "trial.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS visitors (id TEXT PRIMARY KEY, state TEXT);
                CREATE TABLE IF NOT EXISTS canvases (visitor_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, document TEXT NOT NULL);
            """)
            db.execute("INSERT OR IGNORE INTO settings VALUES ('secret',?)", (secrets.token_hex(32),))
            self.secret = db.execute("SELECT value FROM settings WHERE key='secret'").fetchone()[0].encode()
        os.chmod(self.path, 0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def digest(self, value):
        return hmac.new(self.secret, value.encode(), hashlib.sha256).hexdigest()

    def visitor(self, cookie):
        raw, _, signature = (cookie or "").partition(".")
        if len(raw) != 64 or not hmac.compare_digest(signature, self.digest("visitor:" + raw)):
            raw = secrets.token_hex(32)
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO visitors(id) VALUES (?)", (raw,))
        return raw, raw + "." + self.digest("visitor:" + raw)

    def load(self, visitor):
        with self.connect() as db:
            row = db.execute("SELECT state FROM visitors WHERE id=?", (visitor,)).fetchone()
        if not row or not row[0]:
            return None
        saved = json.loads(row[0])
        # Retire synthetic public-demo sessions without discarding real projects.
        if saved.get("fields", {}).get("demo_mode") is True:
            return None
        return decode(saved)

    def save(self, visitor, state):
        with self.connect() as db:
            db.execute("UPDATE visitors SET state=? WHERE id=?", (json.dumps(encode(state), ensure_ascii=False), visitor))

    def load_canvas(self, visitor):
        from .canvas import empty_canvas
        with self.connect() as db:
            row = db.execute("SELECT revision,document FROM canvases WHERE visitor_id=?", (visitor,)).fetchone()
        return {"revision": row[0], "document": json.loads(row[1])} if row else {"revision": 0, "document": empty_canvas()}

    def save_canvas(self, visitor, revision, document):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT revision FROM canvases WHERE visitor_id=?", (visitor,)).fetchone()
            current = row[0] if row else 0
            if isinstance(revision, bool) or not isinstance(revision, int) or revision != current:
                return None
            db.execute("INSERT OR REPLACE INTO canvases VALUES (?,?,?)", (visitor, current + 1, json.dumps(document, ensure_ascii=False)))
        return current + 1
