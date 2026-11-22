"""Persistent, server-owned trial grants, quotas and visitor state (stdlib only)."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import secrets
import sqlite3
import time
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from .domain import models


class AccessError(Exception):
    def __init__(self, message, status=403, code="trial_required"):
        super().__init__(message)
        self.status, self.code = status, code


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
        return cls(**{k: decode(v) for k, v in value["fields"].items()})
    if "$enum" in value:
        cls = getattr(models, value["$enum"], None)
        if not cls or not issubclass(cls, Enum):
            raise ValueError("Invalid stored enum")
        return cls(value["value"])
    return {k: decode(v) for k, v in value.items()}


class TrialStore:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "trial.sqlite3"
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS visitors (id TEXT PRIMARY KEY, failures INTEGER DEFAULT 0, code TEXT, state TEXT);
                CREATE TABLE IF NOT EXISTS grants (visitor TEXT, code TEXT, calls INTEGER DEFAULT 0, PRIMARY KEY(visitor,code));
                CREATE TABLE IF NOT EXISTS reservations (id TEXT PRIMARY KEY, visitor TEXT, code TEXT, seconds REAL, status TEXT, job TEXT);
                CREATE TABLE IF NOT EXISTS attempts (bucket TEXT PRIMARY KEY, start REAL, count INTEGER);
            ''')
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

    def codes(self):
        return [self.digest("code:" + c.strip()) for c in os.getenv("TRIAL_CODES", "").split(",") if c.strip()]

    def limit_rate(self, bucket, limit=15, window=60):
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM attempts WHERE bucket=?", (bucket,)).fetchone()
            count = row["count"] + 1 if row and now - row["start"] < window else 1
            start = row["start"] if row and now - row["start"] < window else now
            db.execute("INSERT OR REPLACE INTO attempts VALUES (?,?,?)", (bucket, start, count))
            db.execute("DELETE FROM attempts WHERE start < ?", (now - window * 2,))
        if count > limit:
            raise AccessError("操作过于频繁，请稍后再试。", 429, "rate_limited")

    def verify(self, visitor, code):
        if not self.codes():
            raise AccessError("体验暂未开放，请联系作品作者。", 503, "trial_unconfigured")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM visitors WHERE id=?", (visitor,)).fetchone()
            if row["failures"] >= 5:
                raise AccessError("体验码输入已锁定，请联系作品作者解锁。", 423, "trial_locked")
            if not isinstance(code, str) or not code.strip():
                raise AccessError("请输入体验码。", 400, "trial_empty")
            candidate = self.digest("code:" + code.strip())
            valid = any(hmac.compare_digest(candidate, c) for c in self.codes())
            if valid:
                db.execute("UPDATE visitors SET failures=0,code=? WHERE id=?", (candidate, visitor))
                db.execute("INSERT OR IGNORE INTO grants(visitor,code) VALUES (?,?)", (visitor, candidate))
            else:
                failures = row["failures"] + 1
                db.execute("UPDATE visitors SET failures=? WHERE id=?", (failures, visitor))
        if not valid:
            if failures == 5:
                raise AccessError("连续输入错误已达 5 次，体验码输入已锁定。如需解锁，请联系作品作者。", 423, "trial_locked")
            raise AccessError(f"体验码不正确，还可尝试 {5-failures} 次。", 403, "trial_invalid")
        return self.status(visitor)

    def _grant(self, db, visitor):
        row = db.execute("SELECT * FROM visitors WHERE id=?", (visitor,)).fetchone()
        if not row or row["code"] not in self.codes():
            raise AccessError("输入体验码，解锁 AI 体验。", 401)
        return row["code"]

    def status(self, visitor):
        with self.connect() as db:
            row = db.execute("SELECT * FROM visitors WHERE id=?", (visitor,)).fetchone()
            authorized = bool(row and row["code"] in self.codes())
            totals = db.execute("SELECT status,SUM(seconds) AS seconds FROM reservations WHERE visitor=? AND code=? GROUP BY status", (visitor, row["code"] if row else "")).fetchall()
        amounts = {r["status"]: r["seconds"] for r in totals}
        used, held = amounts.get("used", 0), amounts.get("held", 0)
        return {"authorized": authorized, "locked": bool(row and row["failures"] >= 5), "attempts_remaining": max(0, 5-(row["failures"] if row else 0)), "remaining_seconds": max(0, 30-used-held) if authorized else 0, "reserved_seconds": held if authorized else 0, "used_seconds": used if authorized else 0}

    def require(self, visitor, paid=False):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            code = self._grant(db, visitor)
            if paid:
                calls = db.execute("SELECT calls FROM grants WHERE visitor=? AND code=?", (visitor, code)).fetchone()[0]
                if calls >= int(os.getenv("TRIAL_AI_CALL_LIMIT", "100")):
                    raise AccessError("AI 体验次数已用完，你仍可查看已有内容和演示案例。", 403, "ai_limit")
                db.execute("UPDATE grants SET calls=calls+1 WHERE visitor=? AND code=?", (visitor, code))

    def reserve(self, visitor, seconds):
        seconds = float(seconds)
        if not math.isfinite(seconds) or not 4 <= seconds <= 15:
            raise AccessError("请选择 4–15 秒的视频时长。", 400, "duration_invalid")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            code = self._grant(db, visitor)
            total = db.execute("SELECT COALESCE(SUM(seconds),0) FROM reservations WHERE visitor=? AND code=? AND status IN ('held','used')", (visitor, code)).fetchone()[0]
            if total + seconds > 30:
                remaining = max(0, 30-total)
                message = "视频额度已用完，暂时无法继续生成。" if remaining == 0 else f"剩余视频额度 {remaining:g} 秒，不足以生成 {seconds:g} 秒视频，请缩短生成时长。"
                raise AccessError(message, 403, "quota_exhausted")
            # A browser may use several tabs; only one unresolved submission at a time.
            if db.execute("SELECT 1 FROM reservations WHERE visitor=? AND status='held'", (visitor,)).fetchone():
                raise AccessError("已有视频生成中，请等待生成结果。", 409, "generation_pending")
            token = secrets.token_hex(24)
            db.execute("INSERT INTO reservations VALUES (?,?,?,?,?,NULL)", (token, visitor, code, seconds, "held"))
        return token

    def release(self, token):
        with self.connect() as db:
            db.execute("UPDATE reservations SET status='released' WHERE id=? AND status='held'", (token,))

    def attach(self, token, job):
        with self.connect() as db:
            db.execute("UPDATE reservations SET job=? WHERE id=?", (job, token))

    def settle(self, visitor, job, success):
        with self.connect() as db:
            row = db.execute("SELECT seconds FROM reservations WHERE visitor=? AND job=? AND status='held'", (visitor, job)).fetchone()
            db.execute("UPDATE reservations SET status=? WHERE visitor=? AND job=? AND status='held'", ("used" if success else "released", visitor, job))
            return row["seconds"] if row else 0

    def load(self, visitor):
        with self.connect() as db:
            row = db.execute("SELECT state FROM visitors WHERE id=?", (visitor,)).fetchone()
        return decode(json.loads(row[0])) if row and row[0] else None

    def save(self, visitor, state):
        with self.connect() as db:
            db.execute("UPDATE visitors SET state=? WHERE id=?", (json.dumps(encode(state), ensure_ascii=False), visitor))
