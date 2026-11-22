"""Persist local projects and isolate browser sessions; no access gate or quota."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import uuid
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
                CREATE TABLE IF NOT EXISTS projects (visitor_id TEXT NOT NULL, id TEXT NOT NULL, title TEXT NOT NULL, title_source TEXT NOT NULL,
                    state TEXT NOT NULL, canvas TEXT NOT NULL, workspace TEXT NOT NULL, summary TEXT NOT NULL,
                    preview_mime TEXT NOT NULL, preview BLOB NOT NULL, preview_kind TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(visitor_id,id));
                CREATE TABLE IF NOT EXISTS image_tasks (visitor_id TEXT NOT NULL, node_id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(visitor_id,node_id));
                CREATE TABLE IF NOT EXISTS image_outputs (visitor_id TEXT NOT NULL, id TEXT NOT NULL, mime TEXT NOT NULL, data BLOB NOT NULL, PRIMARY KEY(visitor_id,id));
            """)
            db.execute("INSERT OR IGNORE INTO settings VALUES ('secret',?)", (secrets.token_hex(32),))
            self.secret = db.execute("SELECT value FROM settings WHERE key='secret'").fetchone()[0].encode()
        os.chmod(self.path, 0o600)
        self._migrate_projects()

    def _migrate_projects(self):
        from .projects import snapshot
        from .demo import create_empty_state
        with self.connect() as db:
            for row in db.execute('SELECT id,state FROM visitors').fetchall():
                raw = json.loads(row['state']) if row['state'] else None
                if raw and raw.get('fields',{}).get('demo_mode') is True:
                    continue
                state = decode(raw) if raw else create_empty_state()
                if not state or getattr(state, 'demo_mode', False):
                    continue
                old = state.project_id
                if state.project_id in {'new-control-plan', 'control-plan', 'context-enrichment'}:
                    state.project_id = uuid.uuid4().hex
                canvas = db.execute('SELECT document FROM canvases WHERE visitor_id=?', (row['id'],)).fetchone()
                if canvas:
                    document = json.loads(canvas[0])
                    for node in document['nodes']:
                        if node.get('project_id') == old:
                            node['project_id'] = state.project_id
                        if node.get('kind') == 'image_task':
                            legacy = db.execute('SELECT data FROM image_tasks WHERE visitor_id=? AND node_id=?', (row['id'],node['id'])).fetchone()
                            if legacy:
                                db.execute('INSERT OR IGNORE INTO image_tasks VALUES (?,?,?)', (row['id'], state.project_id + ':' + node['id'], legacy[0]))
                                db.execute('DELETE FROM image_tasks WHERE visitor_id=? AND node_id=?',(row['id'],node['id']))
                    db.execute('UPDATE canvases SET document=? WHERE visitor_id=?', (json.dumps(document,ensure_ascii=False),row['id']))
                db.execute('UPDATE visitors SET state=? WHERE id=?', (json.dumps(encode(state),ensure_ascii=False),row['id']))
                snapshot(db,row['id'],state)

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
            from .projects import snapshot
            snapshot(db,visitor,state)

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
            row = db.execute('SELECT state FROM visitors WHERE id=?', (visitor,)).fetchone()
            if row and row[0]:
                from .projects import snapshot
                snapshot(db,visitor,decode(json.loads(row[0])))
        return current + 1

    def clear_canvas(self, visitor):
        from .canvas import empty_canvas
        with self.connect() as db:
            row = db.execute('SELECT revision FROM canvases WHERE visitor_id=?',(visitor,)).fetchone()
            db.execute('INSERT OR REPLACE INTO canvases VALUES (?,?,?)',(visitor,row[0]+1 if row else 1,json.dumps(empty_canvas())))
