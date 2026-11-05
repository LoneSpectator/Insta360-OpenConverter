import json
from pathlib import Path
import sqlite3
import time


class State:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL,
                updated REAL NOT NULL, detail TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS artifacts (
                path TEXT PRIMARY KEY, hashes TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS config_aliases (
                old_fingerprint TEXT NOT NULL, new_fingerprint TEXT NOT NULL,
                PRIMARY KEY (old_fingerprint, new_fingerprint));
        """)

    def get(self, key):
        row = self.db.execute("SELECT * FROM jobs WHERE key=?", (str(key),)).fetchone()
        return dict(row) if row else None

    def put(self, key, fingerprint, status, **detail):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?)", (
                str(key), fingerprint, status, time.time(), json.dumps(detail, ensure_ascii=False)))

    def hashes(self, path):
        row = self.db.execute("SELECT hashes FROM artifacts WHERE path=?", (str(path),)).fetchone()
        return json.loads(row[0]) if row else []

    def register_config_alias(self, old, new):
        """Preserve render compatibility across content-identical SDK rebuilds."""
        if old == new:
            return
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO config_aliases VALUES (?,?)", (old, new))

    def config_aliases(self, current):
        rows = self.db.execute("""
            WITH RECURSIVE aliases(fingerprint) AS (
                SELECT old_fingerprint FROM config_aliases WHERE new_fingerprint=?
                UNION
                SELECT old_fingerprint FROM config_aliases JOIN aliases
                    ON new_fingerprint=aliases.fingerprint)
            SELECT fingerprint FROM aliases WHERE fingerprint != ? ORDER BY fingerprint
        """, (current, current)).fetchall()
        return [row[0] for row in rows]

    def intend(self, path, sha):
        # Keep the previous hash until rename is complete, so a crash before or
        # after publication can be recovered without claiming a stranger's file.
        hashes = self.hashes(path)
        if sha not in hashes:
            hashes.append(sha)
        self._artifact(path, hashes[-2:])

    def published(self, path, sha):
        self._artifact(path, [sha])

    def _artifact(self, path, hashes):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO artifacts VALUES (?,?)", (
                str(path), json.dumps(hashes)))

    def close(self):
        self.db.close()
