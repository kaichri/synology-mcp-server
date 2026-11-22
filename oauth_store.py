"""Persistent OAuth state. Secrets and authorization credentials are never logged."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Store:
    def __init__(self, filename):
        path = Path(filename)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        # Restrict the DB before SQLite creates WAL/SHM files: they inherit its mode.
        fd = os.open(filename, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        if os.name != "nt":
            os.chmod(filename, 0o600)
        self.db = sqlite3.connect(filename, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS clients (
                id TEXT PRIMARY KEY, metadata TEXT NOT NULL, fetched INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS codes (
                hash TEXT PRIMARY KEY, client TEXT, redirect TEXT, scope TEXT,
                challenge TEXT, resource TEXT, username TEXT, expires INTEGER, used INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS tokens (
                jti TEXT PRIMARY KEY, client TEXT, username TEXT, scope TEXT, resource TEXT,
                expires INTEGER, refresh_hash TEXT UNIQUE, refresh_expires INTEGER,
                family TEXT, refresh_used INTEGER DEFAULT 0, revoked INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS pending (
                id TEXT PRIMARY KEY, params TEXT, csrf_hash TEXT, expires INTEGER);
            CREATE TABLE IF NOT EXISTS rate_limits (name TEXT PRIMARY KEY, count INTEGER, reset INTEGER);
            CREATE INDEX IF NOT EXISTS idx_tokens_family ON tokens(family);
            CREATE INDEX IF NOT EXISTS idx_tokens_expires ON tokens(expires, refresh_expires);
        """)
        if os.name != "nt":
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(filename) + suffix)
                if sidecar.exists():
                    os.chmod(sidecar, 0o600)
        with self.transaction():
            if not self.one("SELECT value FROM settings WHERE name='signing_key'"):
                key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
                pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()).decode()
                self.execute("INSERT INTO settings VALUES ('signing_key', ?)", (pem,))
            self.private_key = serialization.load_pem_private_key(
                self.one("SELECT value FROM settings WHERE name='signing_key'")["value"].encode(), None)
            if not isinstance(self.private_key, rsa.RSAPrivateKey) or self.private_key.key_size < 2048:
                raise ValueError("Persistent OAuth signing key must be RSA with at least 2048 bits")
            self.public_key = self.private_key.public_key()

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def execute(self, sql, params=()):
        with self.lock:
            return self.db.execute(sql, params)

    def one(self, sql, params=()):
        with self.lock:
            return self.db.execute(sql, params).fetchone()

    def limited(self, name, maximum, seconds=60):
        """Global persisted limit, deliberately independent of proxy/client IP headers."""
        now = int(time.time())
        with self.transaction():
            row = self.one("SELECT * FROM rate_limits WHERE name=?", (name,))
            if not row or row["reset"] <= now:
                self.execute("INSERT OR REPLACE INTO rate_limits VALUES (?, 1, ?)", (name, now + seconds))
                return False
            self.execute("UPDATE rate_limits SET count=count+1 WHERE name=?", (name,))
            return row["count"] >= maximum

    def cleanup(self):
        now = int(time.time())
        with self.transaction():
            self.execute("DELETE FROM pending WHERE expires < ?", (now,))
            # Keep spent code/refresh hashes through their expiry to detect replay.
            self.execute("DELETE FROM codes WHERE expires < ?", (now - 300,))
            self.execute("DELETE FROM tokens WHERE expires < ? AND refresh_expires < ?", (now, now))
            self.execute("DELETE FROM rate_limits WHERE reset < ?", (now,))

    def cache_client(self, client_id, metadata):
        self.execute("INSERT OR REPLACE INTO clients VALUES (?, ?, ?)",
                     (client_id, json.dumps(metadata), int(time.time())))

    def close(self):
        with self.lock:
            self.db.close()
