import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, path, ttl, limit):
        self.path = Path(path)
        self.ttl, self.limit = ttl, limit

    @contextmanager
    def transaction(self, now):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Create a private file before SQLite opens it; do not change global umask.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        db = sqlite3.connect(self.path, timeout=0.3)
        try:
            db.execute('CREATE TABLE IF NOT EXISTS state (scope TEXT, key TEXT, value TEXT, time REAL, PRIMARY KEY(scope,key))')
            db.execute('CREATE TABLE IF NOT EXISTS metrics (name TEXT PRIMARY KEY, count INTEGER)')
            db.commit()
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM state WHERE time < ?', (now - self.ttl,))
            db.execute('DELETE FROM state WHERE rowid IN (SELECT rowid FROM state ORDER BY time DESC LIMIT -1 OFFSET ?)', (self.limit,))
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def get(db, scope, key):
        row = db.execute('SELECT value FROM state WHERE scope=? AND key=?', (scope, key)).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def put(db, scope, key, value, now):
        db.execute('INSERT OR REPLACE INTO state VALUES (?,?,?,?)', (scope, key, json.dumps(value), now))

    @staticmethod
    def count(db, name):
        db.execute('INSERT INTO metrics VALUES (?,1) ON CONFLICT(name) DO UPDATE SET count=count+1', (name,))
