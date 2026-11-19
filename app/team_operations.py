"""Low-cardinality operational events, separate from business evidence."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import math
import time

from app.session_store import archive_connection

EVENTS = ContextVar("operations_events", default=None)


def observe(kind, status, elapsed_ms, tokens=None):
    rows = EVENTS.get()
    if rows is not None:
        rows.append((int(time.time()), kind, status, elapsed_ms, tokens))


class Operations:
    def __init__(self, access):
        self.access = access
        with archive_connection(access.path) as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS operational_events(
                at INTEGER, resource TEXT, kind TEXT, status TEXT, elapsed_ms INTEGER, tokens INTEGER);
            CREATE INDEX IF NOT EXISTS operational_events_time ON operational_events(at);
            """)

    @contextmanager
    def collect(self, rows, resource):
        token = EVENTS.set(rows)
        try:
            yield
        finally:
            EVENTS.reset(token)
            if rows:
                with archive_connection(self.access.path) as db:
                    self.save(db, resource, rows)

    def save(self, db, resource, rows):
        db.executemany("INSERT INTO operational_events VALUES(?,?,?,?,?,?)",
                       ((at, resource, kind, status, ms, tokens) for at, kind, status, ms, tokens in rows))
        db.execute("DELETE FROM operational_events WHERE at<=?", (int(time.time()) - self.access.log_days * 86400,))

    def daily(self, days):
        today = datetime.now(timezone.utc).date()
        first = today - timedelta(days=days - 1)
        since = int(datetime.combine(first, datetime.min.time(), tzinfo=timezone.utc).timestamp())
        with archive_connection(self.access.path) as db:
            db.execute("DELETE FROM operational_events WHERE at<=?", (int(time.time()) - self.access.log_days * 86400,))
            events = db.execute("""SELECT date(at,'unixepoch'),kind,status,elapsed_ms,tokens
                FROM operational_events WHERE at>=? ORDER BY at""", (since,)).fetchall()
            jobs = db.execute("""SELECT date(created,'unixepoch'),kind,status FROM jobs
                WHERE status IN ('queued','running') AND created>=?""", (since,)).fetchall()
        buckets = {}
        for day, kind, status, ms, tokens in events:
            buckets.setdefault((day, kind, status), []).append((ms, tokens))
        for day, kind, status in jobs:
            buckets.setdefault((day, "job_" + kind, status), []).append((None, None))
        result = []
        for (day, kind, status), values in sorted(buckets.items(), reverse=True):
            durations = sorted(ms for ms, _ in values if ms is not None)
            usage = [tokens for _, tokens in values if tokens is not None]
            result.append({"day": day, "kind": kind, "status": status, "count": len(values),
                           "p50_ms": durations[math.ceil(len(durations) * .5)-1] if durations else None,
                           "p95_ms": durations[math.ceil(len(durations) * .95)-1] if durations else None,
                           "tokens": sum(usage) if usage else None, "token_samples": len(usage)})
        return result

    def model_status(self):
        with archive_connection(self.access.path) as db:
            row = db.execute("""SELECT at,status FROM operational_events WHERE kind IN ('model','model_probe')
                AND at>? ORDER BY at DESC,rowid DESC LIMIT 1""",
                (int(time.time()) - self.access.log_days * 86400,)).fetchone()
        return {"at": datetime.fromtimestamp(row[0], timezone.utc).isoformat(), "status": row[1]} if row else None
