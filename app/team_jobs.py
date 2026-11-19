"""Persistent background work for one customer instance."""
import argparse
import json
import logging
import os
import sqlite3
import time
from uuid import uuid4

import duckdb
from fastapi import HTTPException
from filelock import FileLock

from app.config import DATA_DIR
from app.session_store import archive_connection
from app.snapshots import canonical, digest


class JobCancelled(Exception):
    pass


def process_lock(directory, role, timeout=0):
    return FileLock(directory.parent / f".team-{role}.lock", timeout=timeout)


class Jobs:
    def __init__(self, access, operations):
        self.access = access
        self.operations = operations
        self.path = access.path
        self.directory = access.directory / "jobs"
        self.handlers = {}
        with archive_connection(self.path) as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY, identity TEXT, owner TEXT, kind TEXT, input TEXT,
                datasets TEXT, rights TEXT, status TEXT, created REAL, started REAL,
                finished REAL, progress TEXT DEFAULT '[]', result TEXT, error TEXT,
                cancelled INTEGER DEFAULT 0, wait_ms INTEGER
            );
            CREATE INDEX IF NOT EXISTS jobs_owner ON jobs(owner,created);
            CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status,created);
            CREATE INDEX IF NOT EXISTS jobs_identity ON jobs(identity);
            """)

    def enqueue(self, member, kind, payload, *, datasets=(), rights=(), identity=None, key=None, db=None):
        identity = digest([member.name, kind, identity if identity is not None else payload])
        if db is None:
            with archive_connection(self.path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                return self._enqueue(connection, member, kind, payload, datasets, rights, identity, key)
        return self._enqueue(db, member, kind, payload, datasets, rights, identity, key)

    def _enqueue(self, db, member, kind, payload, datasets, rights, identity, key):
        existing = self.reusable(db, identity)
        if existing:
            return {"job": existing, "reused": True}
        key = key or uuid4().hex
        db.execute("""INSERT INTO jobs(id,identity,owner,kind,input,datasets,rights,status,created)
            VALUES(?,?,?,?,?,?,?,'queued',?)""",
            (key, identity, member.name, kind, canonical(payload), canonical(sorted(datasets)),
             canonical(sorted(rights)), time.time()))
        return {"job": key, "reused": False}

    def reusable(self, db, identity):
        rows = db.execute("""SELECT j.id, j.kind, j.status, r.payload
            FROM jobs j LEFT JOIN resources r ON r.id=j.id AND r.kind='file' AND r.expires>?
            WHERE j.identity=? AND (j.status IN ('queued','running') OR
            (j.status='succeeded' AND (j.kind<>'export' OR r.id IS NOT NULL)))
            ORDER BY j.created DESC,j.rowid DESC""", (time.time(), identity))
        for key, kind, status, path in rows:
            if kind != "export" or status != "succeeded" or (self.access.directory.parent / json.loads(path)).is_file():
                return key
        return None

    @staticmethod
    def read(db, key):
        db.row_factory = sqlite3.Row
        return db.execute("SELECT * FROM jobs WHERE id=?", (key,)).fetchone()

    def claim(self):
        with archive_connection(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id,created FROM jobs WHERE status='queued' ORDER BY created,rowid LIMIT 1").fetchone()
            if not row:
                return None
            started = time.time()
            db.execute("UPDATE jobs SET status='running',started=?,wait_ms=COALESCE(wait_ms,?) WHERE id=?",
                       (started, round((started - row[1]) * 1000), row[0]))
            return self.read(db, row[0])

    def recover(self):
        with archive_connection(self.path) as db:
            db.execute("UPDATE jobs SET status='queued',started=NULL WHERE status='running'")
            db.execute("UPDATE jobs SET status='cancelled',finished=? WHERE status='queued' AND cancelled", (time.time(),))

    def finish(self, key, status, *, result=None, error=None, file=None):
        with archive_connection(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.read(db, key)
            now = time.time()
            if file:
                member, datasets, rights, path = file
                self.access.save_resource("file", member, datasets, rights, path, key=key, db=db)
            db.execute("UPDATE jobs SET status=?,result=?,error=?,finished=? WHERE id=?",
                       (status, canonical(result) if result is not None else None, error, now, key))
            self.operations.save(db, key, [
                (int(now), "job_" + row["kind"], status, round((now - row["started"]) * 1000), None),
                (int(now), "job_wait_" + row["kind"], status, row["wait_ms"], None),
            ])

    def checkpoint(self, key, result=None):
        with archive_connection(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.read(db, key)
            steps = json.loads(row["progress"])
            if result is not None:
                steps.append(result)
                db.execute("UPDATE jobs SET progress=? WHERE id=?", (canonical(steps), key))
        if row["cancelled"]:
            raise JobCancelled
        return steps

    def member_for_step(self, key, visible):
        with archive_connection(self.path) as db:
            row = self.read(db, key)
        if row["cancelled"]:
            raise JobCancelled
        member = self.access.member(row["owner"])
        if not member or not allowed(row, member, visible(member)):
            raise HTTPException(403, "执行权限已变更")
        return member

    def run_one(self, visible):
        row = self.claim()
        if row is None:
            return False
        key = row["id"]
        try:
            member = self.member_for_step(key, visible)
            with self.operations.collect([], key):
                result, file = self.handlers[row["kind"]](key, json.loads(row["input"]), member)
            self.finish(key, "succeeded", result=result, file=file)
        except JobCancelled:
            self.finish(key, "cancelled")
        except (HTTPException, ValueError, duckdb.Error) as error:
            self.finish(key, "failed", error=str(error.detail if isinstance(error, HTTPException) else error))
        except Exception:
            logging.exception("Background job failed: %s / %s", row["kind"], key)
            self.finish(key, "failed", error="后台执行失败，请使用任务编号查阅工作进程日志")
        return True


def allowed(row, member, available):
    return (row["owner"] == member.name and set(json.loads(row["datasets"])) <= available.keys()
            and all(member.can(op) for op in json.loads(row["rights"])))


def main():
    from app.team import create_app

    parser = argparse.ArgumentParser(description="运行客户实例的后台工作进程")
    parser.add_argument("--once", action="store_true", help="连续执行当前待处理任务，队列清空后退出")
    args = parser.parse_args()
    if not os.environ.get("ANALYSIS_INSTANCE_DIR"):
        parser.error("请设置 ANALYSIS_INSTANCE_DIR 为客户实例目录")
    access_dir = DATA_DIR
    access_dir.mkdir(parents=True, exist_ok=True)
    with process_lock(access_dir, "worker"):
        web = create_app(access_dir)
        jobs = web.state.jobs
        jobs.recover()
        while True:
            if not jobs.run_one(web.state.visible):
                if args.once:
                    break
                time.sleep(0.25)


if __name__ == "__main__":
    main()
