"""Per-instance accounts and explicit grants for the team entry point."""
import argparse
from contextvars import ContextVar
from dataclasses import dataclass
from getpass import getpass
from pathlib import Path
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from uuid import uuid4
from dotenv import dotenv_values

from app.config import DATA_DIR
from app.session_store import SessionStore, archive_connection

CURRENT_MEMBER = ContextVar("team_member", default=None)
OPERATIONS = {"details", "sql", "export"}


@dataclass(frozen=True)
class Member:
    name: str
    role: str
    grants: frozenset
    datasets: frozenset

    def can(self, op):
        return self.role == "admin" or (self.role == "analyst" and (op == "analyze" or op in self.grants))

    def sees(self, manifest):
        dataset = manifest["dataset_id"]
        source = manifest.get("delivery_source_id") or dataset
        return self.role == "admin" or "*" in self.datasets or dataset in self.datasets or "feed:"+source in self.datasets

    def store(self, directory):
        # Histories produced with raw-data privileges are inaccessible after revocation.
        scope = [op for op in ("details", "sql") if self.can(op)]
        key = hashlib.sha256(json.dumps([self.name, scope, self.role]).encode()).hexdigest()
        return SessionStore(directory / "team-history" / (key + ".sqlite3"))


class TeamAccess:
    def __init__(self, directory=DATA_DIR):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "team.sqlite3"
        settings = dotenv_values(directory.parent / ".env")
        self.report_days = int(os.environ.get("TEAM_REPORT_DAYS", settings.get("TEAM_REPORT_DAYS", 90)))
        self.download_days = int(os.environ.get("TEAM_DOWNLOAD_DAYS", settings.get("TEAM_DOWNLOAD_DAYS", 7)))
        self.log_days = int(os.environ.get("TEAM_LOG_DAYS", settings.get("TEAM_LOG_DAYS", 30)))
        if min(self.report_days, self.download_days, self.log_days) < 1:
            raise ValueError("团队留存天数必须大于零")
        with archive_connection(self.path) as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS members(name TEXT PRIMARY KEY, salt BLOB, digest BLOB,
                role TEXT, grants TEXT, datasets TEXT);
            CREATE TABLE IF NOT EXISTS resources(id TEXT PRIMARY KEY, kind TEXT, owner TEXT,
                recipient TEXT, datasets TEXT, required TEXT, payload TEXT, expires INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS operations(at INTEGER, route TEXT, status INTEGER, elapsed_ms INTEGER);
            CREATE INDEX IF NOT EXISTS operations_time ON operations(at);
            """)

    def set_member(self, name, password, role, grants, datasets):
        if not name.strip() or not password or role not in {"admin", "analyst", "reader"} or set(grants) - OPERATIONS:
            raise ValueError("请提供账号、口令、有效角色和授权范围")
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
        with archive_connection(self.path) as db:
            db.execute("INSERT OR REPLACE INTO members VALUES(?,?,?,?,?,?)",
                       (name, salt, digest, role, json.dumps(grants), json.dumps(datasets)))

    def authenticate(self, name, password):
        with archive_connection(self.path) as db:
            row = db.execute("SELECT salt,digest,role,grants,datasets FROM members WHERE name=?", (name,)).fetchone()
        if row and hmac.compare_digest(row[1], hashlib.scrypt(password.encode(), salt=row[0], n=16384, r=8, p=1)):
            return Member(name, row[2], frozenset(json.loads(row[3])), frozenset(json.loads(row[4])))
        return None

    def change_access(self, name, role, grants, datasets):
        with archive_connection(self.path) as db:
            changed = db.execute("UPDATE members SET role=?,grants=?,datasets=? WHERE name=?",
                                 (role, json.dumps(grants), json.dumps(datasets), name))
            if not changed.rowcount:
                raise ValueError("成员不存在")

    def member(self, name):
        with archive_connection(self.path) as db:
            row = db.execute("SELECT role,grants,datasets FROM members WHERE name=?", (name,)).fetchone()
        if row:
            return Member(name, row[0], frozenset(json.loads(row[1])), frozenset(json.loads(row[2])))

    def save_resource(self, kind, member, datasets, required, payload, recipient=None, *, key=None, db=None):
        if db is None:
            with archive_connection(self.path) as connection:
                return self.save_resource(kind, member, datasets, required, payload, recipient, key=key, db=connection)
        key = key or uuid4().hex
        if kind == "file":
            path = Path(payload)
            if path.is_relative_to(self.directory.parent):
                payload = path.relative_to(self.directory.parent).as_posix()
        days = self.report_days if kind == "report" else self.download_days
        db.execute("INSERT INTO resources VALUES(?,?,?,?,?,?,?,?)", (key, kind, member.name, recipient or member.name,
                   json.dumps(datasets), json.dumps(required), json.dumps(payload, ensure_ascii=False, default=str),
                   int(time.time()) + days*86400))
        return key

    def resources(self, member, kind):
        with archive_connection(self.path) as db:
            db.row_factory = sqlite3.Row
            return db.execute("SELECT * FROM resources WHERE kind=? AND (recipient=? OR owner=?) AND expires>?",
                              (kind, member.name, member.name, int(time.time()))).fetchall()

    def record_operation(self, route, status, elapsed_ms):
        now = int(time.time())
        with archive_connection(self.path) as db:
            db.execute("INSERT INTO operations VALUES(?,?,?,?)", (now, route, status, elapsed_ms))
            db.execute("DELETE FROM operations WHERE at<=?", (now-self.log_days*86400,))
            db.execute("DELETE FROM resources WHERE expires<=?", (now,))


def plan_requires_details(plan):
    def uses_order_key(node):
        return node and (node.field == "order_id" or any(uses_order_key(child) for child in node.children))
    raw_diagnosis = plan.intent == "diagnose" and plan.diagnosis not in {"quality_summary", "sales_spike", "seller_outlier"}
    return bool(plan.intent in {"details", "join_example"} or raw_diagnosis or plan.selected_order_id
                or uses_order_key(plan.order_where) or plan.output_mode in {"seller_audit", "quality_check"})


def check_team_plan(plan):
    member = CURRENT_MEMBER.get()
    if member and not member.can("details") and plan_requires_details(plan):
        raise ValueError("此操作需要订单明细权限")


def main():
    from app.team_jobs import process_lock

    if not os.environ.get("ANALYSIS_INSTANCE_DIR"):
        raise SystemExit("请先设置 ANALYSIS_INSTANCE_DIR 为客户实例目录")
    parser = argparse.ArgumentParser(description="管理本实例的成员及资源权限")
    parser.add_argument("name")
    parser.add_argument("--role", choices=["admin", "analyst", "reader"], required=True)
    parser.add_argument("--dataset", action="append", default=[], help="只授权指定快照，* 表示本实例全部")
    parser.add_argument("--feed", action="append", default=[], help="授权整个交付源及以后同步的快照")
    parser.add_argument("--grant", choices=sorted(OPERATIONS), action="append", default=[])
    parser.add_argument("--access-only", action="store_true", help="只替换已有账号的角色及授权")
    args = parser.parse_args()
    datasets = [*args.dataset, *("feed:"+key for key in args.feed)]
    password = None if args.access_only else getpass("设置此账号的口令：")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with process_lock(DATA_DIR, "maintenance"):
        access = TeamAccess()
        if args.access_only:
            access.change_access(args.name, args.role, args.grant, datasets)
        else:
            access.set_member(args.name, password, args.role, args.grant, datasets)
    print("已保存成员和授权。")


if __name__ == "__main__":
    main()
